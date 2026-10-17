"""Deterministic validation. Pure code, authoritative: nothing an LLM says can override it."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .config import PAYABLE_CURRENCY, TOLERANCE
from .db import Database
from .models import Finding, Invoice, LineItem
from .normalize import canon_item_name, norm_key


def vendor_key(invoice: Invoice) -> str | None:
    return norm_key(invoice.vendor) if invoice.vendor else None


def number_key(invoice: Invoice) -> str | None:
    return norm_key(invoice.invoice_number) if invoice.invoice_number else None


def effective_line_total(li: LineItem) -> float | None:
    if li.line_total is not None:
        return li.line_total
    if li.unit_price is not None:
        return round(li.quantity * li.unit_price, 2)
    return None


def code_resolve(invoice: Invoice, db: Database) -> list[str | None]:
    """Code-only item resolution: exact match after normalization ('Widget A' -> WidgetA)."""
    by_canon = {canon_item_name(r["item"]): r["item"] for r in db.inventory_all()}
    return [by_canon.get(canon_item_name(li.raw_name)) for li in invoice.line_items]


def content_hash(invoice: Invoice) -> str:
    """Hash of the economic content only, so the .txt and .pdf of one invoice compare equal."""
    lines = sorted(
        (canon_item_name(li.raw_name), round(li.quantity, 4), effective_line_total(li))
        for li in invoice.line_items
    )
    charges = sorted((c.kind.casefold(), round(c.amount, 2)) for c in invoice.charges)
    payload = {
        "lines": lines,
        "charges": charges,
        "tax": round(invoice.tax or 0.0, 2),
        "total": None if invoice.total is None else round(invoice.total, 2),
        "currency": invoice.currency.upper(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass
class ValidationResult:
    findings: list[Finding] = field(default_factory=list)
    resolved: list[str | None] = field(default_factory=list)
    facts: dict[str, str] = field(default_factory=dict)
    content_hash: str | None = None
    duplicate_of: int | None = None


def _money(x: float) -> str:
    return f"{x:,.2f}"


def validate(
    invoice: Invoice,
    proposed: list[str | None],
    db: Database,
    searched: list[bool] | None = None,
) -> ValidationResult:
    """``searched[i]`` says whether an LLM inventory search was actually run for line i. A name
    that code could not resolve and nobody searched is a coverage gap (``item_needs_mapping``),
    not evidence that the item is fake."""
    res = ValidationResult()
    f = res.findings

    def add(code: str, severity: Any, message: str, evidence: str = "", stage: str = "") -> None:
        f.append(
            Finding(code=code, severity=severity, message=message, evidence=evidence, stage=stage)
        )

    # 1. Re-check every proposed SKU exactly against the DB; an unknown SKU is simply unknown.
    for i, sku in enumerate(proposed):
        item = db.get_item(sku) if sku else None
        if sku and not item:
            add(
                "sku_not_in_db",
                "note",
                f"Proposed SKU {sku!r} is not in inventory; ignored.",
                invoice.line_items[i].raw_name,
            )
        res.resolved.append(sku if item else None)
    res.content_hash = content_hash(invoice)

    # 2. Duplicate vs revision (runs first; a copy of a known invoice is not re-validated).
    vk, nk = vendor_key(invoice), number_key(invoice)
    if vk and nk:
        orig = db.find_original(vk, nk)
        if orig:
            if orig["content_hash"] == res.content_hash:
                res.duplicate_of = int(orig["id"])
                add(
                    "duplicate",
                    "note",
                    f"Same vendor, invoice number and content as ledger #{orig['id']} "
                    f"({orig['source_file']}, {orig['decision']}).",
                )
                res.facts["check.duplicate"] = f"duplicate of ledger #{orig['id']}"
                return res
            delta = ""
            if orig["total"] is not None and invoice.total is not None:
                delta = (
                    f" Total changed {_money(orig['total'])} -> {_money(invoice.total)} "
                    f"(delta {invoice.total - orig['total']:+,.2f})."
                )
            paid = " The original was paid." if db.payment_exists(f"{vk}|{nk}") else ""
            add(
                "revision",
                "hold",
                f"Revision of already-processed invoice ledger #{orig['id']} "
                f"({orig['source_file']}, {orig['decision']}).{delta}{paid}",
            )
    res.facts["check.duplicate"] = (
        "no prior ledger entry"
        if not (vk and nk and db.find_original(vk, nk))
        else "key seen before with changed content (revision)"
    )

    # 3. Required fields.
    if not invoice.vendor:
        add("missing_vendor", "reject", "Vendor is missing.")
    if invoice.total is None:
        add("missing_total", "reject", "Invoice total is missing.")
    if not invoice.line_items:
        add("missing_items", "reject", "Invoice has no line items.")
    if invoice.due_date is None:
        add("missing_due_date", "hold", "Due date is missing or not parseable.")
    elif invoice.invoice_date and invoice.due_date < invoice.invoice_date:
        add(
            "invalid_due_date",
            "hold",
            f"Due date {invoice.due_date} is before invoice date {invoice.invoice_date}.",
        )

    # 4. Quantities.
    for li in invoice.line_items:
        if li.quantity <= 0:
            add(
                "bad_quantity",
                "reject",
                f"Quantity {li.quantity:g} for {li.raw_name!r} is not positive.",
            )

    # 5. Arithmetic.
    unverifiable = False
    line_sum = 0.0
    for li in invoice.line_items:
        eff = effective_line_total(li)
        if eff is None:
            unverifiable = True
            continue
        line_sum += eff
        if li.line_total is not None and li.unit_price is not None:
            expect = round(li.quantity * li.unit_price, 2)
            if abs(expect - li.line_total) > TOLERANCE:
                add(
                    "line_arithmetic",
                    "reject",
                    f"{li.raw_name}: {li.quantity:g} x {li.unit_price:g} = {_money(expect)}, "
                    f"but line total is {_money(li.line_total)}.",
                )
    charge_sum = sum(c.amount for c in invoice.charges)
    tax = invoice.tax or 0.0
    if (
        invoice.subtotal is not None
        and not unverifiable
        and abs(invoice.subtotal - line_sum) > TOLERANCE
    ):
        add(
            "subtotal_mismatch",
            "reject",
            f"Subtotal {_money(invoice.subtotal)} != sum of lines {_money(line_sum)}.",
        )
    if invoice.total is not None and invoice.line_items:
        if unverifiable:
            add(
                "arithmetic_unverifiable",
                "hold",
                "Some lines have neither a line total nor a unit price; cannot check arithmetic.",
            )
        else:
            expected = line_sum + charge_sum + tax
            ok = abs(invoice.total - expected) <= TOLERANCE
            if not ok:
                add(
                    "arithmetic",
                    "reject",
                    f"Total {_money(invoice.total)} != lines {_money(line_sum)} + charges "
                    f"{_money(charge_sum)} + tax {_money(tax)} = {_money(expected)}.",
                )
            res.facts["validation.arithmetic"] = (
                f"total {_money(invoice.total)} = lines {_money(line_sum)} + charges "
                f"{_money(charge_sum)} + tax {_money(tax)}"
                if ok
                else "arithmetic does not reconcile"
            )

    # 6. Inventory: unknown items and stock (aggregated per SKU).
    unknown = [
        li.raw_name for li, sku in zip(invoice.line_items, res.resolved, strict=True) if not sku
    ]
    searched = searched or [False] * len(invoice.line_items)
    unsearched = [
        li.raw_name
        for li, sku, s in zip(invoice.line_items, res.resolved, searched, strict=True)
        if not sku and not s
    ]
    searched_unknown = [n for n in unknown if n not in unsearched]
    if invoice.line_items:
        if unsearched:
            add(
                "item_needs_mapping",
                "hold",
                "Item name(s) could not be matched by code and no inventory search was run, "
                "so they need mapping.",
                ", ".join(unsearched),
                stage="resolve",
            )
        if searched_unknown and len(unknown) == len(invoice.line_items):
            add(
                "all_items_unknown", "reject", "No line item matches inventory.", ", ".join(unknown)
            )
        elif searched_unknown:
            add(
                "unknown_items",
                "hold",
                "Some line items are not in inventory.",
                ", ".join(searched_unknown),
            )
    res.facts["validation.items"] = (
        f"{len(invoice.line_items) - len(unknown)} of {len(invoice.line_items)} line items "
        "resolved to inventory SKUs"
    )
    need: dict[str, float] = defaultdict(float)
    for li, sku in zip(invoice.line_items, res.resolved, strict=True):
        if sku and li.quantity > 0:
            need[sku] += li.quantity
    for sku, qty in sorted(need.items()):
        stock = (db.get_item(sku) or {}).get("stock", 0)
        res.facts[f"stock.{sku}"] = f"needs {qty:g}, on hand {stock}"
        if qty > stock:
            add(
                "stock_exceeded", "hold", f"{sku}: invoice needs {qty:g} but only {stock} in stock."
            )

    # 7. Currency.
    if invoice.currency.upper() != PAYABLE_CURRENCY:
        add(
            "non_usd",
            "hold",
            f"Currency {invoice.currency} cannot be paid on the USD-only payment rail.",
        )

    # 8. Facts for the approval agents.
    res.facts.update(
        {
            "invoice.vendor": invoice.vendor or "(missing)",
            "invoice.number": invoice.invoice_number or "(missing)",
            "invoice.total": "(missing)" if invoice.total is None else _money(invoice.total),
            "invoice.currency": invoice.currency,
            "invoice.due_date": str(invoice.due_date) if invoice.due_date else "(missing)",
        }
    )
    blockers = [x.code for x in f if x.severity in ("reject", "hold")]
    res.facts["check.blockers"] = ", ".join(blockers) if blockers else "none"
    for x in f:
        if x.severity == "note":
            res.facts[f"finding.{x.code}"] = x.message
    return res
