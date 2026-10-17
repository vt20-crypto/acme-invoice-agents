"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .batch import collect_files, result_record, summary_table, write_results
from .db import Database
from .graph import Context, PipelineState, process_invoice
from .ingestion.parsers import parse_file
from .llm.client import FixtureClient, LiveClient, LLMClient, LLMUnavailable
from .logs import jlog
from .models import Decision
from .presentation import decision_label, split_reasons


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="main.py", description="Acme invoice processing agents")
    p.add_argument("command", nargs="?", choices=["run", "reset"], default="run")
    p.add_argument("--invoice_path", help="path to an invoice file (txt, pdf, csv, json, xml)")
    p.add_argument(
        "--batch",
        nargs="+",
        metavar="DIR",
        help="process every invoice file in these directories, in order: directories as given, "
        "files within each sorted by file name (Python sorted(), byte order)",
    )
    p.add_argument("--out", metavar="FILE", help="write one JSON line per document (overwrites)")
    p.add_argument("--verbose", action="store_true", help="in batch mode print every step")
    p.add_argument("--llm", choices=["fixture", "none", "live"], default="fixture")
    p.add_argument("--db", default="acme.db", help="SQLite database file")
    p.add_argument("--fixtures", default="fixtures/llm", help="fixture directory for --llm fixture")
    p.add_argument("--fresh", action="store_true", help="wipe and reseed the database first")
    return p


def render(state: PipelineState, ctx: Context, prior: dict | None) -> str:
    out = [f"=== Invoice: {state['path']} ===", f"run={ctx.run_id}  llm mode={ctx.llm_mode}"]
    if prior:
        out.append(
            f"NOTE: this exact file was already processed (ledger #{prior['id']}, "
            f"decision {prior['decision']}). Use --fresh to reset the database and reprocess."
        )
    for i, step in enumerate(state["steps"], 1):
        out.append(f"\n[{i}] {step['step']}")
        out += [f"    {line}" for line in step["lines"]]
    out.append("\n--- Result ---")
    decision: Decision = state["decision"]
    out.append(f"Decision: {decision.value}")
    pay = state.get("payment")
    if pay:
        out.append(f"Payment: {pay['status']} ({pay['amount']} to {pay['vendor']})")
    elif decision is Decision.DUPLICATE:
        out.append(f"Payment: none (duplicate of ledger #{state.get('duplicate_of')})")
    else:
        out.append("Payment: none")
    findings = state.get("findings", [])
    out[out.index(f"Decision: {decision.value}")] = (
        f"Decision: {decision_label(decision.value, findings)}"
    )
    if decision is not Decision.APPROVE:
        split = split_reasons(findings)
        if split["execution"]:
            out.append("System issues (not vendor or invoice defects):")
            out += [f"  - {r}" for r in split["execution"]]
        if split["business"]:
            out.append("Business reasons:")
            out += [f"  - {r}" for r in split["business"]]
    calls = state.get("llm_calls", [])
    if calls:
        out.append("LLM provenance by step:")
        out += [
            f"  - {c['agent']}/{c['step']}: {c.get('provenance') or c.get('outcome')}"
            for c in calls
        ]
    else:
        out.append("LLM provenance: none (code-only run)")
    return "\n".join(out)


def make_client(mode: str, fixtures: str) -> LLMClient | None:
    if mode == "none":
        return None
    if mode == "fixture":
        return FixtureClient(fixtures)
    return LiveClient.from_env()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = Database(args.db)
    try:
        if args.command == "reset":
            db.reset()
            print(f"Database {args.db} reset and reseeded.")
            return 0
        if not args.invoice_path and not args.batch:
            print("error: --invoice_path or --batch is required", file=sys.stderr)
            return 2
        if args.invoice_path and not Path(args.invoice_path).exists():
            print(f"error: file not found: {args.invoice_path}", file=sys.stderr)
            return 2
        if args.fresh:
            db.reset()
        try:
            client = make_client(args.llm, args.fixtures)
        except LLMUnavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        paths = collect_files(args.batch) if args.batch else [Path(args.invoice_path)]
        records = []
        for path in paths:
            ctx = Context(db=db, client=client, llm_mode=args.llm)
            prior = db.find_by_fingerprint(parse_file(path).fingerprint)
            jlog("run_start", run_id=ctx.run_id, file=str(path), llm=args.llm)
            state = process_invoice(str(path), ctx)
            jlog("run_end", run_id=ctx.run_id, decision=state["decision"].value)
            records.append(result_record(state, ctx.run_id))
            if not args.batch or args.verbose:
                print(render(state, ctx, prior))
        if args.batch:
            print(summary_table(records))
        if args.out:
            write_results(args.out, records)
        return 0
    finally:
        db.close()
