"""Fetch transcripts from YouTube caption tracks (with optional fallbacks).

Order of preference for every language the user wants:
1. A caption track written by the uploader in that language.
2. YouTube's auto-generated captions in that language (if allowed).
3. YouTube's own translation of the video's original captions.
4. An external translation engine (Google Translate free web service).
5. Optional speech-to-text — only if installed, switched on in Secrets and
   confirmed by the user; otherwise it is skipped gracefully.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .language_processor import language_base
from .transcript_formatter import Segment

try:  # the library is a hard requirement, but keep imports defensive for tests
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api import _errors as yta_errors
except ImportError:  # pragma: no cover
    YouTubeTranscriptApi = None  # type: ignore
    yta_errors = None  # type: ignore

TEMP_PREFIX = "yte_"


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #
@dataclass
class TrackInfo:
    language_code: str
    language: str
    is_generated: bool
    is_translatable: bool
    translation_codes: list[str] = field(default_factory=list)
    handle: Any = None  # the library's Transcript object

    @property
    def base(self) -> str:
        return language_base(self.language_code)

    @property
    def kind(self) -> str:
        return "auto-generated captions" if self.is_generated else "uploaded captions"


@dataclass
class TranscriptResult:
    language_code: str
    language_name: str
    source: str  # plain-language description of where the text came from
    segments: list[Segment]
    machine_translated: bool = False
    is_generated: bool = False


class ExtractionError(Exception):
    """Error with a plain-language explanation for the user."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


# --------------------------------------------------------------------------- #
# Error explanations
# --------------------------------------------------------------------------- #
_ERROR_TABLE = [
    ("TranscriptsDisabled", "no_captions",
     "Captions are turned off for this video, so there is no transcript to read.", False),
    ("NoTranscriptFound", "no_captions",
     "This video has no captions in the requested language.", False),
    ("AgeRestricted", "restricted",
     "The video is age-restricted. YouTube requires a signed-in account, which this app does not use.", False),
    ("VideoUnplayable", "restricted",
     "The video cannot be played from this server (members-only, region-locked or premiere not started).", False),
    ("VideoUnavailable", "unavailable",
     "The video is private, deleted or otherwise unavailable.", False),
    ("InvalidVideoId", "invalid", "This is not a valid YouTube video link.", False),
    ("IpBlocked", "blocked",
     "YouTube is blocking requests from this server's internet address (common on free cloud hosting). "
     "Try again later, run the app on your own computer, or add a proxy in the app's Secrets.", True),
    ("RequestBlocked", "blocked",
     "YouTube is blocking requests from this server's internet address (common on free cloud hosting). "
     "Try again later, run the app on your own computer, or add a proxy in the app's Secrets.", True),
    ("PoTokenRequired", "blocked",
     "YouTube asked for an extra browser check that this app cannot complete. Try again later.", True),
    ("ProxyError", "network",
     "The proxy set in Secrets could not be reached. Check the proxy address, user name and password.", True),
    ("YouTubeRequestFailed", "network",
     "YouTube did not respond properly (possibly too many requests). Wait a minute and retry.", True),
    ("YouTubeDataUnparsable", "network",
     "YouTube returned a page this app could not read. Retry later.", True),
    ("NotTranslatable", "translation", "YouTube cannot translate the captions of this video.", False),
    ("TranslationLanguageNotAvailable", "translation",
     "YouTube does not offer a translation into this language for this video.", False),
]


def explain_error(exc: BaseException) -> ExtractionError:
    """Map any library/network error to a friendly ExtractionError."""
    if isinstance(exc, ExtractionError):
        return exc
    names = {cls.__name__ for cls in type(exc).__mro__}
    for class_name, code, message, retryable in _ERROR_TABLE:
        if class_name in names:
            return ExtractionError(code, message, retryable)
    text = str(exc).lower()
    if "429" in text or "too many requests" in text:
        return ExtractionError("network", "YouTube is limiting requests right now. Wait a minute and retry.", True)
    if "timed out" in text or "connection" in text:
        return ExtractionError("network", "Could not reach YouTube (network problem). Retry shortly.", True)
    return ExtractionError("unknown", f"Unexpected problem while reading captions: {type(exc).__name__}.", True)


