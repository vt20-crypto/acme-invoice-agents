"""Text helpers shared by user interfaces. No Streamlit imports here, so they are unit-testable."""

from __future__ import annotations

from typing import Any

from .models import Finding

# Decision status is always icon plus text, never color alone.
DECISION_BADGE = {
    "APPROVE": "✅ APPROVE",
    "HOLD": "⏸️ HOLD (needs human review)",
    "REJECT": "❌ REJECT",
    "DUPLICATE": "\U0001f501 DUPLICATE (already processed)",
}
SEVERITY_BADGE = {
    "reject": "❌ Reject blockers",
    "hold": "⚠️ Hold blockers",
    "note": "ℹ️ Notes",
}
PROVENANCE_LABEL = {
    "live": "live model call",
    "authored": "authored fixture (hand-written, not a model output)",
    "recorded": "recorded model response (replayed)",
    "code": "code only (no LLM)",
}


def decision_badge(decision: str) -> str:
    return DECISION_BADGE.get(decision, decision)


def provenance_label(provenance: str | None) -> str:
    return PROVENANCE_LABEL.get(provenance or "code", str(provenance))


def group_findings(findings: list[Finding | dict[str, Any]]) -> dict[str, list[str]]:
    """Findings by severity (reject first), each as one plain-English line."""
    out: dict[str, list[str]] = {"reject": [], "hold": [], "note": []}
    for f in findings:
        d = f.model_dump() if isinstance(f, Finding) else f
        line = d["message"] + (f" ({d['evidence']})" if d.get("evidence") else "")
        out[d["severity"]].append(line)
    return out


def paid_text(record: dict[str, Any]) -> str:
    pay = record.get("payment")
    if pay and pay.get("status") == "paid":
        return f"Yes ({pay['amount']:,.2f})"
    return "No"


def key_codes(record: dict[str, Any]) -> str:
    codes = [c for c in record["codes"] if not c.startswith("risk_") or c == "risk_score"]
    return ", ".join(codes) or "-"
