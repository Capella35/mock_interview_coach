"""Step 5 — redacted documents -> LLM proposals -> grounded Fact Sheet.

    build_fact_sheet(client, docs)  ->  (FactSheet, ExtractionReport)

  1. Candidate documents (resume, LinkedIn): `extract_candidate` prompt, per chunk.
  2. Job description: `extract_job` prompt, per chunk.
  3. All proposals of a document are grounded together (grounding.py): ids are
     sequential, duplicates across chunks are dropped, rejections are kept for
     the review report.
  4. If both resume and LinkedIn have facts: `find_discrepancies` prompt over the
     grounded facts; cited ids must exist and span two documents.

Documents are split into chunks on paragraph/page boundaries so each reply stays
well under the token limit. A chunk whose reply is unusable (cut off, not JSON,
wrong shape) is retried once, then recorded in `failed_chunks` (the report makes this loud). Provider
errors (auth, exhausted retries) abort the run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from pydantic import ValidationError

from interview_agent.grounding import Rejection, ground_fact_proposals, ground_requirement_proposals
from interview_agent.llm import LLMClient, LLMRequest, call_json
from interview_agent.prompt_loader import load_prompt
from interview_agent.schemas import (
    CANDIDATE_DOC_KINDS,
    Discrepancy,
    Fact,
    FactSheet,
    JobRequirement,
    SourceDocument,
)

MAX_CHUNK_CHARS = 6000
MAX_OUTPUT_TOKENS = 8192
PROMPTS = {"candidate": ("extract_candidate", 1), "job": ("extract_job", 1), "discrepancies": ("find_discrepancies", 1)}


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


@dataclass
class DocExtraction:
    doc_id: str
    chunks: int = 0
    proposals: int = 0
    accepted: int = 0
    rejections: list[Rejection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failed_chunks: list[str] = field(default_factory=list)


@dataclass
class ExtractionReport:
    documents: list[DocExtraction] = field(default_factory=list)
    discrepancy_warnings: list[str] = field(default_factory=list)
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def has_failures(self) -> bool:
        return any(d.failed_chunks for d in self.documents)


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split on blank lines (paragraphs/pages); pack paragraphs into chunks <= max_chars.
    A single paragraph longer than max_chars becomes its own chunk (never split mid-paragraph)."""
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        if not para.strip():
            continue
        joined = f"{current}\n\n{para}" if current else para
        if len(joined) <= max_chars:
            current = joined
        else:
            if current:
                chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks


def _extract(client, doc, report, prompt_key, purpose, list_key, render_vars, ground) -> list:
    prompt = load_prompt(*PROMPTS[prompt_key])
    rec = DocExtraction(doc_id=doc.doc_id)
    report.documents.append(rec)
    chunks = chunk_text(doc.text, MAX_CHUNK_CHARS)
    rec.chunks = len(chunks)

    proposals: list = []
    for i, chunk in enumerate(chunks, start=1):
        system, user = prompt.render(**render_vars, part=f"{i} of {len(chunks)}", document=chunk)
        request = LLMRequest(purpose=purpose, prompt_id=prompt.prompt_id, system=system, user=user,
                             max_tokens=MAX_OUTPUT_TOKENS)
        data, failure = call_json(client, request, report, list_key)
        if data is None:
            rec.failed_chunks.append(f"part {i} of {len(chunks)}: {failure}")
            continue
        proposals.extend(data[list_key])

    rec.proposals = len(proposals)
    grounded = ground(doc, proposals)
    rec.accepted = len(grounded.accepted)
    rec.rejections = grounded.rejections
    rec.warnings = grounded.warnings
    return grounded.accepted


def extract_candidate_facts(client: LLMClient, doc: SourceDocument, report: ExtractionReport) -> list[Fact]:
    if doc.kind not in CANDIDATE_DOC_KINDS:
        raise ValueError(f"{doc.doc_id}: not a resume/LinkedIn document")
    return _extract(client, doc, report, "candidate", "extract_candidate", "facts",
                    {"doc_kind": doc.kind, "doc_id": doc.doc_id}, ground_fact_proposals)


