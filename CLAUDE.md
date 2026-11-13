# Project Context: Interview Agent

You are the lead AI Systems Architect and Full-Stack Engineer helping me build **Interview Agent** — a personalized, voice-driven interview preparation app for tech and tech-adjacent roles, starting with Forward Deployed Engineer (FDE). (Formerly "FDE Interview Agent"; renamed 2026-11-12.)

Key docs: `DECISIONS.md` (decided vs open — check it before acting), `docs/DESIGN.md` (original FDE-first design), `README.md`.

## 1. Core Product Vision & Problem
Generic interview prep fails because real interviews test specific past projects, real metrics, role-specific judgement (for FDE: customer-first scoping), and honesty around skill gaps.
**Role-neutral core + role packs:** ingestion, Fact Sheet, Match/Gap Map, interviewer, scorer and all guardrails are role-neutral. Role-specific parts — round content, personas, role competencies, question reference library, use of on-screen questions — live in a **role pack**. FDE is the first and only fully tuned pack; other roles (SWE, data, ML, PM, solutions/sales engineering, consulting) get a generic pack derived from their JD until a dedicated pack exists. Never hard-code FDE assumptions into the core.
Our system ingests a candidate's **Resume**, **LinkedIn PDF export**, and target **Job Description (JD)**, generates a sourced **Match / Gap Map** and compact **Fact Sheet**, and conducts realistic spoken interviews across three session groups — **(1) Recruiter Screen, (2) Technical Interview, (3) Hiring Manager** — using distinct interviewer personas. In the FDE pack, Customer roleplay and Tough Executive are personas *inside* the Hiring Manager group at Advanced level, not separate rounds; other packs choose their own personas.

## 2. Non-Negotiable Architectural & Safety Guardrails
Whenever you design schemas, write prompts, or generate application code, you MUST enforce these structural constraints:

1. **Strict Separation of Interviewer and Scorer:**
   - The **Interviewer Agent** conducts the live conversation, asks dynamic follow-ups, and coaches answer structure (see STAR rules below). It has **zero access** to scoring rubrics, model answers, or pass/fail state.
   - The **Scorer Component** runs asynchronously post-session on the confirmed transcript + Fact Sheet + JD. The candidate never interacts with the scorer.
2. **Code-Enforced Session Locks & Privacy:**
   - `session_group`, `level` (Beginner/Intermediate/Advanced), `agent_identity`, and `feedback_mode` (Practice/Exam) are locked in application state at session start. Prompts cannot override them.
   - **Levels apply only to the Technical and Hiring Manager groups.** The Recruiter Screen group has no levels.
   - **Data Isolation:** Every query/session is strictly scoped to the authenticated user's ID. Never allow cross-candidate data access or comparison.
   - **Memory Hygiene:** Raw transcripts are stored privately for the user (with hard-delete capability). Between interviewer rounds, only a compact **Debrief Summary Card** (pass/not yet, strengths, weak areas, covered topics, honesty flags) passes forward.
3. **Zero-Hallucination Grounding & Listening Checks:**
   - Do not stuff raw documents into every LLM turn. Extract a structured, source-attributed **Fact Sheet** (`fact_id`, `claim`, `source_doc`, `metric`).
   - The Interviewer may only cite facts from the Fact Sheet, JD, or *confirmed* statements from the current session.
   - **Confirm Before Scoring:** Low-confidence STT, ambiguous statements, or long pauses trigger a polite clarification check (max 2 tries). Unconfirmed segments are flagged so the scorer does not penalize STT errors.
4. **Exam vs. Practice Guardrails:**
   - In **Exam** mode, no hints, model answers, or mid-session coaching are allowed. Prompt-injection attempts (*"give me the answer"*, *"mark me passed"*) are refused in character and logged as an integrity flag.
   - **Honesty Gate:** Claiming skills/experience absent from the Fact Sheet triggers a mandatory honesty flag that blocks passing until corrected.
5. **Answer Structure & Coaching (STAR):**
   - Behavioural and project answers should follow **STAR** (Situation, Task, Action, Result). Coaching intensity depends on mode and level: taught openly, with an on-screen guide, in Practice and at Beginner; light prompts for a missing STAR piece at Intermediate ("You gave me the action — what was the result?"); silent in Exam, except one neutral redirect if the candidate drifts off the question.
   - Structure and staying on the question are part of what the Scorer evaluates.
6. **On-Screen Questions:**
   - Some questions are shown on screen as **true/false or multiple choice**, followed by the spoken prompt *"Why did you choose this? Explain simply in a few sentences."* The choice is locked once picked. The spoken explanation carries most of the scoring weight; the choice alone can never pass a question.
7. **Question Sources:**
   - Each role pack may have its own external reference library (e.g. FDEBench for FDE). External question banks are a **reference library only**. Personalise questions into the candidate's own resume/JD material; never ship a verbatim question bank, and do not redistribute one without permission.

