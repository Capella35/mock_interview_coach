"""Step 7 tests: CLI end to end (build / dry-run / verify / wipe) and the review report.
FakeLLM only; all people and data fictional; runs in a temporary folder."""

from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest

from interview_agent.cli import EXIT_ERROR, EXIT_INCOMPLETE, EXIT_OK, main
from interview_agent.llm import FakeLLM, LLMRequest
from interview_agent.schemas import FactSheet, MatchGapMap

RESUME = (
    "Alex Rivera\n42 Lantern Row, Springfield | +1 (555) 123-4567 | alex.rivera@example.com\n"
    "www.linkedin.com/in/alex-rivera-123\n\n"
    "Senior Data Engineer, Acme Logistics (2022–2024)\n"
    "- Rebuilt the nightly ETL pipeline in PySpark, cutting runtime from 3 hours to 3 minutes.\n"
    "- Worked directly with 4 enterprise customers to scope data integrations.\n"
)
LINKEDIN = "Contact\nalex.rivera@example.com\n\nAlex Rivera\nExperience\nData Engineer\nAcme Logistics\n2022 - 2024\n"
JD = (
    "Acme Cloud builds data infrastructure for logistics companies.\nRequirements:\n"
    "- 3+ years building data pipelines with Spark.\n"
    "- Experience working directly with customers.\n"
    "- Kubernetes experience is a plus.\n"
    "Questions? jordan.lee@talent-example.com\n"
)
CONTACTS = ["Alex Rivera", "42 Lantern Row", "(555) 123-4567", "alex.rivera@example.com",
            "linkedin.com/in/alex-rivera-123", "jordan.lee@talent-example.com"]

CANDIDATE = {
    "resume": [
        {"quote": "Senior Data Engineer, Acme Logistics (2022–2024)", "claim": "Senior Data Engineer at Acme Logistics.",
         "category": "role", "time_period": "2022–2024"},
        {"quote": "Rebuilt the nightly ETL pipeline in PySpark, cutting runtime from 3 hours to 3 minutes.",
         "claim": "Rebuilt a PySpark ETL pipeline, cutting runtime from 3 hours to 3 minutes.", "category": "achievement",
         "skills": ["pyspark"], "metric": {"raw": "from 3 hours to 3 minutes", "measures": "ETL runtime"}},
        {"quote": "Worked directly with 4 enterprise customers to scope data integrations.",
         "claim": "Scoped integrations with 4 enterprise customers.", "category": "achievement"},
        {"quote": "Led a team of 12 engineers.", "claim": "Led 12 engineers.", "category": "achievement"},
    ],
    "linkedin": [
        {"quote": "Data Engineer Acme Logistics 2022 - 2024", "claim": "Data Engineer at Acme Logistics.",
         "category": "role", "time_period": "2022 - 2024"},
    ],
}
REQS = [
    {"quote": "Acme Cloud builds data infrastructure for logistics companies.",
     "requirement": "Builds data infrastructure for logistics.", "kind": "company_context"},
    {"quote": "3+ years building data pipelines with Spark.", "requirement": "3+ years of Spark pipelines.",
     "kind": "required"},
    {"quote": "Experience working directly with customers.", "requirement": "Customer-facing experience.",
     "kind": "required"},
    {"quote": "Kubernetes experience is a plus.", "requirement": "Kubernetes (nice to have).", "kind": "preferred"},
]
ENTRIES = {
    "R-002": {"req_id": "R-002", "status": "partial_match", "evidence_fact_ids": ["F-resume-002"],
              "rationale": "Built a PySpark pipeline.", "missing": "No stated 3+ years.", "question_angle": "clarify"},
    "R-003": {"req_id": "R-003", "status": "strong_match", "evidence_fact_ids": ["F-resume-003"],
              "rationale": "Worked directly with 4 enterprise customers.", "question_angle": "deep_dive"},
    "R-004": {"req_id": "R-004", "status": "gap", "evidence_fact_ids": [], "rationale": "No Kubernetes fact.",
              "missing": "Kubernetes.", "question_angle": "honest_gap"},
}


