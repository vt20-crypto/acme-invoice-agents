"""Human review of HOLD items.

A reviewer can approve or reject a held invoice and must leave a note. Approval is not a bypass:
payment still goes through ``pay_invoice`` (idempotency key, USD-only), and an invoice with a
REJECT-severity blocker can never be approved from here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .config import PAYABLE_CURRENCY
from .db import Database
from .models import Decision
from .payments import pay_invoice


class ReviewError(Exception):
    """The requested review action is not allowed."""


@dataclass
class ReviewResult:
    ledger_id: int
    action: str
    outcome: str  # approved_paid | approved_not_paid | rejected | refused
    detail: str = ""


def reasons_for(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Blocking findings (reject/hold severity) recorded for a ledger row."""
    findings = json.loads(row.get("findings_json") or "[]")
    return [f for f in findings if f["severity"] in ("reject", "hold")]


@dataclass
class Eligibility:
    ok: bool
    reasons: list[str]


def approval_eligibility(row: dict[str, Any]) -> Eligibility:
    """Can a human approve this ledger row? Pure function of the stored row, shared by the backend
    check and the UI (which disables the button and shows these reasons)."""
    why: list[str] = []
    if row["decision"] != Decision.HOLD.value or row["review_status"]:
        why.append(f"Not an open HOLD (decision {row['decision']}).")
    if not row.get("vendor"):
        why.append("Vendor is missing.")
    if not (row.get("vendor_norm") and row.get("invoice_number_norm")):
        why.append("Invoice number is missing, so no payment key can be formed.")
    total = row.get("total")
    if total is None or total <= 0:
        why.append("Invoice total is missing or not a positive amount.")
    if (row.get("currency") or "").upper() != PAYABLE_CURRENCY:
        why.append(
            f"Currency {row.get('currency')} is not supported: the payment rail is "
            f"{PAYABLE_CURRENCY}-only and there is no FX conversion."
        )
    findings = json.loads(row.get("findings_json") or "[]")
    if any(f["severity"] == "reject" for f in findings):
        why.append("A REJECT blocker cannot be overridden by a reviewer.")
    unverified = [
        f
        for f in findings
        if f.get("kind") == "execution" and f.get("stage") in ("extraction", "resolve")
    ]
    if unverified:
        why.append(
            "A system issue left fields unverified ("
            + ", ".join(sorted({f["code"] for f in unverified}))
            + "); re-run once it is fixed."
        )
    return Eligibility(not why, why)


def review_queue(db: Database) -> list[dict[str, Any]]:
    return [
        r for r in db.ledger() if r["decision"] == Decision.HOLD.value and not r["review_status"]
    ]


def review_hold(
    db: Database,
    ledger_id: int,
    action: str,
    note: str,
    reviewer: str = "reviewer",
    *,
    payer: Any = None,
) -> ReviewResult:
    if action not in ("approve", "reject"):
        raise ReviewError(f"unknown action {action!r}")
    if not note.strip():
        raise ReviewError("A reviewer note is required.")
    row = db.get_ledger(ledger_id)
    if row is None:
        raise ReviewError(f"No ledger entry #{ledger_id}.")
    if row["decision"] != Decision.HOLD.value or row["review_status"]:
        raise ReviewError(f"Ledger #{ledger_id} is not an open HOLD (decision {row['decision']}).")

    if action == "reject":
        db.update_ledger_review(ledger_id, Decision.REJECT.value, "rejected_by_human")
        db.add_review(ledger_id, action, note, reviewer, "rejected")
        db.audit(
            "review", row["source_file"], "human_review", {"ledger_id": ledger_id, "action": action}
        )
        return ReviewResult(ledger_id, action, "rejected")

    elig = approval_eligibility(row)
    if not elig.ok:
        raise ReviewError("Cannot approve: " + " ".join(elig.reasons))

    pay = pay_invoice(
        db,
        vendor=row["vendor"],
        amount=row["total"],
        currency=row["currency"] or "",
        idempotency_key=f"{row['vendor_norm']}|{row['invoice_number_norm']}",
        payer=payer,
    )
    if pay.status == "paid":
        db.update_ledger_review(ledger_id, Decision.APPROVE.value, "approved_by_human")
        outcome = "approved_paid"
    elif pay.status == "duplicate":
        db.update_ledger_review(ledger_id, None, "approved_by_human_already_paid")
        outcome = "approved_not_paid"
    else:  # refused (non-USD) or failed: the invoice stays on hold
        db.add_review(ledger_id, action, note, reviewer, f"refused: {pay.detail}")
        db.audit(
            "review",
            row["source_file"],
            "human_review",
            {"ledger_id": ledger_id, "payment": pay.status},
        )
        return ReviewResult(ledger_id, action, "refused", pay.detail)
    db.add_review(ledger_id, action, note, reviewer, outcome)
    db.audit("review", row["source_file"], "human_review",
             {"ledger_id": ledger_id, "action": action, "payment": pay.status})  # fmt: skip
    return ReviewResult(ledger_id, action, outcome, pay.detail)
