"""Step 3 — turn untrusted LLM proposals into grounded Facts / JobRequirements.

The LLM proposes `{quote, claim, ...}`. Code then decides, deterministically:

  1. Quote is findable   - the quote must occur in the (redacted) document text;
                           only whitespace / line-break differences are tolerated.
                           The stored `source_text` is always the document's own
                           slice, never the LLM's copy.
  2. Quote is specific   - >= MIN_QUOTE_CHARS real characters, <= MAX_QUOTE_CHARS,
                           and not just placeholders like [EMAIL].
  3. No invented numbers - every number in the claim / requirement / time_period /
                           metric must appear in the quote; metric.raw must be in
                           the quote; metric.value must come from metric.raw.
  4. Traceable skills    - a skill tag that does not appear in the quote is
                           dropped (with a warning), not trusted.

Rejected proposals are returned with a reason code for the review report — they
are never silently repaired.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError

from interview_agent.ingest import normalize_text
from interview_agent.redact import PLACEHOLDERS
from interview_agent.schemas import (
    CANDIDATE_DOC_KINDS,
    Fact,
    FactProposal,
    JobRequirement,
    Metric,
    RequirementProposal,
    SourceDocument,
)

MIN_QUOTE_CHARS = 3  # non-space characters
MAX_QUOTE_CHARS = 400  # a whole-document "quote" would support any claim

RejectReason = Literal[
    "invalid_proposal",
    "quote_too_short",
    "quote_too_long",
    "placeholder_only",
    "quote_not_found",
    "number_not_in_quote",
    "metric_not_in_quote",
    "metric_value_mismatch",
]


class GroundingError(Exception):
    def __init__(self, reason: RejectReason, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class Rejection(BaseModel):
    """A proposal that did not survive grounding (shown in the review report)."""

    model_config = ConfigDict(extra="forbid")
    doc_id: str
    index: int  # position in the LLM's proposal list
    reason: RejectReason
    detail: str
    quote: str | None = None


@dataclass
class GroundingReport:
    accepted: list[Any] = field(default_factory=list)  # Fact | JobRequirement
    rejections: list[Rejection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 1-2. Locating quotes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Located:
    char_start: int
    char_end: int
    source_text: str  # exact document slice
    page: int
    occurrences: int


_PLACEHOLDER_RE = re.compile("|".join(re.escape(p) for p in PLACEHOLDERS.values()))


def _flex_pattern(text: str) -> str:
    """Regex matching `text` with any run of whitespace standing for any other."""
    return r"\s+".join(re.escape(tok) for tok in text.split())


def _check_quote(quote: str) -> str:
    q = normalize_text(quote)
    if len(q.replace(" ", "").replace("\n", "")) < MIN_QUOTE_CHARS:
        raise GroundingError("quote_too_short", f"quote has fewer than {MIN_QUOTE_CHARS} characters")
    if len(q) > MAX_QUOTE_CHARS:
        raise GroundingError("quote_too_long", f"quote is {len(q)} characters; limit is {MAX_QUOTE_CHARS}")
    if sum(c.isalnum() for c in _PLACEHOLDER_RE.sub("", q)) < MIN_QUOTE_CHARS:
        raise GroundingError("placeholder_only", "quote contains only redaction placeholders/punctuation")
    return q


def locate_quote(doc: SourceDocument, quote: str) -> Located:
    """Find `quote` in `doc.text` (whitespace-tolerant, case-sensitive). First occurrence wins."""
    q = _check_quote(quote)
    matches = list(re.finditer(_flex_pattern(q), doc.text))
    if not matches:
        raise GroundingError("quote_not_found", f"quote not found in {doc.doc_id!r}")
    m = matches[0]
    return Located(m.start(), m.end(), m.group(), doc.page_of(m.start()), len(matches))


# --------------------------------------------------------------------------- #
# 3. Numbers
# --------------------------------------------------------------------------- #

_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


def numbers_in(text: str) -> set[float]:
    """Numeric values written in `text` ("1,000" -> 1000, "40%" -> 40, "2022-2024" -> 2022, 2024)."""
    out: set[float] = set()
    for tok in _NUM_RE.findall(text):
        try:
            out.add(float(tok.replace(",", "")))
        except ValueError:  # e.g. "1.2.3" version strings: count each part
            out.update(float(p) for p in re.findall(r"\d+", tok))
    return out


def _missing_numbers(text: str | None, quote: str) -> list[float]:
    if not text:
        return []
    have = numbers_in(quote)
    return sorted(n for n in numbers_in(text) if n not in have)


def _fmt(nums: list[float]) -> str:
    return ", ".join(f"{n:g}" for n in nums)


def _check_numbers(fields: dict[str, str | None], quote: str) -> None:
    for name, text in fields.items():
        missing = _missing_numbers(text, quote)
        if missing:
            raise GroundingError("number_not_in_quote", f"{name} has number(s) {_fmt(missing)} not in the quote")


_SCALES = (1.0, 1e3, 1e6, 1e9, 0.01)  # "1.2M" -> 1.2e6, "40%" -> 0.4


def _ground_metric(proposal, source_text: str) -> Metric:
    m = re.search(_flex_pattern(normalize_text(proposal.raw)), source_text)
    if not m:
        raise GroundingError("metric_not_in_quote", f"metric {proposal.raw!r} not found in the quote")
    raw = m.group()
    if proposal.value is not None:
        allowed = [n * s for n in numbers_in(raw) for s in _SCALES]
        if not any(math.isclose(proposal.value, a, rel_tol=1e-9) for a in allowed):
            raise GroundingError("metric_value_mismatch", f"metric value {proposal.value:g} not derivable from {raw!r}")
    return Metric(raw=raw, value=proposal.value, unit=proposal.unit, measures=proposal.measures)


# --------------------------------------------------------------------------- #
# 4. Skill tags
# --------------------------------------------------------------------------- #


def _squash(s: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", s.lower())


def _skill_supported(skill: str, source_text: str) -> bool:
    squashed = _squash(skill)
    if len(squashed) >= 3:
        return squashed in _squash(source_text)
    # short tags ("c++", "go", "r"): require a standalone, case-insensitive match
    return re.search(rf"(?<!\w){re.escape(skill.lower())}(?!\w)", source_text.lower()) is not None


def _split_skills(skills: Sequence[str], source_text: str) -> tuple[list[str], list[str]]:
    kept, dropped = [], []
    for s in skills:
        (kept if _skill_supported(s, source_text) else dropped).append(s)
    return kept, dropped


# --------------------------------------------------------------------------- #
# Single proposals
# --------------------------------------------------------------------------- #


def ground_fact(doc: SourceDocument, proposal: FactProposal, fact_id: str) -> tuple[Fact, list[str]]:
    """Ground one candidate fact. Returns (fact, warnings); raises GroundingError."""
    if doc.kind not in CANDIDATE_DOC_KINDS:
        raise ValueError(f"candidate facts must come from a resume/LinkedIn document, not {doc.kind}")
    loc = locate_quote(doc, proposal.quote)
    _check_numbers({"claim": proposal.claim, "time_period": proposal.time_period}, loc.source_text)
    metric = None
    if proposal.metric is not None:
        metric = _ground_metric(proposal.metric, loc.source_text)  # raw must be in the quote
    skills, dropped = _split_skills(proposal.skills, loc.source_text)

    fact = Fact(
        fact_id=fact_id,
        claim=proposal.claim,
        category=proposal.category,
        metric=metric,
        skills=skills,
        time_period=proposal.time_period,
        source_doc=doc.doc_id,
        source_text=loc.source_text,
        char_start=loc.char_start,
        char_end=loc.char_end,
        page=loc.page,
    )
    return fact, _warnings(fact_id, loc, dropped)


def ground_requirement(
    doc: SourceDocument, proposal: RequirementProposal, req_id: str
) -> tuple[JobRequirement, list[str]]:
    """Ground one job requirement. Returns (requirement, warnings); raises GroundingError."""
    if doc.kind != "job_description":
        raise ValueError(f"requirements must come from a job description, not {doc.kind}")
    loc = locate_quote(doc, proposal.quote)
    _check_numbers({"requirement": proposal.requirement}, loc.source_text)
    skills, dropped = _split_skills(proposal.skills, loc.source_text)

    req = JobRequirement(
        req_id=req_id,
        requirement=proposal.requirement,
        kind=proposal.kind,
        skills=skills,
        source_doc=doc.doc_id,
        source_text=loc.source_text,
        char_start=loc.char_start,
        char_end=loc.char_end,
        page=loc.page,
    )
    return req, _warnings(req_id, loc, dropped)


def _warnings(item_id: str, loc: Located, dropped: list[str]) -> list[str]:
    out = []
    if dropped:
        out.append(f"{item_id}: dropped skill tag(s) not in quote: {', '.join(dropped)}")
    if loc.occurrences > 1:
        out.append(f"{item_id}: quote occurs {loc.occurrences} times; first occurrence used")
    return out


# --------------------------------------------------------------------------- #
# Batches (what extraction calls)
# --------------------------------------------------------------------------- #


def _quote_of(raw: Any) -> str | None:
    if isinstance(raw, dict):
        q = raw.get("quote")
        return q if isinstance(q, str) else None
    return getattr(raw, "quote", None)


def _ground_batch(doc, raw_proposals, model, ground_one, make_id, first_number) -> GroundingReport:
    report = GroundingReport()
    seen: set[tuple] = set()
    number = first_number
    for index, raw in enumerate(raw_proposals):
        quote = _quote_of(raw)
        try:
            proposal = raw if isinstance(raw, model) else model.model_validate(raw)
        except ValidationError as e:
            fields = sorted({".".join(str(p) for p in err["loc"]) or "(root)" for err in e.errors()})
            report.rejections.append(
                Rejection(doc_id=doc.doc_id, index=index, reason="invalid_proposal",
                          detail=f"schema errors in: {', '.join(fields)}", quote=quote)
            )
            continue
        try:
            item, warnings = ground_one(doc, proposal, make_id(number))
        except GroundingError as e:
            report.rejections.append(
                Rejection(doc_id=doc.doc_id, index=index, reason=e.reason, detail=e.detail, quote=quote)
            )
            continue
        key = (item.char_start, item.char_end, getattr(item, "claim", None) or getattr(item, "requirement", None))
        if key in seen:
            report.warnings.append(f"proposal {index}: exact duplicate of an earlier item; skipped")
            continue
        seen.add(key)
        report.accepted.append(item)
        report.warnings.extend(warnings)
        number += 1
    return report


def ground_fact_proposals(
    doc: SourceDocument, proposals: Sequence[FactProposal | dict], first_number: int = 1
) -> GroundingReport:
    """Validate + ground each proposal independently; ids F-<doc_id>-001, 002, ... for accepted ones."""
    return _ground_batch(
        doc, proposals, FactProposal, ground_fact, lambda n: f"F-{doc.doc_id}-{n:03d}", first_number
    )


def ground_requirement_proposals(
    doc: SourceDocument, proposals: Sequence[RequirementProposal | dict], first_number: int = 1
) -> GroundingReport:
    """Validate + ground each proposal independently; ids R-001, 002, ... for accepted ones."""
    return _ground_batch(
        doc, proposals, RequirementProposal, ground_requirement, lambda n: f"R-{n:03d}", first_number
    )
