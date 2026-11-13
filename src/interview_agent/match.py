"""Step 6 — Fact Sheet -> Match / Gap Map.

    build_match_gap_map(client, sheet)  ->  (MatchGapMap, MatchReport)

The model sees only grounded facts and requirements (never raw documents).
Every requirement except `company_context` gets exactly one entry. Each entry
the model returns is checked, in order:

  1. its req_id was asked for in this call (and not already mapped);
  2. schema rules (MatchEntry): match/partial need evidence, gap has none,
     partial/gap say what is missing, question_angle fits the status;
  3. every evidence fact id exists in the Fact Sheet;
  4. every number in `rationale` / `missing` appears in the requirement or in
     the cited facts (no computed durations or invented figures).

Rejected or missing requirements get ONE follow-up call containing only those
requirements. Anything still unmapped is reported, never filled with a default:
a made-up "gap" would itself be an ungrounded claim about the candidate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from pydantic import ValidationError

from interview_agent.grounding import numbers_in
from interview_agent.llm import LLMClient, LLMRequest, call_json
from interview_agent.prompt_loader import load_prompt
from interview_agent.schemas import Fact, FactSheet, JobRequirement, MatchEntry, MatchGapMap

PROMPT = ("match_gap", 1)
MAX_REQS_PER_CALL = 15
MAX_OUTPUT_TOKENS = 8192
MAX_EXTRA_STRENGTHS = 5


@dataclass
class EntryRejection:
    req_id: str
    reason: str  # invalid_entry | unknown_evidence | number_not_supported | not_requested | duplicate
    detail: str


@dataclass
class MatchReport:
    rejections: list[EntryRejection] = field(default_factory=list)
    retried: list[str] = field(default_factory=list)  # req_ids sent in the follow-up call
    unmapped: list[str] = field(default_factory=list)  # still unmapped after the follow-up
    failed_calls: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def complete(self) -> bool:
        return not self.unmapped


# --------------------------------------------------------------------------- #
# Prompt payloads
# --------------------------------------------------------------------------- #


def _req_line(r: JobRequirement) -> str:
    return json.dumps({"req_id": r.req_id, "kind": r.kind, "requirement": r.requirement, "skills": r.skills},
                      ensure_ascii=False)


def _fact_line(f: Fact) -> str:
    return json.dumps(
        {"fact_id": f.fact_id, "document": f.source_doc, "category": f.category, "claim": f.claim,
         "skills": f.skills, "time_period": f.time_period, "metric": f.metric.raw if f.metric else None},
        ensure_ascii=False,
    )


# --------------------------------------------------------------------------- #
# Entry checks
# --------------------------------------------------------------------------- #


def _allowed_numbers(req: JobRequirement, evidence: Sequence[Fact]) -> set[float]:
    texts = [req.requirement, req.source_text]
    for f in evidence:
        texts += [f.claim, f.source_text, f.time_period or "", f.metric.raw if f.metric else ""]
    return set().union(*(numbers_in(t) for t in texts))


def _check_entry(raw: Any, req: JobRequirement, facts: dict[str, Fact]) -> MatchEntry:
    """Return a valid entry or raise EntryProblem."""
    try:
        entry = MatchEntry.model_validate(raw)
    except ValidationError as e:
        msgs = [err["msg"].removeprefix("Value error, ") for err in e.errors()]
        raise _EntryProblem("invalid_entry", "; ".join(msgs)) from None
    unknown = [fid for fid in entry.evidence_fact_ids if fid not in facts]
    if unknown:
        raise _EntryProblem("unknown_evidence", f"evidence ids not in Fact Sheet: {', '.join(unknown)}")
    allowed = _allowed_numbers(req, [facts[fid] for fid in entry.evidence_fact_ids])
    for name in ("rationale", "missing"):
        extra = sorted(n for n in numbers_in(getattr(entry, name) or "") if n not in allowed)
        if extra:
            nums = ", ".join(f"{n:g}" for n in extra)
            raise _EntryProblem("number_not_supported", f"{name} has number(s) {nums} not in requirement or evidence")
    return entry


class _EntryProblem(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason, self.detail = reason, detail


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #


def _run_batch(client, prompt, batch: list[JobRequirement], facts_payload: str, facts: dict[str, Fact],
               mapped: dict[str, MatchEntry], extra: list[str], report: MatchReport) -> None:
    system, user = prompt.render(requirements="\n".join(_req_line(r) for r in batch), facts=facts_payload)
    request = LLMRequest(purpose="match_gap", prompt_id=prompt.prompt_id, system=system, user=user,
                         max_tokens=MAX_OUTPUT_TOKENS)
    data, failure = call_json(client, request, report, "entries")
    if data is None:
        report.failed_calls.append(f"batch {', '.join(r.req_id for r in batch)}: {failure}")
        return
    entries = data["entries"]

    asked = {r.req_id: r for r in batch}
    for raw in entries:
        req_id = raw.get("req_id") if isinstance(raw, dict) else None
        if req_id not in asked:
            report.rejections.append(EntryRejection(str(req_id), "not_requested", "req_id was not in this request"))
            continue
        if req_id in mapped:
            report.rejections.append(EntryRejection(req_id, "duplicate", "requirement already mapped; kept the first"))
            continue
        try:
            mapped[req_id] = _check_entry(raw, asked[req_id], facts)
        except _EntryProblem as p:
            report.rejections.append(EntryRejection(req_id, p.reason, p.detail))

    for fid in data.get("extra_strengths") or []:
        if not isinstance(fid, str) or fid not in facts:
            report.warnings.append(f"extra_strengths: {fid!r} is not a known fact id; dropped")
        elif fid not in extra:
            extra.append(fid)


def build_match_gap_map(client: LLMClient, sheet: FactSheet) -> tuple[MatchGapMap, MatchReport]:
    prompt = load_prompt(*PROMPT)
    report = MatchReport()
    requirements = sheet.matchable_requirements()
    facts = {f.fact_id: f for f in sheet.candidate_facts}
    facts_payload = "\n".join(_fact_line(f) for f in sheet.candidate_facts)
    mapped: dict[str, MatchEntry] = {}
    extra: list[str] = []

    def run(reqs: list[JobRequirement]) -> None:
        for i in range(0, len(reqs), MAX_REQS_PER_CALL):
            _run_batch(client, prompt, reqs[i : i + MAX_REQS_PER_CALL], facts_payload, facts, mapped, extra, report)

    run(requirements)
    missing = [r for r in requirements if r.req_id not in mapped]
    if missing:
        report.retried = [r.req_id for r in missing]
        run(missing)
    report.unmapped = [r.req_id for r in requirements if r.req_id not in mapped]

    if len(extra) > MAX_EXTRA_STRENGTHS:
        report.warnings.append(f"extra_strengths: kept the first {MAX_EXTRA_STRENGTHS} of {len(extra)}")
    gmap = MatchGapMap(
        owner_id=sheet.owner_id,
        fact_sheet_sha256=sheet.content_hash(),
        entries=[mapped[r.req_id] for r in requirements if r.req_id in mapped],  # requirement order
        extra_strengths=extra[:MAX_EXTRA_STRENGTHS],
        matching_model=f"{client.provider}:{client.model}",
        prompt_ids=[prompt.prompt_id],
    )
    return gmap, report
