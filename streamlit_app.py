"""YouTube Batch Transcript Extractor — Streamlit app.

Four simple steps: Add Links -> Settings -> Process -> Download.
Run locally with:  streamlit run streamlit_app.py
"""

from __future__ import annotations

import hashlib
import html
import os
from typing import Any, Optional

import streamlit as st

from modules.batch_processor import BatchSettings, VideoResult, process_batch
from modules.export_manager import (
    FORMATS,
    MIME_TYPES,
    build_files,
    build_report,
    document_basename,
    export_single,
    zip_files,
)
from modules.language_processor import OUTPUT_MODES, TRANSLATION_ENGINES, Translator, google_translate_available
from modules.transcript_extractor import (
    SpeechToTextFallback,
    TranscriptExtractor,
    build_proxy_config,
    cleanup_stale_temp,
)
from modules.youtube_handler import (
    DEFAULT_MAX_BATCH,
    VideoRef,
    fetch_video_title,
    looks_like_playlist,
    parse_links,
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STEPS = ["Add Links", "Settings", "Process", "Download"]

st.set_page_config(
    page_title="YouTube Transcript Extractor",
    page_icon="📝",
    layout="centered",
    initial_sidebar_state="collapsed",
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def get_secrets() -> dict[str, Any]:
    """Secrets from Streamlit Cloud (or .streamlit/secrets.toml); {} if none."""
    try:
        return {k: st.secrets[k] for k in st.secrets.keys()}
    except Exception:
        return {}


def secret_int(name: str, default: int) -> int:
    try:
        return int(get_secrets().get(name, default))
    except (TypeError, ValueError):
        return default


MAX_BATCH = secret_int("MAX_VIDEOS_PER_BATCH", DEFAULT_MAX_BATCH)


@st.cache_data(show_spinner=False)
def load_css() -> str:
    with open(os.path.join(APP_DIR, "assets", "style.css"), encoding="utf-8") as fh:
        return fh.read()


@st.cache_data(ttl=3600, max_entries=500, show_spinner=False)
def cached_title(video_id: str) -> tuple[Optional[str], Optional[str]]:
    return fetch_video_title(video_id)


@st.cache_resource(show_spinner=False)
def get_stt() -> SpeechToTextFallback:
    return SpeechToTextFallback(get_secrets())


@st.cache_resource(show_spinner=False)
def startup_cleanup() -> int:
    """Runs once per server start: remove temp folders left by a restart."""
    return cleanup_stale_temp(max_age_seconds=0)


def esc(text: Any) -> str:
    return html.escape(str(text or ""))


def tiles(items: list[tuple[Any, str]]) -> None:
    cells = "".join(f'<div class="tile"><div class="v">{esc(v)}</div><div class="k">{esc(k)}</div></div>' for v, k in items)
    st.markdown(f'<div class="tiles">{cells}</div>', unsafe_allow_html=True)


def note(text: str, kind: str = "") -> None:
    st.markdown(f'<div class="note {kind}">{text}</div>', unsafe_allow_html=True)


def page_heading(question: str, subtitle: str) -> None:
    st.markdown(f'<div class="page-q">{esc(question)}</div><div class="page-sub">{esc(subtitle)}</div>',
                unsafe_allow_html=True)


def render_header() -> None:
    st.markdown(
        """
        <div class="app-header">
          <div class="app-brand"><div class="app-logo">YT</div>
            <div class="app-name">YouTube Transcript Extractor</div></div>
          <div class="app-note">Online app · processed on a temporary server · nothing is kept</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_stepper(current: int) -> None:
    parts = []
    for i, name in enumerate(STEPS, start=1):
        state = "done" if i < current else ("active" if i == current else "")
        mark = "✓" if i < current else str(i)
        parts.append(f'<div class="step {state}"><div class="dot">{mark}</div><div class="label">{name}</div></div>')
        if i < len(STEPS):
            parts.append(f'<div class="step-line {"done" if i < current else ""}"></div>')
    st.markdown(f'<div class="stepper">{"".join(parts)}</div>', unsafe_allow_html=True)


def status_tag(status: str) -> str:
    return {
        "done": '<span class="tag ok">Ready</span>',
        "failed": '<span class="tag bad">Error</span>',
        "skipped": '<span class="tag warn">Skipped</span>',
        "running": '<span class="tag run">Working…</span>',
        "waiting": '<span class="tag">Waiting</span>',
    }.get(status, f'<span class="tag">{esc(status)}</span>')


def video_list_html(rows: list[tuple[str, str, str, str]]) -> str:
    """rows: (number, title, subtitle, tag_html)."""
    body = "".join(
        f'<div class="vrow"><div class="num">{esc(n)}</div>'
        f'<div class="title">{esc(t)}<div class="sub">{esc(s)}</div></div>{tag}</div>'
        for n, t, s, tag in rows
    )
    return f'<div class="vlist">{body}</div>'


# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #
def init_state() -> None:
    defaults = {
        "step": 1,
        "links_raw": "",
        "mode": "hindi",
        "formats": ["txt", "docx", "pdf"],
        "include_timestamps": True,
        "translation_engine": "auto",
        "allow_generated": True,
        "drop_sound_tags": True,
        "block_seconds": 60,
        "pause_between_videos": 1.0,
        "use_stt": False,
        "stt_confirmed": False,
        "results": {},          # video_id -> VideoResult
        "titles": {},           # video_id -> title
        "run_signature": "",
        "edit_version": 0,
        "files_cache": None,    # (signature, {path: bytes}, zip bytes)
        "authenticated": False,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def current_refs() -> list[VideoRef]:
    return parse_links(st.session_state.links_raw, MAX_BATCH).valid


def current_settings() -> BatchSettings:
    s = st.session_state
    return BatchSettings(
        mode=s.mode,
        include_timestamps=s.include_timestamps,
        drop_sound_tags=s.drop_sound_tags,
        translation_engine=s.translation_engine,
        allow_generated=s.allow_generated,
        block_seconds=int(s.block_seconds),
        pause_between_videos=float(s.pause_between_videos),
        use_speech_to_text=bool(s.use_stt and s.stt_confirmed),
    )


def settings_signature() -> str:
    """Results are reused only while links + settings are unchanged."""
    from dataclasses import replace

    output_settings = replace(current_settings(), pause_between_videos=0.0)  # pause doesn't change the text
    raw = repr((tuple(r.video_id for r in current_refs()), output_settings))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def go(step: int) -> None:
    st.session_state.step = step


def ordered_results() -> list[VideoResult]:
    results: dict[str, VideoResult] = st.session_state.results
    return [results[r.video_id] for r in current_refs() if r.video_id in results]


PERSISTENT_WIDGETS = [
    "links_raw", "mode", "formats", "include_timestamps", "translation_engine", "allow_generated",
    "drop_sound_tags", "block_seconds", "pause_between_videos", "use_stt", "stt_confirmed",
]


def keep_widget_values() -> None:
    """Streamlit forgets widget values on pages that are not shown; re-store them every run."""
    for key in PERSISTENT_WIDGETS:
        if key in st.session_state:
            st.session_state[key] = st.session_state[key]


# --------------------------------------------------------------------------- #
# Access control (optional)
# --------------------------------------------------------------------------- #
def password_gate() -> bool:
    password = str(get_secrets().get("APP_PASSWORD", "") or "")
    if not password or st.session_state.authenticated:
        return True
    page_heading("This app is private", "Enter the access password to continue.")
    with st.form("login"):
        typed = st.text_input("Password", type="password")
        if st.form_submit_button("Open app", type="primary"):
            if typed == password:
                st.session_state.authenticated = True
                st.rerun()
            st.error("Wrong password.")
    return False


def add_links_from_file() -> None:
    uploaded = st.session_state.get("links_file")
    if uploaded is None:
        return
    text = uploaded.getvalue().decode("utf-8", errors="ignore")
    st.session_state.links_raw = (st.session_state.links_raw.rstrip() + "\n" + text).strip()


# --------------------------------------------------------------------------- #
# Step 1 — Add links
# --------------------------------------------------------------------------- #
def step_add_links() -> None:
    page_heading("Which videos do you want transcripts for?",
                 f"Paste YouTube links — one per line, up to {MAX_BATCH} at a time.")

    st.text_area(
        "YouTube links",
        key="links_raw",
        height=170,
        placeholder="https://www.youtube.com/watch?v=...\nhttps://youtu.be/...\nhttps://www.youtube.com/shorts/...",
        label_visibility="collapsed",
    )

    with st.expander("More ways to add links"):
        uploaded = st.file_uploader("Upload a .txt or .csv file with links", type=["txt", "csv"], key="links_file")
        st.button("Add links from file", on_click=add_links_from_file, disabled=uploaded is None)

    parsed = parse_links(st.session_state.links_raw, MAX_BATCH)
    if not st.session_state.links_raw.strip():
        note("Works with normal, short (youtu.be), Shorts, live and embed links. "
             "Playlists are not expanded — paste the individual video links.")
        return

    tiles([
        (len(parsed.valid) + len(parsed.over_limit) + len(parsed.invalid) + len(parsed.duplicates), "Links pasted"),
        (len(parsed.valid), "Ready"),
        (len(parsed.duplicates), "Duplicates removed"),
        (len(parsed.invalid), "Not recognised"),
    ])

    rows = [(str(i + 1), r.video_id, r.original_input, status_tag("done"))
            for i, r in enumerate(parsed.valid)]
    rows += [("–", bad, "Not a YouTube video link", '<span class="tag bad">Skipped</span>') for bad in parsed.invalid[:20]]
    st.markdown(video_list_html(rows), unsafe_allow_html=True)

    if parsed.over_limit:
        note(f"Only the first <b>{MAX_BATCH}</b> videos will be processed in this batch "
             f"({len(parsed.over_limit)} more were left out). Process them as a second batch.", "warn")
    if looks_like_playlist(st.session_state.links_raw):
        note("A playlist link was found. Playlists are not expanded — paste each video's link.", "warn")


# --------------------------------------------------------------------------- #
# Step 2 — Settings
# --------------------------------------------------------------------------- #
def step_settings() -> None:
    page_heading("How should the transcripts look?", "Choose the language and the file types you need.")

    with st.container(border=True):
        st.radio(
            "Transcript language",
            options=list(OUTPUT_MODES.keys()),
            format_func=lambda k: OUTPUT_MODES[k],
            key="mode",
        )
        st.multiselect(
            "File types",
            options=list(FORMATS.keys()),
            format_func=lambda k: FORMATS[k],
            key="formats",
        )
        st.toggle("Show time stamps like [00:01:05]", key="include_timestamps")

    with st.expander("Advanced options"):
        engines = dict(TRANSLATION_ENGINES)
        if not google_translate_available():
            engines.pop("google", None)
        st.selectbox("Translation", options=list(engines.keys()), format_func=lambda k: engines[k],
                     key="translation_engine")
        st.checkbox("Use YouTube's auto-generated captions when no uploaded captions exist",
                    key="allow_generated")
        st.checkbox("Remove [Music] / [Applause] type tags", key="drop_sound_tags")
        st.slider("Paragraph length (seconds)", 20, 180, step=10, key="block_seconds")
        st.slider("Pause between videos (seconds) — higher is gentler on YouTube", 0.0, 5.0, step=0.5,
                  key="pause_between_videos")

        stt_status = get_stt().status()
        st.checkbox("Speech-to-text when a video has no captions (slow, experimental)",
                    key="use_stt", disabled=not stt_status.available)
        st.caption(stt_status.reason)
        if stt_status.available and st.session_state.use_stt:
            st.checkbox("I confirm I have permission to download and process the audio of these videos.",
                        key="stt_confirmed")

    if not st.session_state.formats:
        note("Pick at least one file type.", "warn")
    if st.session_state.mode in ("hindi", "english", "bilingual") and st.session_state.translation_engine != "none":
        note("If a video has no captions in the language you chose, the text is <b>machine translated</b>. "
             "You can correct it in the next step.")


# --------------------------------------------------------------------------- #
# Step 3 — Process
# --------------------------------------------------------------------------- #
def run_processing(refs: list[VideoRef]) -> None:
    secrets = get_secrets()
    settings = current_settings()
    extractor = TranscriptExtractor(proxy_config=build_proxy_config(secrets))
    translator = Translator() if google_translate_available() else None
    stt = get_stt() if settings.use_speech_to_text else None

    total = len(refs)
    progress = st.progress(0.0, text="Starting…")
    board = st.empty()
    statuses = {r.video_id: "waiting" for r in refs}
    titles: dict[str, str] = st.session_state.titles
    position = {r.video_id: n for n, r in enumerate(current_refs())}

    def draw() -> None:
        rows = []
        for i, r in enumerate(refs):
            res = st.session_state.results.get(r.video_id)
            sub = res.message if res and statuses[r.video_id] in ("done", "failed", "skipped") else r.url
            rows.append((str(i + 1), titles.get(r.video_id) or r.video_id, sub, status_tag(statuses[r.video_id])))
        board.markdown(video_list_html(rows), unsafe_allow_html=True)

    def on_start(i: int, ref: VideoRef) -> None:
        if ref.video_id not in titles:
            title, _problem = cached_title(ref.video_id)
            titles[ref.video_id] = title or ""
        statuses[ref.video_id] = "running"
        progress.progress(i / total, text=f"Video {i + 1} of {total}: {titles.get(ref.video_id) or ref.video_id}")
        draw()

    def on_done(i: int, result: VideoResult) -> None:
        # i counts within this run (retries/continuations are a subset);
        # keep the video's number from the full list instead.
        result.index = position.get(result.video_id, i)
        result.title = result.title or titles.get(result.video_id, "")
        st.session_state.results[result.video_id] = result   # saved immediately
        statuses[result.video_id] = result.status
        progress.progress((i + 1) / total, text=f"Finished {i + 1} of {total}")
        draw()

    draw()
    process_batch(refs, titles, settings, extractor, translator, stt, on_start=on_start, on_done=on_done)
    progress.progress(1.0, text="All done")
    st.session_state.files_cache = None
    st.session_state.edit_version += 1


def save_edit(video_id: str, lang: str, key: str) -> None:
    result: VideoResult = st.session_state.results[video_id]
    result.texts[lang] = st.session_state[key]
    st.session_state.edit_version += 1
    st.session_state.files_cache = None


def preview_and_correct(done: list[VideoResult]) -> None:
    st.markdown('<div class="page-q" style="font-size:18px;margin-top:14px">Preview and correct</div>'
                '<div class="page-sub">Edit any word below — your changes are used in the downloads. '
                'Keep the [time] labels at the start of paragraphs.</div>', unsafe_allow_html=True)
    options = {r.video_id: f"{r.index + 1}. {r.display_title}" for r in done}
    chosen = st.selectbox("Video", options=list(options.keys()), format_func=lambda v: options[v],
                          label_visibility="collapsed")
    result: VideoResult = st.session_state.results[chosen]

    for warning in result.warnings:
        note(esc(warning), "warn")
    meta = " · ".join(f"<b>{esc(result.labels[k])}</b>: {esc(result.sources.get(k, ''))}" for k in result.order)
    note(meta)

    tabs = st.tabs([result.labels[k] for k in result.order])
    for tab, lang in zip(tabs, result.order):
        with tab:
            key = f"edit_{chosen}_{lang}"
            if key not in st.session_state:
                st.session_state[key] = result.texts[lang]
            st.text_area(result.labels[lang], key=key, height=380, label_visibility="collapsed",
                         on_change=save_edit, args=(chosen, lang, key))
            st.caption(f"{len(result.texts[lang].split()):,} words")


def step_process() -> None:
    refs = current_refs()
    page_heading("Ready to get the transcripts?",
                 "Each video is handled one by one. If one fails, the rest still continue.")

    signature = settings_signature()
    if st.session_state.run_signature != signature:
        # Links or settings changed: old results no longer match.
        if st.session_state.results:
            st.session_state.results = {}
            st.session_state.files_cache = None
            for key in [k for k in st.session_state.keys() if str(k).startswith("edit_")]:
                del st.session_state[key]
        st.session_state.run_signature = signature

    results = ordered_results()
    pending = [r for r in refs if r.video_id not in st.session_state.results]
    retryable = [r for r in refs if r.video_id in st.session_state.results
                 and st.session_state.results[r.video_id].status in ("failed", "skipped")]

    if not results:
        tiles([
            (len(refs), "Videos"),
            (OUTPUT_MODES[st.session_state.mode].split(" (")[0].split(" —")[0], "Language"),
            (", ".join(f.upper() for f in st.session_state.formats) or "—", "Files"),
            ("On" if st.session_state.include_timestamps else "Off", "Time stamps"),
        ])
        if st.button(f"Start processing {len(refs)} video(s)", type="primary", width="stretch"):
            run_processing(refs)
            st.rerun()
        note("Uses the captions already on YouTube (fast and light). Not every video can be transcribed — "
             "videos without captions, private or age-restricted videos may fail and will be explained.")
        return

    if pending:
        note(f"{len(pending)} video(s) were not processed yet (the page may have been interrupted).", "warn")
        if st.button(f"Continue with {len(pending)} remaining video(s)", type="primary"):
            run_processing(pending)
            st.rerun()

    done = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    tiles([
        (len(results), "Processed"),
        (len(done), "Ready"),
        (len(failed), "Could not process"),
        (f"{sum(r.word_count() for r in done):,}", "Words"),
    ])

    rows = [(str(r.index + 1), r.display_title,
             r.message if not r.ok else " · ".join(r.labels[k] for k in r.order), status_tag(r.status))
            for r in results]
    st.markdown(video_list_html(rows), unsafe_allow_html=True)

    if failed:
        if any(r.error_code == "blocked" for r in failed):
            note("<b>YouTube is blocking this server.</b> Free cloud servers share internet addresses that YouTube "
                 "sometimes blocks. Wait and retry, run the app on your own computer, or add a proxy in Secrets "
                 "(see README).", "bad")
        if retryable and st.button(f"Retry {len(retryable)} failed video(s)"):
            for r in retryable:
                st.session_state.results.pop(r.video_id, None)
            run_processing(retryable)
            st.rerun()

    if done:
        preview_and_correct(done)
    note("Results are kept only while this browser tab stays open. If the app restarts or the session expires, "
         "process the links again.")


# --------------------------------------------------------------------------- #
# Step 4 — Download
# --------------------------------------------------------------------------- #
def prepared_files() -> tuple[dict[str, bytes], bytes]:
    formats = st.session_state.formats or ["txt"]
    signature = (st.session_state.run_signature, st.session_state.edit_version, tuple(formats))
    cache = st.session_state.files_cache
    if cache and cache[0] == signature:
        return cache[1], cache[2]

    results = ordered_results()
    docs = [r.to_document() for r in results if r.ok]
    report = build_report((str(r.index + 1), r.display_title, r.status.capitalize(), r.message) for r in results)
    bar = st.progress(0.0, text="Preparing files…")
    files = build_files(docs, formats, report=report,
                        on_progress=lambda d, t: bar.progress(d / t, text=f"Preparing files… {d}/{t}"))
    archive = zip_files(files)
    bar.empty()
    st.session_state.files_cache = (signature, files, archive)
    return files, archive


def step_download() -> None:
    page_heading("Download your transcripts", "Everything in one ZIP, or pick single files below.")
    results = ordered_results()
    done = [r for r in results if r.ok]
    if not done:
        note("No transcripts are ready yet. Go back to <b>Process</b>.", "warn")
        return

    files, archive = prepared_files()
    tiles([
        (len(done), "Transcripts"),
        (len(results) - len(done), "Not processed"),
        (len(files), "Files in ZIP"),
        (f"{len(archive) / 1024:,.0f} KB", "ZIP size"),
    ])

    st.download_button("Download all as ZIP", data=archive, file_name="YouTube_Transcripts.zip",
                       mime=MIME_TYPES["zip"], type="primary", width="stretch", on_click="ignore")

    formats = st.session_state.formats or ["txt"]
    st.markdown("**One combined file with all videos**")
    cols = st.columns(len(formats))
    for col, fmt in zip(cols, formats):
        with col:
            st.download_button(FORMATS[fmt], data=files[f"All_Transcripts.{fmt}"],
                               file_name=f"All_Transcripts.{fmt}", mime=MIME_TYPES[fmt],
                               width="stretch", key=f"all_{fmt}", on_click="ignore")

    with st.expander("Individual files"):
        for r in done:
            doc = r.to_document()
            base = document_basename(doc, r.index)
            st.markdown(f"**{r.index + 1}. {esc(r.display_title)}**")
            cols = st.columns(len(formats))
            for col, fmt in zip(cols, formats):
                path = f"Individual/{base}.{fmt}"
                data = files.get(path) or export_single(doc, fmt)
                with col:
                    st.download_button(fmt.upper(), data=data, file_name=f"{base}.{fmt}", mime=MIME_TYPES[fmt],
                                       key=f"dl_{r.video_id}_{fmt}", width="stretch", on_click="ignore")

    failed = [r for r in results if not r.ok]
    if failed:
        with st.expander(f"Videos that could not be processed ({len(failed)})"):
            for r in failed:
                st.markdown(f"**{r.index + 1}. {esc(r.display_title)}** — {esc(r.message)}")

    note("Files are created on a temporary server only for this download and are not stored. "
         "Machine translations should be checked before official use.")


# --------------------------------------------------------------------------- #
# Navigation bar
# --------------------------------------------------------------------------- #
def nav_bar(step: int) -> None:
    refs = current_refs()
    can_next = {
        1: bool(refs),
        2: bool(st.session_state.formats),
        3: any(r.ok for r in ordered_results()),
        4: False,
    }[step]
    with st.container(key="nav_bar"):
        left, mid, right = st.columns([1, 2, 1])
        with left:
            if step > 1:
                st.button("← Back", on_click=go, args=(step - 1,), width="stretch")
        with mid:
            st.markdown(f'<div style="text-align:center;color:#6b7280;font-size:13px;padding-top:8px">'
                        f'Step {step} of 4 · {STEPS[step - 1]}</div>', unsafe_allow_html=True)
        with right:
            if step < 4:
                st.button("Next →", on_click=go, args=(step + 1,), type="primary",
                          disabled=not can_next, width="stretch")
            else:
                st.button("Start over", on_click=start_over, width="stretch")


def start_over() -> None:
    keep = {"authenticated": st.session_state.get("authenticated", False)}
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.session_state.update(keep)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    st.markdown(f"<style>{load_css()}</style>", unsafe_allow_html=True)
    startup_cleanup()
    init_state()
    keep_widget_values()
    render_header()
    if not password_gate():
        return

    step = int(st.session_state.step)
    if step > 1 and not current_refs():
        step = st.session_state.step = 1
    render_stepper(step)

    if step == 1:
        step_add_links()
    elif step == 2:
        step_settings()
    elif step == 3:
        step_process()
    else:
        step_download()
    nav_bar(step)


main()
