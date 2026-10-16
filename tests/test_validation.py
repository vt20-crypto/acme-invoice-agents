from conftest import INVOICES

from invoice_agents.ingestion.extractor import extract, extract_labelled
from invoice_agents.ingestion.parsers import parse_file
from invoice_agents.models import Charge, Invoice, LineItem
from invoice_agents.validation import code_resolve, content_hash, number_key, validate, vendor_key


def make(**kw) -> Invoice:
    base = dict(
        vendor="Widgets Inc.",
        invoice_number="INV-1",
        invoice_date="2026-01-15",
        due_date="2026-02-01",
        line_items=[LineItem(raw_name="WidgetA", quantity=10, unit_price=250.0)],
        subtotal=2500.0,
        tax=0.0,
        total=2500.0,
    )
    base.update(kw)
    return Invoice(**base)


def run(inv, db, proposed=None):
    return validate(inv, proposed or code_resolve(inv, db), db)


def codes(res, severity=None):
    return {f.code for f in res.findings if severity in (None, f.severity)}


def test_clean_invoice_has_no_blockers(db):
    res = run(make(), db)
    assert not codes(res, "reject") and not codes(res, "hold")


def test_arithmetic_with_shipping_charge_reconciles(db):
    inv = extract_labelled(parse_file(INVOICES / "invoice_1010.txt")).invoice
    res = run(inv, db)
    assert not codes(res, "reject"), res.findings
    assert "arithmetic" not in codes(res)


def test_arithmetic_fails_if_charge_is_not_counted(db):
    inv = make(charges=[Charge(kind="shipping", description="s", amount=150.0)], total=2650.0)
    assert not codes(run(inv, db), "reject")
    inv_bad = make(total=2650.0)  # same total but no charge line to explain it
    assert "arithmetic" in codes(run(inv_bad, db), "reject")


def test_line_total_must_match_qty_times_price(db):
    inv = make(
        line_items=[LineItem(raw_name="WidgetA", quantity=10, unit_price=250.0, line_total=2400.0)]
    )
    assert "line_arithmetic" in codes(run(inv, db), "reject")


def test_sample_1007_totals_do_not_reconcile(db):
    # Real data: 14,750 + 885 tax = 15,635, but the file states 15,525.
    inv = extract(parse_file(INVOICES / "invoice_1007.csv"), None).invoice
    assert "arithmetic" in codes(run(inv, db), "reject")


def test_unknown_items_and_stock(db):
    inv = make(
        line_items=[
            LineItem(raw_name="WidgetA", quantity=10, unit_price=250.0),
            LineItem(raw_name="SuperGizmo", quantity=1, unit_price=10.0),
        ],
        subtotal=2510.0,
        total=2510.0,
    )
    assert "unknown_items" in codes(run(inv, db), "hold")
    all_unknown = make(
        line_items=[LineItem(raw_name="SuperGizmo", quantity=1, unit_price=10.0)],
        subtotal=10.0,
        total=10.0,
    )
    assert "all_items_unknown" in codes(run(all_unknown, db), "reject")


def test_stock_is_aggregated_per_sku(db):
    # 10 + 8 WidgetA = 18 > 15 in stock, although each line alone is fine
    inv = make(
        line_items=[
            LineItem(raw_name="WidgetA", quantity=10, unit_price=250.0),
            LineItem(raw_name="WidgetA (rush order)", quantity=8, unit_price=250.0),
        ],
        subtotal=4500.0,
        total=4500.0,
    )
    assert "stock_exceeded" in codes(run(inv, db), "hold")


def test_llm_proposed_sku_not_in_db_is_treated_as_unknown(db):
    inv = make()
    res = run(inv, db, proposed=["WidgetZ"])
    assert res.resolved == [None]
    assert "all_items_unknown" in codes(res, "reject") and "sku_not_in_db" in codes(res)


def test_non_usd_missing_due_date_and_bad_quantity(db):
    assert "non_usd" in codes(run(make(currency="EUR"), db), "hold")
    assert "missing_due_date" in codes(run(make(due_date=None), db), "hold")
    bad = make(line_items=[LineItem(raw_name="WidgetA", quantity=-5, unit_price=250.0)])
    assert "bad_quantity" in codes(run(bad, db), "reject")
    assert "missing_vendor" in codes(run(make(vendor=None), db), "reject")


def _record(db, inv, res, decision="APPROVE", source="a.txt"):
    return db.record_invoice(
        source_file=source,
        fingerprint="f",
        vendor_norm=vendor_key(inv),
        invoice_number_norm=number_key(inv),
        content_hash=res.content_hash,
        decision=decision,
        total=inv.total,
        currency=inv.currency,
        duplicate_of=None,
        run_id="r",
    )


def test_equivalent_content_is_duplicate(db):
    first = make()
    _record(db, first, run(first, db))
    # same invoice re-extracted from another format: different spelling/format, same economics
    again = make(
        vendor="WIDGETS INC",
        invoice_number="inv 1",
        line_items=[
            LineItem(raw_name="Widget A", quantity=10, unit_price=250.0, line_total=2500.0)
        ],
    )
    assert content_hash(first) == content_hash(again)
    res = run(again, db)
    assert res.duplicate_of == 1 and "duplicate" in codes(res)


def test_changed_total_is_revision_hold_with_delta(db):
    first = make()
    _record(db, first, run(first, db))
    db.insert_payment(f"{vendor_key(first)}|{number_key(first)}", 2500.0, "USD")
    revised = make(
        line_items=[LineItem(raw_name="WidgetA", quantity=12, unit_price=250.0)],
        subtotal=3000.0,
        total=3000.0,
    )
    res = run(revised, db)
    assert res.duplicate_of is None
    rev = next(f for f in res.findings if f.code == "revision")
    assert rev.severity == "hold"
    assert "+500.00" in rev.message and "original was paid" in rev.message
