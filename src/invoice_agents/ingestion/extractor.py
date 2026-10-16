"""Document -> Invoice.

* json/csv/xml: pure code mapping.
* txt/pdf with an LLM: structured extraction, Pydantic validation with error-feedback retries,
  then a normalized grounding check against the source text.
* txt/pdf without an LLM: a small label-based extractor. If it cannot extract confidently the
  result is flagged uncertain (a HOLD blocker downstream). No heroic regex for messy files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..config import MAX_EXTRACT_RETRIES
from ..llm.client import LLMClient, LLMUnavailable, SchemaValidationFailed
from ..logs import jlog
from ..models import Charge, Invoice, LineItem
from ..normalize import number_grounded, numbers_in_text, parse_number, text_grounded
from .parsers import RawDocument


@dataclass
class ExtractionResult:
    invoice: Invoice | None
    uncertain: bool
    method: str  # code | llm | label_fallback
    reasons: list[str] = field(default_factory=list)
    provenance: str | None = None
    attempts: int = 0
    llm_calls: list[dict[str, Any]] = field(default_factory=list)


# --- helpers ---------------------------------------------------------------------------

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y")


def parse_date(value: Any) -> date | None:
    if value is None:
        return None
    s = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return parse_number(str(value))


def _text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _date_with_note(value: Any, label: str, notes: list[str]) -> date | None:
    d = parse_date(value)
    if value not in (None, "") and d is None:
        notes.append(f"{label} not parseable: {value!r}")
    return d


# --- structured formats ----------------------------------------------------------------


def _build_from_flat(flat: dict[str, Any], raw_items: list[dict[str, Any]]) -> Invoice:
    notes: list[str] = []
    vendor = flat.get("vendor")
    if isinstance(vendor, dict):
        vendor = vendor.get("name")
    items: list[LineItem] = []
    for it in raw_items:
        name = _first(it, "item", "name", "description", "sku", "Item")
        qty = _num(_first(it, "quantity", "qty", "Qty"))
        items.append(
            LineItem(
                raw_name=str(name) if name is not None else "",
                sku=None,
                quantity=qty if qty is not None else 0.0,
                unit_price=_num(_first(it, "unit_price", "price", "Unit Price")),
                line_total=_num(_first(it, "amount", "line_total", "Line Total")),
            )
        )
        if qty is None:
            notes.append(f"line {name!r}: quantity missing or unparseable")
    charges = [
        Charge(kind=k, description=k, amount=amt)
        for k in ("shipping", "shipping_cost", "fee", "handling")
        if (amt := _num(flat.get(k))) is not None
    ]
    return Invoice(
        vendor=_text(vendor),
        invoice_number=_text(flat.get("invoice_number")),
        invoice_date=_date_with_note(flat.get("date"), "date", notes),
        due_date=_date_with_note(flat.get("due_date"), "due_date", notes),
        currency=(_text(flat.get("currency")) or "USD").upper(),
        line_items=items,
        charges=charges,
        subtotal=_num(flat.get("subtotal")),
        tax=_num(_first(flat, "tax_amount", "tax")),
        total=_num(flat.get("total")),
        notes=notes,
    )


def extract_structured(doc: RawDocument) -> Invoice:
    s = doc.structured
    if s is None:
        raise ValueError(f"{doc.format} is not a structured format")
    if doc.format == "json":
        items = s.get("line_items") or s.get("items") or []
        return _build_from_flat(s, items if isinstance(items, list) else [])
    if doc.format == "xml":
        root = next(iter(s.values()))
        flat: dict[str, Any] = {}
        if isinstance(root, dict):
            for k, v in root.items():
                if isinstance(v, dict) and k != "line_items":
                    flat.update(v)
                elif k != "line_items":
                    flat[k] = v
            li = root.get("line_items", {})
            items = li.get("item", []) if isinstance(li, dict) else []
            if isinstance(items, dict):
                items = [items]
            return _build_from_flat(flat, items)
        return _build_from_flat({}, [])
    if doc.format == "csv":
        if s["layout"] == "kv":
            return _build_from_flat(s["fields"], s["items"])
        if s["layout"] == "table":
            meta = {
                "invoice_number": s["meta"].get("Invoice Number"),
                "vendor": s["meta"].get("Vendor"),
                "date": s["meta"].get("Date"),
                "due_date": s["meta"].get("Due Date"),
            }
            footer = s["footer"]
            meta["subtotal"] = footer.get("subtotal")
            meta["tax"] = next((v for k, v in footer.items() if k.startswith("tax")), None)
            meta["total"] = footer.get("total")
            return _build_from_flat(meta, s["items"])
    raise ValueError(f"cannot map structured {doc.format} layout")


# --- label-based fallback --------------------------------------------------------------

_MONEY = r"\$?\s*([\d,]+(?:\.\d+)?)"
_ITEM_A = re.compile(
    r"^\s*(?P<name>\S.*?)\s+qty:?\s*(?P<qty>\d+(?:\.\d+)?)\s+unit\s*price:?\s*" + _MONEY + r"\s*$",
    re.I,
)
_ITEM_B = re.compile(
    r"^\s*(?P<name>[A-Za-z][\w ()\-]*?)\s+(?P<qty>\d+)\s+\$(?P<price>[\d,]+\.\d{2})"
    r"\s+\$(?P<amt>[\d,]+\.\d{2})(?:\s+[A-Za-z].*)?$"  # optional trailing notes column
)


def _label(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text, re.I | re.M)
    return m.group(1).strip() if m else None


def extract_labelled(doc: RawDocument) -> ExtractionResult:
    """Best-effort extraction for plainly labelled text. Messy files come back uncertain."""
    t = doc.raw_text
    # Labels may share a line ("Vendor: X Due: Y"), so the vendor value stops at the next label.
    vendor = _label(t, r"^\s*vendor\s*:\s*(.+?)(?=\s+(?:due|date|terms)\b\s*:|$)")
    number = (
        _label(t, r"^\s*invoice\s*(?:number|no\.?)\s*[:#]\s*([\w-]+)")
        or _label(t, r"^\s*invoice\s*#\s*([\w-]+)")
        or _label(t, r"^\s*invoice\s*:\s*([\w-]+)")
    )
    items: list[LineItem] = []
    for line in t.splitlines():
        a = _ITEM_A.match(line)
        b = None if a else _ITEM_B.match(line)
        if a:
            items.append(
                LineItem(
                    raw_name=a["name"].strip(),
                    quantity=float(a["qty"]),
                    unit_price=parse_number(a.group(3)),
                )
            )
        elif b:
            items.append(
                LineItem(
                    raw_name=b["name"].strip(),
                    quantity=float(b["qty"]),
                    unit_price=parse_number(b["price"]),
                    line_total=parse_number(b["amt"]),
                )
            )

    def money(label: str) -> float | None:
        v = _label(t, rf"^\s*{label}[^:\n]*:\s*\$?\s*([\d,]+(?:\.\d+)?)")
        return parse_number(v) if v else None

    shipping = money("shipping")
    charges = [Charge(kind="shipping", description="Shipping", amount=shipping)] if shipping else []
    notes: list[str] = []
    inv = Invoice(
        vendor=vendor,
        invoice_number=number,
        invoice_date=_date_with_note(
            _label(t, r"^\s*date\s*:\s*(.+)$") or _label(t, r"(?<!due )\bdate\s*:\s*(\d[\w/-]+)"),
            "date",
            notes,
        ),
        due_date=_date_with_note(
            _label(t, r"^\s*due(?:\s*date)?\s*:\s*(.+?)(?=\s+\w+\s*:|$)")
            or _label(t, r"\bdue\s*:\s*(\d[\w/-]+)"),
            "due_date",
            notes,
        ),
        currency="EUR" if re.search(r"\bEUR\b|€", t) else "USD",
        line_items=items,
        charges=charges,
        subtotal=money("subtotal"),
        tax=money(r"(?:sales\s+)?tax"),
        total=money(r"(?:grand\s+)?total(?:\s+amount)?"),
        notes=notes,
    )
    reasons = []
    if not vendor:
        reasons.append("no 'Vendor:' label found")
    if not items:
        reasons.append("no recognizable item lines")
    if inv.total is None:
        reasons.append("no 'Total:' label found")
    # Self-check: an extractor that misses lines must not look confident.
    if items and inv.subtotal is not None and all(i.line_total or i.unit_price for i in items):
        got = sum(
            i.line_total if i.line_total is not None else i.quantity * (i.unit_price or 0)
            for i in items
        )
        if abs(got - inv.subtotal) > 0.01:
            reasons.append(
                f"extracted lines sum to {got:,.2f} but the stated subtotal is "
                f"{inv.subtotal:,.2f} (items probably missed)"
            )
    if reasons:
        return ExtractionResult(inv if (vendor or items) else None, True, "label_fallback", reasons)
    return ExtractionResult(inv, False, "label_fallback")


# --- LLM extraction --------------------------------------------------------------------

EXTRACTION_SYSTEM = (
    "You extract structured data from one vendor invoice. Copy vendor, invoice number, item "
    "names and every number exactly as written in the document. Do not compute, infer or "
    "correct values: if a field is not stated, use null. Discounts are negative charges; "
    "shipping and fees go in charges, not line_items. Reply with JSON matching the schema."
)


def check_grounding(invoice: Invoice, source: str) -> list[str]:
    """Return a description of every stated field that cannot be found in the source."""
    problems: list[str] = []
    nums = numbers_in_text(source)

    def txt(label: str, value: str | None) -> None:
        if value and not text_grounded(value, source):
            problems.append(f"{label} {value!r} not found in source")

    def num(label: str, value: float | None) -> None:
        if value is not None and not number_grounded(value, nums):
            problems.append(f"{label} {value} not found in source")

    txt("vendor", invoice.vendor)
    txt("invoice_number", invoice.invoice_number)
    for i, li in enumerate(invoice.line_items):
        txt(f"line_items[{i}].raw_name", li.raw_name)
        num(f"line_items[{i}].quantity", li.quantity)
        num(f"line_items[{i}].unit_price", li.unit_price)
        num(f"line_items[{i}].line_total", li.line_total)
    for i, ch in enumerate(invoice.charges):
        num(f"charges[{i}].amount", ch.amount)
    num("subtotal", invoice.subtotal)
    num("tax", invoice.tax)
    num("total", invoice.total)
    return problems


def extract_with_llm(doc: RawDocument, client: LLMClient) -> ExtractionResult:
    """Up to 1 + MAX_EXTRACT_RETRIES model calls; any schema or grounding failure feeds the exact
    problem back. If it still fails the result is uncertain."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": EXTRACTION_SYSTEM},
        {"role": "user", "content": f"Invoice document ({doc.format}):\n\n{doc.raw_text}"},
    ]
    calls: list[dict[str, Any]] = []
    last: Invoice | None = None
    reasons: list[str] = []
    prov: str | None = None
    for attempt in range(1 + MAX_EXTRACT_RETRIES):
        step = "" if attempt == 0 else f"retry{attempt}"
        try:
            inv, prov = client.structured("extraction", messages, Invoice, key=doc.key, step=step)
        except SchemaValidationFailed as exc:
            reasons = [f"schema validation failed: {exc.error_text}"]
            calls.append(
                {"agent": "extraction", "step": step or "initial", "outcome": "schema_error"}
            )
            jlog("extraction_retry", reason="schema", attempt=attempt)
            messages += [
                {"role": "assistant", "content": exc.raw},
                {
                    "role": "user",
                    "content": "Your output failed schema validation with this exact error:\n"
                    f"{exc.error_text}\nReturn corrected JSON.",
                },
            ]
            continue
        except LLMUnavailable as exc:
            reasons = [f"LLM unavailable: {exc}"]
            calls.append(
                {"agent": "extraction", "step": step or "initial", "outcome": "unavailable"}
            )
            return ExtractionResult(last, True, "llm", reasons, prov, attempt + 1, calls)
        last = inv
        problems = check_grounding(inv, doc.raw_text)
        calls.append(
            {
                "agent": "extraction",
                "step": step or "initial",
                "provenance": prov,
                "outcome": "grounded" if not problems else "ungrounded",
            }
        )
        if not problems:
            return ExtractionResult(inv, False, "llm", [], prov, attempt + 1, calls)
        reasons = [f"grounding failed: {p}" for p in problems]
        jlog("extraction_retry", reason="grounding", attempt=attempt, problems=problems)
        messages += [
            {"role": "assistant", "content": inv.model_dump_json()},
            {
                "role": "user",
                "content": "These values do not appear in the document:\n- "
                + "\n- ".join(problems)
                + "\nReturn corrected JSON using only values that appear in the document.",
            },
        ]
    return ExtractionResult(last, True, "llm", reasons, prov, 1 + MAX_EXTRACT_RETRIES, calls)


def extract(doc: RawDocument, client: LLMClient | None) -> ExtractionResult:
    if doc.structured is not None:
        try:
            return ExtractionResult(extract_structured(doc), False, "code")
        except Exception as exc:  # malformed structure -> uncertain, never a crash
            return ExtractionResult(None, True, "code", [f"structured mapping failed: {exc}"])
    if client is None:
        return extract_labelled(doc)
    return extract_with_llm(doc, client)