# --------------------------------------------------------------------------- #
# Proxy configuration from hosting secrets
# --------------------------------------------------------------------------- #
def build_proxy_config(secrets: Mapping[str, Any]) -> Optional[Any]:
    """Create a proxy config from Secrets, or None if not configured.

    Supported keys:
      WEBSHARE_PROXY_USERNAME / WEBSHARE_PROXY_PASSWORD  (rotating residential)
      PROXY_HTTP_URL / PROXY_HTTPS_URL                  (any HTTP proxy)
    """
    if not secrets:
        return None
    try:
        from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig
    except ImportError:  # pragma: no cover
        return None
    user = str(secrets.get("WEBSHARE_PROXY_USERNAME", "") or "").strip()
    password = str(secrets.get("WEBSHARE_PROXY_PASSWORD", "") or "").strip()
    if user and password:
        return WebshareProxyConfig(proxy_username=user, proxy_password=password)
    http_url = str(secrets.get("PROXY_HTTP_URL", "") or "").strip()
    https_url = str(secrets.get("PROXY_HTTPS_URL", "") or "").strip()
    if http_url or https_url:
        return GenericProxyConfig(http_url=http_url or None, https_url=https_url or http_url or None)
    return None


# --------------------------------------------------------------------------- #
# Caption extractor
# --------------------------------------------------------------------------- #
def _to_segments(fetched: Any) -> list[Segment]:
    segments: list[Segment] = []
    for item in fetched:
        if isinstance(item, Mapping):
            text, start, duration = item.get("text", ""), item.get("start", 0), item.get("duration", 0)
        else:
            text, start, duration = getattr(item, "text", ""), getattr(item, "start", 0), getattr(item, "duration", 0)
        segments.append(Segment(start=float(start or 0), duration=float(duration or 0), text=str(text or "")))
    return segments


class TranscriptExtractor:
    """Thin, retry-aware wrapper around youtube-transcript-api (v1.x)."""

    def __init__(self, api: Any = None, proxy_config: Any = None, retries: int = 2, backoff: float = 1.5):
        if api is None:
            if YouTubeTranscriptApi is None:  # pragma: no cover
                raise RuntimeError("youtube-transcript-api is not installed")
            api = YouTubeTranscriptApi(proxy_config=proxy_config) if proxy_config else YouTubeTranscriptApi()
        self.api = api
        self.retries = retries
        self.backoff = backoff
        self._fetch_cache: dict[tuple[str, str, bool, str], list[Segment]] = {}

    def _retry(self, fn, *args):
        last: Optional[ExtractionError] = None
        for attempt in range(self.retries + 1):
            try:
                return fn(*args)
            except Exception as exc:
                err = explain_error(exc)
                # Blocked IPs rarely recover within seconds; don't hammer YouTube.
                if not err.retryable or err.code == "blocked" or attempt == self.retries:
                    raise err from exc
                last = err
                time.sleep(self.backoff * (2 ** attempt))
        raise last or ExtractionError("unknown", "Unknown error")  # pragma: no cover

    def list_tracks(self, video_id: str) -> list[TrackInfo]:
        transcript_list = self._retry(self.api.list, video_id)
        tracks: list[TrackInfo] = []
        for t in transcript_list:
            codes = []
            for lang in getattr(t, "translation_languages", []) or []:
                code = lang.get("language_code") if isinstance(lang, Mapping) else getattr(lang, "language_code", "")
                if code:
                    codes.append(code)
            tracks.append(TrackInfo(
                language_code=t.language_code,
                language=getattr(t, "language", t.language_code),
                is_generated=bool(getattr(t, "is_generated", False)),
                is_translatable=bool(getattr(t, "is_translatable", False)),
                translation_codes=codes,
                handle=t,
            ))
        if not tracks:
            raise ExtractionError("no_captions", "This video has no captions at all.")
        return tracks

    def fetch_track(self, video_id: str, track: TrackInfo) -> list[Segment]:
        key = (video_id, track.language_code, track.is_generated, "")
        if key not in self._fetch_cache:
            self._fetch_cache[key] = _to_segments(self._retry(track.handle.fetch))
        return self._fetch_cache[key]

    def fetch_youtube_translation(self, video_id: str, track: TrackInfo, target: str) -> list[Segment]:
        key = (video_id, track.language_code, track.is_generated, target)
        if key not in self._fetch_cache:
            translated = self._retry(track.handle.translate, target)
            self._fetch_cache[key] = _to_segments(self._retry(translated.fetch))
        return self._fetch_cache[key]

    def clear_cache(self) -> None:
        self._fetch_cache.clear()


# --------------------------------------------------------------------------- #
# Track selection helpers
# --------------------------------------------------------------------------- #
def find_track(tracks: list[TrackInfo], lang: str, allow_generated: bool) -> Optional[TrackInfo]:
    """Uploaded track in `lang` first, then auto-generated (if allowed)."""
    base = language_base(lang)
    manual = [t for t in tracks if t.base == base and not t.is_generated]
    if manual:
        exact = [t for t in manual if t.language_code.lower() == lang.lower()]
        return (exact or manual)[0]
    if allow_generated:
        generated = [t for t in tracks if t.base == base and t.is_generated]
        if generated:
            return generated[0]
    return None


