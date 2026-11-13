"""Step 3 tests: LLM proposals -> grounded facts/requirements, or rejections. All data fictional."""

from __future__ import annotations

import hashlib

import pytest

from interview_agent.grounding import (
    MAX_QUOTE_CHARS,
    GroundingError,
    ground_fact,
    ground_fact_proposals,
    ground_requirement,
    ground_requirement_proposals,
    locate_quote,
    numbers_in,
)
from interview_agent.ingest import assemble_pages
from interview_agent.schemas import FactProposal, RequirementProposal, SourceDocument

RESUME_PAGES = (
    "[NAME]\n[EMAIL] | [PHONE]\n"
    "Senior Data Engineer, Acme Logistics (2022–2024)\n"
    "- Rebuilt the nightly ETL pipeline in PySpark, cutting runtime\nfrom 3 hours to 3 minutes.\n"
    "- Worked directly with 4 enterprise customers to scope data integrations.\n"
    "- Reduced cloud spend by 40% ($1.2M per year).",
    "Skills\nPython, SQL, Go, C++, Node.js\nTeam lead for the data platform.\nTeam lead for the data platform.",
)
JD_TEXT = (
    "Forward Deployed Engineer\nRequirements:\n"
    "- 3+ years building data pipelines with Spark.\n"
    "- Experience working directly with customers.\n"
    "- Kubernetes experience is a plus."
)


def _doc(doc_id, kind, pages) -> SourceDocument:
    text, starts = assemble_pages(pages)
    return SourceDocument(
        doc_id=doc_id, owner_id="u1", kind=kind, filename=f"{doc_id}.pdf",
        sha256=hashlib.sha256(text.encode()).hexdigest(), text=text, page_starts=starts,
    )


RESUME = _doc("resume", "resume", RESUME_PAGES)
JD = _doc("jd", "job_description", (JD_TEXT,))


def _fact(**kw) -> FactProposal:
    base = dict(
        quote="Worked directly with 4 enterprise customers to scope data integrations.",
        claim="Scoped data integrations directly with 4 enterprise customers.",
        category="achievement",
    )
    base.update(kw)
    return FactProposal(**base)


def _reason(fn) -> str:
    with pytest.raises(GroundingError) as exc:
        fn()
    return exc.value.reason


# --------------------------------------------------------------------------- #
# locate_quote
# --------------------------------------------------------------------------- #


def test_exact_quote_located_with_offsets_and_page():
    q = "Worked directly with 4 enterprise customers"
    loc = locate_quote(RESUME, q)
    assert RESUME.text[loc.char_start : loc.char_end] == loc.source_text == q
    assert loc.page == 1 and loc.occurrences == 1


def test_whitespace_and_line_break_differences_tolerated():
    # Document has a line break inside; LLM wrote a space and extra spaces.
    loc = locate_quote(RESUME, "cutting runtime   from 3 hours to 3 minutes")
    assert loc.source_text == "cutting runtime\nfrom 3 hours to 3 minutes"  # document's own text kept


def test_quote_on_second_page():
    assert locate_quote(RESUME, "Python, SQL, Go").page == 2


def test_repeated_quote_reports_occurrences():
    assert locate_quote(RESUME, "Team lead for the data platform.").occurrences == 2


@pytest.mark.parametrize(
    "quote, reason",
    [
        ("Worked directly with 6 enterprise customers", "quote_not_found"),  # altered number
        ("worked directly with 4 enterprise customers", "quote_not_found"),  # case differs
        ("Led a team of 12 engineers", "quote_not_found"),  # invented
        ("PySpark cutting runtime", "quote_not_found"),  # words dropped (comma missing)
        ("ab", "quote_too_short"),
        ("   ", "quote_too_short"),
        ("[EMAIL] | [PHONE]", "placeholder_only"),
        ("x" * (MAX_QUOTE_CHARS + 1), "quote_too_long"),
    ],
)
def test_bad_quotes_rejected(quote, reason):
    assert _reason(lambda: locate_quote(RESUME, quote)) == reason


# --------------------------------------------------------------------------- #
# numbers_in
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text, expected",
    [
        ("40% of 1,000 users", {40.0, 1000.0}),
        ("2022–2024", {2022.0, 2024.0}),
        ("$1.2M", {1.2}),
        ("3+ years", {3.0}),
        ("Spark 3.5.1", {3.0, 5.0, 1.0}),
        ("no numbers here", set()),
    ],
)
def test_numbers_in(text, expected):
    assert numbers_in(text) == expected


