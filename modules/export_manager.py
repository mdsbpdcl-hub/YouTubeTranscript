"""Create TXT, DOCX, PDF and ZIP files in memory (nothing is written to disk).

Hindi is exported as real Unicode Devanagari:
* TXT  — UTF-8.
* DOCX — complex-script font set to "Nirmala UI" (falls back to "Mangal").
* PDF  — bundled Noto Sans Devanagari font with HarfBuzz text shaping, so
         conjuncts and matras are drawn correctly.
"""

from __future__ import annotations

import io
import os
import re
import unicodedata
import zipfile
from datetime import datetime
from typing import Iterable, Optional, Sequence

from .transcript_formatter import TranscriptDocument

ACCENT_DARK = (0x2F, 0x6F, 0x47)  # dark green headings
TEXT_GREY = (0x4B, 0x55, 0x63)
MUTED = (0x6B, 0x72, 0x80)

FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")
SYSTEM_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Regular.ttf",
]

FORMATS = {"txt": "Text (.txt)", "docx": "Word (.docx)", "pdf": "PDF (.pdf)"}


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def clean_for_export(text: str) -> str:
    """Remove control characters (they make Word files invalid)."""
    return _CONTROL_RE.sub("", text or "")


# --------------------------------------------------------------------------- #
# File names
# --------------------------------------------------------------------------- #
def safe_filename(name: str, fallback: str = "transcript", max_length: int = 60) -> str:
    """Make a file name safe on Windows/macOS/Linux while keeping Hindi letters."""
    name = unicodedata.normalize("NFC", name or "")
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", name)
    name = re.sub(r"[\s_]+", "_", name).strip("._ ")
    if len(name) > max_length:
        cut = max_length
        # never cut between a letter and its matra / virama / nukta
        while cut > 1 and unicodedata.category(name[cut]) in ("Mn", "Mc"):
            cut -= 1
        if cut > 1 and name[cut - 1] == "\u094d":  # dangling virama
            cut -= 1
        name = name[:cut].rstrip("._ ")
    return name or fallback


def document_basename(doc: TranscriptDocument, index: Optional[int] = None) -> str:
    base = safe_filename(doc.title, fallback=doc.video_id)
    if base != doc.video_id:
        base = f"{base}_{doc.video_id}"
    return f"{index + 1:02d}_{base}" if index is not None else base


# --------------------------------------------------------------------------- #
# TXT
# --------------------------------------------------------------------------- #
def _doc_txt_lines(doc: TranscriptDocument) -> list[str]:
    lines = [doc.title, doc.url, " | ".join(doc.columns)]
    lines += [f"Note: {n}" for n in doc.notes]
    lines.append("=" * 60)
    lines.append("")
    for row in doc.rows:
        prefix = f"[{row.label}] " if row.label else ""
        if doc.is_bilingual:
            if row.label:
                lines.append(f"[{row.label}]")
            for column, text in zip(doc.columns, row.texts):
                if text:
                    lines.append(f"{column}: {text}")
        else:
            lines.append(prefix + row.texts[0])
        lines.append("")
    return lines


def export_txt(doc: TranscriptDocument) -> bytes:
    return clean_for_export("\n".join(_doc_txt_lines(doc))).encode("utf-8")


def export_txt_combined(docs: Sequence[TranscriptDocument]) -> bytes:
    parts = []
    for i, doc in enumerate(docs):
        parts.append(f"##### {i + 1}. #####")
        parts.extend(_doc_txt_lines(doc))
        parts.append("")
    return clean_for_export("\n".join(parts)).encode("utf-8")


# --------------------------------------------------------------------------- #
# DOCX
# --------------------------------------------------------------------------- #
def _set_run_fonts(run, latin: str = "Calibri", complex_script: str = "Nirmala UI") -> None:
    from docx.oxml.ns import qn

    run.font.name = latin
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), latin)
    rfonts.set(qn("w:hAnsi"), latin)
    rfonts.set(qn("w:cs"), complex_script)
    rfonts.set(qn("w:eastAsia"), latin)
    lang = rpr.find(qn("w:lang"))
    if lang is None:
        lang = rpr.makeelement(qn("w:lang"), {})
        rpr.append(lang)
    lang.set(qn("w:bidi"), "hi-IN")


