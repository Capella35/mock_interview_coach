"""Step 7 — human review report (`out/review.md`).

Built for the owner's manual check (Milestone 1 test plan): every requirement
with its status and the exact evidence quotes (document + page), everything
the model proposed that was rejected, and anything incomplete — problems first.
Contains redaction COUNTS only, never original contact values.
"""

from __future__ import annotations

from collections import Counter
from typing import Mapping, Sequence

from interview_agent.extract import ExtractionReport
from interview_agent.match import MatchReport
from interview_agent.redact import RedactionMap
from interview_agent.schemas import FactSheet, MatchGapMap, SourceDocument

STATUS_LABEL = {"strong_match": "✅ strong match", "partial_match": "🟡 partial match", "gap": "❌ gap"}


def _one_line(text: str) -> str:
    return " / ".join(part.strip() for part in text.splitlines() if part.strip())


def _cell(text: str | None) -> str:
    return _one_line(text or "").replace("|", "\\|")


def _where(item) -> str:
    return f"{item.source_doc}, p.{item.page}" if item.page else item.source_doc


def problems(
    sheet: FactSheet,
    gmap: MatchGapMap,
    docs: Mapping[str, SourceDocument],
    extraction: ExtractionReport,
    matching: MatchReport,
) -> list[str]:
    """Everything that makes this run incomplete or untrustworthy. Empty == clean."""
    out: list[str] = []
    for d in extraction.documents:
        out += [f"{d.doc_id}: failed chunk — {c}" for c in d.failed_chunks]
    out += [f"match: failed call — {c}" for c in matching.failed_calls]
    out += [f"{rid}: not mapped (model gave no valid entry after a follow-up call)" for rid in matching.unmapped]
    out += [f"integrity: {p}" for p in sheet.verify_spans(dict(docs))]
    out += [f"integrity: {p}" for p in gmap.cross_check(sheet) if "not mapped" not in p]
    return out


