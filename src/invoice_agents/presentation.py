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


SYSTEM_ISSUE_TEXT = {
    "llm_unavailable": "LLM unavailable",
    "fixture_missing": "no fixture for this document or step",
    "fixture_stale": "fixture is stale (prompt, schema or input changed)",
    "extraction_failed": "extraction failed",
    "item_needs_mapping": "item names need mapping (no inventory search was run)",
    "critic_unresolved": "approval critique did not converge",
    "vp_requires_llm": "VP review requires an LLM",
    "payment_failed": "payment call failed",
}


def _as_dict(f: Finding | dict[str, Any]) -> dict[str, Any]:
    return f.model_dump() if isinstance(f, Finding) else f


def blocking(findings: list[Finding | dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in map(_as_dict, findings) if d["severity"] in ("reject", "hold")]


def cause(decision: str, findings: list[Finding | dict[str, Any]]) -> str:
    """Why the decision is not an approval: 'business' (vendor/invoice defects), 'system'
    (execution failures only), 'both', or '-' when nothing blocks."""
    if decision in ("APPROVE", "DUPLICATE"):
        return "-"
    kinds = {d["kind"] for d in blocking(findings)}
    if kinds == {"execution"}:
        return "system"
    if kinds == {"business"}:
        return "business"
    return "both" if kinds else "-"


def decision_label(decision: str, findings: list[Finding | dict[str, Any]]) -> str:
    """e.g. 'HOLD - system issue: no fixture for this document or step'."""
    c = cause(decision, findings)
    if c in ("system", "both"):
        sys_codes = [d["code"] for d in blocking(findings) if d["kind"] == "execution"]
        text = "; ".join(dict.fromkeys(SYSTEM_ISSUE_TEXT.get(x, x) for x in sys_codes))
        head = "system issue" if c == "system" else "system issue and business reasons"
        return f"{decision} \u2013 {head}: {text}"
    if c == "business":
        return f"{decision} \u2013 business reason"
    return decision


def split_reasons(findings: list[Finding | dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"business": [], "execution": []}
    for d in blocking(findings):
        out[d["kind"]].append(f"{d['code']}: {d['message']}")
    return out
