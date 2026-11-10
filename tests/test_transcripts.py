"""Tests for link parsing, language handling, extraction and batch processing."""

import unicodedata

import pytest

from modules.batch_processor import BatchSettings, process_batch, process_video
from modules.language_processor import (
    Translator,
    detect_script_language,
    is_hindi_unicode,
    normalize_text,
    remove_sound_tags,
)
from modules.transcript_extractor import (
    ExtractionError,
    TranscriptExtractor,
    build_proxy_config,
    explain_error,
)
from modules.transcript_formatter import (
    Segment,
    align_to_blocks,
    blocks_to_text,
    build_blocks,
    format_timestamp,
    text_to_blocks,
)
from modules.youtube_handler import VideoRef, extract_video_id, looks_like_playlist, parse_links
from tests.conftest import fake_google

VID = "dQw4w9WgXcQ"


# ----------------------------------------------------------------- links ----
@pytest.mark.parametrize("link", [
    f"https://www.youtube.com/watch?v={VID}",
    f"https://youtube.com/watch?v={VID}&t=42s",
    f"https://m.youtube.com/watch?feature=share&v={VID}",
    f"https://youtu.be/{VID}?si=abc",
    f"youtu.be/{VID}",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://www.youtube.com/embed/{VID}",
    f"https://www.youtube.com/live/{VID}?feature=share",
    f"https://music.youtube.com/watch?v={VID}&list=RD",
    f"https://www.youtube-nocookie.com/embed/{VID}",
    VID,
])
def test_extract_video_id_variants(link):
    assert extract_video_id(link) == VID


@pytest.mark.parametrize("bad", ["https://vimeo.com/123", "hello", "https://www.youtube.com/@channel",
                                 "https://www.youtube.com/playlist?list=PL123", ""])
def test_extract_video_id_rejects(bad):
    assert extract_video_id(bad) is None


def test_parse_links_dedupe_invalid_and_limit():
    raw = f"""https://youtu.be/{VID}
    https://www.youtube.com/watch?v={VID}, not-a-link
    https://youtu.be/aaaaaaaaaaa https://youtu.be/bbbbbbbbbbb"""
    parsed = parse_links(raw, max_batch=2)
    assert [r.video_id for r in parsed.valid] == [VID, "aaaaaaaaaaa"]
    assert len(parsed.duplicates) == 1
    assert parsed.invalid == ["not-a-link"]
    assert [r.video_id for r in parsed.over_limit] == ["bbbbbbbbbbb"]


def test_playlist_detection():
    assert looks_like_playlist("https://www.youtube.com/playlist?list=PL123")
    assert not looks_like_playlist(f"https://www.youtube.com/watch?v={VID}&list=PL1")


# ------------------------------------------------------------- language ----
def test_normalize_keeps_hindi_unicode_and_joiners():
    decomposed = unicodedata.normalize("NFD", "क़िला")      # nukta form
    text = normalize_text(f"  {decomposed}​ &amp; क्‍ष  \n")
    assert text == unicodedata.normalize("NFC", "क़िला") + " & क्‍ष"
    assert "‍" in text  # ZWJ preserved


def test_sound_tags_removed_in_both_languages():
    assert remove_sound_tags("[Music] नमस्ते [संगीत] दोस्तों") == "नमस्ते दोस्तों"


def test_script_detection():
    assert detect_script_language("आज हम बिजली के बारे में बात करेंगे") == "hi"
    assert detect_script_language("today we talk about power") == "en"
    assert detect_script_language("आज smart meter की बात करेंगे about billing system") == "mixed"
    assert is_hindi_unicode("नमस्ते")
    assert not is_hindi_unicode("namaste")


def test_translator_batches_and_falls_back_on_line_mismatch():
    calls = []

    def engine(text, source, target):
        calls.append(text)
        return text.replace("\n", " ") if len(calls) == 1 else f"T:{text}"

    translator = Translator(engine_fn=engine, pause_seconds=0)
    out = translator.translate_texts(["one", "two"], target="hi")
    assert out == ["T:one", "T:two"]  # first batch merged lines -> per-item retry


