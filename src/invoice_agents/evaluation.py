"""Evaluation of one execution mode against the PLAN section 4 baseline (developer view).

The baseline (docs/baseline_plan4.json) is a documented policy baseline, not ground truth.
Modes are always evaluated separately; results of different modes are never combined.
"""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from .batch import collect_files, result_record, run_batch
from .db import Database
from .llm.client import FixtureClient

ROOT = Path(__file__).resolve().parent.parent.parent
BASELINE_PATH = ROOT / "docs" / "baseline_plan4.json"
CLASSES = (
    "match",
    "expected exception",
    "missing fixture",
    "stale fixture",
    "mismatch",
    "no baseline",
)


def load_baseline() -> dict[str, str | None]:
    return json.loads(BASELINE_PATH.read_text())


def classify(mode: str, record: dict[str, Any], baseline: str | None) -> str:
    """Failure class of one result against the section 4 decision."""
    if baseline is None:
        return "no baseline"
    codes = set(record["codes"])
    if record["cause"] in ("system", "both") or record["execution_failures"]:
        if "fixture_stale" in codes:
            return "stale fixture"
        if "fixture_missing" in codes:
            return "missing fixture"
        return "expected exception"
    return "match" if record["decision"] == baseline else "mismatch"


def evaluate(mode: str, fixtures: Path | None = None) -> list[dict[str, Any]]:
    """Run all sample documents in the defined order in a throwaway DB and classify each."""
    if mode not in ("none", "fixture"):
        raise ValueError("only none and fixture can run offline; live needs an API key")
    baseline = load_baseline()
    client = FixtureClient(fixtures or ROOT / "fixtures" / "llm") if mode == "fixture" else None
    files = collect_files([ROOT / "data" / "invoices", ROOT / "data" / "synthetic"])
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "eval.db")
        try:
            results = run_batch(files, db, client, mode)
        finally:
            db.close()
    rows = []
    for state, ctx in results:
        rec = result_record(state, ctx.run_id)
        key = str(Path(state["path"]).relative_to(ROOT))
        rows.append(
            {
                "file": Path(state["path"]).name,
                "actual": rec["decision"],
                "baseline": baseline.get(key) or "none",
                "class": classify(mode, rec, baseline.get(key)),
                "cause": rec["cause"],
                "codes": rec["codes"],
            }
        )
    return rows


def counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    c = Counter(r["class"] for r in rows)
    return {k: c[k] for k in CLASSES if c[k]}
