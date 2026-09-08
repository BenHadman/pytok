"""Daily, de-duplicated discovery of public TikTok creators discussing NYC Marathon 2027.

This example uses PyTok's three discovery surfaces (user search, video search,
and hashtag feeds).  It only records information exposed by a public TikTok
profile.  In particular, it extracts an email only when the profile biography
itself publishes one, and stores the profile's link-in-bio URL without opening
or crawling that destination.

Before the first run, register and log in a pool account as described in the
README.  A long-running daily process can be started with::

    python examples/nyc_marathon_2027_influencers.py --schedule daily --run-at 07:00 --headless

For a service manager or cron, use the default one-shot mode instead; the
SQLite database makes repeated invocations idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from pytok.tiktok import PyTok

USER_QUERIES = (
    "nyc marathon 2027",
    "new york marathon 2027",
    "tcs nyc marathon 2027",
    "nyc marathon runner",
    "nyc marathon training",
    "nyc marathon charity runner",
)
VIDEO_QUERIES = (
    "nyc marathon 2027",
    "new york city marathon 2027",
    "running the nyc marathon",
    "nyc marathon training",
    "nyc marathon charity bib",
)
HASHTAGS = (
    "nycmarathon",
    "tcsnycmarathon",
    "newyorkmarathon",
    "nycmarathontraining",
    "marathontraining",
)
MARATHON_TERMS = (
    "nyc marathon",
    "new york marathon",
    "new york city marathon",
    "tcsnycmarathon",
)
EMAIL_RE = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w.+-])", re.IGNORECASE
)
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def nested(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return a nested value without assuming which TikTok response shape arrived."""
    value: Any = mapping
    for key in keys:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return default if value is None else value