# ------------------------------------------------------------ formatter ----
def test_timestamp_and_blocks_roundtrip():
    assert format_timestamp(3725) == "01:02:05"
    segs = [Segment(0, 2, "पहला वाक्य।"), Segment(31, 2, "दूसरा"), Segment(65, 2, "तीसरा")]
    blocks = build_blocks(segs, window_seconds=60)
    assert [b.label for b in blocks] == ["00:00:00", "00:00:31"]
    text = blocks_to_text(blocks)
    edited = text.replace("दूसरा", "दूसरा (सुधारा गया)")
    parsed = text_to_blocks(edited)
    assert parsed[1].text.startswith("दूसरा (सुधारा गया)")
    assert parsed[1].label == "00:00:31"


def test_align_to_blocks_shares_timestamps():
    ref = build_blocks([Segment(0, 2, "a."), Segment(40, 2, "b"), Segment(70, 2, "c")], window_seconds=60)
    other = align_to_blocks([Segment(0, 2, "x"), Segment(41, 2, "y"), Segment(71, 2, "z")], ref)
    assert [b.label for b in other] == [b.label for b in ref]


def test_text_without_timestamps_parses():
    blocks = text_to_blocks("पहला अनुच्छेद\n\nदूसरा अनुच्छेद")
    assert [b.text for b in blocks] == ["पहला अनुच्छेद", "दूसरा अनुच्छेद"]
    assert all(b.label == "" for b in blocks)


# ------------------------------------------------------------ extractor ----
def test_error_explanations():
    from youtube_transcript_api import RequestBlocked, TranscriptsDisabled, VideoUnavailable

    assert explain_error(TranscriptsDisabled("x")).code == "no_captions"
    assert explain_error(VideoUnavailable("x")).code == "unavailable"
    blocked = explain_error(RequestBlocked("x"))
    assert blocked.code == "blocked" and "proxy" in blocked.message
    assert explain_error(RuntimeError("HTTP 429 Too Many Requests")).retryable


def test_proxy_config_from_secrets():
    assert build_proxy_config({}) is None
    assert build_proxy_config({"WEBSHARE_PROXY_USERNAME": "u", "WEBSHARE_PROXY_PASSWORD": "p"}) is not None
    assert build_proxy_config({"PROXY_HTTP_URL": "http://user:pw@host:8080"}) is not None


def _run(fake_api, video_id, mode, engine="auto", allow_generated=True):
    extractor = TranscriptExtractor(api=fake_api, retries=0)
    translator = Translator(engine_fn=fake_google, pause_seconds=0)
    settings = BatchSettings(mode=mode, translation_engine=engine, allow_generated=allow_generated,
                             pause_between_videos=0)
    ref = VideoRef(video_id, f"https://www.youtube.com/watch?v={video_id}", video_id)
    return process_video(0, ref, "Test", settings, extractor, translator)


def test_hindi_from_uploaded_captions(fake_api):
    r = _run(fake_api, "hindiManual", "hindi")
    assert r.ok, r.message
    assert r.order == ["hi"]
    assert "नमस्ते दोस्तों" in r.texts["hi"]
    assert "[संगीत]" not in r.texts["hi"]
    assert r.texts["hi"].startswith("[00:00:00]")
    assert "uploaded captions" in r.sources["hi"]


def test_english_via_youtube_translation(fake_api):
    r = _run(fake_api, "hindiManual", "english")
    assert r.ok
    assert "[YT-en]" in r.texts["en"]
    assert "machine translated" in r.sources["en"]


def test_hindi_via_google_when_youtube_cannot_translate(fake_api):
    r = _run(fake_api, "noYtTransla", "hindi")
    assert r.ok, r.message
    assert "[G-hi]" in r.texts["hi"]
    assert "Google Translate" in r.sources["hi"]


def test_translation_off_gives_clear_error(fake_api):
    r = _run(fake_api, "englishOnly", "hindi", engine="none")
    assert r.status == "failed"
    assert "translation is switched off" in r.message


