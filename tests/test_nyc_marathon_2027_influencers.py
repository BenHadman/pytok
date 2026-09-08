import importlib.util
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1] / "examples" / "nyc_marathon_2027_influencers.py"
)
SPEC = importlib.util.spec_from_file_location("nyc_marathon_example", MODULE_PATH)
example = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(example)


def test_user_fields_extracts_public_bio_contact_and_link():
    fields = example.user_fields(
        {
            "userInfo": {
                "user": {
                    "id": "42",
                    "uniqueId": "Runner",
                    "nickname": "Runner Name",
                    "signature": "Race enquiries: race@example.org",
                    "bioLink": {"link": "https://example.org"},
                    "verified": True,
                },
                "stats": {"followerCount": 12, "videoCount": 3},
            },
        }
    )
    assert fields["platform_user_id"] == "42"
    assert fields["bio_email"] == "race@example.org"
    assert fields["bio_link"] == "https://example.org"
    assert fields["bio_links"] == ["https://example.org"]
    assert fields["followers"] == 12


def test_store_deduplicates_by_platform_id_and_exports(tmp_path):
    store = example.Store(tmp_path / "state.sqlite")
    profile = {
        "platform_user_id": "42",
        "username": "runner",
        "display_name": "Runner",
        "bio": "bio",
        "bio_email": None,
        "bio_link": "",
        "followers": 1,
        "following": 2,
        "likes": 3,
        "video_count": 4,
        "verified": False,
    }
    store.upsert(profile, ["hashtag:nycmarathon"], 1)
    profile["followers"] = 99
    store.upsert(profile, ["video-search:nyc marathon 2027"], 2)
    output = tmp_path / "output.json"
    store.export(output)
    store.close()
    records = example.json.loads(output.read_text())
    assert len(records) == 1
    assert records[0]["followers"] == 99
    assert records[0]["marathon_post_count"] == 2
    assert records[0]["sources"] == [
        "hashtag:nycmarathon",
        "video-search:nyc marathon 2027",
    ]


def test_store_merges_username_fallback_with_later_platform_id(tmp_path):
    store = example.Store(tmp_path / "state.sqlite")
    profile = {
        "platform_user_id": "",
        "username": "runner",
        "display_name": "Runner",
        "bio": "https://links.example/runner",
        "bio_email": None,
        "bio_link": "https://links.example/runner",
        "bio_links": ["https://links.example/runner"],
        "followers": 1,
        "following": 2,
        "likes": 3,
        "video_count": 4,
        "verified": False,
    }
    store.upsert(profile, ["hashtag:nycmarathon"], 1)
    profile["platform_user_id"] = "42"
    store.upsert(profile, ["video-search:nyc marathon 2027"], 2)
    output = tmp_path / "output.json"
    store.export(output)
    store.close()
    records = example.json.loads(output.read_text())
    assert len(records) == 1
    assert records[0]["platform_user_id"] == "42"
    assert records[0]["sources"] == [
        "hashtag:nycmarathon",
        "video-search:nyc marathon 2027",
    ]


def test_marathon_post_detection_is_case_insensitive():
    assert example.is_marathon_post({"desc": "Training for the New York Marathon!"})
    assert not example.is_marathon_post({"desc": "A relaxed weekend run"})


def test_marathon_post_detection_includes_hashtags():
    assert example.is_marathon_post(
        {"desc": "Race day", "challenges": [{"title": "TCSNYCMarathon"}]}
    )
