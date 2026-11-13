# Handoff

*Written 2026-11-13 at the end of the Milestone 1 build session. Project: `~/Projects/FDE_Interview_Agent` (start Claude Code from here). Repo: https://github.com/Capella35/mock_interview_coach, branch `main`.*

## Where things stand
All Milestone 1 code is built and tested: ingest → redact → grounding → guarded LLM client → extraction → Match/Gap Map → `out/review.md`, driven by the `interview-agent` CLI (`build [--dry-run]`, `verify`, `wipe`). 296 tests pass, all on fictional data with a fake LLM; no real document or real model has been used yet. The code is pushed to the public GitHub repo with private-data safeguards, and the project now lives outside iCloud.

## Last thing completed, and how it was verified
Moved the project from `~/Desktop` (iCloud-synced) to `~/Projects/FDE_Interview_Agent` and rebuilt `.venv`. Verified by:
- `uv run pytest` → **296 passed in 1.22s**
- `uv run interview-agent --help` works without any workaround
- a re-check 90 seconds after the move found 0 hidden files in `.venv`
- `scripts/check_private_data.py --all` → OK, 45 files

## Next step
**Real-document dry run.** Once the owner has put their resume, LinkedIn PDF, one FDE job description and `redact_terms.txt` in `data/private/`, run:

```bash
uv run interview-agent build --resume data/private/<resume> --linkedin data/private/<linkedin> --jd data/private/<jd> --dry-run
```

Then review `out/redacted/` with the owner: is every contact detail masked, and is all content kept? No LLM call is made.

## Blockers or questions waiting on me
1. **Create `data/private/redact_terms.txt`** with `name: …` lines (your name, including variants such as "Surname, Firstname") and `address: …` (your street line). Without it, your name isn't masked and the pre-commit check can't catch it.
2. **Put the documents in `data/private/`:** your resume, your LinkedIn PDF export, and the JDs.
3. **Pick the job descriptions:** Databricks FDE or Google FDE, plus one non-FDE tech JD for the role-neutrality check.
4. **Check Groq's data policy** (retention and training use on the free tier). This blocks the real LLM run, but not the dry run.
5. **Choose the Groq model,** then copy `.env.example` to `.env` and fill in `GROQ_API_KEY` and `LLM_MODEL`. Needed for the real run.
6. *Not blocking Milestone 1:* scoring scale, voice stack, web framework, database, role-pack format, second role pack (all in `DECISIONS.md` → Open).

## Files changed today that are not yet committed
None. The end-of-session changes were committed and pushed to `main` together, as the last commit of the session ("Session handoff…"):
- `CLAUDE.md`: iCloud workaround replaced by a "Location" note; install step includes `git config core.hooksPath .githooks`
- `DECISIONS.md`: iCloud open item → decision; new "Branches" and "Session records" entries
- `tests/test_environment.py`: fix hint points to CLAUDE.md "Location"
- `BUILD_LOG.md` (new): session log
- `HANDOFF.md` (new): this file
