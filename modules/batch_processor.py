"""Process a list of videos one by one, never stopping on a single failure."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from .language_processor import (
    LANGUAGE_NAMES,
    MODE_TARGETS,
    TranslationError,
    Translator,
    detect_script_language,
    language_base,
)
from .transcript_extractor import (
    ExtractionError,
    SpeechToTextFallback,
    TrackInfo,
    TranscriptExtractor,
    TranscriptResult,
    explain_error,
    find_track,
    original_track,
)
from .transcript_formatter import (
    Block,
    Segment,
    align_to_blocks,
    blocks_to_text,
    build_blocks,
    build_document,
    clean_segments,
    TranscriptDocument,
)
from .youtube_handler import VideoRef

COLUMN_LABELS = {"hi": "हिंदी (Hindi)", "en": "English"}


@dataclass(frozen=True)
class BatchSettings:
    mode: str = "hindi"                 # hindi | english | original | bilingual
    include_timestamps: bool = True
    drop_sound_tags: bool = True
    translation_engine: str = "auto"    # auto | youtube | google | none
    allow_generated: bool = True
    block_seconds: int = 60
    pause_between_videos: float = 1.0
    use_speech_to_text: bool = False
    stop_after_blocked: int = 3


@dataclass
class VideoResult:
    index: int
    video_id: str
    url: str
    title: str
    status: str = "pending"             # done | failed | skipped
    message: str = ""
    error_code: str = ""
    texts: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    original_language: str = ""
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "done"

    @property
    def display_title(self) -> str:
        return self.title or self.video_id

    def word_count(self) -> int:
        return sum(len(t.split()) for t in self.texts.values())

    def to_document(self) -> TranscriptDocument:
        notes = [f"{self.labels.get(k, k)}: {self.sources.get(k, '')}" for k in self.order if k in self.sources]
        notes += self.warnings
        return build_document(
            title=self.display_title,
            video_id=self.video_id,
            url=self.url,
            texts=self.texts,
            column_names=self.labels,
            order=self.order,
            notes=notes,
        )


class _VideoJob:
    """Works out every language needed for one video, reusing fetched data."""

    def __init__(self, video_id: str, settings: BatchSettings, extractor: TranscriptExtractor,
                 translator: Optional[Translator], stt: Optional[SpeechToTextFallback]):
        self.video_id = video_id
        self.settings = settings
        self.extractor = extractor
        self.translator = translator
        self.stt = stt
        self.tracks: Optional[list[TrackInfo]] = None
        self.warnings: list[str] = []
        self._original: Optional[TranscriptResult] = None

    # -- sources ---------------------------------------------------------------
    def load_tracks(self) -> None:
        try:
            self.tracks = self.extractor.list_tracks(self.video_id)
        except ExtractionError as err:
            if err.code == "no_captions" and self._stt_allowed():
                self.tracks = None
                self.warnings.append("No captions on YouTube — text was produced by speech-to-text; please review.")
                self._original = self.stt.transcribe(self.video_id)  # type: ignore[union-attr]
                return
            raise

    def _stt_allowed(self) -> bool:
        return bool(self.settings.use_speech_to_text and self.stt and self.stt.status().available)

    def original(self) -> TranscriptResult:
        if self._original is None:
            assert self.tracks is not None
            track = original_track(self.tracks, self.settings.allow_generated)
            if track.is_generated and not self.settings.allow_generated:
                raise ExtractionError("no_captions", "Only auto-generated captions exist and you chose not to use them.")
            segments = self.extractor.fetch_track(self.video_id, track)
            self._original = TranscriptResult(
                language_code=track.language_code,
                language_name=track.language,
                source=f"YouTube {track.kind} ({track.language})",
                segments=segments,
                is_generated=track.is_generated,
            )
        return self._original

    def language(self, lang: str) -> TranscriptResult:
        name = LANGUAGE_NAMES.get(lang, lang)
        if self.tracks:
            track = find_track(self.tracks, lang, self.settings.allow_generated)
            if track is not None:
                return TranscriptResult(
                    language_code=track.language_code,
                    language_name=track.language,
                    source=f"YouTube {track.kind} ({track.language})",
                    segments=self.extractor.fetch_track(self.video_id, track),
                    is_generated=track.is_generated,
                )

        source = self.original()
        if language_base(source.language_code) == lang:
            return source

        engine = self.settings.translation_engine
        if engine == "none":
            raise ExtractionError("no_language", f"No {name} captions on this video, and translation is switched off.")

        if engine in ("auto", "youtube") and self.tracks:
            track = original_track(self.tracks, self.settings.allow_generated)
            offers = (not track.translation_codes) or any(language_base(c) == lang for c in track.translation_codes)
            if track.is_translatable and offers:
                try:
                    segments = self.extractor.fetch_youtube_translation(self.video_id, track, lang)
                    return TranscriptResult(
                        language_code=lang,
                        language_name=name,
                        source=f"YouTube's translation of the {track.language} captions (machine translated)",
                        segments=segments,
                        machine_translated=True,
                    )
                except ExtractionError as err:
                    if engine == "youtube":
                        raise ExtractionError("translation", f"YouTube could not translate into {name}: {err.message}")
                    self.warnings.append(f"YouTube's {name} translation was not available; Google Translate was used.")

        if engine in ("auto", "google") and self.translator is not None and self.translator.available:
            blocks = build_blocks(
                clean_segments(source.segments, self.settings.drop_sound_tags),
                window_seconds=self.settings.block_seconds,
            )
            try:
                translated = self.translator.translate_texts(
                    [b.text for b in blocks], target=lang, source=language_base(source.language_code) or "auto"
                )
            except TranslationError as exc:
                raise ExtractionError("translation", f"Translation into {name} failed: {exc}", retryable=True) from exc
            segments = [Segment(start=b.start, duration=max(b.end - b.start, 0), text=t) for b, t in zip(blocks, translated)]
            return TranscriptResult(
                language_code=lang,
                language_name=name,
                source=f"Google Translate of the {source.language_name} captions (machine translated)",
                segments=segments,
                machine_translated=True,
            )

        raise ExtractionError(
            "no_language",
            f"No {name} captions on this video and no translation engine could be used.",
        )


def process_video(
    index: int,
    ref: VideoRef,
    title: str,
    settings: BatchSettings,
    extractor: TranscriptExtractor,
    translator: Optional[Translator] = None,
    stt: Optional[SpeechToTextFallback] = None,
) -> VideoResult:
    """Process one video. Never raises — failures are returned as results."""
    result = VideoResult(index=index, video_id=ref.video_id, url=ref.url, title=title or "")
    job = _VideoJob(ref.video_id, settings, extractor, translator, stt)
    try:
        job.load_tracks()
        if job.tracks:
            result.original_language = original_track(job.tracks, settings.allow_generated).language
        elif job._original is not None:
            result.original_language = job._original.language_name
        targets = MODE_TARGETS.get(settings.mode, ["orig"])
        reference_blocks: Optional[list[Block]] = None
        for target in targets:
            transcript = job.original() if target == "orig" else job.language(target)
            segments = clean_segments(transcript.segments, settings.drop_sound_tags)
            if not segments:
                raise ExtractionError("empty", "The captions for this video are empty.")
            if reference_blocks is None:
                blocks = build_blocks(segments, window_seconds=settings.block_seconds)
                reference_blocks = blocks
            else:
                blocks = align_to_blocks(segments, reference_blocks)

            key = target
            label = COLUMN_LABELS.get(target) or f"{transcript.language_name} (original)"
            result.texts[key] = blocks_to_text(blocks, settings.include_timestamps)
            result.labels[key] = label
            result.sources[key] = transcript.source
            result.order.append(key)

            last = segments[-1]
            result.duration_seconds = max(result.duration_seconds, last.start + last.duration)

            if transcript.machine_translated:
                continue  # script checks only make sense for real caption tracks
            joined = " ".join(s.text for s in segments[:400])
            script = detect_script_language(joined)
            if language_base(transcript.language_code) == "hi" and script == "en":
                result.warnings.append(
                    "The Hindi captions are written in Roman letters (Hinglish), not Devanagari. "
                    "Please review and correct the text in the preview before use."
                )
            if script == "mixed":
                note = "Mixed Hindi-English speech detected; English words may appear in Devanagari or vice versa."
                if note not in result.warnings:
                    result.warnings.append(note)

        result.warnings = job.warnings + result.warnings
        result.status = "done"
        result.message = "Transcript ready"
    except Exception as exc:  # any failure becomes a readable message
        err = explain_error(exc)
        result.status = "failed"
        result.error_code = err.code
        result.message = err.message
        result.texts.clear()
        result.order.clear()
    return result


def process_batch(
    refs: Sequence[VideoRef],
    titles: dict[str, str],
    settings: BatchSettings,
    extractor: TranscriptExtractor,
    translator: Optional[Translator] = None,
    stt: Optional[SpeechToTextFallback] = None,
    on_start: Optional[Callable[[int, VideoRef], None]] = None,
    on_done: Optional[Callable[[int, VideoResult], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[VideoResult]:
    """Process every video; if YouTube blocks the server several times in a
    row, the remaining videos are marked 'skipped' (they can be retried)."""
    results: list[VideoResult] = []
    blocked_in_a_row = 0
    total = len(refs)
    for i, ref in enumerate(refs):
        if settings.stop_after_blocked and blocked_in_a_row >= settings.stop_after_blocked:
            skipped = VideoResult(
                index=i, video_id=ref.video_id, url=ref.url, title=titles.get(ref.video_id, ""),
                status="skipped", error_code="blocked",
                message="Not attempted: YouTube is currently blocking this server. Use 'Retry' later.",
            )
            results.append(skipped)
            if on_done:
                on_done(i, skipped)
            continue

        if on_start:
            on_start(i, ref)
        result = process_video(i, ref, titles.get(ref.video_id, ""), settings, extractor, translator, stt)
        results.append(result)
        blocked_in_a_row = blocked_in_a_row + 1 if result.error_code == "blocked" else 0
        if on_done:
            on_done(i, result)
        extractor.clear_cache()  # keep memory low on small cloud servers
        if i < total - 1 and settings.pause_between_videos > 0:
            sleep(settings.pause_between_videos)
    return results
