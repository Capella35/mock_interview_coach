# Decision Log

Every product or technical decision is recorded here. **Decided** items change only with the owner's explicit approval (add a new dated entry; don't rewrite history). **Open** items must not be implemented until moved to Decided.

---

## Decided

### Non-negotiables (from CLAUDE.md §2–3)
- **Interviewer ≠ Scorer.** The Interviewer Agent runs the conversation and has zero access to rubrics, model answers, or pass/fail state. The Scorer runs asynchronously post-session on the confirmed transcript + Fact Sheet + JD; the candidate never interacts with it.
- **Session locks in code.** `session_group`, `level`, `agent_identity`, `feedback_mode` are locked in application state at session start; prompts cannot override them. Levels apply only to the Technical and Hiring Manager groups.
- **Data isolation.** Every query/session is scoped to the authenticated user's ID. No cross-candidate access or comparison.
- **Summary-card-only memory.** Raw transcripts are private to the user with hard delete. Only a compact Debrief Summary Card passes between interviewer rounds.
- **Grounding.** Interviewer cites only the Fact Sheet, the JD, or confirmed in-session statements. Every extracted item keeps an exact trace to its source text.
- **Milestone 1 scope.** Document ingestion + sourced Fact Sheet + Match/Gap Map only. No voice, no UI.

