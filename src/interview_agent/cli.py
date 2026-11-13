"""Step 7 — command line.

    interview-agent build --resume R.pdf [--linkedin L.pdf] --jd JD.txt [--dry-run]
    interview-agent verify
    interview-agent wipe [--all] [--yes]

build    ingest -> redact -> extract -> match -> write outputs.
         --dry-run stops after redaction: writes the exact redacted text that
         WOULD be sent to out/redacted/ and makes no LLM call.
verify   re-check saved outputs: every quote against its document, the map
         against the Fact Sheet it was built from.
wipe     delete generated outputs (out/) and private redaction maps;
         --all also deletes everything in the private folder (your uploaded
         documents and terms file).

Exit codes: 0 ok · 1 error · 3 finished but incomplete (see out/review.md).
Outputs: out/ (gitignored) — fact_sheet.json, match_gap_map.json, review.md,
run_report.json, documents/<doc>.json (redacted). Private: data/private/redaction/
<doc>.json (placeholder -> original values; never sent, never printed).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel

from interview_agent.config import ConfigError, load_settings
from interview_agent.extract import build_fact_sheet
from interview_agent.ingest import IngestError, load_document
from interview_agent.llm import LLMCallError, LLMClient, make_client
from interview_agent.match import build_match_gap_map
from interview_agent.prompt_loader import PromptError
from interview_agent.redact import PIILeakError, load_terms, redact_document
from interview_agent.report import problems, render_review
from interview_agent.schemas import FactSheet, MatchGapMap, SourceDocument

EXIT_OK, EXIT_ERROR, EXIT_INCOMPLETE = 0, 1, 3
_EXPECTED_ERRORS = (IngestError, ConfigError, LLMCallError, PIILeakError, PromptError)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="interview-agent", description="Interview Agent — Milestone 1 tools.")
    p.add_argument("-v", "--verbose", action="store_true", help="log LLM call metadata (never text)")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="build Fact Sheet + Match/Gap Map from your documents")
    b.add_argument("--resume", type=Path, required=True)
    b.add_argument("--linkedin", type=Path)
    b.add_argument("--jd", type=Path, required=True, help="job description (.pdf/.txt/.md)")
    b.add_argument("--terms", type=Path, default=Path("data/private/redact_terms.txt"),
                   help="private list of strings to mask (name:, address:, ...)")
    b.add_argument("--owner", default="local-owner")
    b.add_argument("--out", type=Path, default=Path("out"))
    b.add_argument("--private-dir", type=Path, default=Path("data/private"))
    b.add_argument("--env-file", type=Path, default=Path(".env"))
    b.add_argument("--dry-run", action="store_true", help="redact only; show what would be sent; no LLM call")

    v = sub.add_parser("verify", help="re-check saved outputs")
    v.add_argument("--out", type=Path, default=Path("out"))

    w = sub.add_parser("wipe", help="delete generated outputs and private redaction maps")
    w.add_argument("--all", action="store_true", help="also delete ALL files in the private folder")
    w.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    w.add_argument("--out", type=Path, default=Path("out"))
    w.add_argument("--private-dir", type=Path, default=Path("data/private"))
    return p


def main(argv: Sequence[str] | None = None, *, client: LLMClient | None = None,
         environ: dict[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.WARNING, format="%(message)s")
        logging.getLogger("interview_agent").setLevel(logging.INFO)
    try:
        if args.command == "build":
            return _build(args, client, environ)
        if args.command == "verify":
            return _verify(args)
        return _wipe(args)
    except _EXPECTED_ERRORS as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    return obj


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build(args, client: LLMClient | None, environ) -> int:
    inputs = [(args.resume, "resume")] + ([(args.linkedin, "linkedin")] if args.linkedin else []) + \
             [(args.jd, "job_description")]
    raws = [load_document(path, kind) for path, kind in inputs]
    terms = load_terms(args.terms)
    print(f"Read {len(raws)} document(s); {len(terms)} private redaction term(s) "
          f"{'from ' + str(args.terms) if terms else '(no terms file found)'}.")

    docs: dict[str, SourceDocument] = {}
    maps = []
    for raw in raws:
        doc, rmap = redact_document(raw, owner_id=args.owner, terms=terms)
        docs[doc.doc_id] = doc
        maps.append(rmap)
        _write(args.private_dir / "redaction" / f"{doc.doc_id}.json", rmap.model_dump_json(indent=2))
        _write(args.out / "documents" / f"{doc.doc_id}.json", doc.model_dump_json(indent=2))
        counts = ", ".join(f"{n} {k}" for k, n in sorted(rmap.counts().items())) or "nothing"
        print(f"  {doc.doc_id}: {len(raw.pages)} page(s), {len(doc.text)} chars; masked {counts}")
        for w in [*raw.warnings, *rmap.warnings]:
            print(f"  ⚠️  {doc.doc_id}: {w}")

    if args.dry_run:
        for doc in docs.values():
            _write(args.out / "redacted" / f"{doc.doc_id}.txt", doc.text)
        print(f"Dry run: nothing was sent. Check exactly what would be sent in {args.out / 'redacted'}/.")
        return EXIT_OK

    client = client or make_client(load_settings(args.env_file, environ))
    print(f"Extracting with {client.provider}:{client.model} ...")
    sheet, extraction = build_fact_sheet(client, list(docs.values()))
    print("Matching ...")
    gmap, matching = build_match_gap_map(client, sheet)

    _write(args.out / "fact_sheet.json", sheet.model_dump_json(indent=2))
    _write(args.out / "match_gap_map.json", gmap.model_dump_json(indent=2))
    _write(args.out / "run_report.json",
           json.dumps({"extraction": _jsonable(extraction), "matching": _jsonable(matching)}, indent=2))
    _write(args.out / "review.md", render_review(sheet, gmap, docs, extraction, matching, maps))

    issues = problems(sheet, gmap, docs, extraction, matching)
    statuses = [e.status for e in gmap.entries]
    print(f"Facts: {len(sheet.candidate_facts)} accepted, "
          f"{sum(len(d.rejections) for d in extraction.documents)} rejected · "
          f"Requirements: {len(sheet.matchable_requirements())} "
          f"(strong {statuses.count('strong_match')}, partial {statuses.count('partial_match')}, "
          f"gap {statuses.count('gap')}) · LLM calls: {extraction.llm_calls + matching.llm_calls}")
    print(f"Review: {args.out / 'review.md'}")
    if issues:
        print(f"⚠️  Incomplete — {len(issues)} problem(s), listed at the top of the review.")
        return EXIT_INCOMPLETE
    return EXIT_OK


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #


def _verify(args) -> int:
    try:
        sheet = FactSheet.model_validate_json((args.out / "fact_sheet.json").read_text(encoding="utf-8"))
        gmap = MatchGapMap.model_validate_json((args.out / "match_gap_map.json").read_text(encoding="utf-8"))
        docs = {}
        for p in sorted((args.out / "documents").glob("*.json")):
            d = SourceDocument.model_validate_json(p.read_text(encoding="utf-8"))
            docs[d.doc_id] = d
    except FileNotFoundError as e:
        print(f"error: missing output file {Path(e.filename).name}; run build first", file=sys.stderr)
        return EXIT_ERROR
    found = sheet.verify_spans(docs) + gmap.cross_check(sheet)
    if found:
        print(f"⚠️  {len(found)} problem(s):")
        for p in found:
            print(f"  - {p}")
        return EXIT_INCOMPLETE
    print(f"OK: {len(sheet.candidate_facts)} facts and {len(sheet.job_requirements)} requirements match their "
          f"documents exactly; the map covers every requirement and is tied to this Fact Sheet.")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# wipe
# --------------------------------------------------------------------------- #


def _safe_target(path: Path) -> Path:
    root = Path.cwd().resolve()
    resolved = path.resolve()
    if resolved == root or root not in resolved.parents:
        raise ConfigError(f"refusing to delete {path}: must be a folder inside {root}")
    return resolved


def _wipe(args) -> int:
    targets = [_safe_target(args.out)]
    targets.append(_safe_target(args.private_dir if args.all else args.private_dir / "redaction"))
    existing = [t for t in targets if t.exists()]
    if not existing:
        print("Nothing to delete.")
        return EXIT_OK
    print("This permanently deletes:")
    for t in existing:
        n = sum(1 for p in t.rglob("*") if p.is_file())
        print(f"  {t}  ({n} file(s))")
    if args.all:
        print("  — including your uploaded documents and redaction terms in the private folder.")
    if not args.yes and input('Type "delete" to confirm: ').strip() != "delete":
        print("Cancelled.")
        return EXIT_OK
    for t in existing:
        shutil.rmtree(t)
    print("Deleted.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
