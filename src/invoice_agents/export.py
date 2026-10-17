"""Result rows for download (CSV / JSON) and the expected-decision comparison.

Pure functions shared by the CLI and the Streamlit app; no Streamlit imports here.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from .presentation import blocking, ledger_reason, plain

RESULT_COLUMNS = [
    "file",
    "invoice_number",
    "vendor",
    "total",
    "currency",
    "decision",
    "reasons",
    "processed_at",
    "ai_provenance",
    "paid",
]
EXPECTED_COLUMNS = ["file", "expected_decision"]


def ai_provenance(calls: list[dict[str, Any]]) -> str:
    """One word for how the model was used in a run: 'recorded', 'live', 'recorded+live',
    'rules_only' (no recorded response and no key) or 'code' (no model step ran)."""
    seen = {c.get("provenance") or ("rules_only" if c.get("outcome") == "rules_only" else None)
            for c in calls}  # fmt: skip
    seen.discard(None)
    return "+".join(sorted(seen)) if seen else "code"


def _reasons(row: dict[str, Any], findings: list[dict[str, Any]]) -> str:
    if row.get("review_status"):
        return ledger_reason(row)
    if row["decision"] == "APPROVE":
        return "All checks passed"
    if row["decision"] == "DUPLICATE":
        dup = next((f for f in findings if f["code"] == "duplicate"), None)
        return plain(dup) if dup else "Already processed"
    return "; ".join(plain(f) for f in blocking(findings)) or "No reason recorded"


def result_rows(
    ledger: list[dict[str, Any]], payments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One download row per ledger row, oldest first. ``paid`` is 'yes' only for a row whose
    final decision is APPROVE and whose payment key was sent (a duplicate is never 'paid')."""
    sent = {p["idempotency_key"] for p in payments if p["status"] == "sent"}
    out = []
    for r in sorted(ledger, key=lambda r: r["id"]):
        findings = json.loads(r.get("findings_json") or "[]")
        details = json.loads(r.get("details_json") or "{}")
        key = f"{r['vendor_norm']}|{r['invoice_number_norm']}"
        out.append(
            {
                "file": Path(r["source_file"]).name,
                "invoice_number": r["invoice_number"] or "",
                "vendor": r["vendor"] or "",
                "total": r["total"],
                "currency": r["currency"] or "",
                "decision": r["decision"],
                "reasons": _reasons(r, findings),
                "processed_at": r["processed_at"],
                "ai_provenance": details.get("ai_provenance", ""),
                "paid": "yes" if r["decision"] == "APPROVE" and key in sent else "no",
            }
        )
    return out


def to_csv(rows: list[dict[str, Any]], columns: list[str] = RESULT_COLUMNS) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def to_json(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, indent=2) + "\n"


def write_results(rows: list[dict[str, Any]], path: str | Path) -> None:
    """CSV, or JSON when the path ends in .json."""
    p = Path(path)
    p.write_text(to_json(rows) if p.suffix.lower() == ".json" else to_csv(rows))


def expected_template(files: list[str]) -> str:
    """CSV with one line per processed file name and a blank expected_decision to fill in."""
    return to_csv([{"file": f, "expected_decision": ""} for f in files], EXPECTED_COLUMNS)


def parse_expected(text: str) -> list[dict[str, str]]:
    """Rows of {file, expected_decision} from a CSV. Header names are matched ignoring case and
    spacing; a missing column raises ValueError with a message meant for the user."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    names = {(n or "").strip().lower(): n for n in reader.fieldnames or []}
    missing = [c for c in EXPECTED_COLUMNS if c not in names]
    if missing:
        raise ValueError(
            "The CSV needs the columns file and expected_decision "
            f"(missing: {', '.join(missing)}; found: {', '.join(names) or 'none'})."
        )
    return [
        {c: (row.get(names[c]) or "").strip() for c in EXPECTED_COLUMNS}
        for row in reader
        if any((v or "").strip() for v in row.values())
    ]


def compare_expected(processed: list[dict[str, Any]], expected_text: str) -> dict[str, Any]:
    """Match expected rows to processed ones by file name (basename, case-insensitive) and compare
    decisions (case and spacing ignored). ``processed`` needs ``file`` and ``decision``; when a name
    occurs more than once the last one wins. Returns the compared rows, ``matched``, ``total`` (rows
    that could be compared) and the ``not_processed`` file names."""
    actual = {Path(p["file"]).name.casefold(): p["decision"] for p in processed}
    rows, missing = [], []
    for e in parse_expected(expected_text):
        name = Path(e["file"]).name
        if name.casefold() not in actual:
            missing.append(name)
            continue
        want, got = e["expected_decision"].strip().upper(), actual[name.casefold()]
        rows.append({"file": name, "expected": want, "actual": got, "match": want == got})
    matched = sum(r["match"] for r in rows)
    return {"rows": rows, "matched": matched, "total": len(rows), "not_processed": missing}


def summary_line(cmp: dict[str, Any]) -> str:
    return f"{cmp['matched']} of {cmp['total']} match"