def original_track(tracks: list[TrackInfo], allow_generated: bool = True) -> TrackInfo:
    """Best guess at the spoken language's track.

    YouTube's auto-generated track is created from the audio, so its language
    is the spoken language. Prefer an uploaded track in that same language.
    """
    generated = [t for t in tracks if t.is_generated]
    if generated:
        spoken = generated[0].base
        manual_same = [t for t in tracks if not t.is_generated and t.base == spoken]
        if manual_same:
            return manual_same[0]
        if allow_generated:
            return generated[0]
    manual = [t for t in tracks if not t.is_generated]
    if manual:
        return manual[0]
    return tracks[0]


# --------------------------------------------------------------------------- #
# Optional speech-to-text fallback
# --------------------------------------------------------------------------- #
@dataclass
class SpeechToTextStatus:
    available: bool
    reason: str


class SpeechToTextFallback:
    """Optional: download audio with yt-dlp and transcribe with faster-whisper.

    Disabled unless (a) both packages are installed, (b) the secret
    ENABLE_SPEECH_TO_TEXT is "true", and (c) the user confirms in the app that
    they are permitted to process the audio. Audio is written to a temporary
    folder and deleted immediately after transcription.
    """

    def __init__(self, secrets: Optional[Mapping[str, Any]] = None):
        secrets = secrets or {}
        self.enabled_flag = str(secrets.get("ENABLE_SPEECH_TO_TEXT", "false")).lower() in ("1", "true", "yes")
        self.model_size = str(secrets.get("WHISPER_MODEL", "tiny"))
        try:
            self.max_minutes = int(secrets.get("STT_MAX_MINUTES", 20))
        except (TypeError, ValueError):
            self.max_minutes = 20
        self._model = None

    def status(self) -> SpeechToTextStatus:
        missing = [m for m in ("yt_dlp", "faster_whisper") if importlib.util.find_spec(m) is None]
        if missing:
            return SpeechToTextStatus(False, "Not installed on this server (needs yt-dlp and faster-whisper).")
        if not self.enabled_flag:
            return SpeechToTextStatus(False, "Switched off. Set ENABLE_SPEECH_TO_TEXT = \"true\" in Secrets to allow it.")
        return SpeechToTextStatus(True, f"Available for videos up to {self.max_minutes} minutes.")

    def transcribe(self, video_id: str, language: Optional[str] = None) -> TranscriptResult:
        state = self.status()
        if not state.available:
            raise ExtractionError("stt_unavailable", f"Speech-to-text is not available: {state.reason}")
        import yt_dlp  # type: ignore
        from faster_whisper import WhisperModel  # type: ignore

        workdir = tempfile.mkdtemp(prefix=TEMP_PREFIX)
        try:
            url = f"https://www.youtube.com/watch?v={video_id}"
            opts = {
                "format": "bestaudio[abr<=96]/bestaudio/worst",
                "outtmpl": os.path.join(workdir, "audio.%(ext)s"),
                "quiet": True,
                "noplaylist": True,
                "no_warnings": True,
            }
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    duration = (info or {}).get("duration") or 0
                    if duration and duration > self.max_minutes * 60:
                        raise ExtractionError(
                            "too_long",
                            f"Video is longer than {self.max_minutes} minutes — too long for speech-to-text on free hosting.",
                        )
                    ydl.download([url])
            except ExtractionError:
                raise
            except Exception as exc:
                raise ExtractionError("stt_download", f"Audio could not be downloaded from this server ({type(exc).__name__}).") from exc

            files = [os.path.join(workdir, f) for f in os.listdir(workdir)]
            if not files:
                raise ExtractionError("stt_download", "Audio could not be downloaded from this server.")
            if self._model is None:
                self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
            segments_iter, info = self._model.transcribe(files[0], language=language, vad_filter=True)
            segments = [Segment(start=s.start, duration=s.end - s.start, text=s.text) for s in segments_iter]
            code = getattr(info, "language", language or "und")
            return TranscriptResult(
                language_code=code,
                language_name=code,
                source="speech-to-text (machine generated, please review)",
                segments=segments,
                is_generated=True,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def cleanup_stale_temp(max_age_seconds: int = 3600) -> int:
    """Delete leftover temporary folders (e.g. after a server restart)."""
    removed = 0
    base = tempfile.gettempdir()
    now = time.time()
    try:
        entries = os.listdir(base)
    except OSError:
        return 0
    for name in entries:
        if not name.startswith(TEMP_PREFIX):
            continue
        path = os.path.join(base, name)
        try:
            if now - os.path.getmtime(path) > max_age_seconds:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