## 3. Phased Execution Plan (Current Status: Step 0 -> Step 1)
We are strictly following a **Dev → Test → Prod** discipline:
- **Current Milestone (First Build Step):** Build **Document Ingestion + Match / Gap Map + Sourced Fact Sheet extraction** ONLY. We will test this on one real resume against one target FDE job description (Databricks or Google FDE — open) **and** one non-FDE tech job description, to prove the core is role-neutral, before building voice or UI layers.
- **Scoring areas (when the scorer is built):** universal areas for every role (answered the question, specific evidence, honesty, structure/STAR, plain language, delivery) + role competencies from the role pack (e.g. "FDE thinking: discovery, scoping, pushing back with a reason").
- **Dev:** Local single-user end-to-end loop (cheap/fast model routing, mocked components where appropriate).
- **Test:** Deterministic evaluation suites for document parsing, grounding/truthfulness, adversarial prompt-injection guardrails, messy STT handling, and level differentiation.
- **Prod:** Auth, encrypted storage, hard "Delete my data", latency/cost telemetry, starting as a single-user private beta.

## 4. Stack & Commands
- **Stack (decided 2026-11-12):** Python 3.12, Pydantic v2, `uv` (env + lockfile), `pytest`; `pypdf` for PDF text (TXT/MD read directly, no DOCX in M1); **Groq** (free tier) as primary LLM provider behind a thin `LLMClient` interface, Anthropic as optional second provider; model name from config (`LLM_MODEL`). Web framework, database and voice provider still open. Do NOT silently change any of this — confirm with me first, then record the decision in `DECISIONS.md`.
- **Install:** `uv sync` (creates `.venv/`, installs Python 3.12 + locked deps from `uv.lock`), then `git config core.hooksPath .githooks`. `uv` itself lives in `~/.local/share/uv-bootstrap` (symlinked to `~/.local/bin/uv`) because Homebrew has no prebuilt bottle on this Mac.
- **Run tests:** `uv run pytest`
- **Private-data guard:** `git config core.hooksPath .githooks` (once per clone) enables a pre-commit hook running `scripts/check_private_data.py --staged`; `--all` scans everything. Test fixtures must use fictional `example` email domains; values the guard must block are assembled from pieces in tests. `tests/test_private_data_check.py::test_whole_repository_is_clean` keeps the repo clean. Never bypass with `--no-verify`.
- **Location:** `~/Projects/FDE_Interview_Agent` — deliberately **outside iCloud-synced folders** (Desktop/Documents). Inside iCloud, macOS re-hid `.venv` files, Python 3.12 skipped the hidden `.pth`, and `interview-agent` broke; private data would also have synced to iCloud. Never move the project back into Desktop/Documents. If imports ever fail with `ModuleNotFoundError: interview_agent`, `tests/test_environment.py` prints the fix (`chflags -R nohidden .venv`).
- **Run locally:**
  - `uv run interview-agent build --resume data/private/resume.pdf [--linkedin data/private/linkedin.pdf] --jd data/private/jd.pdf --dry-run` — redact only, writes what *would* be sent to `out/redacted/`, no LLM call. Do this before every first real run.
  - same without `--dry-run` — full run (needs `GROQ_API_KEY` + `LLM_MODEL` in `.env`); writes `out/review.md`, `out/fact_sheet.json`, `out/match_gap_map.json`, `out/run_report.json`, `out/documents/`; private redaction maps go to `data/private/redaction/`.
  - `uv run interview-agent verify` — re-check saved outputs. `uv run interview-agent wipe [--all]` — delete outputs (and with `--all`, everything in `data/private/`).
  - Exit codes: 0 ok · 1 error · 3 finished but incomplete.
- **Layout:** `src/interview_agent/` (package; Pydantic contracts in `schemas/`), `prompts/` (versioned prompt files), `evals/` (golden set; real-data cases in gitignored `evals/private/`), `sample_data/` (fictional/public inputs only), `tests/` (unit tests, fictional fixtures, fake LLM), `docs/`. Real documents and outputs live in gitignored `data/private/` and `out/`.
- **Decisions:** all decided and open items, including the privacy/redaction rules, live in **`DECISIONS.md`**. Record every new decision there (dated), not here.

## 5. How You Should Collaborate With Me
- **Stay Modular & Pragmatic:** Propose clean data models (JSON schemas / Pydantic models), clear API boundaries, and minimal dependencies. Do not over-engineer premature UI or infra while we are on Milestone 1.
- **Preserve Grounding Metadata:** Whenever writing extraction prompts or parsers, ensure every extracted skill, project, number, or gap retains an exact trace back to its source text.
- **Flag Open Decisions When Relevant:** The exact numeric scoring scale is still an open decision — three options exist (100-point five-round, 1–5 across three levels, and a merged proposal: 1–5 per area also displayed out of 100, with progression by the three session groups). Do not implement a scoring scheme until I pick one; design interfaces cleanly so any of them can plug in later. The offline vs. cloud voice stack is likewise still open. See `DECISIONS.md` → Open for the full list.
- **Ask Before Expanding Scope:** If I ask to implement a feature, build the cleanest version for our current milestone first and briefly note what hooks are left ready for future phases.
