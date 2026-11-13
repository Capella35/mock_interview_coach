"""Step 2 tests: file reading, normalization, page assembly. All content fictional."""

from __future__ import annotations

import hashlib
import io

import pytest

from interview_agent import ingest
from interview_agent.ingest import (
    IngestError,
    NoTextError,
    UnsupportedFileError,
    assemble_pages,
    load_document,
    normalize_text,
)
from interview_agent.schemas import SourceDocument

RESUME_P1 = [
    "Alex Rivera",
    "Senior Data Engineer, Acme Logistics (2022-2024)",
    "- Rebuilt the nightly ETL pipeline in PySpark (3 hours to 3 minutes).",
]
RESUME_P2 = [
    "Education",
    "BSc Computer Science, Example University, 2018",
    "Page 2 of 2",
]
JD_TEXT = (
    "Forward Deployed Engineer\r\n"
    "Requirements:\r\n"
    "• 3+ years building data pipelines with Spark.\r\n"
    "• Experience working directly with customers.\r\n"
)


def _write(tmp_path, name: str, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return p


# --------------------------------------------------------------------------- #
# normalize_text
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("a\r\nb\rc", "a\nb\nc"),  # line endings
        ("a  b", "a b"),  # non-breaking spaces
        ("a\t\t b   c", "a b c"),  # tabs and runs of spaces
        ("data​bricks﻿", "databricks"),  # zero-width chars
        ("infra­structure", "infrastructure"),  # soft hyphen
        ("ﬁne-tuned workﬂow", "fine-tuned workflow"),  # PDF ligatures
        ("  line one   \n   line two  ", "line one\nline two"),  # trimmed lines
        ("a\n\n\n\n\nb", "a\n\nb"),  # blank-line runs
        ("a\fb", "a\nb"),  # form feed
        ("a\x00b\x07c", "abc"),  # control chars
        ("Skills\nPage 1 of 3\nPython", "Skills\nPython"),  # LinkedIn footers
        ("PAGE 2 OF 3", ""),
    ],
)
def test_normalize_rules(raw, expected):
    assert normalize_text(raw) == expected


def test_normalize_keeps_meaningful_characters():
    text = "2022–2024 • cut cost 40% — €1.2M, café, Page 3 of the report"
    assert normalize_text(text) == text  # en/em dash, bullet, %, €, accents, prose "Page 3"


def test_normalize_is_idempotent():
    messy = " ﬁrst line \r\n\r\n\r\n\tsecond​ line \fPage 1 of 2\n"
    once = normalize_text(messy)
    assert normalize_text(once) == once


# --------------------------------------------------------------------------- #
# assemble_pages
# --------------------------------------------------------------------------- #


def test_assemble_pages_offsets_point_at_each_page():
    pages = ("first page", "second", "third page text")
    text, starts = assemble_pages(pages)
    assert starts[0] == 0
    for start, page in zip(starts, pages):
        assert text[start : start + len(page)] == page


def test_assembled_output_is_valid_for_source_document():
    text, starts = assemble_pages(("page one", "", "page three"))  # includes an empty page
    doc = SourceDocument(
        doc_id="resume", owner_id="u", kind="resume", filename="r.pdf",
        sha256="0" * 64, text=text, page_starts=starts,
    )
    assert doc.page_of(text.index("three")) == 3


# --------------------------------------------------------------------------- #
# load_document: text files
# --------------------------------------------------------------------------- #


def test_load_txt(tmp_path):
    data = JD_TEXT.encode()
    doc = load_document(_write(tmp_path, "jd.txt", data), "job_description")
    assert doc.doc_id == "jd" and doc.kind == "job_description" and doc.filename == "jd.txt"
    assert doc.sha256 == hashlib.sha256(data).hexdigest()  # hash of original bytes
    assert len(doc.pages) == 1
    assert doc.pages[0].startswith("Forward Deployed Engineer\nRequirements:\n• 3+ years")
    assert "\r" not in doc.pages[0]
    assert doc.warnings == ()


def test_load_md_keeps_markdown_literally(tmp_path):
    md = "# Alex Rivera\n\n**Senior Data Engineer** at Acme Logistics, building data platforms.\n"
    doc = load_document(_write(tmp_path, "resume.md", md.encode()), "resume")
    assert doc.pages[0] == md.strip()


