"""Batch processing and result records."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .db import Database
from .graph import Context, PipelineState, process_invoice
from .llm.client import LLMClient

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
        "file": state["path"],
        "run_id": run_id,
        "decision": state["decision"].value,
        "vendor": inv.vendor if inv else None,
        "invoice_number": inv.invoice_number if inv else None,
        "total": inv.total if inv else None,
        "currency": inv.currency if inv else None,
        "codes": sorted({f.code for f in findings}),
        "findings": [f.model_dump() for f in findings],
        "reasons": [r.model_dump() for r in state.get("reasons", [])],
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


def write_results(path: str | Path, records: Iterable[dict[str, Any]]) -> None:
    """One JSON line per document. The file is overwritten on each invocation."""
    with open(path, "w") as fh:
        for r in records:
            fh.write(json.dumps(r, default=str) + "\n")


def summary_table(records: list[dict[str, Any]]) -> str:
    rows = [("file", "decision", "key findings")]
    for r in records:
        keys = [c for c in r["codes"] if not c.startswith("risk_") or c == "risk_score"]
        rows.append((Path(r["file"]).name, r["decision"], ", ".join(keys) or "-"))
    w = [max(len(row[i]) for row in rows) for i in range(2)]
    lines = [f"{a:<{w[0]}}  {b:<{w[1]}}  {c}" for a, b, c in rows]
    lines.insert(1, "-" * len(lines[0]))
    return "\n".join(lines)
