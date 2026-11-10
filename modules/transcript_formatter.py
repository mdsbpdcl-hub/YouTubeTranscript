"""Turn caption segments into readable paragraphs and editable text.

The editable text format (shown in the preview box) is simple:

    [00:01:05] paragraph text ...

    [00:02:10] next paragraph ...

Lines that start with a [time] label begin a new paragraph, so the user can
correct words freely and the exports keep the timestamps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .language_processor import normalize_text, remove_sound_tags

TIMESTAMP_LINE_RE = re.compile(r"^\s*\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.*)$")


@dataclass
class Segment:
    start: float
    duration: float
    text: str


@dataclass
class Block:
    start: float
    end: float
    text: str
    label: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            self.label = format_timestamp(self.start)


def format_timestamp(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def timestamp_to_seconds(label: str) -> float:
    parts = [int(p) for p in label.split(":")]
    total = 0
    for part in parts:
        total = total * 60 + part
    return float(total)


def clean_segments(segments: Sequence[Segment], drop_sound_tags: bool = True) -> list[Segment]:
    cleaned: list[Segment] = []
    for seg in segments:
        text = normalize_text(seg.text)
        if drop_sound_tags:
            text = remove_sound_tags(text)
        if text:
            cleaned.append(Segment(start=float(seg.start), duration=float(seg.duration or 0), text=text))
    return cleaned


def build_blocks(
    segments: Sequence[Segment],
    window_seconds: int = 60,
    max_chars: int = 1200,
) -> list[Block]:
    """Group segments into paragraphs of roughly `window_seconds`.

    A paragraph also closes early at a sentence end once it is at least
    half the window long, which keeps paragraphs readable.
    """
    blocks: list[Block] = []
    current: list[Segment] = []

    def close() -> None:
        if not current:
            return
        text = " ".join(s.text for s in current)
        end = current[-1].start + current[-1].duration
        blocks.append(Block(start=current[0].start, end=end, text=text))
        current.clear()

    for seg in segments:
        if current:
            span = seg.start - current[0].start
            length = sum(len(s.text) + 1 for s in current)
            ends_sentence = current[-1].text.rstrip().endswith((".", "?", "!", "।", "॥"))
            if (
                span >= window_seconds
                or length + len(seg.text) > max_chars
                or (ends_sentence and span >= window_seconds / 2)
            ):
                close()
        current.append(seg)
    close()
    return blocks


def align_to_blocks(segments: Sequence[Segment], reference: Sequence[Block]) -> list[Block]:
    """Group `segments` using the same paragraph start times as `reference`.

    Used for bilingual output so both languages share the same timestamps.
    """
    if not reference:
        return build_blocks(segments)
    starts = [b.start for b in reference]
    buckets: list[list[Segment]] = [[] for _ in reference]
    for seg in segments:
        index = 0
        for i, start in enumerate(starts):
            if seg.start + 0.5 >= start:
                index = i
            else:
                break
        buckets[index].append(seg)
    aligned: list[Block] = []
    for ref, bucket in zip(reference, buckets):
        text = " ".join(s.text for s in bucket)
        aligned.append(Block(start=ref.start, end=ref.end, text=text, label=ref.label))
    return aligned


def blocks_to_text(blocks: Sequence[Block], include_timestamps: bool = True) -> str:
    if include_timestamps:
        return "\n\n".join(f"[{b.label}] {b.text}" for b in blocks)
    return "\n\n".join(b.text for b in blocks)


def text_to_blocks(text: str) -> list[Block]:
    """Parse (possibly user-edited) preview text back into paragraphs."""
    blocks: list[Block] = []
    current_label: Optional[str] = None
    current_lines: list[str] = []
    untimed_index = 0

    def close() -> None:
        nonlocal untimed_index
        joined = " ".join(line.strip() for line in current_lines if line.strip())
        if not joined:
            return
        if current_label is not None:
            start = timestamp_to_seconds(current_label)
            label = format_timestamp(start)
        else:
            start, label = float(untimed_index), ""
            untimed_index += 1
        block = Block(start=start, end=start, text=joined, label=label or "-")
        if not label:
            block.label = ""
        blocks.append(block)

    for raw_line in (text or "").splitlines():
        match = TIMESTAMP_LINE_RE.match(raw_line)
        if match:
            close()
            current_label = match.group(1)
            current_lines = [match.group(2)]
        elif not raw_line.strip():
            close()
            current_label = None
            current_lines = []
        else:
            current_lines.append(raw_line)
    close()
    return blocks


@dataclass
class DocumentRow:
    label: str
    texts: list[str]


@dataclass
class TranscriptDocument:
    """Language-neutral model used by every export format."""

    title: str
    video_id: str
    url: str
    columns: list[str]
    rows: list[DocumentRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_bilingual(self) -> bool:
        return len(self.columns) > 1

    @property
    def word_count(self) -> int:
        return sum(len(t.split()) for row in self.rows for t in row.texts)


def pair_blocks(primary: Sequence[Block], secondary: Sequence[Block]) -> list[DocumentRow]:
    """Align two languages by timestamp label (falls back to position)."""
    by_label = {b.label: b.text for b in secondary if b.label}
    labels_match = primary and all(b.label in by_label for b in primary if b.label)
    rows: list[DocumentRow] = []
    if labels_match and len(by_label) >= len(primary) * 0.8:
        used = set()
        for block in primary:
            other = by_label.get(block.label, "")
            used.add(block.label)
            rows.append(DocumentRow(block.label, [block.text, other]))
        for block in secondary:
            if block.label and block.label not in used:
                rows.append(DocumentRow(block.label, ["", block.text]))
        rows.sort(key=lambda r: timestamp_to_seconds(r.label) if r.label else 0)
        return rows
    for i in range(max(len(primary), len(secondary))):
        a = primary[i] if i < len(primary) else None
        b = secondary[i] if i < len(secondary) else None
        label = (a.label if a else "") or (b.label if b else "")
        rows.append(DocumentRow(label, [a.text if a else "", b.text if b else ""]))
    return rows


def build_document(
    title: str,
    video_id: str,
    url: str,
    texts: dict[str, str],
    column_names: dict[str, str],
    order: Sequence[str],
    notes: Optional[Sequence[str]] = None,
) -> TranscriptDocument:
    """Build an export document from the (edited) preview texts."""
    keys = [k for k in order if k in texts]
    columns = [column_names.get(k, k) for k in keys]
    doc = TranscriptDocument(title=title, video_id=video_id, url=url, columns=columns, notes=list(notes or []))
    if len(keys) == 1:
        doc.rows = [DocumentRow(b.label, [b.text]) for b in text_to_blocks(texts[keys[0]])]
    elif len(keys) >= 2:
        doc.rows = pair_blocks(text_to_blocks(texts[keys[0]]), text_to_blocks(texts[keys[1]]))
    return doc
