"""Text helpers shared by user interfaces. No Streamlit imports here, so they are unit-testable."""

from __future__ import annotations

from pathlib import Path
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
        out[d["severity"]].append(finding_line(d))
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
        out[d["kind"]].append(finding_line(d))
    return out


_SYMBOL = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}


def money(amount: float | None, currency: str = "USD") -> str:
    """Display only: '$1,890.00', '\u20ac4,125.00'. No conversion is ever done."""
    if amount is None:
        return "n/a"
    cur = (currency or "USD").upper()
    sym = _SYMBOL.get(cur, f"{cur} ")
    return f"-{sym}{abs(amount):,.2f}" if amount < 0 else f"{sym}{amount:,.2f}"


def display_path(path: str | Path) -> str:
    """Repo-relative path for display, or the bare file name for anything outside the repo."""
    p = Path(path)
    root = Path(__file__).resolve().parent.parent.parent
    if p.is_absolute():
        try:
            return str(p.relative_to(root))
        except ValueError:
            return p.name
    return str(p)


_RISK_TEXT = {
    "risk_pressure_language": "Contains urgency or pressure language",
    "risk_vendor_identity_change": 'The vendor name appears to have changed ("formerly")',
    "risk_prompt_injection": "Contains text that tries to instruct the reviewer",
    "risk_near_threshold": "Total is just under the $10,000 review threshold",
    "risk_round_total": "Total is an exact multiple of $10,000",
}
_STATIC = {
    "missing_vendor": "The vendor name is missing",
    "missing_total": "The invoice total is missing",
    "missing_items": "The invoice has no line items",
    "missing_due_date": "The due date is missing or could not be read",
    "arithmetic_unverifiable": "Some lines have no price, so the arithmetic cannot be checked",
    "revision_before_original": "Looks like a revision, but the original is not processed yet",
    "vp_requires_llm": "Total is over $10,000 and VP review needs an LLM (not available)",
    "critic_unresolved": "The approval review did not settle after the allowed revisions",
    "payment_failed": "The payment call failed",
    "payment_refused": "Payment was refused by the payment guard",
    "llm_unavailable": "The LLM is not available for this step",
    "fixture_missing": "No recorded or authored LLM fixture exists for this document and step",
    "fixture_stale": "The stored LLM fixture no longer matches the current prompt, schema or input",
    "extraction_failed": "The document could not be read reliably by this mode",
}


def plain(f: Finding | dict[str, Any]) -> str:
    """One short human sentence for a finding (the code stays available as a tag)."""
    d = _as_dict(f)
    c, x = d["code"], d.get("data", {})
    cur = x.get("currency", "USD")
    try:
        if c == "stock_exceeded":
            return f"Ordered {x['needed']:g} {x['sku']}, only {x['stock']} in stock"
        if c == "unknown_items":
            return "Not found in inventory: " + ", ".join(x["items"])
        if c == "all_items_unknown":
            return "None of the items exist in inventory: " + ", ".join(x["items"])
        if c == "item_needs_mapping":
            return "Item names still need mapping to inventory: " + ", ".join(x["items"])
        if c == "arithmetic":
            return (
                f"Invoice states {money(x['stated'], cur)} but lines, charges and tax add up to "
                f"{money(x['expected'], cur)}"
            )
        if c == "line_arithmetic":
            return (
                f"{x['item']}: {x['quantity']:g} x {money(x['unit_price'], cur)} = "
                f"{money(x['expected'], cur)}, but the line says {money(x['stated'], cur)}"
            )
        if c == "subtotal_mismatch":
            return (
                f"Stated subtotal {money(x['subtotal'], cur)} does not match the lines "
                f"({money(x['lines'], cur)})"
            )
        if c == "bad_quantity":
            return f"Quantity {x['quantity']:g} for {x['item']} is not a positive number"
        if c == "invalid_due_date":
            return f"Due date {x['due']} is before the invoice date {x['invoice_date']}"
        if c == "non_usd":
            return (
                f"Invoice is in {cur}; the payment rail is USD-only, so it is held "
                "(no currency conversion)"
            )
        if c == "duplicate":
            return (
                f"Same invoice already processed ({x['original_file']}, {x['original_decision']}); "
                "nothing to do"
            )
        if c == "revision":
            delta = ""
            if x.get("original_total") is not None and x.get("new_total") is not None:
                delta = (
                    f": total changed from {money(x['original_total'], cur)} to "
                    f"{money(x['new_total'], cur)} ({x['new_total'] - x['original_total']:+,.2f})"
                )
            paid = "; the original was already paid" if x.get("paid") else ""
            return f"Revision of {x['original_file']}{delta}{paid}"
        if c == "sku_not_in_db":
            return f"Suggested SKU {x['sku']} for {x['item']} does not exist in inventory"
        if c == "risk_invalid_due_date":
            return f"Due date looks invalid ({x['evidence']})"
        if c == "risk_score":
            level = {"reject": "high", "hold": "medium", "note": "low"}[d["severity"]]
            return f"Combined risk score is {x['score']} ({level})"
    except (KeyError, TypeError):
        return d["message"]
    return _RISK_TEXT.get(c) or _STATIC.get(c) or d["message"]


def finding_line(f: Finding | dict[str, Any]) -> str:
    d = _as_dict(f)
    return f"{plain(d)} [{d['code']}]"