def render_review(
    sheet: FactSheet,
    gmap: MatchGapMap,
    docs: Mapping[str, SourceDocument],
    extraction: ExtractionReport,
    matching: MatchReport,
    redaction: Sequence[RedactionMap] = (),
) -> str:
    facts = {f.fact_id: f for f in sheet.candidate_facts}
    entries = {e.req_id: e for e in gmap.entries}
    matchable = sheet.matchable_requirements()
    context = [r for r in sheet.job_requirements if r.kind == "company_context"]
    status_counts = Counter(e.status for e in gmap.entries)
    issues = problems(sheet, gmap, docs, extraction, matching)
    L: list[str] = []

    # ------------------------------------------------------------------ header
    L += [
        "# Review — Fact Sheet and Match / Gap Map",
        "",
        f"Created {sheet.created_at:%Y-%m-%d %H:%M} UTC · extraction `{sheet.extraction_model}` · "
        f"matching `{gmap.matching_model}` · prompts {', '.join(f'`{p}`' for p in sheet.prompt_ids + gmap.prompt_ids)}",
        "",
    ]
    if issues:
        L += ["## ⚠️ Problems — this run is incomplete", ""] + [f"- {p}" for p in issues] + [""]
    else:
        L += ["**No problems:** every chunk was processed, every requirement is mapped, and every quote "
              "matches its document exactly.", ""]

    # ----------------------------------------------------------------- summary
    L += ["## Summary", "", "| | |", "|---|---|"]
    L.append(f"| Requirements matched | {len(matchable)} "
             f"(✅ {status_counts['strong_match']} · 🟡 {status_counts['partial_match']} · ❌ {status_counts['gap']}"
             f"{f' · ⚠️ {len(matching.unmapped)} unmapped' if matching.unmapped else ''}) |")
    L.append(f"| Company-context items | {len(context)} |")
    for d in extraction.documents:
        L.append(f"| `{d.doc_id}` extraction | {d.accepted} accepted · {len(d.rejections)} rejected · "
                 f"{d.chunks} chunk(s){f' · ⚠️ {len(d.failed_chunks)} failed' if d.failed_chunks else ''} |")
    L.append(f"| Discrepancies (resume vs LinkedIn) | {len(sheet.discrepancies)} |")
    for r in redaction:
        counts = ", ".join(f"{n} {k}" for k, n in sorted(r.counts().items())) or "nothing"
        L.append(f"| `{r.doc_id}` redaction | {counts} masked |")
    calls = extraction.llm_calls + matching.llm_calls
    tokens_in = extraction.prompt_tokens + matching.prompt_tokens
    tokens_out = extraction.completion_tokens + matching.completion_tokens
    L += [f"| LLM usage | {calls} call(s) · {tokens_in} prompt tokens · {tokens_out} completion tokens |", ""]

    # ------------------------------------------------------------ requirements
    L += ["## Requirements", ""]
    for req in matchable:
        e = entries.get(req.req_id)
        label = STATUS_LABEL[e.status] if e else "⚠️ not mapped"
        L += [f"### {req.req_id} · {req.kind} · {label}", "", f"**{req.requirement}**", "",
              f"> {_one_line(req.source_text)}  ", f"> — *{_where(req)}*", ""]
        if e is None:
            continue
        L.append(f"- **Why:** {e.rationale}")
        if e.missing:
            L.append(f"- **Missing:** {e.missing}")
        L.append(f"- **Interview angle:** `{e.question_angle}`")
        for fid in e.evidence_fact_ids:
            f = facts[fid]
            L.append(f"- **Evidence `{fid}`** ({_where(f)}): {f.claim}  \n  > {_one_line(f.source_text)}")
        L.append("")

    if context:
        L += ["## Company context (not matched)", ""]
        L += [f"- `{r.req_id}` {r.requirement} — *{_where(r)}*: \"{_one_line(r.source_text)}\"" for r in context]
        L.append("")

    if gmap.extra_strengths:
        L += ["## Extra strengths (not asked for by the job)", ""]
        L += [f"- `{fid}` {facts[fid].claim} — *{_where(facts[fid])}*" for fid in gmap.extra_strengths if fid in facts]
        L.append("")

    if sheet.discrepancies:
        L += ["## Discrepancies — resume vs LinkedIn", ""]
        for d in sheet.discrepancies:
            L.append(f"- {d.description}")
            L += [f"  - `{fid}` ({_where(facts[fid])}): \"{_one_line(facts[fid].source_text)}\"" for fid in d.fact_ids]
        L.append("")

    # ------------------------------------------------------------------- facts
    L += ["## All candidate facts", "", "| id | category | claim | quote | where | metric |", "|---|---|---|---|---|---|"]
    for f in sheet.candidate_facts:
        L.append(f"| `{f.fact_id}` | {f.category} | {_cell(f.claim)} | {_cell(f.source_text)} | {_where(f)} | "
                 f"{_cell(f.metric.raw) if f.metric else ''} |")
    L.append("")

    # -------------------------------------------------------- rejections, notes
    rejected = [(d.doc_id, r) for d in extraction.documents for r in d.rejections]
    if rejected or matching.rejections:
        L += ["## Rejected by grounding (what the model said that is not supported)", ""]
        L += [f"- `{doc_id}` proposal {r.index}: **{r.reason}** — {r.detail}"
              + (f"; quote: \"{_one_line(r.quote)}\"" if r.quote else "") for doc_id, r in rejected]
        L += [f"- match `{r.req_id}`: **{r.reason}** — {r.detail}" for r in matching.rejections]
        L.append("")

    notes = [w for d in extraction.documents for w in d.warnings] + extraction.discrepancy_warnings + matching.warnings
    notes += [f"{r.doc_id}: {w}" for r in redaction for w in r.warnings]
    if notes:
        L += ["## Notes", ""] + [f"- {n}" for n in notes] + [""]

    L += [
        "## How to review",
        "",
        "1. Every **claim** says no more than its quote (no added numbers, scope or praise).",
        "2. Every requirement in the job post is listed above (none missing).",
        "3. Each status is right: would you call it strong / partial / gap?",
        "4. Your key metrics are in **All candidate facts**.",
        "",
        "Record disagreements in `evals/private/` — they become the golden set.",
        "",
    ]
    return "\n".join(L)