def test_original_language_and_generated_toggle(fake_api):
    r = _run(fake_api, "englishOnly", "original")
    assert r.ok and r.order == ["orig"]
    assert "Hello friends" in r.texts["orig"]
    r2 = _run(fake_api, "englishOnly", "original", allow_generated=False)
    assert r2.status == "failed"


def test_bilingual_uses_both_tracks_with_same_timestamps(fake_api):
    r = _run(fake_api, "bothLangsXX", "bilingual")
    assert r.ok
    assert r.order == ["hi", "en"]
    hi_labels = [b.label for b in text_to_blocks(r.texts["hi"])]
    en_labels = [b.label for b in text_to_blocks(r.texts["en"])]
    assert hi_labels == en_labels
    doc = r.to_document()
    assert doc.is_bilingual and doc.columns == ["हिंदी (Hindi)", "English"]
    assert all(len(row.texts) == 2 for row in doc.rows)


@pytest.mark.parametrize("vid,code", [("captionsOff", "no_captions"), ("privateVide", "unavailable"),
                                      ("blockedVide", "blocked")])
def test_failures_are_explained(fake_api, vid, code):
    r = _run(fake_api, vid, "hindi")
    assert r.status == "failed" and r.error_code == code and r.message


# ---------------------------------------------------------------- batch ----
def test_batch_continues_after_failures(fake_api):
    refs = [VideoRef(v, f"https://youtu.be/{v}", v) for v in ["captionsOff", "hindiManual", "privateVide", "englishOnly"]]
    extractor = TranscriptExtractor(api=fake_api, retries=0)
    translator = Translator(engine_fn=fake_google, pause_seconds=0)
    seen = []
    results = process_batch(refs, {}, BatchSettings(mode="hindi", pause_between_videos=0), extractor, translator,
                            on_done=lambda i, r: seen.append(r.status))
    assert [r.status for r in results] == ["failed", "done", "failed", "done"]
    assert seen == ["failed", "done", "failed", "done"]


def test_batch_skips_rest_after_repeated_blocking(fake_api):
    refs = [VideoRef("blockedVide", "u", "x")] * 3 + [VideoRef("hindiManual", "u", "x")]
    extractor = TranscriptExtractor(api=fake_api, retries=0)
    results = process_batch(refs, {}, BatchSettings(mode="hindi", pause_between_videos=0, stop_after_blocked=2),
                            extractor, None)
    assert [r.status for r in results] == ["failed", "failed", "skipped", "skipped"]


def test_fetch_is_cached_within_video(fake_api):
    _run(fake_api, "bothLangsXX", "bilingual")
    hi_track = fake_api.videos["bothLangsXX"][0]
    assert hi_track.fetch_calls == 1


def test_extraction_error_type():
    err = ExtractionError("x", "msg")
    assert explain_error(err) is err


# ---------------------------------------------------------- regressions ----
def test_ordinary_words_are_not_video_ids_and_trailing_punctuation_is_ok():
    parsed = parse_links(f"Please check Information here: (https://youtu.be/{VID}).")
    assert [r.video_id for r in parsed.valid] == [VID]
    assert extract_video_id("Information") is None
    assert extract_video_id("abcdefghij1") == "abcdefghij1"


def test_control_characters_removed_from_captions():
    assert normalize_text("एक\x07दो\x0bतीन") == "एकदोतीन"


def test_unsupported_source_language_code_falls_back_to_auto(monkeypatch):
    import deep_translator

    seen = []

    class FakeGoogle:
        def __init__(self, source, target):
            if source not in ("auto", "hi", "en"):
                raise deep_translator.exceptions.LanguageNotSupportedException(source)
            seen.append(source)

        def translate(self, text):
            return text.upper()

    monkeypatch.setattr(deep_translator, "GoogleTranslator", FakeGoogle)
    out = Translator(pause_seconds=0).translate_texts(["hello"], target="hi", source="zh-Hans")
    assert out == ["HELLO"] and seen == ["auto"]
