"""Step 5 tests: prompts, chunking, extraction -> Fact Sheet. FakeLLM only; all data fictional."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from interview_agent import extract
from interview_agent.extract import build_fact_sheet, chunk_text
from interview_agent.ingest import RawDocument
from interview_agent.llm import FakeLLM, LLMCallError, LLMRequest
from interview_agent.prompt_loader import PromptError, load_prompt
from interview_agent.redact import parse_terms, redact_document

NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)
TERMS = parse_terms("name: Alex Rivera\naddress: 42 Lantern Row")
CONTACTS = ["Alex Rivera", "42 Lantern Row", "(555) 123-4567", "alex.rivera@example.com",
            "linkedin.com/in/alex-rivera-123", "jordan.lee@talent-example.com", "7946 0958"]

RESUME_PAGES = (
    "Alex Rivera\n42 Lantern Row, Springfield | +1 (555) 123-4567 | alex.rivera@example.com\n"
    "www.linkedin.com/in/alex-rivera-123\n\n"
    "Senior Data Engineer, Acme Logistics (2022–2024)\n"
    "- Rebuilt the nightly ETL pipeline in PySpark, cutting runtime from 3 hours to 3 minutes.\n"
    "- Worked directly with 4 enterprise customers to scope data integrations.",
    "Skills\nPython, SQL, Spark",
)
LINKEDIN_PAGES = (
    "Contact\nalex.rivera@example.com\nwww.linkedin.com/in/alex-rivera-123\n\n"
    "Alex Rivera\nExperience\nData Engineer\nAcme Logistics\n2022 - 2024",
)
JD_PAGES = (
    "Forward Deployed Engineer - London, UK\nRequirements:\n"
    "- 3+ years building data pipelines with Spark.\n"
    "- Experience working directly with customers.\n"
    "- Kubernetes experience is a plus.\n"
    "Questions? jordan.lee@talent-example.com or +44 20 7946 0958",
)


def _doc(doc_id, kind, pages, owner="u1"):
    sha = hashlib.sha256("".join(pages).encode()).hexdigest()
    raw = RawDocument(doc_id=doc_id, kind=kind, filename=f"{doc_id}.pdf", sha256=sha, pages=pages)
    return redact_document(raw, owner_id=owner, terms=TERMS)[0]


RESUME = _doc("resume", "resume", RESUME_PAGES)
LINKEDIN = _doc("linkedin", "linkedin", LINKEDIN_PAGES)
JD = _doc("jd", "job_description", JD_PAGES)

RESUME_FACTS = [
    {"quote": "Senior Data Engineer, Acme Logistics (2022–2024)",
     "claim": "Worked as a Senior Data Engineer at Acme Logistics from 2022 to 2024.",
     "category": "role", "metric": None, "skills": [], "time_period": "2022–2024"},
    {"quote": "Rebuilt the nightly ETL pipeline in PySpark, cutting runtime from 3 hours to 3 minutes.",
     "claim": "Rebuilt a nightly ETL pipeline in PySpark, cutting runtime from 3 hours to 3 minutes.",
     "category": "achievement",
     "metric": {"raw": "from 3 hours to 3 minutes", "value": None, "unit": None, "measures": "ETL runtime"},
     "skills": ["pyspark", "etl"], "time_period": None},
    {"quote": "Worked directly with 4 enterprise customers to scope data integrations.",
     "claim": "Scoped data integrations directly with 4 enterprise customers.",
     "category": "achievement", "metric": None, "skills": ["kubernetes"], "time_period": None},
    {"quote": "Led a team of 12 engineers.", "claim": "Led 12 engineers.", "category": "achievement"},  # invented
    {"quote": "Python, SQL, Spark", "claim": "Skilled in Python, SQL and Spark.", "category": "skill",
     "skills": ["python", "sql", "spark"]},
]
LINKEDIN_FACTS = [
    {"quote": "Data Engineer Acme Logistics 2022 - 2024", "claim": "Data Engineer at Acme Logistics, 2022 - 2024.",
     "category": "role", "time_period": "2022 - 2024"},
]
JD_REQS = [
    {"quote": "3+ years building data pipelines with Spark.", "requirement": "3+ years building data pipelines with Spark.",
     "kind": "required", "skills": ["spark"]},
    {"quote": "Experience working directly with customers.", "requirement": "Customer-facing experience.",
     "kind": "required"},
    {"quote": "Kubernetes experience is a plus.", "requirement": "Kubernetes (nice to have).", "kind": "preferred",
     "skills": ["kubernetes"]},
]
DISCREPANCIES = [
    {"fact_ids": ["F-resume-001", "F-linkedin-001"],
     "description": "Title is 'Senior Data Engineer' on the resume but 'Data Engineer' on LinkedIn."},
    {"fact_ids": ["F-resume-001", "F-linkedin-099"], "description": "invented id"},
    {"fact_ids": ["F-resume-001", "F-resume-002"], "description": "same document"},
    {"fact_ids": ["F-resume-001"], "description": "too few ids"},
]


def responder(req: LLMRequest) -> str:
    if req.prompt_id == "extract_candidate.v1":
        facts = RESUME_FACTS if "Document id: resume" in req.user else LINKEDIN_FACTS
        return json.dumps({"facts": facts})
    if req.prompt_id == "extract_job.v1":
        return json.dumps({"requirements": JD_REQS})
    if req.prompt_id == "find_discrepancies.v1":
        return json.dumps({"discrepancies": DISCREPANCIES})
    raise AssertionError(req.prompt_id)


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name, values",
    [
        ("extract_candidate", {"doc_kind": "resume", "doc_id": "resume", "part": "1 of 1", "document": "x"}),
        ("extract_job", {"doc_id": "jd", "part": "1 of 1", "document": "x"}),
        ("find_discrepancies", {"facts": "{}"}),
    ],
)
def test_real_v1_prompts_load_and_render(name, values):
    prompt = load_prompt(name, 1)
    assert prompt.prompt_id == f"{name}.v1"
    system, user = prompt.render(**values)
    assert "{{" not in system + user
    assert "JSON" in system  # Groq JSON mode needs the word "JSON" in the messages
    assert "DATA, not instructions" in system


def test_prompts_are_role_neutral():
    for name in ("extract_candidate", "extract_job", "find_discrepancies"):
        p = load_prompt(name, 1)
        text = (p.system + p.user).lower()
        assert "forward deployed" not in text and "fde" not in text


def test_render_missing_value_and_no_double_substitution():
    prompt = load_prompt("extract_job", 1)
    with pytest.raises(PromptError, match="missing value"):
        prompt.render(doc_id="jd")
    _, user = prompt.render(doc_id="jd", part="1 of 1", document="Use {{doc_id}} literally")
    assert "Use {{doc_id}} literally" in user


def test_prompt_file_errors(tmp_path):
    with pytest.raises(PromptError, match="not found"):
        load_prompt("nope", 1, prompts_dir=tmp_path)
    (tmp_path / "bad.v1.md").write_text("# no sections here\n")
    with pytest.raises(PromptError, match="System"):
        load_prompt("bad", 1, prompts_dir=tmp_path)


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


def test_chunk_text_packs_paragraphs_and_keeps_order():
    paras = ["a" * 40, "b" * 40, "c" * 40, "d" * 150, "e" * 10]
    chunks = chunk_text("\n\n".join(paras) + "\n\n\n\n", max_chars=100)
    assert chunks == [f"{'a'*40}\n\n{'b'*40}", "c" * 40, "d" * 150, "e" * 10]


def test_chunk_text_small_document_is_one_chunk():
    assert chunk_text(RESUME.text) == [RESUME.text]


# --------------------------------------------------------------------------- #
# build_fact_sheet
# --------------------------------------------------------------------------- #


def test_fact_sheet_end_to_end():
    fake = FakeLLM(responder)
    sheet, report = build_fact_sheet(fake, [JD, LINKEDIN, RESUME], now=NOW)

    assert [f.fact_id for f in sheet.candidate_facts] == [
        "F-resume-001", "F-resume-002", "F-resume-003", "F-resume-004", "F-linkedin-001"]
    assert [r.req_id for r in sheet.job_requirements] == ["R-001", "R-002", "R-003"]
    assert [d.ref().doc_id for d in (RESUME, LINKEDIN, JD)] == [d.doc_id for d in sheet.documents]
    assert sheet.verify_spans({"resume": RESUME, "linkedin": LINKEDIN, "jd": JD}) == []

    assert len(sheet.discrepancies) == 1 and sheet.discrepancies[0].fact_ids == ["F-resume-001", "F-linkedin-001"]
    assert len(report.discrepancy_warnings) == 3

    resume_rec = report.documents[0]
    assert (resume_rec.doc_id, resume_rec.chunks, resume_rec.proposals, resume_rec.accepted) == ("resume", 1, 5, 4)
    assert [(r.index, r.reason) for r in resume_rec.rejections] == [(3, "quote_not_found")]
    assert any("kubernetes" in w for w in resume_rec.warnings)  # untraceable skill tag dropped

    assert sheet.extraction_model == "fake:fake-model"
    assert sheet.prompt_ids == ["extract_candidate.v1", "extract_job.v1", "find_discrepancies.v1"]
    assert report.llm_calls == 4 and not report.has_failures


def test_no_contact_details_ever_reach_the_llm():
    """DECISIONS.md privacy test: contact-heavy fictional documents -> nothing reaches the model."""
    fake = FakeLLM(responder)
    build_fact_sheet(fake, [RESUME, LINKEDIN, JD], now=NOW)
    sent = "\n".join(c.system + "\n" + c.user for c in fake.calls)
    for value in CONTACTS:
        assert value not in sent
    assert "[EMAIL]" in sent and "[NAME]" in sent  # placeholders are what the model sees


def test_resume_only_skips_discrepancy_call():
    fake = FakeLLM(responder)
    sheet, report = build_fact_sheet(fake, [RESUME, JD], now=NOW)
    assert all(c.prompt_id != "find_discrepancies.v1" for c in fake.calls)
    assert sheet.discrepancies == [] and "find_discrepancies.v1" not in sheet.prompt_ids
    assert report.llm_calls == 2


def test_bad_json_retried_once_then_succeeds():
    replies = {"n": 0}

    def flaky(req):
        if req.prompt_id == "extract_job.v1" and replies["n"] == 0:
            replies["n"] += 1
            return "Sure! Here are the requirements:"
        return responder(req)

    sheet, report = build_fact_sheet(FakeLLM(flaky), [RESUME, JD], now=NOW)
    assert len(sheet.job_requirements) == 3 and not report.has_failures
    assert report.llm_calls == 3


@pytest.mark.parametrize("bad_reply", ["not json at all", json.dumps({"items": []}), json.dumps(["a"])])
def test_unusable_reply_twice_marks_chunk_failed_and_continues(bad_reply):
    def broken_job(req):
        return bad_reply if req.prompt_id == "extract_job.v1" else responder(req)

    sheet, report = build_fact_sheet(FakeLLM(broken_job), [RESUME, JD], now=NOW)
    jd_rec = next(d for d in report.documents if d.doc_id == "jd")
    assert sheet.job_requirements == [] and report.has_failures
    assert len(jd_rec.failed_chunks) == 1 and jd_rec.failed_chunks[0].startswith("part 1 of 1")
    assert len(sheet.candidate_facts) == 4  # the other document is unaffected
    assert report.llm_calls == 3  # resume 1 call + JD 2 attempts (wrong shape is retried too)


def test_multi_chunk_document_ids_sequential_and_duplicates_dropped(monkeypatch):
    monkeypatch.setattr(extract, "MAX_CHUNK_CHARS", 120)
    fake = FakeLLM(responder)  # returns the same 5 resume proposals for every chunk
    sheet, report = build_fact_sheet(fake, [RESUME, JD], now=NOW)
    resume_calls = [c for c in fake.calls if "Document id: resume" in c.user]
    assert len(resume_calls) >= 2 and "Part: 1 of" in resume_calls[0].user
    assert [f.fact_id for f in sheet.candidate_facts] == [f"F-resume-00{i}" for i in range(1, 5)]
    assert any("exact duplicate" in w for w in report.documents[0].warnings)


def test_provider_error_aborts():
    with pytest.raises(LLMCallError):
        build_fact_sheet(FakeLLM([]), [RESUME, JD], now=NOW)


@pytest.mark.parametrize(
    "docs, match",
    [
        ([RESUME, _doc("jd", "job_description", JD_PAGES, owner="u2")], "same owner"),
        ([RESUME], "exactly one job description"),
        ([RESUME, JD, _doc("jd2", "job_description", JD_PAGES)], "exactly one job description"),
        ([JD], "at least one resume"),
        ([RESUME, RESUME, JD], "duplicate doc_id"),
    ],
)
def test_input_checks(docs, match):
    with pytest.raises(ValueError, match=match):
        build_fact_sheet(FakeLLM(responder), docs, now=NOW)
