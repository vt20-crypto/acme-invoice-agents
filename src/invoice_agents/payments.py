"""Guarded payment. Only code calls this, only after an APPROVE, and only once per invoice key."""

from __future__ import annotations

import contextlib
import io
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import PAYABLE_CURRENCY
from .db import Database


def mock_payment(vendor, amount):
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}


@dataclass
class PaymentResult:
    status: str  # paid | duplicate | refused | failed
    detail: str = ""
    output: str = ""  # what the payer printed (mock_payment prints a line); captured, not leaked


def pay_invoice(
    db: Database,
    *,
    vendor: str | None,
    amount: float | None,
    currency: str,
    idempotency_key: str,
    payer: Callable[[str, float], Any] | None = None,
) -> PaymentResult:
    """Last line of defence: refuse unless vendor, invoice number (in the key), a positive amount
    and USD are all present, whatever any earlier step decided. Then insert the payment row first
    (UNIQUE key); call the payer only if the insert succeeded."""
    if not (vendor or "").strip():
        return PaymentResult("refused", "vendor is missing")
    if not idempotency_key or not all(part.strip() for part in idempotency_key.split("|")):
        return PaymentResult("refused", "invoice number is missing, so there is no payment key")
    if amount is None or not amount > 0:
        return PaymentResult("refused", "amount is missing or not positive")
    if currency.upper() != PAYABLE_CURRENCY:
        return PaymentResult("refused", f"payment rail is {PAYABLE_CURRENCY}-only, got {currency}")
    try:
        payment_id = db.insert_payment(idempotency_key, amount, currency.upper())
    except sqlite3.IntegrityError:
        return PaymentResult("duplicate", f"payment key {idempotency_key!r} already used")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):  # keep the CLI summary in order
            result = (payer or mock_payment)(vendor, amount)
    except Exception as exc:
        db.set_payment_status(payment_id, "failed")
        return PaymentResult("failed", f"{type(exc).__name__}: {exc}", buf.getvalue().strip())
    ok = isinstance(result, dict) and result.get("status") == "success"
    db.set_payment_status(payment_id, "sent" if ok else "failed")
    return PaymentResult("paid" if ok else "failed", str(result), buf.getvalue().strip())