def test_utf8_bom_is_stripped(tmp_path):
    doc = load_document(_write(tmp_path, "jd.txt", b"\xef\xbb\xbf" + JD_TEXT.encode()), "job_description")
    assert doc.pages[0].startswith("Forward")


def test_non_utf8_text_rejected(tmp_path):
    data = "Café data engineer with lots of Spark experience and customers".encode("latin-1")
    with pytest.raises(IngestError, match="UTF-8"):
        load_document(_write(tmp_path, "jd.txt", data), "job_description")


def test_empty_text_rejected(tmp_path):
    with pytest.raises(NoTextError):
        load_document(_write(tmp_path, "jd.txt", b"  \n\n hi \n"), "job_description")


# --------------------------------------------------------------------------- #
# load_document: PDFs
# --------------------------------------------------------------------------- #


def test_load_multipage_pdf(tmp_path, make_pdf):
    doc = load_document(_write(tmp_path, "resume.pdf", make_pdf([RESUME_P1, RESUME_P2])), "resume")
    assert len(doc.pages) == 2
    assert doc.pages[0].splitlines() == RESUME_P1
    assert doc.pages[1].splitlines() == RESUME_P2[:2]  # "Page 2 of 2" footer removed
    assert "(3 hours to 3 minutes)" in doc.pages[0]  # escaped parentheses survive


def test_pdf_with_blank_page_warns(tmp_path, make_pdf):
    doc = load_document(_write(tmp_path, "resume.pdf", make_pdf([RESUME_P1, []])), "resume")
    assert doc.pages[1] == ""
    assert doc.warnings == ("page 2 has no text",)


def test_pdf_without_text_rejected_as_scanned(tmp_path, make_pdf):
    with pytest.raises(NoTextError, match="scanned"):
        load_document(_write(tmp_path, "resume.pdf", make_pdf([[], []])), "resume")


def test_corrupt_pdf_rejected(tmp_path):
    with pytest.raises(IngestError, match="could not read PDF"):
        load_document(_write(tmp_path, "resume.pdf", b"%PDF-1.4 this is not really a pdf"), "resume")


def test_password_protected_pdf_rejected(tmp_path, make_pdf):
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf([RESUME_P1]))))
    writer.encrypt(user_password="secret", algorithm="RC4-128")
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(IngestError, match="password-protected"):
        load_document(_write(tmp_path, "resume.pdf", buf.getvalue()), "resume")


def test_too_many_pages_rejected(tmp_path, make_pdf, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_PAGES", 1)
    with pytest.raises(IngestError, match="limit is 1"):
        load_document(_write(tmp_path, "resume.pdf", make_pdf([RESUME_P1, RESUME_P2])), "resume")


# --------------------------------------------------------------------------- #
# Input checks
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["resume.docx", "resume.png", "resume"])
def test_unsupported_types_rejected(tmp_path, name):
    with pytest.raises(UnsupportedFileError):
        load_document(_write(tmp_path, name, b"whatever"), "resume")


def test_missing_file_rejected(tmp_path):
    with pytest.raises(IngestError, match="not found"):
        load_document(tmp_path / "nope.pdf", "resume")


def test_oversized_file_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_BYTES", 10)
    with pytest.raises(IngestError, match="limit"):
        load_document(_write(tmp_path, "jd.txt", JD_TEXT.encode()), "job_description")


def test_default_and_custom_doc_ids(tmp_path):
    p = _write(tmp_path, "li.txt", JD_TEXT.encode())
    assert load_document(p, "linkedin").doc_id == "linkedin"
    assert load_document(p, "job_description", doc_id="jd_google").doc_id == "jd_google"
    with pytest.raises(ValueError):
        load_document(p, "job_description", doc_id="JD Google")


def test_repr_never_contains_document_text(tmp_path, make_pdf):
    doc = load_document(_write(tmp_path, "resume.pdf", make_pdf([RESUME_P1])), "resume")
    assert "Alex" not in repr(doc) and "Acme" not in repr(doc)
    assert "chars=" in repr(doc)