### 2026-11-12 — Milestone 1 plan
Approved. Build order: (1) schemas ✅ → (2) ingest ✅ → (2b) redact ✅ → (3) grounding ✅ → (4) LLM client + FakeLLM ✅ → (5) extract ✅ → (6) match ✅ → (7) report + CLI ✅ → real-document validation run. Match status is categorical (strong / partial / gap) — no numeric score.
- *2026-11-13 (step 2):* ingest returns an in-memory, **unredacted** `RawDocument` (normalized page texts; its repr never shows text). Only `redact.py` may turn it into a `SourceDocument`, so an unredacted `SourceDocument` can never exist. Text normalization is deterministic and idempotent (NFKC, whitespace, zero-width chars, "Page N of M" footers) so offsets are stable across runs. Limits: 10 MB, 30 pages; scanned/password-protected PDFs are rejected with a clear message (no OCR in M1).
- *2026-11-13 (step 2b):* `redact_document()` is the only way to build a `SourceDocument`. All detectors run on the original page text; overlaps resolved by priority (terms file > URL > email > phone > street address > postcode); one replacement pass, so the private `RedactionMap` records each original value and its placeholder's exact position. Generic placeholders (`[EMAIL]`, not `[EMAIL_1]`). Name masking relies on `name:` lines in `data/private/redact_terms.txt` (warning if missing for resume/LinkedIn). Address patterns cover English + Turkish street formats, UK postcodes, US ZIP after a state code. **Known gaps → use the terms file:** personal sites without a path (`myname.dev`), unusual address formats, 7-digit local phones. A plain 9–15 digit number (e.g. a raw ten-digit revenue figure) is masked as `[PHONE]`. Redaction fails closed: if any email/phone pattern survives, it raises instead of returning. `assert_no_pii()` is the send guard for step 4.
- *2026-11-13 (step 3):* The LLM returns **proposals** (`FactProposal`, `RequirementProposal`: quote + claim, no offsets/ids). `grounding.py` accepts or rejects each one independently with a reason code; nothing is silently repaired. Rules: quote must be found in the redacted text (whitespace/line-break differences only; **case and punctuation must match**), 3–400 chars, not placeholder-only; every number in claim / requirement / time_period must appear in the quote (so derived numbers like "2 years" from "2022–2024" are rejected); `metric.raw` must be in the quote and `metric.value` derivable from it (×1, k, M, B, or %); skill tags not traceable to the quote are dropped with a warning. Ids are assigned only to accepted items. Repeated quotes use the first occurrence (warning). **Lever if the real run shows many `quote_not_found` rejections:** also tolerate typographic differences (curly vs straight quotes, dash variants) — would need owner approval.
- *2026-11-13 (step 4):* `LLMClient.complete()` is the only door to a model: send guard on system + user text (fails closed, provider never called), then the call, then a metadata-only log line (purpose, prompt id, model, tokens, latency). Groq via official `groq` SDK 1.7 — **retries are the SDK's own** (429 / 5xx / 408 / connection errors; honours `retry-after` ≤ 60 s, else 0.5→8 s exponential), configured `max_retries=5`, `timeout=60s`; no custom retry loop. Refuses to start if `GROQ_LOG=debug` (the SDK would log request bodies). JSON mode on by default; a reply cut off at `max_tokens` is an error, not partial data. Settings from env + `.env` (env wins), no extra dependency; API keys never appear in reprs or errors.
- *2026-11-13 (step 5):* Three role-neutral prompts: `extract_candidate.v1`, `extract_job.v1`, `find_discrepancies.v1` (format: `## System` / `## User`, `{{var}}` filled in one pass so document text is never re-interpreted). **Discrepancy detection moved into step 5** so the Fact Sheet is final before matching (the map is tied to the sheet's hash). Documents are sent in chunks ≤ 6,000 chars split on paragraph/page boundaries; all proposals of a document are grounded together (sequential ids, cross-chunk duplicates dropped). Unusable reply (not JSON, wrong shape, cut off) → retried once → then recorded as a failed chunk and the run continues (report flags it); provider errors abort. Inputs: exactly one JD, ≥1 resume/LinkedIn, all same owner. `FactSheet.prompt_ids` records which prompt versions produced it. Documents are framed as data, not instructions.
- *2026-11-13 (step 6):* `match_gap.v1` sees only grounded facts + requirements (never raw documents), in batches of ≤ 15 requirements. **`company_context` items are not matched** (they're "why this company?" material, not something a candidate meets or misses); `FactSheet.matchable_requirements()` and `MatchGapMap.cross_check()` reflect this. Each returned entry is checked: requested req_id, not a duplicate (first kept), MatchEntry schema rules, evidence ids exist, and every number in `rationale`/`missing` appears in the requirement or cited facts (no computed durations). Rejected/missing requirements get **one** follow-up call with only those ids; anything still unmapped is **reported, never filled with a default "gap"** (that would be an ungrounded claim about the candidate). `extra_strengths` capped at 5, unknown ids dropped. `MatchGapMap.prompt_ids` added. Shared `llm.call_json()` now also retries **wrong-shape** JSON (previously only unparseable/cut-off) — aligns code with the step 5 entry above.
- *2026-11-13 (step 7):* CLI `interview-agent` with `build` (+ `--dry-run`: redact only, write exactly what would be sent, no LLM call), `verify` (re-check every quote and the map↔sheet tie from saved files) and `wipe` (outputs + private redaction maps; `--all` also the private folder with uploaded documents; asks for confirmation; refuses paths outside the project). `out/review.md` puts problems first, then each requirement with status, rationale, missing, interview angle and evidence quotes with document + page, company context, extra strengths, discrepancies, all facts, everything rejected, and a review checklist; redaction shown as counts only. Exit codes 0 ok / 1 error / 3 incomplete. Tests verify contact details appear only in `data/private/redaction/`, never in `out/` or anything sent.

### 2026-11-12 — Stack
Python 3.12, Pydantic v2, `uv` (env + lockfile), `pytest`; `pypdf` for PDF text (TXT/MD read directly, no DOCX in M1). All model calls go through a thin `LLMClient` interface; model name comes from config (`LLM_MODEL`), never hard-coded.

### 2026-11-12 — LLM provider
**Groq** (free tier) is the primary dev provider, via the official `groq` Python package behind `LLMClient`. **Anthropic** is an optional second provider (not wired yet). Client retries with back-off on rate-limit responses.

### 2026-11-12 — Privacy of real documents
A cloud LLM API may process the owner's real resume / LinkedIn / JD, under these conditions:
1. Development and tests use **fictional fixtures + `FakeLLM`**. Real documents are sent only for validation runs (and re-runs after prompt changes).
2. **Mask** — `redact.py` runs on *every* document (resume, LinkedIn, **and JD**, which may contain recruiter contact details) before anything else: email → `[EMAIL]`, phone → `[PHONE]`, street address / postcode → `[ADDRESS]`, name → `[NAME]`, LinkedIn/GitHub/personal URLs → `[URL]`. Pattern-based detection plus exact strings from `data/private/redact_terms.txt`. City/country kept by default (useful for location requirements). Employers, titles, projects, skills, metrics kept. Redacted text is the canonical `SourceDocument.text`; the placeholder→real-value map stays in `data/private/` and is never sent.
3. **Block** — `LLMClient` re-scans every outgoing payload for email/phone patterns and **refuses to send** (raises) if any are found.
4. **Test** — a fictional resume full of contact details goes through the pipeline; the test asserts none of it reaches `FakeLLM`.
5. Own API key in gitignored `.env`; **no prompt/response text in logs** (counts, token usage, errors only); outputs in gitignored `data/private/` and `out/`, with a one-command wipe.

### 2026-11-12 — Product scope: role-neutral core + role packs
- The core (ingestion, Fact Sheet, Match/Gap Map, interviewer, scorer, guardrails) is **role-neutral**.
- Role-specific parts — round content, personas (e.g. Customer roleplay), role competencies, question reference library, use of on-screen questions — live in a **role pack**.
- **FDE is the first and only fully tuned role pack.** Other roles get a generic pack derived from their JD until a dedicated pack exists.
- **Target range:** tech and tech-adjacent roles (SWE, data, ML, PM, solutions/sales engineering, FDE, consulting).
- Universal scoring areas (answered the question, specific evidence, honesty, structure/STAR, plain language, delivery) apply to every role; role competencies (e.g. "FDE thinking") come from the role pack.
- **M1 validation includes one FDE JD and one non-FDE tech JD** to prove the core is role-neutral.

### 2026-11-12 — Name
Project renamed **Interview Agent**; Python package `interview_agent`. (Local folder name unchanged.)

### 2026-11-13 — Git safeguards
Document files (`.pdf .docx .doc .rtf .odt .pages`) are gitignored everywhere except `sample_data/`. A versioned pre-commit hook (`.githooks/pre-commit` → `scripts/check_private_data.py`) blocks staged content containing: files from `data/`, `out/`, `evals/private/`; documents outside `sample_data/`; API/private keys; emails without a fictional `example` domain; phone numbers (outside `tests/`); local home paths; any value from `data/private/redact_terms.txt`. First commits go on a branch, not straight to the default branch.

### 2026-11-13 — Public GitHub repository
Owner chose to publish to the **public** repo `Capella35/mock_interview_coach` (supersedes the earlier "private for now"). Commit author: `Capella35` with the GitHub noreply email. What may never be pushed, enforced by `.gitignore` **and** the pre-commit check (which also catches `git add -f`):
1. **Secrets** — `.env*` except `.env.example` (fake values only), key/cert files, credentials / service-account / token / client_secret JSONs. If a secret is ever committed, deleting it is not enough: **rotate the key**.
2. **Candidate data** — resumes and LinkedIn exports (incl. the owner's own; samples in git must be fictional), transcripts, audio recordings, sessions, extracted Fact Sheets / maps / redaction maps / run reports, local databases.
3. **Local junk** — `.venv/`, caches, `.DS_Store`, editor settings, logs, model weights.

### 2026-11-13 — Branches
`main` is the default branch on GitHub (created from `milestone-1`, which was then deleted locally and remotely). New work goes on feature branches merged into `main`.

### 2026-11-13 — Session records
`BUILD_LOG.md` (append-only, one entry per session: built/changed, exact test results, unfinished items) and `HANDOFF.md` (overwritten each session: where things stand, next step, blockers, uncommitted files).

### 2026-11-13 — Project location: outside iCloud
Moved from `~/Desktop/FDE_Interview_Agent` (synced by iCloud Drive "Desktop & Documents") to `~/Projects/FDE_Interview_Agent`. Reasons: (a) iCloud re-hid `.venv` files, so Python skipped the editable install's `.pth` and the `interview-agent` command failed; (b) **privacy** — `data/private/` (real resume, LinkedIn, redaction maps with original contact values) and `out/` must stay on this Mac, not sync to iCloud. At the time of the move `data/private/` was empty, so no real personal data had been synced. `.venv` was rebuilt in the new location. The project must not be moved back into Desktop/Documents.

### 2026-11-12 — Repository layout
- `src/interview_agent/schemas/` — Pydantic contracts (importable package, not a top-level folder).
- `prompts/` — versioned prompt files at the top level.
- `evals/` — golden set; cases built from the owner's real documents live in gitignored `evals/private/`.
- `sample_data/` — fictional resumes and public JDs only. The owner's real resume lives in gitignored `data/private/`.
- `docs/DESIGN.md` — original FDE-first design document.

---

## Open

- **Scoring scale.** 100-point across five rounds vs 1–5 across three levels vs a merged proposal (1–5 per area, also shown out of 100, progression by the three session groups)? **Not decided — do not implement scoring until the owner chooses.** Interfaces must allow any of the three to plug in.
- **Voice stack.** Offline vs cloud? Which STT/TTS providers? Not decided. (Note only: Groq also offers speech-to-text — an option to evaluate, not a choice.)
- **First test job description.** Databricks FDE vs Google FDE? Not decided. Plus: which non-FDE tech JD for the role-neutrality check?
- **Groq data policy.** Does Groq's current free-tier API policy (retention, training use) fit our privacy decision? **Blocks the real-document validation run** until checked by the owner; does not block anything before it.
- **Which LLM model** on Groq for extraction and matching? Config only (`LLM_MODEL`), so no code depends on it. To be chosen before the first real run from Groq's current model list (needs strong JSON output and exact quoting).
- **Web framework.** Not decided (not needed in M1).
- **Database / storage.** Not decided (not needed in M1).
- **Role-pack format.** File format and fields of a role pack (rounds, personas, competencies, reference library). Not decided (needed after M1).
- **Second role pack.** Which role after FDE? Not decided.
