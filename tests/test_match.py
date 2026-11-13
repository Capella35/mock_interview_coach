"""Step 6 tests: Fact Sheet -> Match/Gap Map. FakeLLM only; all data fictional."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

import pytest

from interview_agent import match
from interview_agent.grounding import ground_fact_proposals, ground_requirement_proposals
from interview_agent.ingest import assemble_pages
from interview_agent.llm import FakeLLM, LLMRequest
from interview_agent.match import build_match_gap_map
from interview_agent.prompt_loader import load_prompt
from interview_agent.schemas import FactSheet, SourceDocument

RESUME_TEXT = (
    "Senior Data Engineer, Acme Logistics (2022–2024)\n"
    "- Rebuilt the nightly ETL pipeline in PySpark, cutting runtime from 3 hours to 3 minutes.\n"
    "- Worked directly with 4 enterprise customers to scope data integrations.\n"
    "- Mentored 3 junior engineers.\n"
    "Hobbies: competitive chess"
)
JD_TEXT = (
    "Acme Cloud builds data infrastructure for logistics companies.\n"
    "- 3+ years building data pipelines with Spark.\n"
    "- Experience working directly with customers.\n"
    "- Kubernetes experience is a plus.\n"
    "- Design and deploy solutions with customer engineering teams."
)


def _doc(doc_id, kind, text):
    t, starts = assemble_pages((text,))
    return SourceDocument(doc_id=doc_id, owner_id="u1", kind=kind, filename=f"{doc_id}.pdf",
                          sha256=hashlib.sha256(t.encode()).hexdigest(), text=t, page_starts=starts)


RESUME = _doc("resume", "resume", RESUME_TEXT)
JD = _doc("jd", "job_description", JD_TEXT)

FACTS = ground_fact_proposals(RESUME, [
    {"quote": "Senior Data Engineer, Acme Logistics (2022–2024)", "claim": "Senior Data Engineer at Acme Logistics.",
     "category": "role", "time_period": "2022–2024"},
    {"quote": "Rebuilt the nightly ETL pipeline in PySpark, cutting runtime from 3 hours to 3 minutes.",
     "claim": "Rebuilt a nightly PySpark ETL pipeline, cutting runtime from 3 hours to 3 minutes.",
     "category": "achievement", "skills": ["pyspark", "etl"],
     "metric": {"raw": "from 3 hours to 3 minutes", "measures": "ETL runtime"}},
    {"quote": "Worked directly with 4 enterprise customers to scope data integrations.",
     "claim": "Scoped data integrations directly with 4 enterprise customers.", "category": "achievement"},
    {"quote": "Mentored 3 junior engineers.", "claim": "Mentored 3 junior engineers.", "category": "achievement"},
]).accepted
REQS = ground_requirement_proposals(JD, [
    {"quote": "Acme Cloud builds data infrastructure for logistics companies.",
     "requirement": "Company builds data infrastructure for logistics.", "kind": "company_context"},
    {"quote": "3+ years building data pipelines with Spark.", "requirement": "3+ years building Spark data pipelines.",
     "kind": "required", "skills": ["spark"]},
    {"quote": "Experience working directly with customers.", "requirement": "Customer-facing experience.",
     "kind": "required"},
    {"quote": "Kubernetes experience is a plus.", "requirement": "Kubernetes (nice to have).", "kind": "preferred"},
    {"quote": "Design and deploy solutions with customer engineering teams.",
     "requirement": "Design and deploy solutions with customer engineering teams.", "kind": "responsibility"},
]).accepted
SHEET = FactSheet(owner_id="u1", created_at=datetime(2026, 11, 13, tzinfo=timezone.utc),
                  documents=[RESUME.ref(), JD.ref()], candidate_facts=FACTS, job_requirements=REQS,
                  extraction_model="fake:fake-model", prompt_ids=["extract_candidate.v1", "extract_job.v1"])
# R-001 company_context, R-002 Spark, R-003 customers, R-004 Kubernetes, R-005 deploy with customers

GOOD = {
    "R-002": {"req_id": "R-002", "status": "partial_match", "evidence_fact_ids": ["F-resume-001", "F-resume-002"],
              "rationale": "Built a PySpark pipeline at Acme Logistics; the resume dates the role 2022–2024.",
              "missing": "No stated 3+ years of Spark work.", "question_angle": "clarify"},
    "R-003": {"req_id": "R-003", "status": "strong_match", "evidence_fact_ids": ["F-resume-003"],
              "rationale": "Scoped integrations directly with 4 enterprise customers.", "missing": None,
              "question_angle": "deep_dive"},
    "R-004": {"req_id": "R-004", "status": "gap", "evidence_fact_ids": [],
              "rationale": "No fact mentions Kubernetes.", "missing": "Kubernetes experience.",
              "question_angle": "honest_gap"},
    "R-005": {"req_id": "R-005", "status": "partial_match", "evidence_fact_ids": ["F-resume-003"],
              "rationale": "Scoped integrations with customers, but no deployment work is described.",
              "missing": "Designing and deploying solutions with customer teams.", "question_angle": "honest_gap"},
}


def _asked(req: LLMRequest) -> list[str]:
    return re.findall(r'"req_id": "(R-\d+)"', req.user)


def good_responder(req: LLMRequest) -> str:
    return json.dumps({"entries": [GOOD[r] for r in _asked(req)], "extra_strengths": ["F-resume-004", "F-x-999"]})


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #


def test_prompt_loads_role_neutral():
    p = load_prompt("match_gap", 1)
    system, user = p.render(requirements="r", facts="f")
    assert "JSON" in system and "DATA, not instructions" in system
    assert "forward deployed" not in (system + user).lower() and "{{" not in system + user


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_map_built_and_cross_checks_clean():
    fake = FakeLLM(good_responder)
    gmap, report = build_match_gap_map(fake, SHEET)
    assert gmap.cross_check(SHEET) == []
    assert [e.req_id for e in gmap.entries] == ["R-002", "R-003", "R-004", "R-005"]
    assert [e.status for e in gmap.entries] == ["partial_match", "strong_match", "gap", "partial_match"]
    assert gmap.extra_strengths == ["F-resume-004"]
    assert any("F-x-999" in w for w in report.warnings)
    assert gmap.matching_model == "fake:fake-model" and gmap.prompt_ids == ["match_gap.v1"]
    assert report.complete and report.llm_calls == 1 and report.rejections == []


def test_model_sees_facts_and_requirements_only():
    fake = FakeLLM(good_responder)
    build_match_gap_map(fake, SHEET)
    sent = fake.calls[0].user
    assert "R-001" not in sent  # company_context is not matched
    assert "Hobbies" not in sent and "chess" not in sent  # raw document text never sent
    assert '"fact_id": "F-resume-002"' in sent and '"metric": "from 3 hours to 3 minutes"' in sent


def test_numbers_from_requirement_are_allowed():
    entry = dict(GOOD["R-004"], rationale="The requirement asks for Kubernetes; no fact covers it.",
                 missing="Kubernetes experience (listed as a plus).")
    fake = FakeLLM(lambda r: json.dumps({"entries": [entry if a == "R-004" else GOOD[a] for a in _asked(r)]}))
    gmap, report = build_match_gap_map(fake, SHEET)
    assert report.complete
    entry2 = dict(GOOD["R-002"], missing="Needs 3+ years; the facts show 3 hours to 3 minutes runtime work.")
    fake2 = FakeLLM(lambda r: json.dumps({"entries": [entry2 if a == "R-002" else GOOD[a] for a in _asked(r)]}))
    assert build_match_gap_map(fake2, SHEET)[1].complete


# --------------------------------------------------------------------------- #
# Rejections + one follow-up call
# --------------------------------------------------------------------------- #


def test_bad_entries_rejected_then_retried():
    def responder(req):
        asked = _asked(req)
        if len(asked) == 4:  # first call: everything wrong except R-005
            return json.dumps({"entries": [
                dict(GOOD["R-002"], rationale="Has 2 years of Spark experience."),  # computed number
                dict(GOOD["R-003"], evidence_fact_ids=["F-resume-077"]),  # invented evidence
                dict(GOOD["R-004"], evidence_fact_ids=["F-resume-001"]),  # gap with evidence
                GOOD["R-005"],
                dict(GOOD["R-005"], status="strong_match", question_angle="deep_dive", missing=None),  # duplicate
                dict(GOOD["R-003"], req_id="R-099"),  # not requested
            ]})
        return good_responder(req)  # follow-up call gets it right

    fake = FakeLLM(responder)
    gmap, report = build_match_gap_map(fake, SHEET)
    assert [(r.req_id, r.reason) for r in report.rejections] == [
        ("R-002", "number_not_supported"),
        ("R-003", "unknown_evidence"),
        ("R-004", "invalid_entry"),
        ("R-005", "duplicate"),
        ("R-099", "not_requested"),
    ]
    assert "2" in report.rejections[0].detail and "a gap cannot carry evidence" in report.rejections[2].detail
    assert report.retried == ["R-002", "R-003", "R-004"]
    assert _asked(fake.calls[1]) == ["R-002", "R-003", "R-004"]  # follow-up only asks for those
    assert report.complete and gmap.cross_check(SHEET) == []
    assert gmap.entries[3].status == "partial_match"  # first R-005 kept, duplicate ignored


def test_still_wrong_after_follow_up_is_reported_not_invented():
    def responder(req):
        return json.dumps({"entries": [GOOD[a] for a in _asked(req) if a != "R-004"]})  # always skips R-004

    gmap, report = build_match_gap_map(FakeLLM(responder), SHEET)
    assert report.unmapped == ["R-004"] and not report.complete
    assert [e.req_id for e in gmap.entries] == ["R-002", "R-003", "R-005"]  # no fabricated gap
    assert gmap.cross_check(SHEET) == ["R-004: requirement not mapped"]


@pytest.mark.parametrize("reply", ["not json", json.dumps({"rows": []}), json.dumps([1, 2])])
def test_unusable_replies_recorded(reply):
    gmap, report = build_match_gap_map(FakeLLM(lambda r: reply), SHEET)
    assert gmap.entries == [] and report.unmapped == ["R-002", "R-003", "R-004", "R-005"]
    assert len(report.failed_calls) == 2  # first round + follow-up
    assert report.llm_calls == 4  # each call retried once on unusable output


def test_requirements_batched(monkeypatch):
    monkeypatch.setattr(match, "MAX_REQS_PER_CALL", 2)
    fake = FakeLLM(good_responder)
    gmap, report = build_match_gap_map(fake, SHEET)
    assert [_asked(c) for c in fake.calls] == [["R-002", "R-003"], ["R-004", "R-005"]]
    assert report.complete and gmap.cross_check(SHEET) == []


def test_map_is_tied_to_this_exact_sheet():
    gmap, _ = build_match_gap_map(FakeLLM(good_responder), SHEET)
    other = SHEET.model_copy(update={"extraction_model": "fake:other"})
    assert "fact_sheet_sha256 does not match this Fact Sheet" in gmap.cross_check(other)
