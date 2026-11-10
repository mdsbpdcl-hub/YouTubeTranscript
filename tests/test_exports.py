"""Tests for TXT / DOCX / PDF / ZIP exports, with Hindi Unicode content."""

import io
import zipfile

import pytest

from modules.export_manager import (
    build_files,
    build_report,
    build_zip,
    export_docx,
    export_docx_combined,
    export_pdf,
    export_pdf_combined,
    export_txt,
    find_pdf_fonts,
    safe_filename,
)
from modules.transcript_formatter import build_document

HINDI = "[00:00:00] नमस्ते दोस्तों, आज हम स्मार्ट मीटर और बिजली आपूर्ति (क्षेत्र, श्रृंखला, द्वारा) की बात करेंगे।\n\n" \
        "[00:01:10] बिल सही आता है। ₹500 तक की बचत 😀"
ENGLISH = "[00:00:00] Hello friends, today we discuss smart meters and power supply.\n\n" \
          "[00:01:10] Bills are correct. Savings up to ₹500."


def make_doc(bilingual=False, title="बिजली संवाद: Episode 1"):
    texts = {"hi": HINDI, "en": ENGLISH} if bilingual else {"hi": HINDI}
    return build_document(
        title=title, video_id="abcdefghijk", url="https://www.youtube.com/watch?v=abcdefghijk",
        texts=texts, column_names={"hi": "हिंदी (Hindi)", "en": "English"}, order=["hi", "en"],
        notes=["हिंदी (Hindi): YouTube uploaded captions (Hindi)"],
    )


def test_safe_filename_keeps_hindi_and_removes_bad_chars():
    name = safe_filename('बिजली: "संवाद"/भाग?1*')
    assert "बिजली" in name and not any(c in name for c in ':"/?*')
    assert safe_filename("   ") == "transcript"
    assert len(safe_filename("क" * 200)) <= 60


def test_txt_is_utf8_hindi():
    data = export_txt(make_doc())
    text = data.decode("utf-8")
    assert "नमस्ते दोस्तों" in text and "[00:01:10]" in text


def test_txt_bilingual_lists_both_languages():
    text = export_txt(make_doc(bilingual=True)).decode("utf-8")
    assert "हिंदी (Hindi): नमस्ते" in text and "English: Hello friends" in text


def test_docx_contains_hindi_and_complex_script_font():
    docx = pytest.importorskip("docx")
    data = export_docx(make_doc())
    document = docx.Document(io.BytesIO(data))
    full = "\n".join(p.text for p in document.paragraphs)
    assert "नमस्ते दोस्तों" in full
    xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8")
    assert 'w:cs="Nirmala UI"' in xml


def test_docx_bilingual_table():
    docx = pytest.importorskip("docx")
    document = docx.Document(io.BytesIO(export_docx(make_doc(bilingual=True))))
    table = document.tables[0]
    assert table.rows[0].cells[1].text == "हिंदी (Hindi)"
    assert "नमस्ते" in table.rows[1].cells[1].text
    assert "Hello" in table.rows[1].cells[2].text


def test_docx_combined_has_all_videos():
    docx = pytest.importorskip("docx")
    data = export_docx_combined([make_doc(title="पहला"), make_doc(title="Second")])
    text = "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)
    assert "पहला" in text and "Second" in text


def test_pdf_font_available():
    regular, bold = find_pdf_fonts()
    assert regular.endswith(".ttf")


def test_pdf_single_and_bilingual_and_combined():
    for data in (export_pdf(make_doc()), export_pdf(make_doc(bilingual=True)),
                 export_pdf_combined([make_doc(), make_doc(bilingual=True)])):
        assert data.startswith(b"%PDF") and len(data) > 2000
        assert b"NotoSansDevanagari" in data  # font embedded


def test_pdf_text_is_extractable_when_pypdf_available():
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(io.BytesIO(export_pdf(make_doc())))
    text = reader.pages[0].extract_text()
    assert "00:01:10" in text


def test_zip_structure_and_report():
    docs = [make_doc(), make_doc(bilingual=True, title="Second video")]
    report = build_report([("1", "पहला", "Done", ""), ("2", "Broken", "Failed", "Captions are turned off")])
    data = build_zip(docs, ["txt", "docx", "pdf"], report=report)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "All_Transcripts.txt" in names and "All_Transcripts.pdf" in names
    assert "Processing_Report.txt" in names
    individual = [n for n in names if n.startswith("Individual/")]
    assert len(individual) == 6
    assert any("बिजली" in n for n in individual)  # Hindi file names survive
    report_text = zipfile.ZipFile(io.BytesIO(data)).read("Processing_Report.txt").decode("utf-8")
    assert "Captions are turned off" in report_text


def test_build_files_progress_callback():
    ticks = []
    build_files([make_doc()], ["txt"], on_progress=lambda d, t: ticks.append((d, t)))
    assert ticks[-1] == (2, 2)


# ---------------------------------------------------------- regressions ----
def test_control_characters_do_not_break_word_files():
    docx = pytest.importorskip("docx")
    doc = build_document("t", "abcdefghijk", "u", {"hi": "[00:00:00] टाइप \x07 किया \x0b गया"},
                         {"hi": "H"}, ["hi"])
    text = "\n".join(p.text for p in docx.Document(io.BytesIO(export_docx(doc))).paragraphs)
    assert "टाइप" in text and "\x07" not in text
    assert b"\x07" not in export_txt(doc)
    assert export_pdf(doc).startswith(b"%PDF")


def test_filename_truncation_keeps_whole_letters():
    name = safe_filename("क" * 59 + "क्षि", max_length=61)
    assert not name.endswith("्")  # no dangling virama
    import unicodedata
    assert unicodedata.category(name[-1]) not in ("Mn",) or name[-2] != "्"


def test_pdf_other_scripts_use_fallback_fonts_when_available():
    from modules.export_manager import fallback_font_paths, _font_cmap
    bengali = "আমি বাংলায় কথা বলি"
    if not any(ord("আ") in _font_cmap(p) for p in fallback_font_paths()):
        pytest.skip("no Bengali font on this machine (Streamlit Cloud installs fonts-noto-core)")
    pypdf = pytest.importorskip("pypdf")
    doc = build_document("t", "abcdefghijk", "u", {"orig": f"[00:00:00] {bengali}"}, {"orig": "O"}, ["orig"])
    text = pypdf.PdfReader(io.BytesIO(export_pdf(doc))).pages[0].extract_text()
    assert "?" not in text.split("00:00:00]")[-1]


def test_long_transcript_pdf_is_fast_and_wraps():
    import time
    from modules.transcript_formatter import Segment, blocks_to_text, build_blocks
    segs = [Segment(i * 3, 3, "आज हम बिजली के बारे में बात करेंगे और smart meter से बिल सही आता है।")
            for i in range(2400)]  # about two hours
    doc = build_document("long", "abcdefghijk", "u", {"hi": blocks_to_text(build_blocks(segs))}, {"hi": "H"}, ["hi"])
    start = time.time()
    data = export_pdf(doc)
    assert time.time() - start < 6
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(io.BytesIO(data))
    assert len(reader.pages) > 20
