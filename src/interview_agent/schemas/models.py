"""Pydantic schemas for Milestone 1: SourceDocument, Fact Sheet, Match/Gap Map.

Grounding contract
------------------
Every Fact and JobRequirement is *Grounded*: it carries `source_doc` plus the
exact `source_text` it came from and the character offsets of that text inside
`SourceDocument.text` (the canonical, redacted text). Offsets are filled in by
code (grounding.py), never trusted from an LLM.

Checks are split in three layers:
  1. Field/model validators  - self-contained shape rules (run on construction).
  2. FactSheet validators    - ids unique, docs referenced exist, doc kinds fit.
  3. Cross-object checks     - `FactSheet.verify_spans(docs)` and
                               `MatchGapMap.cross_check(fact_sheet)` return a
                               list of problems (empty == OK) for reporting.

Deliberately absent: any numeric score. Scoring scale is an open decision.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]

DocKind = Literal["resume", "linkedin", "job_description"]
CANDIDATE_DOC_KINDS: frozenset[str] = frozenset({"resume", "linkedin"})

FACT_ID_RE = re.compile(r"^F-[a-z0-9_]+-\d{3,}$")  # e.g. F-resume-012
REQ_ID_RE = re.compile(r"^R-\d{3,}$")  # e.g. R-007


class _Strict(BaseModel):
    # No whitespace stripping anywhere: source_text must stay byte-exact.
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


class SourceDocument(_Strict):
    """One ingested file. Private: never passed into LLM turns after extraction."""

    doc_id: NonEmptyStr  # "resume" | "linkedin" | "jd"
    owner_id: NonEmptyStr  # data-isolation hook (single user for now)
    kind: DocKind
    filename: NonEmptyStr
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]  # of the original file bytes
    text: str  # canonical normalized + redacted text; all offsets index into this
    page_starts: list[int] = Field(default_factory=lambda: [0])  # char offset where each page begins

    @model_validator(mode="after")
    def _pages_valid(self) -> SourceDocument:
        ps = self.page_starts
        if not ps or ps[0] != 0:
            raise ValueError("page_starts must start with 0")
        if any(b <= a for a, b in zip(ps, ps[1:])):
            raise ValueError("page_starts must be strictly increasing")
        if ps[-1] > len(self.text):
            raise ValueError("page_starts beyond end of text")
        return self

    def page_of(self, char_offset: int) -> int:
        """1-based page number containing `char_offset`."""
        page = 1
        for i, start in enumerate(self.page_starts, start=1):
            if start <= char_offset:
                page = i
            else:
                break
        return page

    def ref(self) -> DocumentRef:
        return DocumentRef(doc_id=self.doc_id, kind=self.kind, filename=self.filename, sha256=self.sha256)


class DocumentRef(_Strict):
    """Pointer to a SourceDocument, stored in the Fact Sheet (no full text)."""

    doc_id: NonEmptyStr
    kind: DocKind
    filename: NonEmptyStr
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


# --------------------------------------------------------------------------- #
# Grounded items
# --------------------------------------------------------------------------- #


class Grounded(_Strict):
    """Exact trace back to source: doc_id + verbatim slice + offsets."""

    source_doc: NonEmptyStr
    source_text: NonEmptyStr  # EXACT: doc.text[char_start:char_end]
    char_start: Annotated[int, Field(ge=0)]
    char_end: int
    page: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def _span_shape(self) -> Grounded:
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be > char_start")
        if self.char_end - self.char_start != len(self.source_text):
            raise ValueError("source_text length does not match char_start/char_end")
        return self

    def span_problem(self, doc: SourceDocument) -> str | None:
        """None if the span slices `doc.text` exactly, else a description."""
        if doc.doc_id != self.source_doc:
            return f"span points to {self.source_doc!r}, checked against {doc.doc_id!r}"
        if self.char_end > len(doc.text):
            return f"span [{self.char_start}:{self.char_end}] past end of {doc.doc_id!r}"
        actual = doc.text[self.char_start : self.char_end]
        if actual != self.source_text:
            return f"source_text does not match {doc.doc_id!r}[{self.char_start}:{self.char_end}]: {actual!r}"
        return None


class Metric(_Strict):
    raw: NonEmptyStr  # as written in the source: "3 hours to 3 minutes", "40%"
    value: float | None = None
    unit: str | None = None
    measures: NonEmptyStr  # what it measures: "nightly pipeline runtime"


FactCategory = Literal["role", "project", "skill", "achievement", "education", "certification", "other"]


class Fact(Grounded):
    """A candidate claim from the resume or LinkedIn export."""

    fact_id: Annotated[str, StringConstraints(pattern=FACT_ID_RE.pattern)]
    claim: NonEmptyStr  # one plain sentence; may paraphrase, must not add
    category: FactCategory
    metric: Metric | None = None
    skills: list[NonEmptyStr] = Field(default_factory=list)  # normalized tags for matching
    time_period: str | None = None  # as written, e.g. "2022–2024"

    @model_validator(mode="after")
    def _metric_in_quote(self) -> Fact:
        # No invented or inflated numbers: the metric must literally appear in the quote.
        if self.metric is not None and self.metric.raw not in self.source_text:
            raise ValueError(f"metric {self.metric.raw!r} does not appear in source_text of {self.fact_id}")
        return self


RequirementKind = Literal["required", "preferred", "responsibility", "company_context"]


class JobRequirement(Grounded):
    """One requirement / responsibility from the job description."""

    req_id: Annotated[str, StringConstraints(pattern=REQ_ID_RE.pattern)]
    requirement: NonEmptyStr  # plain restatement
    kind: RequirementKind
    skills: list[NonEmptyStr] = Field(default_factory=list)


class Discrepancy(_Strict):
    """Resume vs LinkedIn mismatch — input for the recruiter consistency check."""

    fact_ids: list[NonEmptyStr] = Field(min_length=2)
    description: NonEmptyStr


# --------------------------------------------------------------------------- #
# Fact Sheet
# --------------------------------------------------------------------------- #


class FactSheet(_Strict):
    schema_version: Literal["1"] = "1"
    owner_id: NonEmptyStr
    created_at: datetime
    documents: list[DocumentRef] = Field(min_length=1)
    candidate_facts: list[Fact]
    job_requirements: list[JobRequirement]
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    extraction_model: NonEmptyStr  # "<provider>:<model>"
    prompt_ids: list[NonEmptyStr] = Field(default_factory=list)  # e.g. ["extract_candidate.v1", ...]

    @model_validator(mode="after")
    def _consistent(self) -> FactSheet:
        doc_kinds: dict[str, str] = {}
        for d in self.documents:
            if d.doc_id in doc_kinds:
                raise ValueError(f"duplicate doc_id {d.doc_id!r}")
            doc_kinds[d.doc_id] = d.kind

        fact_docs: dict[str, str] = {}
        for f in self.candidate_facts:
            if f.fact_id in fact_docs:
                raise ValueError(f"duplicate fact_id {f.fact_id}")
            kind = doc_kinds.get(f.source_doc)
            if kind is None:
                raise ValueError(f"{f.fact_id}: unknown source_doc {f.source_doc!r}")
            if kind not in CANDIDATE_DOC_KINDS:
                raise ValueError(f"{f.fact_id}: candidate fact sourced from a {kind} document")
            fact_docs[f.fact_id] = f.source_doc

        req_ids: set[str] = set()
        for r in self.job_requirements:
            if r.req_id in req_ids:
                raise ValueError(f"duplicate req_id {r.req_id}")
            req_ids.add(r.req_id)
            kind = doc_kinds.get(r.source_doc)
            if kind is None:
                raise ValueError(f"{r.req_id}: unknown source_doc {r.source_doc!r}")
            if kind != "job_description":
                raise ValueError(f"{r.req_id}: requirement sourced from a {kind} document")

        for i, disc in enumerate(self.discrepancies):
            unknown = [fid for fid in disc.fact_ids if fid not in fact_docs]
            if unknown:
                raise ValueError(f"discrepancy {i}: unknown fact_ids {unknown}")
            if len({fact_docs[fid] for fid in disc.fact_ids}) < 2:
                raise ValueError(f"discrepancy {i}: facts must come from at least two documents")
        return self

    def fact(self, fact_id: str) -> Fact | None:
        return next((f for f in self.candidate_facts if f.fact_id == fact_id), None)

    def matchable_requirements(self) -> list[JobRequirement]:
        """Requirements the Match/Gap Map must cover (everything except company_context)."""
        return [r for r in self.job_requirements if r.kind != "company_context"]

    def content_hash(self) -> str:
        """sha256 of the canonical JSON — ties a MatchGapMap to this exact sheet."""
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()

    def verify_spans(self, docs: dict[str, SourceDocument]) -> list[str]:
        """Check every span against the real document text. Empty list == OK."""
        problems: list[str] = []
        for ref in self.documents:
            doc = docs.get(ref.doc_id)
            if doc is None:
                problems.append(f"document {ref.doc_id!r} not provided")
            elif doc.sha256 != ref.sha256:
                problems.append(f"document {ref.doc_id!r}: sha256 differs from Fact Sheet")
            elif doc.owner_id != self.owner_id:
                problems.append(f"document {ref.doc_id!r}: owner differs from Fact Sheet")
        items: list[tuple[str, Grounded]] = [(f.fact_id, f) for f in self.candidate_facts]
        items += [(r.req_id, r) for r in self.job_requirements]
        for item_id, item in items:
            doc = docs.get(item.source_doc)
            if doc is None:
                continue  # already reported above
            problem = item.span_problem(doc)
            if problem:
                problems.append(f"{item_id}: {problem}")
        return problems


# --------------------------------------------------------------------------- #
# Match / Gap Map
# --------------------------------------------------------------------------- #

MatchStatus = Literal["strong_match", "partial_match", "gap"]
QuestionAngle = Literal["deep_dive", "honest_gap", "clarify"]


class MatchEntry(_Strict):
    req_id: Annotated[str, StringConstraints(pattern=REQ_ID_RE.pattern)]
    status: MatchStatus  # categorical on purpose — no numeric score in M1
    evidence_fact_ids: list[NonEmptyStr] = Field(default_factory=list)
    rationale: NonEmptyStr  # 1–2 sentences, referring only to evidence facts
    missing: str | None = None  # what is absent (required for partial/gap)
    question_angle: QuestionAngle  # hook for the future interviewer

    @model_validator(mode="after")
    def _status_rules(self) -> MatchEntry:
        if self.status == "gap":
            if self.evidence_fact_ids:
                raise ValueError(f"{self.req_id}: a gap cannot carry evidence")
            if self.question_angle != "honest_gap":
                raise ValueError(f"{self.req_id}: a gap must use question_angle 'honest_gap'")
        else:
            if not self.evidence_fact_ids:
                raise ValueError(f"{self.req_id}: {self.status} requires at least one evidence fact")
            if self.status == "strong_match" and self.question_angle == "honest_gap":
                raise ValueError(f"{self.req_id}: strong_match cannot use 'honest_gap'")
        if self.status != "strong_match" and not (self.missing and self.missing.strip()):
            raise ValueError(f"{self.req_id}: {self.status} must say what is missing")
        if len(set(self.evidence_fact_ids)) != len(self.evidence_fact_ids):
            raise ValueError(f"{self.req_id}: duplicate evidence_fact_ids")
        return self


class MatchGapMap(_Strict):
    schema_version: Literal["1"] = "1"
    owner_id: NonEmptyStr
    fact_sheet_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    entries: list[MatchEntry]
    extra_strengths: list[NonEmptyStr] = Field(default_factory=list)  # strong fact_ids the JD didn't ask for
    matching_model: NonEmptyStr  # "<provider>:<model>"
    prompt_ids: list[NonEmptyStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_reqs(self) -> MatchGapMap:
        seen: set[str] = set()
        for e in self.entries:
            if e.req_id in seen:
                raise ValueError(f"requirement {e.req_id} mapped more than once")
            seen.add(e.req_id)
        return self

    def cross_check(self, sheet: FactSheet) -> list[str]:
        """Validate against the Fact Sheet it claims to be built from. Empty list == OK."""
        problems: list[str] = []
        if self.owner_id != sheet.owner_id:
            problems.append("owner_id differs from Fact Sheet")
        if self.fact_sheet_sha256 != sheet.content_hash():
            problems.append("fact_sheet_sha256 does not match this Fact Sheet")

        fact_ids = {f.fact_id for f in sheet.candidate_facts}
        # company_context items ("why this company?" material) are not matched against the candidate
        sheet_reqs = {r.req_id for r in sheet.matchable_requirements()}
        context_reqs = {r.req_id for r in sheet.job_requirements} - sheet_reqs
        mapped = {e.req_id for e in self.entries}
        for rid in sorted(sheet_reqs - mapped):
            problems.append(f"{rid}: requirement not mapped")
        for rid in sorted(mapped & context_reqs):
            problems.append(f"{rid}: company_context item should not be mapped")
        for rid in sorted(mapped - sheet_reqs - context_reqs):
            problems.append(f"{rid}: mapped but not in Fact Sheet")
        for e in self.entries:
            for fid in e.evidence_fact_ids:
                if fid not in fact_ids:
                    problems.append(f"{e.req_id}: evidence {fid} not in Fact Sheet")
        for fid in self.extra_strengths:
            if fid not in fact_ids:
                problems.append(f"extra_strengths: {fid} not in Fact Sheet")
        return problems
