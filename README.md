# Interview Agent

Generic interview prep fails because real interviews test *your* specific past projects, *your* real metrics, and honesty about skill gaps. Interview Agent ingests a candidate's **resume**, **LinkedIn PDF export**, and target **job description**, builds a sourced **Fact Sheet** and **Match / Gap Map**, and (in later milestones) runs realistic spoken interviews across three session groups — Recruiter Screen, Technical, and Hiring Manager — with distinct interviewer personas and a separate scorer the candidate never talks to. The core is role-neutral; role-specific rounds, personas and competencies come from **role packs**, starting with Forward Deployed Engineer (FDE) and targeting tech and tech-adjacent roles.

**Current milestone:** Milestone 1 — document ingestion + Match/Gap Map + Fact Sheet.

Design: [`docs/DESIGN.md`](docs/DESIGN.md) · Decisions: [`DECISIONS.md`](DECISIONS.md) · Working rules: [`CLAUDE.md`](CLAUDE.md)

## Setup

Requires [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
git config core.hooksPath .githooks   # enable the private-data pre-commit check (once per clone)
```

The pre-commit check (`scripts/check_private_data.py`) blocks commits that contain files from private folders, document files outside `sample_data/`, API keys, non-fictional emails, phone numbers, local home paths, or anything listed in your `data/private/redact_terms.txt`. Run it on everything with `uv run python scripts/check_private_data.py --all`.

## Usage

Put your documents in `data/private/` (gitignored) and list strings to mask (your name, street) in `data/private/redact_terms.txt`, one per line as `name: …` / `address: …`.

```bash
# 1. See exactly what would be sent to the LLM (no LLM call)
uv run interview-agent build --resume data/private/resume.pdf --linkedin data/private/linkedin.pdf \
  --jd data/private/jd.pdf --dry-run

# 2. Real run (needs GROQ_API_KEY and LLM_MODEL in .env — copy .env.example)
uv run interview-agent build --resume data/private/resume.pdf --linkedin data/private/linkedin.pdf \
  --jd data/private/jd.pdf

# 3. Read out/review.md; re-check saved outputs any time
uv run interview-agent verify

# Delete generated outputs (add --all to also delete everything in data/private/)
uv run interview-agent wipe
```

Exit codes: `0` ok · `1` error · `3` finished but incomplete (problems listed at the top of `out/review.md`).

## Tests

```bash
uv run pytest
```
