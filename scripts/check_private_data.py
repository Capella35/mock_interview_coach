#!/usr/bin/env python3
"""Block commits that would put private data into git.

    python scripts/check_private_data.py --staged   # what the pre-commit hook runs
    python scripts/check_private_data.py --all      # every tracked + untracked, not-ignored file

Blocks:
  - files in private folders (data/, out/, evals/private/), even if force-added
  - document files (.pdf .docx .doc .rtf .odt .pages) outside sample_data/
  - API keys / private keys
  - email addresses, unless the domain is a reserved fictional one ("example")
  - phone numbers (the project's own detector; skipped in tests/, whose fixtures
    are fictional by design)
  - local home-folder paths (/Users/<name>/, /home/<name>/)
  - any value from your private data/private/redact_terms.txt (your real name,
    street, ...) — anywhere, including tests

Findings are printed with the value partly masked. Exit 1 = blocked.
To bypass for a single, deliberate commit: git commit --no-verify (don't).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # independent of the editable install (see CLAUDE.md §4)

from interview_agent.redact import Term, _email_hits, _phone_hits, _term_hits, load_terms  # noqa: E402

PRIVATE_DIRS = ("data/", "out/", "evals/private/")
DOC_EXTENSIONS = (".pdf", ".docx", ".doc", ".rtf", ".odt", ".pages")
DOCS_ALLOWED_UNDER = "sample_data/"
TERMS_FILE = ROOT / "data" / "private" / "redact_terms.txt"

_SECRET_RE = re.compile(
    r"gsk_[A-Za-z0-9]{40,}"  # Groq
    r"|sk-ant-[A-Za-z0-9_-]{20,}"  # Anthropic
    r"|sk-[A-Za-z0-9]{32,}"  # OpenAI-style
    r"|gh[pousr]_[A-Za-z0-9]{30,}"  # GitHub
    r"|AKIA[0-9A-Z]{16}"  # AWS
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
_HOME_PATH_RE = re.compile(r"(?:/Users|/home)/(?!runner/|USER|<)[A-Za-z0-9._-]+/")
_FICTIONAL_EMAIL_DOMAIN_RE = re.compile(r"(^|[.@-])example([.-]|$)", re.IGNORECASE)


def _mask(value: str) -> str:
    value = " ".join(value.split())
    return value[:2] + "***" if len(value) > 4 else "***"


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def check_path(path: str) -> list[str]:
    p = path.replace("\\", "/")
    out = []
    if p.startswith(PRIVATE_DIRS):
        out.append(f"{path}: file is in a private folder ({p.split('/')[0]}/...) and must never be committed")
    if p.lower().endswith(DOC_EXTENSIONS) and not p.startswith(DOCS_ALLOWED_UNDER):
        out.append(f"{path}: document file outside {DOCS_ALLOWED_UNDER} (could be a real resume)")
    return out


def check_text(path: str, text: str, terms: list[Term]) -> list[str]:
    out = []
    for m in _SECRET_RE.finditer(text):
        out.append(f"{path}:{_line(text, m.start())}: looks like a secret key ({_mask(m.group())})")
    for m in _HOME_PATH_RE.finditer(text):
        out.append(f"{path}:{_line(text, m.start())}: local home-folder path ({_mask(m.group())})")
    for h in _email_hits(text):
        email = text[h.start:h.end]
        if not _FICTIONAL_EMAIL_DOMAIN_RE.search(email.split("@", 1)[1]):
            out.append(f"{path}:{_line(text, h.start)}: email with a non-fictional domain ({_mask(email)}); "
                       f"use an 'example' domain in fixtures")
    if not path.replace("\\", "/").startswith("tests/"):
        for h in _phone_hits(text):
            out.append(f"{path}:{_line(text, h.start)}: phone number ({_mask(text[h.start:h.end])})")
    for h in _term_hits(text, terms):
        out.append(f"{path}:{_line(text, h.start)}: matches your private redact_terms.txt "
                   f"[{h.kind}] ({_mask(text[h.start:h.end])})")
    return out


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def staged_files() -> list[tuple[str, str | None]]:
    """(path, staged text or None for binary/unreadable)."""
    names = [n for n in _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0") if n]
    out = []
    for n in names:
        blob = subprocess.run(["git", "show", f":{n}"], cwd=ROOT, capture_output=True).stdout
        try:
            out.append((n, blob.decode("utf-8")))
        except UnicodeDecodeError:
            out.append((n, None))
    return out


def all_files() -> list[tuple[str, str | None]]:
    names = [n for n in _git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0") if n]
    out = []
    for n in names:
        try:
            out.append((n, (ROOT / n).read_text(encoding="utf-8")))
        except (UnicodeDecodeError, FileNotFoundError):
            out.append((n, None))
    return out


def scan(files: list[tuple[str, str | None]], terms: list[Term]) -> list[str]:
    findings = []
    for path, text in files:
        findings += check_path(path)
        if text is not None:
            findings += check_text(path, text, terms)
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true", help="scan staged changes (default)")
    group.add_argument("--all", action="store_true", help="scan all tracked and untracked, not-ignored files")
    args = ap.parse_args(argv)

    terms = load_terms(TERMS_FILE)
    files = all_files() if args.all else staged_files()
    findings = scan(files, terms)
    if findings:
        print(f"✋ private-data check: {len(findings)} problem(s) — nothing was committed.", file=sys.stderr)
        for f in findings:
            print(f"  - {f}", file=sys.stderr)
        return 1
    note = f", {len(terms)} private term(s)" if terms else " (no redact_terms.txt — name/street not checked)"
    print(f"private-data check: OK ({len(files)} file(s){note})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
