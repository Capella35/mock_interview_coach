"""Tests for scripts/check_private_data.py (the pre-commit guard). All values fictional.

Values the guard must block are assembled from pieces ("a" + "b") so this file
itself passes the whole-repository check."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from interview_agent.redact import parse_terms

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_private_data", ROOT / "scripts" / "check_private_data.py")
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

TERMS = parse_terms("name: Jamie Q Fictional\naddress: 12 Harbor Road")


@pytest.mark.parametrize(
    "path, blocked",
    [
        ("data/private/resume.pdf", True),
        ("data/private/notes.txt", True),
        ("out/review.md", True),
        ("evals/private/case.yaml", True),
        ("docs/my_resume.pdf", True),
        ("CV.DOCX", True),
        ("sample_data/jd_public.pdf", False),
        ("src/interview_agent/cli.py", False),
        ("evals/README.md", False),
        # 1. secrets
        (".env", True),
        (".env.local", True),
        ("config/.env.production", True),
        (".env.example", False),
        ("keys/server.pem", True),
        ("gcp-service-account.json", True),
        ("my-sa-key.json", True),
        ("token.json", True),
        ("client_secret_123.json", True),
        ("cert.p12", True),
        # 2. candidate data
        ("transcripts/session1.txt", True),
        ("src/transcripts/x.json", True),
        ("recordings/answer.txt", True),
        ("voice/answer.opus", True),
        ("answer.wav", True),
        ("fact_sheet.json", True),
        ("exports/match_gap_map_2026.json", True),
        ("sessions/s1.json", True),
        ("app.db", True),
        ("local.sqlite3", True),
        ("src/interview_agent/schemas/models.py", False),
        ("src/interview_agent/sessions/__init__.py", False),  # a future code package is fine
        # 3. junk
        ("app.log", True),
        ("models/llm.gguf", True),
    ],
)
def test_paths(path, blocked):
    assert bool(check.check_path(path)) is blocked


@pytest.mark.parametrize(
    "path, text, expect",
    [
        ("src/a.py", 'KEY = "gsk_' + "A1b2C3d4E5" * 5 + '"', "secret key"),
        ("README.md", "-----BEGIN RSA PRIVATE " + "KEY-----", "secret key"),
        ("docs/x.md", "see /Us" + "ers/jamie/Desktop/project", "home-folder path"),
        ("docs/x.md", "contact jamie@" + "gmail.com", "non-fictional domain"),
        ("tests/test_x.py", 'EMAIL = "jamie@' + 'realcompany.io"', "non-fictional domain"),
        ("DECISIONS.md", "call +44 113 496 0000", "phone number"),
        ("src/a.py", "# Jamie Q Fictional wrote this", "redact_terms"),
        ("tests/test_x.py", "lives at 12 Harbor Road", "redact_terms"),  # terms checked in tests too
    ],
)
def test_text_blocked(path, text, expect):
    findings = check.check_text(path, text, TERMS)
    assert findings and expect in findings[0]


@pytest.mark.parametrize(
    "path, text",
    [
        ("tests/test_x.py", 'EMAIL = "alex.rivera@example.com"; OTHER = "a@x.example"'),
        ("tests/test_x.py", 'PHONE = "+44 20 7946 0958"'),  # phone-shaped fixtures allowed in tests/
        ("tests/test_llm.py", 'groq_api_key="gsk_supersecret"'),  # obviously fake, too short to be real
        ("src/a.py", "cut runtime from 3 hours to 3 minutes (2022-2024), 40% cheaper"),
        ("docs/x.md", "Use /Users/<you>/Projects or ~/Projects"),
    ],
)
def test_text_allowed(path, text):
    assert check.check_text(path, text, TERMS) == []


def test_findings_mask_the_value():
    email = "jamie.private@" + "gmail.com"  # assembled so this file passes the check itself
    finding = check.check_text("docs/x.md", f"contact {email}", [])[0]
    assert email not in finding and "ja***" in finding


def test_whole_repository_is_clean():
    """Regression guard: the files git would commit right now pass the check."""
    terms = check.load_terms(check.TERMS_FILE)
    assert check.scan(check.all_files(), terms) == []
