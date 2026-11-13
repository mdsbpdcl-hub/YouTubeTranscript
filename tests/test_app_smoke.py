"""End-to-end run of the Streamlit app through all four steps (YouTube faked)."""

import os

import pytest

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest  # noqa: E402

from tests.conftest import fake_google  # noqa: E402

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "streamlit_app.py")


@pytest.fixture
def patched(monkeypatch, fake_api):
    import modules.language_processor as lp
    import modules.transcript_extractor as te
    import modules.youtube_handler as yh

    monkeypatch.setattr(te, "YouTubeTranscriptApi", lambda *a, **k: fake_api)
    monkeypatch.setattr(yh, "fetch_video_title", lambda vid, **k: (f"वीडियो {vid}", None))
    monkeypatch.setattr(lp.Translator, "_call", lambda self, text, s, t: fake_google(text, s, t))
    monkeypatch.setattr(lp, "google_translate_available", lambda: True)
    monkeypatch.setattr(lp.time, "sleep", lambda s: None)
    return fake_api


def click(at, label):
    for button in at.button:
        if button.label.startswith(label):
            button.click()
            return at.run()
    raise AssertionError(f"button {label!r} not found: {[b.label for b in at.button]}")


def test_full_flow(patched):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    at.text_area(key="links_raw").input(
        "https://youtu.be/hindiManual\nhttps://youtu.be/captionsOff\nhttps://youtu.be/noYtTransla\nnot a link"
    ).run()
    assert not at.exception
    at = click(at, "Next")                         # -> Settings
    at.radio(key="mode").set_value("bilingual").run()
    at.slider(key="pause_between_videos").set_value(0.0).run()
    at = click(at, "Next")                         # -> Process
    at = click(at, "Start processing")
    assert not at.exception, at.exception
    results = at.session_state["results"]
    assert results["hindiManual"].status == "done"
    assert results["captionsOff"].status == "failed"
    assert results["noYtTransla"].status == "done"
    # Preview & correction box exists and edits are kept
    boxes = [t for t in at.text_area if t.key and t.key.startswith("edit_")]
    assert boxes
    boxes[0].input(boxes[0].value.replace("नमस्ते", "नमस्कार")).run()
    assert "नमस्कार" in at.session_state["results"][boxes[0].key.split("_")[1]].texts["hi"]
    at = click(at, "Next")                         # -> Download
    assert not at.exception, at.exception
    zip_bytes = at.session_state["files_cache"][2]
    assert zip_bytes[:2] == b"PK"
    files = at.session_state["files_cache"][1]
    assert any("नमस्कार" in data.decode("utf-8") for name, data in files.items() if name.endswith(".txt"))


def test_password_gate(patched, monkeypatch):
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["APP_PASSWORD"] = "secret123"
    at.run()
    assert "private" in " ".join(m.value for m in at.markdown).lower()
    at.text_input[0].input("secret123")
    at.button[0].click()
    at.run()
    assert at.session_state["authenticated"]


def test_retry_keeps_original_video_numbers(patched):
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.text_area(key="links_raw").input(
        "https://youtu.be/captionsOff\nhttps://youtu.be/hindiManual\nhttps://youtu.be/blockedVide").run()
    at = click(at, "Next")
    at.slider(key="pause_between_videos").set_value(0.0).run()
    at = click(at, "Next")
    at = click(at, "Start processing")
    at = click(at, "Retry")
    assert not at.exception
    results = at.session_state["results"]
    assert results["captionsOff"].index == 0
    assert results["blockedVide"].index == 2


def test_blocked_server_offers_offline_download(patched):
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.text_area(key="links_raw").input("https://youtu.be/blockedVide").run()
    at = click(at, "Next")
    at.slider(key="pause_between_videos").set_value(0.0).run()
    at = click(at, "Next")
    at = click(at, "Start processing")
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "blocking this online server" in text
