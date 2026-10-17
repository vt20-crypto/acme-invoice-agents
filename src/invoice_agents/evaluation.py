"""Expected-vs-actual evaluation for one execution mode.

``tests/regression/expected.yaml`` holds the documented baseline decisions. Modes are always
evaluated separately; an entry may carry a ``none`` decision for the rules-only run.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .batch import collect_files, result_record, run_batch
from .db import Database
from .llm.client import make_client

ROOT = Path(__file__).resolve().parent.parent.parent
EXPECTED_PATH = ROOT / "tests" / "regression" / "expected.yaml"
SAMPLE_DIRS = [ROOT / "data" / "invoices", ROOT / "data" / "synthetic"]


def load_expected() -> list[dict[str, Any]]:
    import yaml  # dev dependency: only the evaluation command and the tests read expected.yaml

    return yaml.safe_load(EXPECTED_PATH.read_text())


def evaluate(mode: str, cache_dir: str | Path | None = None) -> dict[str, Any]:
    """Run all sample documents in the defined order in a throwaway DB and compare with
    expected.yaml. Returns rows, payments and a summary."""
    expected = {Path(e["file"]).name: e for e in load_expected()}
    client = make_client(mode, cache_dir or ROOT / "cache" / "llm")
    files = collect_files(SAMPLE_DIRS)
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "eval.db")
        try:
            results = run_batch(files, db, client, mode)
            payments = db.payments()
        finally:
            db.close()
    rows = []
    for state, ctx in results:
        rec = result_record(state, ctx.run_id)
        name = Path(state["path"]).name
        exp = expected.get(name, {})
        want = exp.get("none", exp.get("decision")) if mode == "none" else exp.get("decision")
        rows.append(
            {
                "file": name,
                "expected": want,
                "actual": rec["decision"],
                "match": want == rec["decision"],
                "why_expected": exp.get("why", ""),
                "reason": rec["reason"],
                "cause": rec["cause"],
                "codes": rec["codes"],
                "paid": (rec["payment"] or {}).get("status") == "paid",
                "total": rec["total"],
                "currency": rec["currency"],
            }
        )
    matched = sum(r["match"] for r in rows)
    return {
        "rows": rows,
        "payments": payments,
        "matched": matched,
        "total": len(rows),
        "summary": f"{matched}/{len(rows)} match",
    }
