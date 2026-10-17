"""Batch processing and result records."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .db import Database
from .graph import Context, PipelineState, process_invoice
from .llm.client import LLMClient
from .presentation import (
    cause,
    decision_label,
    display_path,
    key_codes,
    reason_sentence,
    vendor_message,
)

SUPPORTED = {".txt", ".pdf", ".csv", ".json", ".xml"}


def collect_files(dirs: Iterable[str | Path]) -> list[Path]:
    """Processing order: directories in the order given; within each, Python ``sorted()`` on the
    file name (plain byte order). That puts ``invoice_1004.json`` before
    ``invoice_1004_revised.json`` because '.' sorts before '_'. Shell/locale ordering may differ."""
    out: list[Path] = []
    for d in dirs:
        files = [p for p in Path(d).iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED]
        out += sorted(files, key=lambda p: p.name)
    return out


def result_record(state: PipelineState, run_id: str) -> dict[str, Any]:
    inv = state.get("invoice")
    findings = state.get("findings", [])
    return {
        "file": display_path(state["path"]),
        "run_id": run_id,
        "decision": state["decision"].value,
        "vendor": inv.vendor if inv else None,
        "invoice_number": inv.invoice_number if inv else None,
        "total": inv.total if inv else None,
        "currency": inv.currency if inv else None,
        "codes": sorted({f.code for f in findings}),
        "cause": cause(state["decision"].value, findings),
        "decision_label": decision_label(state["decision"].value, findings),
        "execution_failures": sorted(
            {f.code for f in findings if f.kind == "execution" and f.severity != "note"}
        ),
        "business_findings": sorted(
            {f.code for f in findings if f.kind == "business" and f.severity != "note"}
        ),
        "findings": [f.model_dump() for f in findings],
        "reasons": [r.model_dump() for r in state.get("reasons", [])],
        "reason": reason_sentence(state["decision"].value, findings),
        "vendor_message": (
            vendor_message(
                inv.vendor if inv else None, inv.invoice_number if inv else None, findings
            )
            if state["decision"].value == "REJECT"
            else None
        ),
        "payment": state.get("payment"),
        "duplicate_of": state.get("duplicate_of"),
        "ledger_id": state.get("ledger_id"),
        "llm_calls": state.get("llm_calls", []),
    }


def run_batch(
    paths: Iterable[Path],
    db: Database,
    client: LLMClient | None,
    llm_mode: str,
    *,
    payer: Any = None,
) -> list[tuple[PipelineState, Context]]:
    results = []
    for p in paths:
        ctx = Context(db=db, client=client, llm_mode=llm_mode, payer=payer)
        results.append((process_invoice(str(p), ctx), ctx))
    return results


def file_fingerprint(path: str | Path) -> str:
    """Same value the parsers store as the document fingerprint (sha256 of the file bytes)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def split_loaded(paths: Iterable[Path], db: Database) -> tuple[list[Path], list[Path]]:
    """(new, already loaded): files whose exact bytes are already in the ledger are not a new
    business event. Decided up front, so identical files inside one batch are all processed."""
    new: list[Path] = []
    seen: list[Path] = []
    for p in paths:
        (seen if db.find_by_fingerprint(file_fingerprint(p)) else new).append(p)
    return new, seen


def run_new(
    paths: Iterable[Path],
    db: Database,
    client: LLMClient | None,
    llm_mode: str,
    *,
    payer: Any = None,
) -> tuple[list[tuple[PipelineState, Context]], int]:
    """Process only files not yet in the ledger. Returns (results, number skipped). Business
    duplicates between different files are still recorded as DUPLICATE by the pipeline."""
    new, seen = split_loaded(paths, db)
    return run_batch(new, db, client, llm_mode, payer=payer), len(seen)


def summary_table(records: list[dict[str, Any]]) -> str:
    rows = [("file", "decision", "cause", "key findings")]
    for r in records:
        rows.append((Path(r["file"]).name, r["decision"], r["cause"], key_codes(r)))
    w = [max(len(row[i]) for row in rows) for i in range(3)]
    lines = [f"{a:<{w[0]}}  {b:<{w[1]}}  {c:<{w[2]}}  {d}" for a, b, c, d in rows]
    lines.insert(1, "-" * len(lines[0]))
    return "\n".join(lines)
