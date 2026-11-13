"""Step 1 tests: schema shape rules and cross-object grounding checks.

All data here is FICTIONAL ("Alex Rivera" is not a real candidate).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from interview_agent.schemas import (
    Discrepancy,
    Fact,
    FactSheet,
    JobRequirement,
    MatchEntry,
    MatchGapMap,
    Metric,
    SourceDocument,
)

OWNER = "user-test"

RESUME_TEXT = (
    "[NAME]\n"
    "Senior Data Engineer, Acme Logistics (2022–2024)\n"
    "- Rebuilt the nightly ETL pipeline in PySpark, cutting runtime from 3 hours to 3 minutes.\n"
    "- Worked directly with 4 enterprise customers to scope data integrations.\n"
)
LINKEDIN_TEXT = "Experience\nData Engineer\nAcme Logistics\n2022 - 2024\n"
JD_TEXT = (
    "Forward Deployed Engineer\n"
    "Requirements:\n"
    "- 3+ years building data pipelines with Spark.\n"
    "- Experience working directly with customers.\n"
    "- Kubernetes experience is a plus.\n"
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _doc(doc_id: str, kind: str, text: str) -> SourceDocument:
    return SourceDocument(
        doc_id=doc_id, owner_id=OWNER, kind=kind, filename=f"{doc_id}.pdf", sha256=_sha(text), text=text
    )


DOCS = {
    "resume": _doc("resume", "resume", RESUME_TEXT),
    "linkedin": _doc("linkedin", "linkedin", LINKEDIN_TEXT),
    "jd": _doc("jd", "job_description", JD_TEXT),
}


def _span(doc_id: str, quote: str) -> dict:
    text = DOCS[doc_id].text
    start = text.index(quote)
    return dict(source_doc=doc_id, source_text=quote, char_start=start, char_end=start + len(quote), page=1)


def _facts() -> list[Fact]:
    return [
        Fact(
            fact_id="F-resume-001",
            claim="Was a Senior Data Engineer at Acme Logistics from 2022 to 2024.",
            category="role",
            time_period="2022–2024",
            **_span("resume", "Senior Data Engineer, Acme Logistics (2022–2024)"),
        ),
        Fact(
            fact_id="F-resume-002",
            claim="Rebuilt a nightly ETL pipeline in PySpark, reducing runtime from 3 hours to 3 minutes.",
            category="achievement",
            skills=["pyspark", "etl"],
            metric=Metric(raw="from 3 hours to 3 minutes", measures="nightly ETL runtime"),
            **_span("resume", "Rebuilt the nightly ETL pipeline in PySpark, cutting runtime from 3 hours to 3 minutes."),
        ),
        Fact(
            fact_id="F-resume-003",
            claim="Scoped data integrations directly with 4 enterprise customers.",
            category="achievement",
            skills=["customer-facing", "scoping"],
            metric=Metric(raw="4 enterprise customers", value=4, unit="customers", measures="customers worked with"),
            **_span("resume", "Worked directly with 4 enterprise customers to scope data integrations."),
        ),
        Fact(
            fact_id="F-linkedin-001",
            claim="Was a Data Engineer at Acme Logistics from 2022 to 2024.",
            category="role",
            **_span("linkedin", "Data Engineer\nAcme Logistics\n2022 - 2024"),  # crosses line breaks
        ),
    ]


def _reqs() -> list[JobRequirement]:
    return [
        JobRequirement(
            req_id="R-001",
            requirement="3+ years building data pipelines with Spark.",
            kind="required",
            skills=["spark"],
            **_span("jd", "3+ years building data pipelines with Spark."),
        ),
        JobRequirement(
            req_id="R-002",
            requirement="Experience working directly with customers.",
            kind="required",
            **_span("jd", "Experience working directly with customers."),
        ),
        JobRequirement(
            req_id="R-003",
            requirement="Kubernetes experience (nice to have).",
            kind="preferred",
            skills=["kubernetes"],
            **_span("jd", "Kubernetes experience is a plus."),
        ),
    ]


def _sheet(**overrides) -> FactSheet:
    data = dict(
        owner_id=OWNER,
        created_at=datetime(2026, 11, 12, tzinfo=timezone.utc),
        documents=[d.ref() for d in DOCS.values()],
        candidate_facts=_facts(),
        job_requirements=_reqs(),
        discrepancies=[
            Discrepancy(
                fact_ids=["F-resume-001", "F-linkedin-001"],
                description="Title 'Senior Data Engineer' on resume vs 'Data Engineer' on LinkedIn.",
            )
        ],
        extraction_model="fake",
    )
    data.update(overrides)
    return FactSheet(**data)


def _entries() -> list[MatchEntry]:
    return [
        MatchEntry(
            req_id="R-001",
            status="partial_match",
            evidence_fact_ids=["F-resume-001", "F-resume-002"],
            rationale="Built a PySpark ETL pipeline at Acme (2022–2024).",
            missing="Documented tenure is ~2 years, not 3+.",
            question_angle="clarify",
        ),
        MatchEntry(
            req_id="R-002",
            status="strong_match",
            evidence_fact_ids=["F-resume-003"],
            rationale="Scoped integrations directly with 4 enterprise customers.",
            question_angle="deep_dive",
        ),
        MatchEntry(
            req_id="R-003",
            status="gap",
            rationale="No Kubernetes experience in resume or LinkedIn.",
            missing="Kubernetes.",
            question_angle="honest_gap",
        ),
    ]


def _map(sheet: FactSheet, **overrides) -> MatchGapMap:
    data = dict(
        owner_id=OWNER,
        fact_sheet_sha256=sheet.content_hash(),
        entries=_entries(),
        matching_model="fake",
    )
    data.update(overrides)
    return MatchGapMap(**data)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_valid_sheet_and_map_pass_all_checks():
    sheet = _sheet()
    assert sheet.verify_spans(DOCS) == []
    assert _map(sheet).cross_check(sheet) == []


def test_json_round_trip_preserves_exact_source_text():
    sheet = _sheet()
    again = FactSheet.model_validate_json(sheet.model_dump_json())
    assert again == sheet
    assert again.content_hash() == sheet.content_hash()
    assert "\n" in again.fact("F-linkedin-001").source_text


def test_source_text_is_not_whitespace_stripped():
    span = _span("resume", "Senior Data Engineer, Acme Logistics (2022–2024)\n")
    fact = Fact(fact_id="F-resume-009", claim="x", category="role", **span)
    assert fact.source_text.endswith("\n")


def test_page_of():
    doc = SourceDocument(
        doc_id="resume", owner_id=OWNER, kind="resume", filename="r.pdf",
        sha256=_sha("ab"), text="aaaa" + "bbbb", page_starts=[0, 4],
    )
    assert doc.page_of(0) == 1 and doc.page_of(3) == 1 and doc.page_of(4) == 2


# --------------------------------------------------------------------------- #
# Grounded span shape
# --------------------------------------------------------------------------- #


def test_span_length_mismatch_rejected():
    span = _span("resume", "Senior Data Engineer")
    span["char_end"] += 1
    with pytest.raises(ValidationError, match="length"):
        Fact(fact_id="F-resume-009", claim="x", category="role", **span)


def test_empty_or_inverted_span_rejected():
    span = _span("resume", "Senior")
    span["char_end"] = span["char_start"]
    with pytest.raises(ValidationError):
        Fact(fact_id="F-resume-009", claim="x", category="role", **span)


@pytest.mark.parametrize("bad_id", ["F-12", "fact-resume-001", "F-resume-1", "F-Resume-001"])
def test_bad_fact_id_rejected(bad_id):
    with pytest.raises(ValidationError):
        Fact(fact_id=bad_id, claim="x", category="role", **_span("resume", "Senior"))


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        Fact(fact_id="F-resume-009", claim="x", category="role", score=5, **_span("resume", "Senior"))


# --------------------------------------------------------------------------- #
# No invented / inflated numbers
# --------------------------------------------------------------------------- #


def test_inflated_metric_rejected():
    with pytest.raises(ValidationError, match="does not appear"):
        Fact(
            fact_id="F-resume-009",
            claim="Worked with 6 enterprise customers.",
            category="achievement",
            metric=Metric(raw="6 enterprise customers", measures="customers"),
            **_span("resume", "Worked directly with 4 enterprise customers to scope data integrations."),
        )


# --------------------------------------------------------------------------- #
# Spans checked against the real documents
# --------------------------------------------------------------------------- #


def test_tampered_quote_detected_against_document():
    # Shape-valid (length matches offsets) but text is not what the document says.
    real = _span("resume", "cutting runtime from 3 hours to 3 minutes")
    forged = dict(real, source_text="cutting runtime from 9 hours to 3 minutes")
    facts = _facts() + [Fact(fact_id="F-resume-009", claim="x", category="achievement", **forged)]
    problems = _sheet(candidate_facts=facts).verify_spans(DOCS)
    assert len(problems) == 1 and problems[0].startswith("F-resume-009")


def test_changed_document_detected_by_hash():
    docs = dict(DOCS, resume=_doc("resume", "resume", RESUME_TEXT + "extra"))
    assert any("sha256" in p for p in _sheet().verify_spans(docs))


def test_document_of_other_owner_detected():
    other = DOCS["resume"].model_copy(update={"owner_id": "someone-else"})
    assert any("owner" in p for p in _sheet().verify_spans(dict(DOCS, resume=other)))


def test_missing_document_reported():
    docs = {k: v for k, v in DOCS.items() if k != "jd"}
    assert any("'jd' not provided" in p for p in _sheet().verify_spans(docs))


# --------------------------------------------------------------------------- #
# Fact Sheet consistency
# --------------------------------------------------------------------------- #


def test_duplicate_fact_id_rejected():
    with pytest.raises(ValidationError, match="duplicate fact_id"):
        _sheet(candidate_facts=_facts() + [_facts()[0]])


def test_candidate_fact_from_jd_rejected():
    bad = Fact(fact_id="F-jd-001", claim="x", category="skill", **_span("jd", "Kubernetes"))
    with pytest.raises(ValidationError, match="job_description document"):
        _sheet(candidate_facts=_facts() + [bad])


def test_requirement_from_resume_rejected():
    bad = JobRequirement(req_id="R-009", requirement="x", kind="required", **_span("resume", "PySpark"))
    with pytest.raises(ValidationError, match="resume document"):
        _sheet(job_requirements=_reqs() + [bad])


def test_fact_from_unknown_document_rejected():
    span = _span("resume", "PySpark")
    span["source_doc"] = "github"
    with pytest.raises(ValidationError, match="unknown source_doc"):
        _sheet(candidate_facts=_facts() + [Fact(fact_id="F-github-001", claim="x", category="skill", **span)])


def test_discrepancy_needs_two_documents():
    d = Discrepancy(fact_ids=["F-resume-001", "F-resume-002"], description="x")
    with pytest.raises(ValidationError, match="two documents"):
        _sheet(discrepancies=[d])


def test_discrepancy_unknown_fact_rejected():
    d = Discrepancy(fact_ids=["F-resume-001", "F-linkedin-999"], description="x")
    with pytest.raises(ValidationError, match="unknown fact_ids"):
        _sheet(discrepancies=[d])


# --------------------------------------------------------------------------- #
# Match entry rules: a gap can never pose as a match
# --------------------------------------------------------------------------- #


def _entry(**kw) -> dict:
    base = dict(req_id="R-001", rationale="r", missing="m", question_angle="clarify")
    base.update(kw)
    return base


def test_match_without_evidence_rejected():
    with pytest.raises(ValidationError, match="requires at least one evidence"):
        MatchEntry(**_entry(status="strong_match", question_angle="deep_dive"))


def test_gap_with_evidence_rejected():
    with pytest.raises(ValidationError, match="cannot carry evidence"):
        MatchEntry(**_entry(status="gap", evidence_fact_ids=["F-resume-001"], question_angle="honest_gap"))


def test_gap_must_use_honest_gap_angle():
    with pytest.raises(ValidationError, match="honest_gap"):
        MatchEntry(**_entry(status="gap", question_angle="deep_dive"))


def test_strong_match_cannot_use_honest_gap_angle():
    with pytest.raises(ValidationError):
        MatchEntry(**_entry(status="strong_match", evidence_fact_ids=["F-resume-001"], question_angle="honest_gap"))


@pytest.mark.parametrize("status", ["partial_match", "gap"])
def test_partial_and_gap_must_say_what_is_missing(status):
    ev = ["F-resume-001"] if status == "partial_match" else []
    angle = "honest_gap"
    with pytest.raises(ValidationError, match="missing"):
        MatchEntry(**_entry(status=status, evidence_fact_ids=ev, question_angle=angle, missing="  "))


def test_duplicate_evidence_rejected():
    with pytest.raises(ValidationError, match="duplicate"):
        MatchEntry(**_entry(status="partial_match", evidence_fact_ids=["F-resume-001", "F-resume-001"]))


# --------------------------------------------------------------------------- #
# Map ↔ Fact Sheet cross-check
# --------------------------------------------------------------------------- #


def test_requirement_mapped_twice_rejected():
    sheet = _sheet()
    with pytest.raises(ValidationError, match="more than once"):
        _map(sheet, entries=_entries() + [_entries()[0]])


def test_unmapped_requirement_reported():
    sheet = _sheet()
    problems = _map(sheet, entries=_entries()[:2]).cross_check(sheet)
    assert problems == ["R-003: requirement not mapped"]


def test_mapping_unknown_requirement_reported():
    sheet = _sheet()
    extra = MatchEntry(req_id="R-099", status="gap", rationale="r", missing="m", question_angle="honest_gap")
    assert "R-099: mapped but not in Fact Sheet" in _map(sheet, entries=_entries() + [extra]).cross_check(sheet)


def test_invented_evidence_fact_reported():
    sheet = _sheet()
    entries = _entries()
    entries[1] = entries[1].model_copy(update={"evidence_fact_ids": ["F-resume-777"]})
    assert "R-002: evidence F-resume-777 not in Fact Sheet" in _map(sheet, entries=entries).cross_check(sheet)


def test_map_built_from_different_sheet_reported():
    sheet = _sheet()
    other = _sheet(extraction_model="other")
    assert "fact_sheet_sha256 does not match this Fact Sheet" in _map(other).cross_check(sheet)


def test_map_of_other_owner_reported():
    sheet = _sheet()
    assert "owner_id differs from Fact Sheet" in _map(sheet, owner_id="someone-else").cross_check(sheet)


def test_company_context_is_not_matched():
    ctx = JobRequirement(req_id="R-004", requirement="Builds logistics software.", kind="company_context",
                         **_span("jd", "Forward Deployed Engineer"))
    sheet = _sheet(job_requirements=_reqs() + [ctx])
    assert [r.req_id for r in sheet.matchable_requirements()] == ["R-001", "R-002", "R-003"]
    assert _map(sheet).cross_check(sheet) == []  # R-004 not required in the map
    extra = MatchEntry(req_id="R-004", status="gap", rationale="r", missing="m", question_angle="honest_gap")
    assert "R-004: company_context item should not be mapped" in _map(sheet, entries=_entries() + [extra]).cross_check(sheet)


def test_unknown_extra_strength_reported():
    sheet = _sheet()
    assert any("F-resume-555" in p for p in _map(sheet, extra_strengths=["F-resume-555"]).cross_check(sheet))
