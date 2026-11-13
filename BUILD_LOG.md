# Build Log

Append-only. One entry per working session: what was built or changed, which tests ran (exact results), and what was left unfinished.

---

## 2026-11-12 → 2026-11-13 — Milestone 1 build (one session, spanning midnight)

### Built or changed
**Planning and setup (2026-11-12)**
- Milestone 1 plan approved: ingestion → redaction → grounding → LLM client → extraction → matching → report + CLI.
- Stack decided: Python 3.12, Pydantic v2, `uv`, `pytest`, `pypdf`; Groq (free tier) as LLM provider behind `LLMClient`, Anthropic as optional second provider.
- Privacy decision: real documents go to the cloud API only after local redaction, only in validation runs; send guard; metadata-only logs.
- Scope changed to a **role-neutral core + role packs** (FDE first; tech and tech-adjacent roles). Project renamed **Interview Agent**, package `interview_agent`.
- "Project brain": `DECISIONS.md`, `.env.example`, `.gitignore`, README skeleton, `prompts/`, `evals/`, `sample_data/` READMEs; original design moved to `docs/DESIGN.md`; CLAUDE.md updated.
- `uv` installed into its own venv (`~/.local/share/uv-bootstrap`, linked at `~/.local/bin/uv`) — Homebrew had no prebuilt bottle and was compiling from source.

**Build steps (2026-11-13)** — all in `src/interview_agent/`
1. `schemas/models.py` — SourceDocument, Fact, JobRequirement, Discrepancy, FactSheet, MatchEntry, MatchGapMap; every fact carries `fact_id`, `claim`, `source_doc`, exact `source_text` + char offsets.
2. `ingest.py` — PDF/TXT/MD → normalized page text as an in-memory, unredacted `RawDocument`.
2b. `redact.py` — masks email, phone, street address/postcode, name (from private terms file), URLs; private `RedactionMap`; `assert_no_pii` send guard.
3. `grounding.py` + `schemas/proposals.py` — LLM proposals accepted only if the quote is found verbatim (whitespace-tolerant), numbers appear in the quote, skill tags are traceable.
4. `llm.py` + `config.py` — guarded `LLMClient`, `GroqClient` (SDK retries), `FakeLLM`; metadata-only logging; refuses `GROQ_LOG=debug`.
5. `extract.py` + `prompt_loader.py` + prompts `extract_candidate.v1`, `extract_job.v1`, `find_discrepancies.v1` — chunked extraction, discrepancies, Fact Sheet.
6. `match.py` + prompt `match_gap.v1` — Match/Gap Map; company_context not matched; one follow-up call; never invents a default "gap".
7. `report.py` + `cli.py` + `__main__.py` — `interview-agent build [--dry-run] | verify | wipe`; `out/review.md`.

**Repository and environment (2026-11-13)**
- Git safeguards: documents gitignored outside `sample_data/`; pre-commit hook `.githooks/pre-commit` → `scripts/check_private_data.py` (blocks secrets, candidate data, private folders, non-fictional emails, phones, home paths, private terms — also on `git add -f`). Hardened for a public repo per the owner's never-push checklist.
- Commits: `b78596d` (Milestone 1), `fa267b3` (hardened safeguards). Author `Capella35` with GitHub noreply email.
- Pushed to **public** repo https://github.com/Capella35/mock_interview_coach; `main` created from `milestone-1`, set as default branch; `milestone-1` deleted locally and on GitHub.
- Project moved out of iCloud: `~/Desktop/FDE_Interview_Agent` → `~/Projects/FDE_Interview_Agent`; `.venv` rebuilt.

### Tests (all `uv run pytest`, FakeLLM only, fictional data, no network)
| After | Result |
|---|---|
| Step 1 (models) | 36 passed |
| Step 2 (ingest) | 70 passed |
| Step 2b (redact) — first run | 3 collection errors (`ModuleNotFoundError: interview_agent`: macOS-hidden `.pth`) → fixed → 128 passed |
| Step 3 (grounding) | 168 passed |
| Step 4 (LLM client) | 197 passed |
| Step 5 (extraction) — first run | 1 collection error (test helper used a non-hex fake sha256) → fixed → 219 passed |
| Step 6 (matching) | 220 passed after refactor; then 2 failed, 229 passed (wrong-shape JSON was not retried — real bug, fixed in `llm.call_json`) → 231 passed |
| Step 7 (report + CLI) | 248 passed |
| Private-data guard | 1 failed, 271 passed (guard flagged its own test file's literal examples → assembled from pieces) → 272 passed |
| Public-repo hardening | 296 passed |
| After move to `~/Projects` | 296 passed |
| **Final run at session end** | **296 passed in 1.22s** |

Other verification:
- Real CLI smoke test (`interview-agent build --dry-run` on fictional files): exit 0, name/street/phone/emails masked, city/employer/dates/metrics kept.
- Hook proven to block a force-added `data/private/` file (exit 1, nothing committed).
- `scripts/check_private_data.py --all`: OK, 45 files. History scan of both commits: no problems.
- GitHub tree after push: 45 files, none private; local == remote.
- `.venv` flags re-checked 90 s after the move: 0 hidden files, CLI works.

### Left unfinished
- ~~Uncommitted post-move doc updates~~ — committed and pushed at session end together with `BUILD_LOG.md` and `HANDOFF.md` (owner approved).
- **Real-document validation run not started** — blocked on owner inputs (see HANDOFF.md).
- No real prompt has met a real model yet; prompt quality is untested.