# --------------------------------------------------------------------------- #
# ground_fact
# --------------------------------------------------------------------------- #


def test_good_fact_grounded():
    fact, warnings = ground_fact(RESUME, _fact(skills=["customers", "data integration"]), "F-resume-001")
    assert fact.source_doc == "resume" and fact.page == 1
    assert RESUME.text[fact.char_start : fact.char_end] == fact.source_text
    assert fact.skills == ["customers", "data integration"]
    assert warnings == []


def test_claim_with_invented_number_rejected():
    p = _fact(claim="Scoped data integrations with 6 enterprise customers.")
    assert _reason(lambda: ground_fact(RESUME, p, "F-resume-001")) == "number_not_in_quote"


def test_claim_cannot_derive_new_numbers():
    p = _fact(
        quote="Senior Data Engineer, Acme Logistics (2022–2024)",
        claim="Has 2 years of experience as a Senior Data Engineer.",
        category="role",
    )
    assert _reason(lambda: ground_fact(RESUME, p, "F-resume-001")) == "number_not_in_quote"


def test_time_period_numbers_checked():
    p = _fact(
        quote="Senior Data Engineer, Acme Logistics (2022–2024)",
        claim="Senior Data Engineer at Acme Logistics.",
        category="role",
        time_period="2021–2024",
    )
    assert _reason(lambda: ground_fact(RESUME, p, "F-resume-001")) == "number_not_in_quote"


def test_metric_grounded_to_exact_quote_text():
    p = _fact(
        quote="Rebuilt the nightly ETL pipeline in PySpark, cutting runtime from 3 hours to 3 minutes.",
        claim="Cut nightly ETL runtime from 3 hours to 3 minutes by rebuilding it in PySpark.",
        metric={"raw": "from 3 hours to 3 minutes", "measures": "nightly ETL runtime"},
    )
    fact, _ = ground_fact(RESUME, p, "F-resume-002")
    assert fact.metric.raw == "from 3 hours to 3 minutes"
    assert fact.source_text.endswith("runtime\nfrom 3 hours to 3 minutes.")


def test_inflated_metric_rejected():
    p = _fact(metric={"raw": "6 enterprise customers", "measures": "customers"})
    assert _reason(lambda: ground_fact(RESUME, p, "F-resume-001")) == "metric_not_in_quote"


@pytest.mark.parametrize("value, ok", [(40, True), (0.4, True), (1.2e6, True), (1.2, True), (50, False), (2e6, False)])
def test_metric_value_must_come_from_raw(value, ok):
    p = _fact(
        quote="Reduced cloud spend by 40% ($1.2M per year).",
        claim="Reduced cloud spend by 40% ($1.2M per year).",
        metric={"raw": "40% ($1.2M per year)", "value": value, "measures": "cloud spend reduction"},
    )
    if ok:
        assert ground_fact(RESUME, p, "F-resume-003")[0].metric.value == value
    else:
        assert _reason(lambda: ground_fact(RESUME, p, "F-resume-003")) == "metric_value_mismatch"


def test_unsupported_skill_tags_dropped_with_warning():
    p = _fact(
        quote="Python, SQL, Go, C++, Node.js",
        claim="Skilled in Python, SQL, Go, C++ and Node.js.",
        category="skill",
        skills=["python", "sql", "go", "c++", "nodejs", "kubernetes", "r"],
    )
    fact, warnings = ground_fact(RESUME, p, "F-resume-004")
    assert fact.skills == ["python", "sql", "go", "c++", "nodejs"]
    assert warnings == ["F-resume-004: dropped skill tag(s) not in quote: kubernetes, r"]


def test_substring_skill_match():
    p = _fact(
        quote="Rebuilt the nightly ETL pipeline in PySpark",
        claim="Rebuilt a nightly ETL pipeline in PySpark.",
        skills=["spark", "etl", "pipeline"],
    )
    assert ground_fact(RESUME, p, "F-resume-005")[0].skills == ["spark", "etl", "pipeline"]


def test_fact_from_jd_is_programming_error():
    with pytest.raises(ValueError):
        ground_fact(JD, _fact(quote="Kubernetes experience is a plus."), "F-jd-001")


# --------------------------------------------------------------------------- #
# ground_requirement
# --------------------------------------------------------------------------- #


def test_requirement_grounded():
    p = RequirementProposal(
        quote="3+ years building data pipelines with Spark.",
        requirement="3+ years building data pipelines with Spark.",
        kind="required",
        skills=["spark", "data pipelines"],
    )
    req, warnings = ground_requirement(JD, p, "R-001")
    assert req.source_doc == "jd" and JD.text[req.char_start : req.char_end] == req.source_text
    assert warnings == []


