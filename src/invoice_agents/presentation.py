"""Plain-language text helpers shared by the CLI and the UI (no Streamlit imports here)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import Finding

DECISION_BADGE = {
    "APPROVE": "\u2705 Approved",
    "HOLD": "\u23f8\ufe0f Needs review",
    "REJECT": "\u274c Rejected",
    "DUPLICATE": "\U0001f501 Duplicate",
}
PROVENANCE_LABEL = {
    "live": "live model call",
    "recorded": "recorded model response (cache)",
    "code": "code only (no LLM)",
}

SYSTEM_ISSUE_TEXT = {
    "llm_unavailable": "the LLM is not available",
    "cache_missing": "no recorded LLM response for this document and step",
    "extraction_failed": "the document could not be read in this mode",
    "item_needs_mapping": "item names need mapping (no inventory search was run)",
    "critic_unresolved": "the approval review did not settle",
    "vp_requires_llm": "VP review needs an LLM",
    "payment_failed": "the payment call failed",
}


def decision_badge(decision: str) -> str:
    return DECISION_BADGE.get(decision, decision)


def provenance_label(provenance: str | None) -> str:
    return PROVENANCE_LABEL.get(provenance or "code", str(provenance))


def key_codes(record: dict[str, Any]) -> str:
    codes = [c for c in record["codes"] if not c.startswith("risk_") or c == "risk_level"]
    return ", ".join(codes) or "-"


def _as_dict(f: Finding | dict[str, Any]) -> dict[str, Any]:
    return f.model_dump() if isinstance(f, Finding) else f


def blocking(findings: list[Finding | dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in map(_as_dict, findings) if d["severity"] in ("reject", "hold")]


def cause(decision: str, findings: list[Finding | dict[str, Any]]) -> str:
    """Why the decision is not an approval: 'business', 'system' (execution failures only),
    'both', or '-' when nothing blocks."""
    if decision in ("APPROVE", "DUPLICATE"):
        return "-"
    kinds = {d["kind"] for d in blocking(findings)}
    if kinds == {"execution"}:
        return "system"
    if kinds == {"business"}:
        return "business"
    return "both" if kinds else "-"


def decision_label(decision: str, findings: list[Finding | dict[str, Any]]) -> str:
    """e.g. 'HOLD \u2013 system issue: no recorded LLM response for this document and step'."""
    c = cause(decision, findings)
    if c in ("system", "both"):
        codes = [d["code"] for d in blocking(findings) if d["kind"] == "execution"]
        text = "; ".join(dict.fromkeys(SYSTEM_ISSUE_TEXT.get(x, x) for x in codes))
        return f"{decision} \u2013 system issue: {text}"
    return decision


def split_reasons(findings: list[Finding | dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"business": [], "execution": []}
    for d in blocking(findings):
        out[d["kind"]].append(finding_line(d))
    return out


def reason_sentence(decision: str, findings: list[Finding | dict[str, Any]]) -> str:
    """One plain sentence for the decision card."""
    if decision == "APPROVE":
        return "All checks passed. Payment sent."
    if decision == "DUPLICATE":
        dup = next((d for d in map(_as_dict, findings) if d["code"] == "duplicate"), None)
        return plain(dup) if dup else "Already processed."
    block = blocking(findings)
    if not block:
        return "No reason recorded."
    block.sort(key=lambda d: d["severity"] != "reject")
    first = plain(block[0])
    more = f" (+{len(block) - 1} more)" if len(block) > 1 else ""
    return f"{first}{more}. Not paid."


def vendor_message(
    vendor: str | None, number: str | None, findings: list[Finding | dict[str, Any]]
) -> str:
    """Simple template for a rejected invoice (a person sends it; nothing is sent automatically)."""
    reasons = [f"- {plain(d)}" for d in blocking(findings) if d["kind"] == "business"]
    body = "\n".join(reasons) or "- The invoice could not be validated."
    return (
        f"Dear {vendor or 'vendor'},\n\n"
        f"We could not process invoice {number or '(number missing)'} for these reasons:\n"
        f"{body}\n\nPlease correct the issues and resubmit.\n\nAccounts Payable, Acme Corp"
    )


_SYMBOL = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}


def money(amount: float | None, currency: str = "USD") -> str:
    """Display only: '$1,890.00', '\u20ac4,125.00'. No conversion is ever done."""
    if amount is None:
        return "\u2014"
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


RULE_WORDS = {
    "prompt_injection": "instructions aimed at the system hidden in the invoice",
    "pressure_language": "pressure or urgency wording",
    "vendor_identity_change": "the vendor name changed",
    "invalid_due_date": "an invalid due date",
    "near_threshold": "total just under $10,000",
}
_RISK_TEXT = {
    "risk_pressure_language": "Contains urgency or pressure language",
    "risk_vendor_identity_change": 'The vendor name appears to have changed ("formerly")',
    "risk_prompt_injection": "Contains text that tries to instruct the reviewer or the system",
    "risk_near_threshold": "Total is just under the $10,000 review threshold",
}
_STATIC = {
    "missing_vendor": "The vendor name is missing",
    "missing_total": "The invoice total is missing",
    "missing_items": "The invoice has no line items",
    "missing_due_date": "The due date is missing or could not be read",
    "arithmetic_unverifiable": "Some lines have no price, so the arithmetic cannot be checked",
    "revision_before_original": "Looks like a revision, but the original is not processed yet",
    "vp_requires_llm": "Total is over $10,000 and VP review needs an LLM (not available)",
    "critic_unresolved": "The approval review did not settle after the allowed revision",
    "payment_failed": "The payment call failed",
    "payment_refused": "Payment was refused by the payment guard",
    "llm_unavailable": "The LLM is not available for this step",
    "cache_missing": "No recorded LLM response exists for this document and step",
    "extraction_failed": "The document could not be read reliably in this mode",
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
        if c == "risk_level":
            words = ", ".join(RULE_WORDS.get(r, r) for r in x["rules"])
            return f"Risk is {x['level']}: {words}"
    except (KeyError, TypeError):
        return d["message"]
    return _RISK_TEXT.get(c) or _STATIC.get(c) or d["message"]


def finding_line(f: Finding | dict[str, Any]) -> str:
    d = _as_dict(f)
    return f"{plain(d)} [{d['code']}]"


def timeline(state: dict[str, Any]) -> list[dict[str, str]]:
    """Four steps for the decision card: Ingestion, Validation, Approval, Payment.
    Each is {step, icon, line}; icon is one of check / warn / fail / dash."""
    findings = [_as_dict(f) for f in state.get("findings", [])]
    ex = state.get("extraction")
    decision = state["decision"].value

    # Ingestion
    ing = [d for d in findings if d.get("stage") == "extraction"]
    if ing:
        steps = [{"step": "Ingestion", "icon": "fail", "line": plain(ing[0]) + "."}]
    elif ex is not None:
        how = {
            "code": "read directly",
            "llm": "read by the model",
            "label_fallback": "read by rules",
        }
        fmt = state["doc"].format.upper()
        steps = [
            {"step": "Ingestion", "icon": "check", "line": f"{fmt} {how.get(ex.method, ex.method)}"}
        ]
    else:
        steps = [{"step": "Ingestion", "icon": "dash", "line": "Not run"}]

    # Validation
    if ing:
        steps.append({"step": "Validation", "icon": "dash", "line": "Skipped: nothing to validate"})
    else:
        if decision == "DUPLICATE":
            dup = next((d for d in findings if d["code"] == "duplicate"), None)
            steps.append(
                {"step": "Validation", "icon": "warn", "line": plain(dup) if dup else "Duplicate"}
            )
        else:
            biz = [
                d
                for d in findings
                if d["severity"] in ("reject", "hold")
                and d.get("stage") != "approval"
                and d["code"] not in ("vp_hold", "vp_reject")
            ]
            if not biz:
                steps.append({"step": "Validation", "icon": "check", "line": "All checks passed"})
            else:
                biz.sort(key=lambda d: d["severity"] != "reject")
                icon = "fail" if biz[0]["severity"] == "reject" else "warn"
                steps.append({"step": "Validation", "icon": icon, "line": plain(biz[0])})

    # Approval
    appr = state.get("approval")
    if appr is None:
        steps.append({"step": "Approval", "icon": "dash", "line": "Not needed"})
    elif appr.llm_calls:
        critic = " and critic" if appr.critic_ran else ""
        word = {"APPROVE": "approve", "HOLD": "hold", "REJECT": "reject"}[appr.decision.value]
        icon = "check" if appr.decision.value == "APPROVE" else "warn"
        steps.append({"step": "Approval", "icon": icon, "line": f"VP{critic}: {word}"})
    elif appr.decision.value == "APPROVE":
        steps.append({"step": "Approval", "icon": "check", "line": "Approved by rules (no LLM)"})
    else:
        steps.append(
            {"step": "Approval", "icon": "dash", "line": "Not requested: blocked by checks"}
        )

    # Payment
    pay = state.get("payment")
    if pay and pay["status"] == "paid":
        steps.append(
            {
                "step": "Payment",
                "icon": "check",
                "line": f"Paid {money(pay['amount'], pay.get('currency', 'USD'))}",
            }
        )
    else:
        steps.append({"step": "Payment", "icon": "dash", "line": "Not sent"})
    return steps


def extracted_lines(state: dict[str, Any]) -> list[str]:
    """Plain 'Label: value' lines for the extracted fields; '—' for anything missing."""
    inv = state.get("invoice")
    if inv is None:
        return ["Nothing could be extracted."]
    cur = inv.currency
    res = state.get("resolve")
    out = [
        f"Vendor: {inv.vendor or '—'}",
        f"Invoice number: {inv.invoice_number or '—'}",
        f"Invoice date: {inv.invoice_date or '—'}   Due: {inv.due_date or '—'}",
    ]
    for i, li in enumerate(inv.line_items):
        sku = res.proposed[i] if res and i < len(res.proposed) else None
        mapped = f" → {sku}" if sku else ""
        out.append(
            f"Item: {li.raw_name}{mapped} · qty {li.quantity:g} · {money(li.unit_price, cur)}"
        )
    for c in inv.charges:
        out.append(f"{c.kind.title()}: {money(c.amount, cur)}")
    out += [f"Tax: {money(inv.tax, cur)}", f"Total: {money(inv.total, cur)}"]
    return out
