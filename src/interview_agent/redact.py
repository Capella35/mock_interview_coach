"""Step 2b — mask contact details before any text can reach an LLM.

`redact_document(raw, owner_id, terms)` is the ONLY way to turn an unredacted
`RawDocument` into a `SourceDocument`. It returns the redacted document plus a
PRIVATE `RedactionMap` (placeholder -> original value) that must stay in
`data/private/` and is never sent anywhere.

What gets masked (see DECISIONS.md, privacy decision):
  [NAME]     exact strings from the terms file (no reliable pattern for names)
  [EMAIL]    email addresses
  [PHONE]    phone numbers (international and local formats)
  [ADDRESS]  street addresses (English + Turkish formats), UK postcodes,
             US ZIP codes after a state code; plus terms-file entries
  [URL]      http(s)/www links, and lowercase bare domains with a path
             (linkedin.com/in/..., github.com/...)
  [REDACTED] terms-file entries without a kind
City, country, employers, titles, projects, skills and metrics are kept.

Known limits (use the terms file for these): personal sites without a path
(e.g. "myname.dev"), unusual address formats, 7-digit local phone numbers.

`assert_no_pii(text)` is the send guard: the LLM client calls it on every
outgoing payload and refuses to send if an email or phone pattern is present.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from interview_agent.ingest import RawDocument, assemble_pages, normalize_text
from interview_agent.schemas import SourceDocument

RedactionKind = Literal["name", "email", "phone", "address", "url", "other"]
PLACEHOLDERS: dict[str, str] = {
    "name": "[NAME]",
    "email": "[EMAIL]",
    "phone": "[PHONE]",
    "address": "[ADDRESS]",
    "url": "[URL]",
    "other": "[REDACTED]",
}
MIN_TERM_LEN = 3


class PIILeakError(RuntimeError):
    """Raised by the send guard. The message never contains the detected value."""


# --------------------------------------------------------------------------- #
# Terms file: data/private/redact_terms.txt
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Term:
    kind: RedactionKind
    value: str

    def __repr__(self) -> str:
        return f"Term(kind={self.kind!r}, len={len(self.value)})"


def parse_terms(text: str) -> list[Term]:
    """One term per line: `name: Jane Doe`, `address: 12 Example Street`, ...

    Blank lines and `#` comments are ignored. A line without a known `kind:`
    prefix is masked as [REDACTED].
    """
    terms: list[Term] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kind: RedactionKind = "other"
        value = line
        prefix, sep, rest = line.partition(":")
        if sep and prefix.strip().lower() in PLACEHOLDERS:
            kind = prefix.strip().lower()  # type: ignore[assignment]
            value = rest
        value = normalize_text(value)  # same normalization as the documents
        if len(value) < MIN_TERM_LEN:
            raise ValueError(f"redact terms line {lineno}: value too short (min {MIN_TERM_LEN} chars)")
        terms.append(Term(kind=kind, value=value))
    return terms


def load_terms(path: str | Path) -> list[Term]:
    path = Path(path)
    return parse_terms(path.read_text(encoding="utf-8")) if path.is_file() else []


# --------------------------------------------------------------------------- #
# Detectors
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Hit:
    start: int
    end: int
    kind: RedactionKind


_EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")

_URL_SCHEME_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
# Lowercase only, so tech terms like "ASP.NET/C#" or "Node.js/React" are not hit.
_URL_BARE_RE = re.compile(r"(?<![\w@./-])(?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|me|ai|co|app|tech|info|xyz|site|page|uk|de|tr|eu)/\S*")
_URL_TRAILING = ".,;:!?)]}'\""

_PHONE_RE = re.compile(r"(?<![\w+])(?:\+\d{1,3}[ .-]?)?(?:\(\d{1,4}\)[ .-]?)?\d(?:[ .-]?\d){6,14}(?!\w)")
_THOUSANDS_RE = re.compile(r"\d{1,3}(?:\.\d{3})+")

_UNIT = r"(?:,? (?:Apt|Apartment|Suite|Ste|Unit|Flat)\.? ?[\w-]+)?"
_STREET_EN_RE = re.compile(
    r"\b\d{1,5}[A-Za-z]? (?:[A-Z][\w'.-]* ){1,4}"  # single spaces only: never span lines
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Place|Pl|Way|"
    r"Terrace|Close|Crescent|Square|Sq|Parkway|Pkwy|Highway|Hwy)\b\.?" + _UNIT
)
_STREET_TR_RE = re.compile(
    r"(?:[A-ZÇĞİÖŞÜ][\w'.-]* ){1,4}"
    r"(?:Mahallesi|Mah\.|Caddesi|Cad\.|Cd\.|Sokağı|Sokak|Sok\.|Sk\.|Bulvarı|Blv\.)"
    r"(?: ?No ?:? ?\d+[A-Za-z]?(?:/\d+)?)?"
)
_UK_POSTCODE_RE = re.compile(r"\b(?:[A-Z]{1,2}\d[A-Z\d]?) ?\d[A-Z]{2}\b")
_US_ZIP_RE = re.compile(r"\b([A-Z]{2}) (\d{5}(?:-\d{4})?)\b")
_US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
    "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)
_ADDRESS_JOIN_RE = re.compile(r"[ ,]*")  # same line only


def _term_hits(text: str, terms: Sequence[Term]) -> list[_Hit]:
    hits: list[_Hit] = []
    for term in sorted(terms, key=lambda t: -len(t.value)):  # longest first
        pattern = r"\s+".join(re.escape(tok) for tok in term.value.split())
        for m in re.finditer(rf"(?<!\w){pattern}(?!\w)", text, re.IGNORECASE):
            hits.append(_Hit(m.start(), m.end(), term.kind))
    return hits


def _url_hits(text: str) -> list[_Hit]:
    hits = []
    for rx in (_URL_SCHEME_RE, _URL_BARE_RE):
        for m in rx.finditer(text):
            end = m.end()
            while end > m.start() and text[end - 1] in _URL_TRAILING:
                end -= 1
            hits.append(_Hit(m.start(), end, "url"))
    return hits


def _email_hits(text: str) -> list[_Hit]:
    return [_Hit(m.start(), m.end(), "email") for m in _EMAIL_RE.finditer(text)]


def _is_phone(candidate: str) -> bool:
    digits = sum(c.isdigit() for c in candidate)
    has_plus = candidate.startswith("+")
    if digits > 15 or digits < (8 if has_plus else 9):
        return False
    if _THOUSANDS_RE.fullmatch(candidate):  # 100.000.000
        return False
    groups = re.findall(r"\d+", candidate)
    if all(len(g) == 4 and g[:2] in ("19", "20") for g in groups):  # "2019 2020 2021"
        return False
    return True


def _phone_hits(text: str) -> list[_Hit]:
    return [_Hit(m.start(), m.end(), "phone") for m in _PHONE_RE.finditer(text) if _is_phone(m.group())]


def _street_hits(text: str) -> list[_Hit]:
    return [_Hit(m.start(), m.end(), "address") for rx in (_STREET_EN_RE, _STREET_TR_RE) for m in rx.finditer(text)]


def _postcode_hits(text: str) -> list[_Hit]:
    hits = [_Hit(m.start(), m.end(), "address") for m in _UK_POSTCODE_RE.finditer(text)]
    hits += [_Hit(m.start(2), m.end(2), "address") for m in _US_ZIP_RE.finditer(text) if m.group(1) in _US_STATES]
    return hits


def detect(text: str, terms: Sequence[Term] = ()) -> list[_Hit]:
    """All non-overlapping hits, earlier detector wins on overlap; sorted by position."""
    accepted: list[_Hit] = []
    for group in (
        _term_hits(text, terms),
        _url_hits(text),
        _email_hits(text),
        _phone_hits(text),
        _street_hits(text),
        _postcode_hits(text),
    ):
        for hit in sorted(group, key=lambda h: (h.start, -(h.end - h.start))):
            if all(hit.end <= a.start or hit.start >= a.end for a in accepted):
                accepted.append(hit)
    accepted.sort(key=lambda h: h.start)

    merged: list[_Hit] = []  # "Moda Mah., Şair Nefi Sok. No: 7" -> one [ADDRESS]
    for hit in accepted:
        prev = merged[-1] if merged else None
        if (
            prev
            and prev.kind == hit.kind == "address"
            and _ADDRESS_JOIN_RE.fullmatch(text[prev.end : hit.start])
        ):
            merged[-1] = _Hit(prev.start, hit.end, "address")
        else:
            merged.append(hit)
    return merged


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


class RedactionEntry(BaseModel):
    """PRIVATE. One masked value and where its placeholder sits in SourceDocument.text."""

    model_config = ConfigDict(extra="forbid")
    kind: RedactionKind
    placeholder: str
    original: str = Field(repr=False)
    char_start: int
    char_end: int
    page: int


class RedactionMap(BaseModel):
    """PRIVATE — stays in data/private/, never sent to an LLM, never logged."""

    model_config = ConfigDict(extra="forbid")
    doc_id: str
    owner_id: str
    source_sha256: str
    entries: list[RedactionEntry] = Field(default_factory=list, repr=False)
    warnings: list[str] = Field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.kind] = out.get(e.kind, 0) + 1
        return out

    def __repr__(self) -> str:
        return f"RedactionMap(doc_id={self.doc_id!r}, counts={self.counts()}, warnings={len(self.warnings)})"

    __str__ = __repr__


def redact_text(text: str, terms: Sequence[Term] = ()) -> tuple[str, list[tuple[_Hit, str, int]]]:
    """Return (redacted_text, [(hit, original, placeholder_start_in_redacted)])."""
    out: list[str] = []
    replaced: list[tuple[_Hit, str, int]] = []
    cursor = 0
    length = 0
    for hit in detect(text, terms):
        chunk = text[cursor : hit.start]
        out.append(chunk)
        length += len(chunk)
        placeholder = PLACEHOLDERS[hit.kind]
        replaced.append((hit, text[hit.start : hit.end], length))
        out.append(placeholder)
        length += len(placeholder)
        cursor = hit.end
    out.append(text[cursor:])
    return "".join(out), replaced


def redact_document(raw: RawDocument, owner_id: str, terms: Sequence[Term] = ()) -> tuple[SourceDocument, RedactionMap]:
    """Mask every page of `raw`; build the canonical SourceDocument and the private map."""
    redacted_pages: list[str] = []
    per_page: list[list[tuple[_Hit, str, int]]] = []
    for page in raw.pages:
        red, replaced = redact_text(page, terms)
        redacted_pages.append(red)
        per_page.append(replaced)

    text, page_starts = assemble_pages(redacted_pages)
    entries = [
        RedactionEntry(
            kind=hit.kind,
            placeholder=PLACEHOLDERS[hit.kind],
            original=original,
            char_start=page_starts[i] + pos,
            char_end=page_starts[i] + pos + len(PLACEHOLDERS[hit.kind]),
            page=i + 1,
        )
        for i, replaced in enumerate(per_page)
        for hit, original, pos in replaced
    ]

    warnings: list[str] = []
    if raw.kind in ("resume", "linkedin") and not any(t.kind == "name" for t in terms):
        warnings.append("no 'name:' entry in the terms file; the candidate's name will not be masked")
    leaks = find_pii(text)
    if leaks:  # should be impossible: same detectors ran above
        raise PIILeakError(f"{raw.doc_id}: redacted text still contains {sorted(set(leaks))} patterns")

    doc = SourceDocument(
        doc_id=raw.doc_id,
        owner_id=owner_id,
        kind=raw.kind,
        filename=raw.filename,
        sha256=raw.sha256,
        text=text,
        page_starts=page_starts,
    )
    rmap = RedactionMap(
        doc_id=raw.doc_id, owner_id=owner_id, source_sha256=raw.sha256, entries=entries, warnings=warnings
    )
    return doc, rmap


# --------------------------------------------------------------------------- #
# Send guard
# --------------------------------------------------------------------------- #


def find_pii(text: str) -> list[str]:
    """Kinds of email/phone patterns present in `text` (values are never returned)."""
    return [h.kind for h in _email_hits(text) + _phone_hits(text)]


def assert_no_pii(text: str) -> None:
    """Send guard for the LLM client: raise if any email/phone pattern is present."""
    kinds = find_pii(text)
    if kinds:
        summary = ", ".join(f"{kinds.count(k)} {k}" for k in sorted(set(kinds)))
        raise PIILeakError(f"refusing to send: outgoing text contains {summary} pattern(s)")