def responder(req: LLMRequest, skip: str | None = None) -> str:
    if req.prompt_id == "extract_candidate.v1":
        return json.dumps({"facts": CANDIDATE["resume" if "Document id: resume" in req.user else "linkedin"]})
    if req.prompt_id == "extract_job.v1":
        return json.dumps({"requirements": REQS})
    if req.prompt_id == "find_discrepancies.v1":
        return json.dumps({"discrepancies": [{"fact_ids": ["F-resume-001", "F-linkedin-001"],
                                              "description": "Title differs: Senior Data Engineer vs Data Engineer."}]})
    asked = re.findall(r'"req_id": "(R-\d+)"', req.user)
    return json.dumps({"entries": [ENTRIES[r] for r in asked if r != skip], "extra_strengths": []})


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "private").mkdir(parents=True)
    (tmp_path / "data/private/resume.txt").write_text(RESUME)
    (tmp_path / "data/private/linkedin.txt").write_text(LINKEDIN)
    (tmp_path / "data/private/jd.txt").write_text(JD)
    (tmp_path / "data/private/redact_terms.txt").write_text("name: Alex Rivera\naddress: 42 Lantern Row\n")
    return tmp_path


BUILD = ["build", "--resume", "data/private/resume.txt", "--linkedin", "data/private/linkedin.txt",
         "--jd", "data/private/jd.txt"]


def _all_text(folder) -> str:
    return "\n".join(p.read_text() for p in folder.rglob("*") if p.is_file())


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #


def test_build_end_to_end(workdir, capsys):
    fake = FakeLLM(responder)
    assert main(BUILD, client=fake) == EXIT_OK
    out = workdir / "out"
    for name in ("fact_sheet.json", "match_gap_map.json", "review.md", "run_report.json",
                 "documents/resume.json", "documents/linkedin.json", "documents/jd.json"):
        assert (out / name).is_file(), name

    sheet = FactSheet.model_validate_json((out / "fact_sheet.json").read_text())
    gmap = MatchGapMap.model_validate_json((out / "match_gap_map.json").read_text())
    assert gmap.cross_check(sheet) == [] and len(sheet.discrepancies) == 1
    run_report = json.loads((out / "run_report.json").read_text())
    assert run_report["extraction"]["documents"][0]["rejections"][0]["reason"] == "quote_not_found"

    review = (out / "review.md").read_text()
    assert "No problems" in review
    assert "### R-002 · required · 🟡 partial match" in review and "❌ gap" in review and "✅ strong match" in review
    assert "> Rebuilt the nightly ETL pipeline in PySpark" in review  # evidence quote shown
    assert "## Company context (not matched)" in review and "## Discrepancies" in review
    assert "**quote_not_found**" in review and "Led a team of 12 engineers." in review
    assert "masked" in review  # redaction counts

    printed = capsys.readouterr().out
    assert "strong 1, partial 1, gap 1" in printed and "Review: out/review.md" in printed


def test_privacy_contact_details_only_in_private_redaction_maps(workdir):
    fake = FakeLLM(responder)
    assert main(BUILD, client=fake) == EXIT_OK
    sent = "\n".join(c.system + c.user for c in fake.calls)
    outputs = _all_text(workdir / "out")
    for value in CONTACTS:
        assert value not in sent, f"sent to LLM: {value}"
        assert value not in outputs, f"written to out/: {value}"
    private_maps = _all_text(workdir / "data/private/redaction")
    assert "alex.rivera@example.com" in private_maps  # kept privately for the owner only