def test_requirement_number_inflation_rejected():
    p = RequirementProposal(
        quote="3+ years building data pipelines with Spark.",
        requirement="5+ years building data pipelines with Spark.",
        kind="required",
    )
    assert _reason(lambda: ground_requirement(JD, p, "R-001")) == "number_not_in_quote"


def test_requirement_from_resume_is_programming_error():
    p = RequirementProposal(quote="Python, SQL", requirement="Python", kind="required")
    with pytest.raises(ValueError):
        ground_requirement(RESUME, p, "R-001")


# --------------------------------------------------------------------------- #
# Batches
# --------------------------------------------------------------------------- #


def test_fact_batch_mixed_good_bad_invalid_and_duplicate():
    proposals = [
        _fact(),  # 0 ok
        {"quote": "Led a team of 12 engineers", "claim": "Led 12 engineers.", "category": "achievement"},  # 1 invented
        {"quote": "Python, SQL", "claim": "Knows Python.", "category": "superpower"},  # 2 bad category
        {"quote": "Python, SQL", "claim": "Knows Python.", "category": "skill", "score": 5},  # 3 extra field
        _fact(),  # 4 duplicate of 0
        {"quote": "Team lead for the data platform.", "claim": "Team lead for the data platform.", "category": "role"},  # 5 ok
    ]
    report = ground_fact_proposals(RESUME, proposals)
    assert [f.fact_id for f in report.accepted] == ["F-resume-001", "F-resume-002"]  # ids only for accepted
    assert [(r.index, r.reason) for r in report.rejections] == [
        (1, "quote_not_found"),
        (2, "invalid_proposal"),
        (3, "invalid_proposal"),
    ]
    assert report.rejections[0].quote == "Led a team of 12 engineers"
    assert "category" in report.rejections[1].detail and "score" in report.rejections[2].detail
    assert any("proposal 4: exact duplicate" in w for w in report.warnings)
    assert any("occurs 2 times" in w for w in report.warnings)


def test_fact_batch_first_number_and_linkedin_doc():
    li = _doc("linkedin", "linkedin", ("Experience\nData Engineer\nAcme Logistics\n2022 - 2024",))
    report = ground_fact_proposals(
        li,
        [{"quote": "Data Engineer Acme Logistics 2022 - 2024", "claim": "Data Engineer at Acme Logistics, 2022-2024.",
          "category": "role", "time_period": "2022 - 2024"}],
        first_number=7,
    )
    assert report.accepted[0].fact_id == "F-linkedin-007"
    assert report.accepted[0].source_text == "Data Engineer\nAcme Logistics\n2022 - 2024"


def test_requirement_batch():
    report = ground_requirement_proposals(
        JD,
        [
            {"quote": "Experience working directly with customers.", "requirement": "Customer-facing experience.",
             "kind": "required"},
            {"quote": "Kubernetes experience is a plus.", "requirement": "Kubernetes.", "kind": "preferred",
             "skills": ["kubernetes", "terraform"]},
            {"quote": "Must hold an active security clearance.", "requirement": "Security clearance.",
             "kind": "required"},
        ],
    )
    assert [r.req_id for r in report.accepted] == ["R-001", "R-002"]
    assert report.accepted[1].skills == ["kubernetes"]
    assert [(r.index, r.reason) for r in report.rejections] == [(2, "quote_not_found")]


def test_grounded_items_pass_fact_sheet_span_verification():
    """Everything grounding accepts must pass the independent span check from step 1."""
    from datetime import datetime, timezone

    from interview_agent.schemas import FactSheet

    facts = ground_fact_proposals(RESUME, [_fact(), {"quote": "cutting runtime from 3 hours to 3 minutes",
                                                     "claim": "Cut runtime from 3 hours to 3 minutes.",
                                                     "category": "achievement"}]).accepted
    reqs = ground_requirement_proposals(JD, [{"quote": "Kubernetes experience is a plus.",
                                              "requirement": "Kubernetes.", "kind": "preferred"}]).accepted
    sheet = FactSheet(
        owner_id="u1", created_at=datetime(2026, 11, 13, tzinfo=timezone.utc),
        documents=[RESUME.ref(), JD.ref()], candidate_facts=facts, job_requirements=reqs,
        extraction_model="fake",
    )
    assert sheet.verify_spans({"resume": RESUME, "jd": JD}) == []
