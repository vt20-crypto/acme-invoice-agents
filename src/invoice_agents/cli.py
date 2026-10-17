"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from .batch import collect_files, result_record, run_batch, summary_table
from .db import Database
from .export import compare_expected, result_rows, summary_line, write_results
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
    p.add_argument("command", nargs="?", choices=["run", "reset", "eval"], default="run")
    p.add_argument(
        "dirs",
        nargs="*",
        metavar="DIR",
        help="eval --expected only: directories holding the invoices (default: the folder of "
        "the expected file)",
    )
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
        choices=["auto", "live", "cache", "none"],
        default="auto",
        help="auto (default): per call, the recorded response if there is one, else live Grok "
        "if a key is set, else rules only; live: call Grok and record responses; cache: replay "
        "recorded responses only; none: code only",
    )
    p.add_argument("--db", default="acme.db", help="SQLite database file")
    p.add_argument("--cache", default="cache/llm", help="LLM response cache directory")
    p.add_argument("--fresh", action="store_true", help="wipe and reseed the database first")
    p.add_argument(
        "--out",
        metavar="PATH",
        help="also write the results of this run to PATH (CSV, or JSON if it ends in .json)",
    )
    p.add_argument(
        "--expected",
        metavar="FILE.csv",
        help="eval only: CSV with columns file,expected_decision; process the invoices and "
        "compare (without it, eval checks the built-in samples)",
    )
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


def run_eval(mode: str, cache: str) -> int:
    """Expected vs actual decisions for every sample invoice, in one mode (strict, never auto)."""
    from .evaluation import evaluate

    if mode == "auto":
        mode = "cache"
    try:
        ev = evaluate(mode, cache)
    except LLMUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    width = max(len(r["file"]) for r in ev["rows"])
    print(f"{'file':<{width}}  {'expected':<10} {'actual':<10}")
    for r in ev["rows"]:
        mark = "\u2713" if r["match"] else "\u2717"
        print(f"{r['file']:<{width}}  {r['expected'] or '-':<10} {r['actual']:<10} {mark}")
    bad_pay = sum(1 for r in ev["rows"] if r["paid"] and r["actual"] != "APPROVE")
    print(f"\n{ev['summary']} (mode {mode}); {bad_pay} invalid payments")
    return 0 if ev["matched"] == ev["total"] else 1


def run_eval_expected(args: argparse.Namespace) -> int:
    """Process the invoices next to a user's expected-results CSV and compare decisions."""
    expected = Path(args.expected)
    if not expected.is_file():
        print(f"error: file not found: {args.expected}", file=sys.stderr)
        return 2
    try:
        client = make_client(args.llm, args.cache)
    except LLMUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    files = [
        p
        for p in collect_files(args.dirs or [expected.parent])
        if p.resolve() != expected.resolve()
    ]
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "eval.db")
        try:
            results = run_batch(files, db, client, args.llm)
        finally:
            db.close()
    processed = [
        {"file": Path(s["path"]).name, "decision": s["decision"].value} for s, _ in results
    ]
    try:
        cmp = compare_expected(processed, expected.read_text())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    width = max((len(r["file"]) for r in cmp["rows"]), default=4)
    print(f"{'file':<{width}}  {'expected':<10} {'actual':<10}")
    for r in cmp["rows"]:
        mark = "\u2713" if r["match"] else "\u2717"
        print(f"{r['file']:<{width}}  {r['expected'] or '-':<10} {r['actual']:<10} {mark}")
    if cmp["not_processed"]:
        print("\nExpected but not processed: " + ", ".join(cmp["not_processed"]))
    print(f"\n{summary_line(cmp)} (mode {args.llm})")
    return 0 if cmp["matched"] == cmp["total"] and not cmp["not_processed"] else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = Database(args.db)
    try:
        if args.command == "reset":
            db.reset()
            print(f"Database {args.db} reset and reseeded.")
            return 0
        if args.command == "eval":
            if args.expected:
                return run_eval_expected(args)
            return run_eval(args.llm, args.cache)
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
        run_ids = set()
        for path in paths:
            ctx = Context(db=db, client=client, llm_mode=args.llm)
            prior = db.find_by_fingerprint(parse_file(path).fingerprint)
            jlog("run_start", run_id=ctx.run_id, file=str(path), llm=args.llm)
            state = process_invoice(str(path), ctx)
            jlog("run_end", run_id=ctx.run_id, decision=state["decision"].value)
            records.append(result_record(state, ctx.run_id))
            run_ids.add(ctx.run_id)
            if not args.batch:
                print(render(state, ctx, prior))
        if args.batch:
            print(summary_table(records))
        if args.out:
            ledger = [r for r in db.ledger() if r["run_id"] in run_ids]
            write_results(result_rows(ledger, db.payments()), args.out)
            print(f"Results written to {args.out}")
        return 0
    finally:
        db.close()
