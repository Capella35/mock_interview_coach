"""Step 2 — read a resume / LinkedIn export / job description into normalized page text.

Output is a `RawDocument`: **unredacted**, in memory only. It must never be sent
to an LLM, logged, or written to disk. Only `redact.py` (step 2b) turns it into a
`SourceDocument`, whose text is the canonical string every quote indexes into.

Supported in M1: .pdf (text-based, via pypdf), .txt, .md. No OCR, no DOCX.

Normalization is deterministic and idempotent, so the same file always yields
the same text (and therefore the same quote offsets):
  - line endings -> "\\n"; form feed / vertical tab -> "\\n"
  - Unicode NFKC (PDF ligatures like "ﬁ" -> "fi", non-breaking spaces -> " ")
  - zero-width characters and soft hyphens removed; other control chars removed
  - runs of spaces/tabs -> one space; each line trimmed
  - "Page N of M" footer lines removed (LinkedIn PDF exports add these)
  - 3+ consecutive newlines -> 2; whole page trimmed
"""

from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from interview_agent.schemas import DocKind

SUPPORTED_SUFFIXES = frozenset({".pdf", ".txt", ".md"})
MAX_BYTES = 10 * 1024 * 1024  # 10 MB — far above any resume or JD
MAX_PAGES = 30
MIN_TEXT_CHARS = 50  # fewer non-space chars than this => no usable text (likely scanned)
PAGE_SEPARATOR = "\n\n"

DEFAULT_DOC_IDS: dict[str, str] = {"resume": "resume", "linkedin": "linkedin", "job_description": "jd"}
_DOC_ID_RE = re.compile(r"^[a-z0-9_]+$")  # must fit fact ids like F-<doc_id>-001


class IngestError(Exception):
    """The file could not be turned into usable text."""


class UnsupportedFileError(IngestError):
    pass


class NoTextError(IngestError):
    pass


@dataclass(frozen=True)
class RawDocument:
    """Normalized, UNREDACTED page texts. In-memory only; see module docstring."""

    doc_id: str
    kind: DocKind
    filename: str
    sha256: str  # of the original file bytes
    pages: tuple[str, ...] = field(repr=False)  # repr never shows document text
    warnings: tuple[str, ...] = ()

    @property
    def char_count(self) -> int:
        return sum(len(p) for p in self.pages)

    def __repr__(self) -> str:  # explicit: lengths only, never content
        return (
            f"RawDocument(doc_id={self.doc_id!r}, kind={self.kind!r}, filename={self.filename!r}, "
            f"pages={len(self.pages)}, chars={self.char_count}, warnings={len(self.warnings)})"
        )


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #

_REMOVE_CHARS = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")
_INLINE_SPACE_RE = re.compile(r"[^\S\n]+")  # any whitespace except newline
_PAGE_FOOTER_RE = re.compile(r"^page \d+ of \d+$", re.IGNORECASE)
_MANY_NEWLINES_RE = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """Deterministic, idempotent cleanup of extracted text (see module docstring)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n").replace("\v", "\n")
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_REMOVE_CHARS)
    text = _CONTROL_RE.sub("", text)
    text = _INLINE_SPACE_RE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if not _PAGE_FOOTER_RE.match(line)]
    text = "\n".join(lines)
    text = _MANY_NEWLINES_RE.sub("\n\n", text)
    return text.strip()


def assemble_pages(pages: tuple[str, ...] | list[str]) -> tuple[str, list[int]]:
    """Join page texts into one string; return (text, page_starts) for SourceDocument."""
    starts: list[int] = []
    offset = 0
    for page in pages:
        starts.append(offset)
        offset += len(page) + len(PAGE_SEPARATOR)
    return PAGE_SEPARATOR.join(pages), starts or [0]


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def load_document(path: str | Path, kind: DocKind, doc_id: str | None = None) -> RawDocument:
    """Read one file into a RawDocument. `kind` is given by the caller, never guessed."""
    path = Path(path)
    doc_id = doc_id or DEFAULT_DOC_IDS[kind]
    if not _DOC_ID_RE.match(doc_id):
        raise ValueError(f"doc_id must match [a-z0-9_]+, got {doc_id!r}")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise UnsupportedFileError(
            f"{path.name}: unsupported file type {suffix or '(none)'}; "
            f"supported: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
        )
    if not path.is_file():
        raise IngestError(f"{path.name}: file not found")
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise IngestError(f"{path.name}: file is {size} bytes; limit is {MAX_BYTES}")

    data = path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()

    raw_pages = _pdf_pages(data, path.name) if suffix == ".pdf" else [_decode_text(data, path.name)]
    pages = tuple(normalize_text(p) for p in raw_pages)

    if sum(len(p.replace(" ", "").replace("\n", "")) for p in pages) < MIN_TEXT_CHARS:
        hint = " It may be a scanned image; OCR is not supported in Milestone 1." if suffix == ".pdf" else ""
        raise NoTextError(f"{path.name}: no usable text found.{hint}")

    warnings = tuple(f"page {i} has no text" for i, p in enumerate(pages, start=1) if not p)
    return RawDocument(doc_id=doc_id, kind=kind, filename=path.name, sha256=sha256, pages=pages, warnings=warnings)


def _decode_text(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8-sig")  # strips a UTF-8 BOM if present
    except UnicodeDecodeError as e:
        raise IngestError(f"{name}: not valid UTF-8 text; re-save the file as UTF-8") from e


def _pdf_pages(data: bytes, name: str) -> list[str]:
    from pypdf import PdfReader  # local import: only needed for PDFs

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")  # some PDFs are encrypted with an empty password
            except Exception:
                unlocked = 0
            if not unlocked:
                raise IngestError(f"{name}: PDF is password-protected; export an unprotected copy")
        if len(reader.pages) > MAX_PAGES:
            raise IngestError(f"{name}: {len(reader.pages)} pages; limit is {MAX_PAGES}")
        return [page.extract_text() or "" for page in reader.pages]
    except IngestError:
        raise
    except Exception as e:  # pypdf raises many types for damaged files
        raise IngestError(f"{name}: could not read PDF ({type(e).__name__})") from e