def _set_cs_size(rpr, half_points: int) -> None:
    """Set the complex-script (Devanagari) size right after <w:sz> (schema order)."""
    from docx.oxml.ns import qn

    existing = rpr.find(qn("w:szCs"))
    if existing is not None:
        existing.set(qn("w:val"), str(half_points))
        return
    size_cs = rpr.makeelement(qn("w:szCs"), {qn("w:val"): str(half_points)})
    size = rpr.find(qn("w:sz"))
    if size is not None:
        size.addnext(size_cs)
    else:
        rpr.append(size_cs)


def _style_document(document) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt

    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    rpr = normal.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.insert(0, rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia"):
        rfonts.set(qn(attr), "Calibri")
    rfonts.set(qn("w:cs"), "Nirmala UI")
    _set_cs_size(rpr, 22)
    for section in document.sections:
        section.left_margin = section.right_margin = Pt(60)
        section.top_margin = section.bottom_margin = Pt(54)


def _add_text(paragraph, text: str, bold: bool = False, size: Optional[float] = None,
              color: Optional[tuple[int, int, int]] = None):
    from docx.shared import Pt, RGBColor

    run = paragraph.add_run(clean_for_export(text))
    _set_run_fonts(run)
    run.bold = bold
    if bold:
        run.font.cs_bold = True  # bold for Devanagari (complex script) too
    if color:
        run.font.color.rgb = RGBColor(*color)
    if size:
        run.font.size = Pt(size)
        _set_cs_size(run._element.get_or_add_rPr(), int(round(size * 2)))
    return run


def _docx_write_document(document, doc: TranscriptDocument) -> None:
    from docx.shared import Pt

    heading = document.add_paragraph()
    _add_text(heading, doc.title, bold=True, size=16, color=ACCENT_DARK)
    meta = document.add_paragraph()
    _add_text(meta, doc.url, size=9, color=MUTED)
    meta.paragraph_format.space_after = Pt(2)
    for note in doc.notes:
        p = document.add_paragraph()
        _add_text(p, note, size=9, color=MUTED)
        p.paragraph_format.space_after = Pt(0)
    document.add_paragraph()

    if doc.is_bilingual:
        table = document.add_table(rows=1, cols=len(doc.columns) + 1)
        table.style = "Table Grid"
        header = table.rows[0].cells
        _add_text(header[0].paragraphs[0], "Time", bold=True, size=9, color=ACCENT_DARK)
        for i, column in enumerate(doc.columns):
            _add_text(header[i + 1].paragraphs[0], column, bold=True, size=10, color=ACCENT_DARK)
        for row in doc.rows:
            cells = table.add_row().cells
            _add_text(cells[0].paragraphs[0], row.label, size=8, color=MUTED)
            for i, text in enumerate(row.texts):
                _add_text(cells[i + 1].paragraphs[0], text, size=10.5)
        table.autofit = False
        widths = [Pt(50)] + [Pt(210)] * len(doc.columns)
        for column, width in zip(table.columns, widths):
            column.width = width
        for row in table.rows:
            for cell, width in zip(row.cells, widths):
                cell.width = width
    else:
        for row in doc.rows:
            p = document.add_paragraph()
            if row.label:
                _add_text(p, f"[{row.label}]  ", size=8.5, color=ACCENT_DARK)
            _add_text(p, row.texts[0], size=11)
            p.paragraph_format.space_after = Pt(8)


def export_docx(doc: TranscriptDocument) -> bytes:
    return export_docx_combined([doc], title=None)


def export_docx_combined(docs: Sequence[TranscriptDocument], title: Optional[str] = "All transcripts") -> bytes:
    from docx import Document
    from docx.enum.text import WD_BREAK

    document = Document()
    _style_document(document)
    if title:
        p = document.add_paragraph()
        _add_text(p, title, bold=True, size=20, color=ACCENT_DARK)
        p2 = document.add_paragraph()
        _add_text(p2, f"{len(docs)} video(s) · generated {datetime.now():%d %b %Y, %H:%M}", size=9, color=MUTED)
        for i, d in enumerate(docs):
            item = document.add_paragraph()
            _add_text(item, f"{i + 1}. {d.title}", size=10.5)
    for i, doc in enumerate(docs):
        if title or i > 0:
            document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        _docx_write_document(document, doc)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def find_pdf_fonts() -> tuple[str, str]:
    """Return (regular, bold) font paths that support Devanagari."""
    regular = os.path.join(FONT_DIR, "NotoSansDevanagari-Regular.ttf")
    bold = os.path.join(FONT_DIR, "NotoSansDevanagari-Bold.ttf")
    if os.path.exists(regular):
        return regular, bold if os.path.exists(bold) else regular
    for path in SYSTEM_FONT_CANDIDATES:
        if os.path.exists(path):
            bold_path = path.replace("Regular", "Bold")
            return path, bold_path if os.path.exists(bold_path) else path
    raise FileNotFoundError("No Devanagari font found for PDF export (expected assets/fonts/).")


# Extra fonts used only when a transcript contains other scripts (e.g. Bengali,
# Gurmukhi, Urdu in "original language" mode). On Streamlit Cloud they come
# from the `fonts-noto-core` system package (packages.txt); any .ttf placed in
# assets/fonts/extra/ is also used.
FALLBACK_FONT_DIRS = ["/usr/share/fonts/truetype/noto", "/usr/share/fonts/opentype/noto"]
FALLBACK_FONT_NAMES = [
    "NotoSans-Regular.ttf", "NotoSansBengali-Regular.ttf", "NotoSansGurmukhi-Regular.ttf",
    "NotoSansGujarati-Regular.ttf", "NotoSansOriya-Regular.ttf", "NotoSansTamil-Regular.ttf",
    "NotoSansTelugu-Regular.ttf", "NotoSansKannada-Regular.ttf", "NotoSansMalayalam-Regular.ttf",
    "NotoSansSinhala-Regular.ttf", "NotoNaskhArabic-Regular.ttf", "NotoSansArabic-Regular.ttf",
    "NotoSansThai-Regular.ttf", "NotoSansHebrew-Regular.ttf",
]
_CMAP_CACHE: dict[str, frozenset[int]] = {}


def _font_cmap(path: str) -> frozenset[int]:
    if path not in _CMAP_CACHE:
        from fontTools.ttLib import TTFont

        try:
            _CMAP_CACHE[path] = frozenset(TTFont(path, lazy=True).getBestCmap().keys())
        except Exception:
            _CMAP_CACHE[path] = frozenset()
    return _CMAP_CACHE[path]


def fallback_font_paths() -> list[str]:
    paths = []
    extra_dir = os.path.join(FONT_DIR, "extra")
    if os.path.isdir(extra_dir):
        paths += sorted(os.path.join(extra_dir, f) for f in os.listdir(extra_dir) if f.lower().endswith((".ttf", ".otf")))
    for name in FALLBACK_FONT_NAMES:
        for folder in FALLBACK_FONT_DIRS:
            path = os.path.join(folder, name)
            if os.path.exists(path):
                paths.append(path)
                break
    return paths


def _is_drawable_control(ch: str) -> bool:
    return unicodedata.category(ch) in ("Mn", "Mc", "Cf")


def _supported_text(text: str, cmap: frozenset[int]) -> str:
    """Replace characters no available font can draw (e.g. emoji) to avoid PDF errors."""
    out = []
    for ch in text:
        if ord(ch) in cmap or _is_drawable_control(ch):
            out.append(ch)
        elif ch.isspace():
            out.append(" ")
        else:
            out.append("?" if unicodedata.category(ch).startswith(("L", "N", "P")) else "")
    return "".join(out)


class _PdfWriter:
    def __init__(self, docs: Sequence[TranscriptDocument] = ()):
        from fpdf import FPDF

        regular, bold = find_pdf_fonts()
        self.pdf = FPDF(format="A4")
        self.pdf.set_margins(18, 18, 18)
        self.pdf.set_auto_page_break(True, margin=18)
        self.pdf.add_font("Noto", "", regular)
        self.pdf.add_font("Noto", "B", bold)
        try:
            self.pdf.set_text_shaping(True)
        except Exception:  # uharfbuzz missing: text still readable, shaping basic
            pass
        self.primary_cmap = _font_cmap(regular)
        self.cmap = self._add_fallbacks(docs)
        self._width_cache: dict[tuple[str, float, str], float] = {}
        self.pdf.set_title("YouTube transcripts")
        self.pdf.set_creator("YouTube Batch Transcript Extractor")

    def _add_fallbacks(self, docs: Sequence[TranscriptDocument]) -> frozenset[int]:
        """Register extra fonts only for scripts that actually appear."""
        needed: set[int] = set()
        for doc in docs:
            for chunk in [doc.title, *doc.notes, *(t for row in doc.rows for t in row.texts)]:
                needed.update(ord(c) for c in chunk if ord(c) not in self.primary_cmap and not c.isspace())
        if not needed:
            return self.primary_cmap
        names, cmap = [], set(self.primary_cmap)
        for i, path in enumerate(fallback_font_paths()):
            font_cmap = _font_cmap(path)
            if needed & font_cmap:
                name = f"Fallback{i}"
                try:
                    self.pdf.add_font(name, "", path)
                except Exception:
                    continue
                names.append(name)
                cmap |= font_cmap
                needed -= font_cmap
            if not needed:
                break
        if names:
            self.pdf.set_fallback_fonts(names, exact_match=False)
        return frozenset(cmap)

    def _word_width(self, word: str, style: str, size: float) -> float:
        key = (style, size, word)
        width = self._width_cache.get(key)
        if width is None:
            width = self.pdf.get_string_width(word)
            self._width_cache[key] = width
        return width

    def _fast_lines(self, value: str, style: str, size: float) -> Optional[list[str]]:
        """Greedy line breaking with cached word widths (much faster than
        multi_cell for long transcripts). Returns None when the text needs
        fallback fonts or has an over-long word; multi_cell is used then."""
        if any(ord(c) not in self.primary_cmap and not c.isspace() and not _is_drawable_control(c) for c in value):
            return None
        available = (self.pdf.w - self.pdf.l_margin - self.pdf.r_margin) * 0.97
        space = self._word_width(" ", style, size)
        lines, current, width = [], [], 0.0
        for word in value.split():
            w = self._word_width(word, style, size)
            if w > available:
                return None
            extra = w if not current else space + w
            if current and width + extra > available:
                lines.append(" ".join(current))
                current, width = [word], w
            else:
                current.append(word)
                width += extra
        if current:
            lines.append(" ".join(current))
        return lines

    def text(self, value: str, size: float = 11, bold: bool = False,
             color: tuple[int, int, int] = (0x1F, 0x29, 0x37), height: float = 6.2, gap: float = 1.5) -> None:
        value = clean_for_export(value)
        if not value.strip():
            return
        style = "B" if bold else ""
        self.pdf.set_font("Noto", style, size)
        self.pdf.set_text_color(*color)
        value = _supported_text(value, self.cmap)
        lines = self._fast_lines(value, style, size)
        if lines is None:
            self.pdf.multi_cell(0, height, value, new_x="LMARGIN", new_y="NEXT")
        else:
            for line in lines:
                self.pdf.cell(0, height, line, new_x="LMARGIN", new_y="NEXT")
        if gap:
            self.pdf.ln(gap)

    def rule(self) -> None:
        y = self.pdf.get_y()
        self.pdf.set_draw_color(0xE5, 0xE7, 0xEB)
        self.pdf.line(self.pdf.l_margin, y, self.pdf.w - self.pdf.r_margin, y)
        self.pdf.ln(3)

    def document(self, doc: TranscriptDocument) -> None:
        self.text(doc.title, size=15, bold=True, color=ACCENT_DARK, height=7.5)
        self.text(doc.url, size=8.5, color=MUTED, height=4.5, gap=0.5)
        for note in doc.notes:
            self.text(note, size=8.5, color=MUTED, height=4.5, gap=0.3)
        self.pdf.ln(2)
        self.rule()
        for row in doc.rows:
            if row.label:
                self.text(f"[{row.label}]", size=8, color=ACCENT_DARK, height=4.5, gap=0.3)
            if doc.is_bilingual:
                for column, value in zip(doc.columns, row.texts):
                    if value:
                        self.text(column, size=7.5, bold=True, color=MUTED, height=4, gap=0)
                        self.text(value, size=10.5, color=(0x1F, 0x29, 0x37) if column == doc.columns[0] else TEXT_GREY)
                self.pdf.ln(1.5)
            else:
                self.text(row.texts[0], size=11, gap=2.5)

    def output(self) -> bytes:
        return bytes(self.pdf.output())


def export_pdf(doc: TranscriptDocument) -> bytes:
    writer = _PdfWriter([doc])
    writer.pdf.add_page()
    writer.document(doc)
    return writer.output()


def export_pdf_combined(docs: Sequence[TranscriptDocument], title: str = "All transcripts") -> bytes:
    writer = _PdfWriter(docs)
    writer.pdf.add_page()
    writer.text(title, size=20, bold=True, color=ACCENT_DARK, height=10)
    writer.text(f"{len(docs)} video(s) · generated {datetime.now():%d %b %Y, %H:%M}", size=9, color=MUTED)
    for i, doc in enumerate(docs):
        writer.text(f"{i + 1}. {doc.title}", size=10.5, height=5.5, gap=0.5)
    for doc in docs:
        writer.pdf.add_page()
        writer.document(doc)
    return writer.output()


# --------------------------------------------------------------------------- #
# Dispatch + ZIP
# --------------------------------------------------------------------------- #
MIME_TYPES = {
    "txt": "text/plain; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "zip": "application/zip",
}


def export_single(doc: TranscriptDocument, fmt: str) -> bytes:
    if fmt == "txt":
        return export_txt(doc)
    if fmt == "docx":
        return export_docx(doc)
    if fmt == "pdf":
        return export_pdf(doc)
    raise ValueError(f"Unknown format: {fmt}")


def export_combined(docs: Sequence[TranscriptDocument], fmt: str) -> bytes:
    if fmt == "txt":
        return export_txt_combined(docs)
    if fmt == "docx":
        return export_docx_combined(docs)
    if fmt == "pdf":
        return export_pdf_combined(docs)
    raise ValueError(f"Unknown format: {fmt}")


def build_report(rows: Iterable[tuple[str, str, str, str]]) -> bytes:
    """rows: (number, title, status, message) -> plain-text processing report."""
    lines = [
        "YouTube Batch Transcript Extractor — processing report",
        f"Generated: {datetime.now():%d %b %Y, %H:%M}",
        "",
    ]
    for number, title, status, message in rows:
        lines.append(f"{number}. {title}")
        lines.append(f"   Status: {status}")
        if message:
            lines.append(f"   Details: {message}")
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def build_files(
    docs: Sequence[TranscriptDocument],
    formats: Sequence[str],
    report: Optional[bytes] = None,
    include_individual: bool = True,
    include_combined: bool = True,
    on_progress=None,
) -> dict[str, bytes]:
    """Create every output file once: {zip path: bytes}."""
    files: dict[str, bytes] = {}
    steps = (len(docs) * len(formats) if include_individual else 0) + (len(formats) if include_combined and docs else 0)
    done = 0

    def tick() -> None:
        nonlocal done
        done += 1
        if on_progress:
            on_progress(done, max(steps, 1))

    if include_individual:
        for i, doc in enumerate(docs):
            base = document_basename(doc, i)
            for fmt in formats:
                files[f"Individual/{base}.{fmt}"] = export_single(doc, fmt)
                tick()
    if include_combined and docs:
        for fmt in formats:
            files[f"All_Transcripts.{fmt}"] = export_combined(docs, fmt)
            tick()
    if report:
        files["Processing_Report.txt"] = report
    return files


def zip_files(files: dict[str, bytes]) -> bytes:
    """Bundle files into one ZIP (UTF-8 names, so Hindi titles survive)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, data in files.items():
            zf.writestr(path, data)
    return buffer.getvalue()


def build_zip(
    docs: Sequence[TranscriptDocument],
    formats: Sequence[str],
    report: Optional[bytes] = None,
    include_individual: bool = True,
    include_combined: bool = True,
    on_progress=None,
) -> bytes:
    return zip_files(build_files(docs, formats, report, include_individual, include_combined, on_progress))
