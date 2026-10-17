"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .batch import collect_files, result_record, summary_table
from .db import Database
from .graph import Context, PipelineState, process_invoice
from .ingestion.parsers import parse_file
from .llm.client import LLMUnavailable, make_client
from .logs import jlog
from .models import Decision
from .presentation import (
    decision_label,
    display_path,
    money,
    split_reasons,
    vendor_message,
)


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
    p.add_argument(
        "--llm",
        choices=["live", "cache", "none"],
        default="cache",
        help="live: call Grok and record responses; cache: replay recorded responses only; "
        "none: code only",
    )
    p.add_argument("--db", default="acme.db", help="SQLite database file")
    p.add_argument("--cache", default="cache/llm", help="LLM response cache directory")
    p.add_argument("--fresh", action="store_true", help="wipe and reseed the database first")
    return p


def render(state: PipelineState, ctx: Context, prior: dict | None) -> str:
    out = [
        f"=== Invoice: {display_path(state['path'])} ===",
        f"run={ctx.run_id}  llm mode={ctx.llm_mode}",
    ]
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
    findings = state.get("findings", [])
    out.append(f"Decision: {decision_label(decision.value, findings)}")
    pay = state.get("payment")
    if pay:
        if pay["status"] == "paid":
            amount = money(pay["amount"], pay.get("currency", "USD"))
            line = f"Payment: paid {amount} to {pay['vendor']}"
            if pay.get("output"):
                line += f"  (mock_payment output: {pay['output']})"
            out.append(line)
        else:
            out.append(f"Payment: not made ({pay['status']}: {pay['detail']})")
    elif decision is Decision.DUPLICATE:
        out.append(f"Payment: none (duplicate of ledger #{state.get('duplicate_of')})")
    else:
        out.append("Payment: none")
    if decision is not Decision.APPROVE:
        split = split_reasons(findings)
        if split["execution"]:
            out.append("System issues (not vendor or invoice defects):")
            out += [f"  - {r}" for r in split["execution"]]
        if split["business"]:
            out.append("Business reasons:")
            out += [f"  - {r}" for r in split["business"]]
    if decision is Decision.REJECT:
        inv = state.get("invoice")
        out.append("\nVendor message (template, not sent):")
        msg = vendor_message(
            inv.vendor if inv else None, inv.invoice_number if inv else None, findings
        )
        out += [f"  | {line}" for line in msg.splitlines()]
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
            client = make_client(args.llm, args.cache)
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
            if not args.batch:
                print(render(state, ctx, prior))
        if args.batch:
            print(summary_table(records))
        return 0
    finally:
        db.close()