def user_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize profile fields from user-detail and search-result payloads."""
    user = data.get("userInfo", {}).get("user") or data.get("user") or data
    stats = data.get("userInfo", {}).get("stats") or data.get("stats") or {}
    bio = str(user.get("signature") or "")
    bio_link = user.get("bioLink") or user.get("link") or user.get("bioUrl") or ""
    if isinstance(bio_link, dict):
        bio_link = bio_link.get("link") or bio_link.get("url") or ""
    bio_urls = [url.rstrip(".,;:!?)]") for url in URL_RE.findall(bio)]
    if bio_link:
        bio_urls.insert(0, str(bio_link))
    # Preserve order while avoiding duplicated links returned by different API fields.
    bio_links = list(dict.fromkeys(filter(None, bio_urls)))
    email = next(iter(EMAIL_RE.findall(bio)), None)
    return {
        "platform_user_id": str(user.get("id") or user.get("uid") or ""),
        "username": str(user.get("uniqueId") or user.get("unique_id") or ""),
        "display_name": str(user.get("nickname") or ""),
        "bio": bio,
        "bio_email": email,
        "bio_link": bio_links[0] if bio_links else "",
        "bio_links": bio_links,
        "followers": stats.get("followerCount", user.get("followerCount")),
        "following": stats.get("followingCount", user.get("followingCount")),
        "likes": stats.get("heartCount", stats.get("heart")),
        "video_count": stats.get("videoCount", user.get("videoCount")),
        "verified": bool(user.get("verified", False)),
    }


def video_author(data: dict[str, Any]) -> dict[str, Any]:
    author = data.get("author") or {}
    return {
        "platform_user_id": str(author.get("id") or ""),
        "username": str(author.get("uniqueId") or ""),
    }


def is_marathon_post(data: dict[str, Any]) -> bool:
    """Identify public posts about the NYC Marathon from captions and hashtags."""
    hashtags = data.get("challenges") or data.get("hashtags") or []
    hashtag_text = " ".join(
        str(item.get("title") or item.get("name") or item)
        for item in hashtags
        if isinstance(item, (dict, str))
    )
    text = f"{data.get('desc') or ''} {hashtag_text}".lower()
    return any(term in text for term in MARATHON_TERMS)


class Store:
    """SQLite state: one canonical row per TikTok account, updated each day."""

    def __init__(self, path: Path):
        self.connection = sqlite3.connect(path)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS influencers (
                identity TEXT PRIMARY KEY, platform_user_id TEXT, username TEXT NOT NULL,
                display_name TEXT, bio TEXT, bio_email TEXT, bio_link TEXT,
                bio_links TEXT NOT NULL DEFAULT '[]', followers INTEGER,
                following INTEGER, likes INTEGER, video_count INTEGER, verified INTEGER NOT NULL,
                marathon_post_count INTEGER NOT NULL, sources TEXT NOT NULL, raw_profile_json TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
        """)
        columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(influencers)")
        }
        if "raw_profile_json" not in columns:
            self.connection.execute(
                "ALTER TABLE influencers ADD COLUMN raw_profile_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "bio_links" not in columns:
            self.connection.execute(
                "ALTER TABLE influencers ADD COLUMN bio_links TEXT NOT NULL DEFAULT '[]'"
            )
        self.connection.commit()

    def upsert(
        self, profile: dict[str, Any], sources: Iterable[str], marathon_post_count: int
    ) -> None:
        identity = profile["platform_user_id"] or profile["username"].lower()
        if not identity or not profile["username"]:
            return
        now = datetime.now(UTC).isoformat()
        # A search result can initially lack TikTok's numeric id.  When a later
        # profile lookup supplies it, merge that username-keyed row rather than
        # creating a second record for the same creator.
        existing = self.connection.execute(
            "SELECT identity, sources FROM influencers WHERE platform_user_id = ? "
            "OR lower(username) = ? LIMIT 1",
            (profile["platform_user_id"], profile["username"].lower()),
        ).fetchone()
        if existing:
            identity = existing[0]
            prior_sources = json.loads(existing[1])
        else:
            prior_sources = []
        merged_sources = sorted(set(prior_sources).union(sources))
        self.connection.execute(
            """
            INSERT INTO influencers (identity, platform_user_id, username, display_name, bio, bio_email,
                bio_link, bio_links, followers, following, likes, video_count, verified, marathon_post_count,
                sources, raw_profile_json, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity) DO UPDATE SET
                platform_user_id=excluded.platform_user_id, username=excluded.username,
                display_name=excluded.display_name, bio=excluded.bio, bio_email=excluded.bio_email,
                bio_link=excluded.bio_link, bio_links=excluded.bio_links, followers=excluded.followers, following=excluded.following,
                likes=excluded.likes, video_count=excluded.video_count, verified=excluded.verified,
                marathon_post_count=excluded.marathon_post_count, sources=excluded.sources,
                raw_profile_json=excluded.raw_profile_json, last_seen=excluded.last_seen
        """,
            (
                identity,
                profile["platform_user_id"],
                profile["username"],
                profile["display_name"],
                profile["bio"],
                profile["bio_email"],
                profile["bio_link"],
                json.dumps(profile.get("bio_links", [])),
                profile["followers"],
                profile["following"],
                profile["likes"],
                profile["video_count"],
                profile["verified"],
                marathon_post_count,
                json.dumps(merged_sources),
                json.dumps(profile.get("raw_profile", {})),
                now,
                now,
            ),
        )
        self.connection.commit()

    def export(self, path: Path) -> None:
        columns = [
            row[1] for row in self.connection.execute("PRAGMA table_info(influencers)")
        ]
        records = []
        for row in self.connection.execute(
            "SELECT * FROM influencers ORDER BY followers DESC"
        ):
            record = dict(zip(columns, row, strict=False))
            record["raw_profile"] = json.loads(record.pop("raw_profile_json"))
            record["bio_links"] = json.loads(record["bio_links"])
            record["sources"] = json.loads(record["sources"])
            records.append(record)
        path.write_text(json.dumps(records, indent=2), encoding="utf-8")

    def close(self) -> None:
        self.connection.close()


async def discover(api: PyTok, count: int) -> dict[str, set[str]]:
    """Collect candidate handles from user, video, and hashtag searches."""
    candidates: dict[str, set[str]] = {}

    def add(author: dict[str, Any], source: str) -> None:
        handle = author.get("username", "").strip()
        if handle:
            candidates.setdefault(handle.lower(), set()).add(source)

    for query in USER_QUERIES:
        async for user in api.search(query).users(count=count):
            try:
                data = await user.info()
            except Exception as error:
                logging.warning(
                    "Could not inspect a user-search result for %r: %s", query, error
                )
                continue
            fields = user_fields(data)
            add(fields, f"user-search:{query}")
    for query in VIDEO_QUERIES:
        async for video in api.search(query).videos(count=count):
            data = await video.info()
            if is_marathon_post(data):
                add(video_author(data), f"video-search:{query}")
    for tag in HASHTAGS:
        async for video in api.hashtag(name=tag).videos(count=count):
            data = await video.info()
            add(video_author(data), f"hashtag:{tag}")
    return candidates


async def count_marathon_posts(api: PyTok, username: str, limit: int) -> int:
    count = 0
    async for video in api.user(username=username).videos(count=limit):
        if is_marathon_post(await video.info()):
            count += 1
    return count


async def run_once(
    database: Path,
    output: Path,
    account: str | None,
    headless: bool,
    count: int,
    post_scan_limit: int,
) -> tuple[int, int]:
    # Delay the optional browser stack import so the data-normalization helpers
    # remain usable in lightweight environments.
    from pytok.accounts import AccountsPool
    from pytok.tiktok import PyTok

    store = Store(database)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with await PyTok.from_pool(
            AccountsPool(), username=account, headless=headless
        ) as api:
            candidates = await discover(api, count)
            stored = 0
            for username, sources in candidates.items():
                try:
                    raw_profile = await api.user(username=username).info()
                    profile = user_fields(raw_profile)
                    profile["raw_profile"] = raw_profile
                    if not profile["username"]:
                        continue
                    posts = await count_marathon_posts(
                        api, profile["username"], post_scan_limit
                    )
                except Exception as error:  # One deleted/private profile should not abort the daily job.
                    logging.warning("Could not inspect @%s: %s", username, error)
                    continue
                # A direct profile search may be broad; retain it only if its own posts confirm relevance.
                if posts or any(
                    source.startswith(("video-search", "hashtag")) for source in sources
                ):
                    store.upsert(profile, sources, posts)
                    stored += 1
        store.export(output)
        logging.info(
            "Exported %s qualifying creators from %s candidates to %s",
            stored,
            len(candidates),
            output,
        )
        return stored, len(candidates)
    finally:
        store.close()


async def run_daily(args: argparse.Namespace) -> None:
    while True:
        try:
            await run_once(
                args.database,
                args.output,
                args.account,
                args.headless,
                args.count,
                args.post_scan_limit,
            )
        except Exception:
            logging.exception("Daily discovery run failed; it will be retried tomorrow")
        target = datetime.now().replace(
            hour=args.run_at.hour, minute=args.run_at.minute, second=0, microsecond=0
        ) + timedelta(days=1)
        await asyncio.sleep((target - datetime.now()).total_seconds())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", type=Path, default=Path("nyc_marathon_2027.sqlite")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("nyc_marathon_2027_influencers.json")
    )
    parser.add_argument(
        "--account", default=None, help="Pooled, logged-in TikTok account identifier."
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--count", type=int, default=50, help="Results to inspect per query/feed."
    )
    parser.add_argument(
        "--post-scan-limit",
        type=int,
        default=100,
        help="Recent profile posts to scan for marathon references.",
    )
    parser.add_argument("--schedule", choices=("once", "daily"), default="once")
    parser.add_argument(
        "--run-at",
        type=lambda value: datetime.strptime(value, "%H:%M").time(),
        default=datetime.strptime("07:00", "%H:%M").time(),
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    asyncio.run(
        run_daily(args)
        if args.schedule == "daily"
        else run_once(
            args.database,
            args.output,
            args.account,
            args.headless,
            args.count,
            args.post_scan_limit,
        )
    )


if __name__ == "__main__":
    main()
