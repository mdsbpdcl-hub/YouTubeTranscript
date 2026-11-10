"""Parse YouTube links and look up basic video details.

Everything here is lightweight: link parsing is pure Python and the title
lookup uses YouTube's public oEmbed endpoint (no API key, no downloads).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
    "youtu.be",
    "www.youtu.be",
}

# Paths of the form /<prefix>/<VIDEO_ID>
PATH_PREFIXES = ("shorts", "embed", "live", "v", "e")

DEFAULT_MAX_BATCH = 50


@dataclass(frozen=True)
class VideoRef:
    """One video the user asked for."""

    video_id: str
    url: str
    original_input: str


@dataclass
class ParsedLinks:
    valid: list[VideoRef] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    over_limit: list[VideoRef] = field(default_factory=list)


def canonical_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _plausible_bare_id(candidate: str) -> bool:
    """A bare 11-character ID must not look like an ordinary word.

    Real IDs are random, so they almost always contain a digit, '-' or '_',
    or several capital letters. This stops words like "Information" in
    pasted text from being treated as videos.
    """
    if any(c.isdigit() or c in "-_" for c in candidate):
        return True
    return sum(c.isupper() for c in candidate[1:]) >= 2


def extract_video_id(text: str) -> Optional[str]:
    """Return the 11-character video ID from a link or bare ID, else None."""
    if not text:
        return None
    candidate = text.strip().strip("<>\"'")
    # Links pasted from sentences often end with punctuation: "...?v=ID)."
    candidate = candidate.rstrip(").,;:!]}>\"'").lstrip("([{<")
    if not candidate:
        return None

    if VIDEO_ID_RE.match(candidate):
        return candidate if _plausible_bare_id(candidate) else None

    if "://" not in candidate:
        candidate = "https://" + candidate

    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        return None

    path_parts = [p for p in parsed.path.split("/") if p]

    if host.endswith("youtu.be"):
        if path_parts and VIDEO_ID_RE.match(path_parts[0]):
            return path_parts[0]
        return None

    query = parse_qs(parsed.query)
    if "v" in query and query["v"] and VIDEO_ID_RE.match(query["v"][0]):
        return query["v"][0]

    if len(path_parts) >= 2 and path_parts[0] in PATH_PREFIXES:
        if VIDEO_ID_RE.match(path_parts[1]):
            return path_parts[1]

    # attribution_link?u=/watch%3Fv%3D...
    if "u" in query and query["u"]:
        inner = query["u"][0]
        match = re.search(r"v=([A-Za-z0-9_-]{11})", inner)
        if match:
            return match.group(1)

    return None


def split_input(raw: str) -> list[str]:
    """Split pasted text into candidate links (newlines, commas, spaces)."""
    if not raw:
        return []
    pieces = re.split(r"[\s,;]+", raw)
    return [p.strip() for p in pieces if p.strip()]


def parse_links(raw: str, max_batch: int = DEFAULT_MAX_BATCH) -> ParsedLinks:
    """Turn pasted text into a de-duplicated, size-limited list of videos."""
    result = ParsedLinks()
    seen: set[str] = set()
    for piece in split_input(raw):
        video_id = extract_video_id(piece)
        if not video_id:
            result.invalid.append(piece)
            continue
        if video_id in seen:
            result.duplicates.append(piece)
            continue
        seen.add(video_id)
        ref = VideoRef(video_id=video_id, url=canonical_url(video_id), original_input=piece)
        if len(result.valid) >= max_batch:
            result.over_limit.append(ref)
        else:
            result.valid.append(ref)
    return result


def looks_like_playlist(raw: str) -> bool:
    """True if the text contains a playlist link without a video ID."""
    for piece in split_input(raw):
        if "list=" in piece and extract_video_id(piece) is None:
            return True
    return False


def fetch_video_title(
    video_id: str,
    session: Optional[requests.Session] = None,
    timeout: float = 6.0,
) -> tuple[Optional[str], Optional[str]]:
    """Return (title, problem) using the public oEmbed endpoint.

    `problem` is a short plain-language hint when YouTube says the video is
    private or missing; network errors are ignored (title just stays empty).
    """
    http = session or requests
    try:
        response = http.get(
            "https://www.youtube.com/oembed",
            params={"url": canonical_url(video_id), "format": "json"},
            timeout=timeout,
        )
    except requests.RequestException:
        return None, None

    if response.status_code == 200:
        try:
            return (response.json().get("title") or None), None
        except ValueError:
            return None, None
    if response.status_code in (401, 403):
        return None, "Private or embedding disabled"
    if response.status_code == 404:
        return None, "Video not found"
    return None, None