def extract_job_requirements(client: LLMClient, doc: SourceDocument, report: ExtractionReport) -> list[JobRequirement]:
    if doc.kind != "job_description":
        raise ValueError(f"{doc.doc_id}: not a job description")
    return _extract(client, doc, report, "job", "extract_job", "requirements",
                    {"doc_id": doc.doc_id}, ground_requirement_proposals)


# --------------------------------------------------------------------------- #
# Discrepancies (resume vs LinkedIn)
# --------------------------------------------------------------------------- #


def find_discrepancies(client: LLMClient, facts: Sequence[Fact], report: ExtractionReport) -> list[Discrepancy]:
    doc_of = {f.fact_id: f.source_doc for f in facts}
    if len(set(doc_of.values())) < 2:
        return []
    prompt = load_prompt(*PROMPTS["discrepancies"])
    lines = "\n".join(
        json.dumps({"fact_id": f.fact_id, "document": f.source_doc, "claim": f.claim, "time_period": f.time_period},
                   ensure_ascii=False)
        for f in facts
    )
    system, user = prompt.render(facts=lines)
    data, failure = call_json(
        client, LLMRequest(purpose="find_discrepancies", prompt_id=prompt.prompt_id, system=system, user=user,
                           max_tokens=MAX_OUTPUT_TOKENS),
        report,
        "discrepancies",
    )
    if data is None:
        report.discrepancy_warnings.append(f"discrepancy check failed: {failure}")
        return []
    items = data["discrepancies"]

    out: list[Discrepancy] = []
    for i, item in enumerate(items):
        try:
            disc = Discrepancy.model_validate(item)
        except ValidationError:
            report.discrepancy_warnings.append(f"discrepancy {i}: invalid shape; dropped")
            continue
        unknown = [fid for fid in disc.fact_ids if fid not in doc_of]
        if unknown:
            report.discrepancy_warnings.append(f"discrepancy {i}: unknown fact ids {unknown}; dropped")
            continue
        if len({doc_of[fid] for fid in disc.fact_ids}) < 2:
            report.discrepancy_warnings.append(f"discrepancy {i}: facts from only one document; dropped")
            continue
        out.append(disc)
    return out


# --------------------------------------------------------------------------- #
# Whole Fact Sheet
# --------------------------------------------------------------------------- #

_CANDIDATE_ORDER = {"resume": 0, "linkedin": 1}


def build_fact_sheet(
    client: LLMClient, docs: Sequence[SourceDocument], *, now: datetime | None = None
) -> tuple[FactSheet, ExtractionReport]:
    """Build the Fact Sheet for ONE owner from their redacted documents."""
    owners = {d.owner_id for d in docs}
    if len(owners) != 1:
        raise ValueError("all documents must belong to the same owner")  # data isolation
    if len({d.doc_id for d in docs}) != len(docs):
        raise ValueError("duplicate doc_id")
    jds = [d for d in docs if d.kind == "job_description"]
    candidate_docs = sorted((d for d in docs if d.kind in CANDIDATE_DOC_KINDS), key=lambda d: _CANDIDATE_ORDER[d.kind])
    if len(jds) != 1:
        raise ValueError(f"exactly one job description required, got {len(jds)}")
    if not candidate_docs:
        raise ValueError("at least one resume or LinkedIn document required")

    report = ExtractionReport()
    facts: list[Fact] = []
    for doc in candidate_docs:
        facts += extract_candidate_facts(client, doc, report)
    requirements = extract_job_requirements(client, jds[0], report)
    discrepancies = find_discrepancies(client, facts, report)

    prompt_ids = [f"{PROMPTS['candidate'][0]}.v{PROMPTS['candidate'][1]}", f"{PROMPTS['job'][0]}.v{PROMPTS['job'][1]}"]
    if len({f.source_doc for f in facts}) >= 2:
        prompt_ids.append(f"{PROMPTS['discrepancies'][0]}.v{PROMPTS['discrepancies'][1]}")

    sheet = FactSheet(
        owner_id=owners.pop(),
        created_at=now or datetime.now(timezone.utc),
        documents=[d.ref() for d in [*candidate_docs, jds[0]]],
        candidate_facts=facts,
        job_requirements=requirements,
        discrepancies=discrepancies,
        extraction_model=f"{client.provider}:{client.model}",
        prompt_ids=prompt_ids,
    )
    return sheet, report
