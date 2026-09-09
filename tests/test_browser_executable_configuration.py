from pytok.tiktok import PyTok


def test_pytok_uses_explicit_browser_executable_path(monkeypatch):
    monkeypatch.setattr(PyTok, "__del__", lambda self: None)
    api = PyTok(browser_executable_path="/opt/chromium/chrome")

    assert api._browser_executable_path == "/opt/chromium/chrome"


def test_pytok_uses_browser_executable_environment_variable(monkeypatch):
    monkeypatch.setenv("PYTOK_BROWSER_EXECUTABLE", "/usr/bin/chromium")
    monkeypatch.setattr(PyTok, "__del__", lambda self: None)

    api = PyTok()

    assert api._browser_executable_path == "/usr/bin/chromium"
