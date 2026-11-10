"""Language handling: Unicode clean-up, script detection and translation.

Hindi text is kept as Unicode Devanagari (NFC normalised). Zero-width
joiner/non-joiner are preserved because they change how conjuncts render.
"""

from __future__ import annotations

import html
import importlib.util
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

OUTPUT_MODES = {
    "hindi": "Hindi (Unicode Devanagari)",
    "english": "English",
    "original": "Original language of the video",
    "bilingual": "Bilingual — Hindi + English",
}

LANGUAGE_NAMES = {"hi": "Hindi", "en": "English"}

# Targets needed for each output mode ("orig" = whatever the video uses).
MODE_TARGETS = {
    "hindi": ["hi"],
    "english": ["en"],
    "original": ["orig"],
    "bilingual": ["hi", "en"],
}

TRANSLATION_ENGINES = {
    "auto": "Automatic (YouTube first, then Google Translate)",
    "youtube": "YouTube's own caption translation only",
    "google": "Google Translate (free web service) only",
    "none": "Do not translate",
}

DEVANAGARI_RE = re.compile(r"[ऀ-ॿ꣠-ꣿ]")
LATIN_RE = re.compile(r"[A-Za-z]")
SOUND_TAG_RE = re.compile(
    r"\[(?:music|applause|laughter|laughs|cheering|noise|silence|inaudible|"
    r"संगीत|तालियाँ|तालियां|हंसी|हँसी|म्यूजिक|म्यूज़िक)\]",
    re.IGNORECASE,
)
# Remove only invisible characters that never matter; keep ZWJ/ZWNJ.
INVISIBLE_RE = re.compile("[\u200b\u2060\ufeff\u00ad\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
SPACE_RE = re.compile(r"[ \t ]+")


def normalize_text(text: str) -> str:
    """Clean one caption line: decode entities, NFC, single spaces."""
    if not text:
        return ""
    text = html.unescape(text)
    text = text.replace("\r", " ").replace("\n", " ")
    text = INVISIBLE_RE.sub("", text)
    text = unicodedata.normalize("NFC", text)
    text = SPACE_RE.sub(" ", text)
    return text.strip()


def remove_sound_tags(text: str) -> str:
    cleaned = SOUND_TAG_RE.sub(" ", text)
    return SPACE_RE.sub(" ", cleaned).strip()


@dataclass(frozen=True)
class ScriptProfile:
    devanagari: int
    latin: int

    @property
    def total(self) -> int:
        return self.devanagari + self.latin

    @property
    def devanagari_share(self) -> float:
        return self.devanagari / self.total if self.total else 0.0


def script_profile(text: str) -> ScriptProfile:
    return ScriptProfile(
        devanagari=len(DEVANAGARI_RE.findall(text or "")),
        latin=len(LATIN_RE.findall(text or "")),
    )


def detect_script_language(text: str) -> str:
    """Rough label from the script mix: 'hi', 'en', 'mixed' or 'unknown'."""
    profile = script_profile(text)
    if profile.total == 0:
        return "unknown"
    share = profile.devanagari_share
    if share >= 0.85:
        return "hi"
    if share <= 0.15:
        return "en"
    return "mixed"


def is_hindi_unicode(text: str, threshold: float = 0.5) -> bool:
    return script_profile(text).devanagari_share >= threshold


def language_base(code: str) -> str:
    """'hi-IN' -> 'hi', 'en-GB' -> 'en'."""
    return (code or "").split("-")[0].split("_")[0].lower()


def google_translate_available() -> bool:
    return importlib.util.find_spec("deep_translator") is not None


class TranslationError(RuntimeError):
    pass


class Translator:
    """Translate blocks of text with an external engine (Google free web).

    Texts are sent in batches joined by newlines to keep the number of web
    requests low; if a batch comes back with the wrong number of lines, each
    text in that batch is translated on its own instead.
    """

    MAX_CHARS = 4500

    def __init__(
        self,
        engine_fn: Optional[Callable[[str, str, str], str]] = None,
        pause_seconds: float = 0.3,
        retries: int = 2,
    ):
        self._engine_fn = engine_fn
        self.pause_seconds = pause_seconds
        self.retries = retries

    @property
    def available(self) -> bool:
        return self._engine_fn is not None or google_translate_available()

    def _call(self, text: str, source: str, target: str) -> str:
        if self._engine_fn is not None:
            return self._engine_fn(text, source, target)
        from deep_translator import GoogleTranslator  # imported lazily

        try:
            engine = GoogleTranslator(source=source or "auto", target=target)
        except Exception:
            # YouTube codes such as "zh-Hans", "fil" or "en-GB" are not always
            # accepted; let Google detect the source language instead.
            engine = GoogleTranslator(source="auto", target=target)
        return engine.translate(text) or ""

    def _call_with_retry(self, text: str, source: str, target: str) -> str:
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                return self._call(text, source, target)
            except Exception as exc:  # engine errors vary by library version
                last_error = exc
                time.sleep(self.pause_seconds * (2 ** attempt))
        raise TranslationError(f"Translation service did not respond: {last_error}")

    def _batches(self, texts: Sequence[str]) -> list[list[int]]:
        batches: list[list[int]] = []
        current: list[int] = []
        size = 0
        for i, text in enumerate(texts):
            length = len(text) + 1
            if current and size + length > self.MAX_CHARS:
                batches.append(current)
                current, size = [], 0
            current.append(i)
            size += length
        if current:
            batches.append(current)
        return batches

    def translate_texts(self, texts: Sequence[str], target: str, source: str = "auto") -> list[str]:
        if not self.available:
            raise TranslationError("No translation engine is installed on this server.")
        cleaned = [normalize_text(t) for t in texts]
        output = list(cleaned)
        for batch in self._batches(cleaned):
            pieces = [cleaned[i] for i in batch]
            # Long single texts are split by sentence to fit the limit.
            if len(pieces) == 1 and len(pieces[0]) > self.MAX_CHARS:
                output[batch[0]] = self._translate_long(pieces[0], source, target)
                continue
            joined = "\n".join(pieces)
            translated = self._call_with_retry(joined, source, target)
            lines = [line for line in translated.split("\n")]
            if len(lines) == len(pieces):
                for i, line in zip(batch, lines):
                    output[i] = normalize_text(line)
            else:
                for i in batch:
                    output[i] = normalize_text(self._call_with_retry(cleaned[i], source, target))
            if self.pause_seconds:
                time.sleep(self.pause_seconds)
        return output

    def _translate_long(self, text: str, source: str, target: str) -> str:
        sentences = re.split(r"(?<=[.!?।])\s+", text)
        chunks, current = [], ""
        for sentence in sentences:
            if len(current) + len(sentence) + 1 > self.MAX_CHARS and current:
                chunks.append(current)
                current = ""
            current = f"{current} {sentence}".strip()
        if current:
            chunks.append(current)
        return " ".join(normalize_text(self._call_with_retry(c, source, target)) for c in chunks)