def test_dry_run_sends_nothing(workdir, capsys):
    fake = FakeLLM(responder)
    assert main([*BUILD, "--dry-run"], client=fake) == EXIT_OK
    assert fake.calls == []
    redacted = (workdir / "out/redacted/resume.txt").read_text()
    assert "[EMAIL]" in redacted and "[NAME]" in redacted and "alex.rivera" not in redacted
    assert not (workdir / "out/fact_sheet.json").exists()
    assert "nothing was sent" in capsys.readouterr().out


def test_incomplete_run_exit_code_and_review(workdir, capsys):
    fake = FakeLLM(lambda r: responder(r, skip="R-004"))
    assert main(BUILD, client=fake) == EXIT_INCOMPLETE
    review = (workdir / "out/review.md").read_text()
    assert "## ⚠️ Problems" in review and "R-004: not mapped" in review
    assert "### R-004 · preferred · ⚠️ not mapped" in review
    assert "Incomplete" in capsys.readouterr().out


def test_missing_name_terms_warned(workdir, capsys):
    (workdir / "data/private/redact_terms.txt").unlink()
    assert main([*BUILD, "--dry-run"], client=FakeLLM(responder)) == EXIT_OK
    assert "name will not be masked" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv_extra, environ, message",
    [
        (["--resume", "data/private/nope.pdf"], {}, "file not found"),
        ([], {}, "LLM_MODEL"),
        ([], {"LLM_MODEL": "some-model"}, "GROQ_API_KEY"),
    ],
)
def test_errors_are_clear_and_exit_1(workdir, capsys, argv_extra, environ, message):
    argv = [*BUILD, "--env-file", "missing.env", *argv_extra]
    assert main(argv, environ=environ) == EXIT_ERROR
    assert message in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #


def test_verify_ok_and_detects_tampering(workdir, capsys):
    assert main(BUILD, client=FakeLLM(responder)) == EXIT_OK
    assert main(["verify"]) == EXIT_OK
    assert "OK:" in capsys.readouterr().out

    doc_path = workdir / "out/documents/resume.json"
    doc = json.loads(doc_path.read_text())
    doc["text"] = doc["text"].replace("3 minutes", "9 minutes")
    doc_path.write_text(json.dumps(doc))
    assert main(["verify"]) == EXIT_INCOMPLETE
    assert "does not match" in capsys.readouterr().out


def test_verify_without_outputs(workdir, capsys):
    assert main(["verify"]) == EXIT_ERROR
    assert "run build first" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# wipe
# --------------------------------------------------------------------------- #


def test_wipe_outputs_keeps_uploaded_documents(workdir):
    assert main(BUILD, client=FakeLLM(responder)) == EXIT_OK
    assert main(["wipe", "--yes"]) == EXIT_OK
    assert not (workdir / "out").exists() and not (workdir / "data/private/redaction").exists()
    assert (workdir / "data/private/resume.txt").exists()


def test_wipe_all_deletes_private_folder(workdir):
    assert main(BUILD, client=FakeLLM(responder)) == EXIT_OK
    assert main(["wipe", "--all", "--yes"]) == EXIT_OK
    assert not (workdir / "data/private").exists() and not (workdir / "out").exists()


def test_wipe_asks_and_can_be_cancelled(workdir, monkeypatch, capsys):
    assert main(BUILD, client=FakeLLM(responder)) == EXIT_OK
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert main(["wipe"]) == EXIT_OK
    assert "Cancelled" in capsys.readouterr().out and (workdir / "out").exists()


def test_wipe_refuses_outside_project(workdir, tmp_path_factory, capsys):
    outside = tmp_path_factory.mktemp("elsewhere")
    assert main(["wipe", "--out", str(outside), "--yes"]) == EXIT_ERROR
    assert main(["wipe", "--out", ".", "--yes"]) == EXIT_ERROR
    assert "refusing to delete" in capsys.readouterr().err and outside.exists()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def test_module_entry_point_runs():
    result = subprocess.run([sys.executable, "-m", "interview_agent", "--help"], capture_output=True, text=True)
    assert result.returncode == 0 and "build" in result.stdout and "wipe" in result.stdout
