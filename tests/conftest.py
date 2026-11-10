"""Shared test fixtures: a fake YouTube transcript API (no network needed)."""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class FakeSnippet:
    def __init__(self, text, start, duration):
        self.text, self.start, self.duration = text, start, duration


class FakeTranscript:
    def __init__(self, code, name, generated, snippets, translatable=True, translations=("hi", "en"),
                 translate_error=None):
        self.language_code = code
        self.language = name
        self.is_generated = generated
        self.is_translatable = translatable
        self.translation_languages = [{"language": c, "language_code": c} for c in translations]
        self._snippets = snippets
        self._translate_error = translate_error
        self.fetch_calls = 0

    def fetch(self):
        self.fetch_calls += 1
        return [FakeSnippet(*s) for s in self._snippets]

    def translate(self, code):
        if self._translate_error:
            raise self._translate_error
        prefix = "[YT-" + code + "] "
        return FakeTranscript(code, code, True, [(prefix + t, s, d) for t, s, d in self._snippets],
                              translatable=False)


class FakeApi:
    def __init__(self, videos):
        self.videos = videos  # video_id -> list[FakeTranscript] or Exception

    def list(self, video_id):
        value = self.videos[video_id]
        if isinstance(value, Exception):
            raise value
        return value


HINDI_SNIPPETS = [
    ("नमस्ते दोस्तों,", 0.0, 2.0),
    ("आज हम बिजली के बारे में बात करेंगे।", 2.0, 3.0),
    ("[संगीत]", 5.0, 2.0),
    ("स्मार्ट मीटर से बिल सही आता है।", 70.0, 4.0),
]
ENGLISH_SNIPPETS = [
    ("Hello friends,", 0.0, 2.0),
    ("today we will talk about electricity.", 2.0, 3.0),
    ("[Music]", 5.0, 2.0),
    ("Smart meters give correct bills.", 70.0, 4.0),
]


@pytest.fixture
def fake_api():
    from youtube_transcript_api import TranscriptsDisabled, VideoUnavailable, RequestBlocked

    return FakeApi({
        "hindiManual": [
            FakeTranscript("hi", "Hindi", False, HINDI_SNIPPETS),
            FakeTranscript("hi", "Hindi (auto-generated)", True, HINDI_SNIPPETS),
        ],
        "bothLangsXX": [
            FakeTranscript("hi", "Hindi", False, HINDI_SNIPPETS),
            FakeTranscript("en", "English", False, ENGLISH_SNIPPETS),
        ],
        "englishOnly": [
            FakeTranscript("en", "English (auto-generated)", True, ENGLISH_SNIPPETS),
        ],
        "noYtTransla": [
            FakeTranscript("en", "English", False, ENGLISH_SNIPPETS, translatable=False, translations=()),
        ],
        "captionsOff": TranscriptsDisabled("captionsOff"),
        "privateVide": VideoUnavailable("privateVide"),
        "blockedVide": RequestBlocked("blockedVide"),
    })


def fake_google(text, source, target):
    """Pretend translator: tags every line so tests can see it was used."""
    return "\n".join(f"[G-{target}] {line}" for line in text.split("\n"))
