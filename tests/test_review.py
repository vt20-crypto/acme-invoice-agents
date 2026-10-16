import json

import pytest
from conftest import INVOICES

from invoice_agents.batch import run_batch
from invoice_agents.review import ReviewError, reasons_for, review_hold, review_queue


def process(db, *names):
    return run_batch([INVOICES / n for n in names], db, None, "none")


def ledger_id(results, i):
    return results[i][0]["ledger_id"]


def spy():
    calls = []
    return calls, lambda v, a: calls.append((v, a)) or {"status": "success"}


def test_queue_lists_holds_with_reasons(db):
    res = process(db, "invoice_1016.json", "invoice_1001.txt")
    q = review_queue(db)
    assert [r["id"] for r in q] == [ledger_id(res, 0)]
    assert any(f["code"] == "unknown_items" for f in reasons_for(q[0]))


def test_approve_hold_pays_through_the_guard_once(db):
    res = process(db, "invoice_1016.json")
    lid = ledger_id(res, 0)
    calls, payer = spy()
    out = review_hold(
        db, lid, "approve", "vendor confirmed WidgetC is a new SKU", "ann", payer=payer
    )
    assert out.outcome == "approved_paid" and calls == [("Widgets Inc.", 3233.0)]
    row = db.get_ledger(lid)
    assert row["decision"] == "APPROVE" and row["review_status"] == "approved_by_human"
    assert [p["status"] for p in db.payments()] == ["sent"]
    assert (
        db.reviews()[0]["note"].startswith("vendor confirmed")
        and db.reviews()[0]["reviewer"] == "ann"
    )
    with pytest.raises(ReviewError, match="not an open HOLD"):
        review_hold(db, lid, "approve", "again", payer=payer)
    assert len(calls) == 1


def test_idempotency_key_blocks_second_payment_for_same_invoice(db):
    res = process(db, "invoice_1016.json")
    lid = ledger_id(res, 0)
    row = db.get_ledger(lid)
    db.insert_payment(f"{row['vendor_norm']}|{row['invoice_number_norm']}", 3233.0, "USD")
    calls, payer = spy()
    out = review_hold(db, lid, "approve", "ok", payer=payer)
    assert out.outcome == "approved_not_paid" and calls == []


def test_non_usd_hold_cannot_be_paid_even_when_approved(db):
    res = process(db, "invoice_1014.xml")
    lid = ledger_id(res, 0)
    calls, payer = spy()
    out = review_hold(db, lid, "approve", "treasury says fine", payer=payer)
    assert out.outcome == "refused" and calls == [] and db.payments() == []
    assert db.get_ledger(lid)["decision"] == "HOLD"  # still held


def test_reject_blocker_cannot_be_overridden(db):
    res = process(db, "invoice_1007.csv")
    lid = ledger_id(res, 0)
    assert db.get_ledger(lid)["decision"] == "REJECT"
    with pytest.raises(ReviewError, match="not an open HOLD"):
        review_hold(db, lid, "approve", "please")
    # even a HOLD row carrying a reject-severity finding is refused
    res = process(db, "invoice_1016.json")
    lid = ledger_id(res, 0)
    db.conn.execute(
        "UPDATE invoices SET findings_json = ? WHERE id = ?",
        (
            json.dumps(
                [{"code": "arithmetic", "severity": "reject", "message": "x", "evidence": ""}]
            ),
            lid,
        ),
    )
    calls, payer = spy()
    with pytest.raises(ReviewError, match="REJECT blocker"):
        review_hold(db, lid, "approve", "override", payer=payer)
    assert calls == [] and db.payments() == []


def test_note_is_required_and_reject_action_is_recorded(db):
    res = process(db, "invoice_1016.json")
    lid = ledger_id(res, 0)
    with pytest.raises(ReviewError, match="note"):
        review_hold(db, lid, "approve", "   ")
    out = review_hold(db, lid, "reject", "vendor sent wrong item")
    assert out.outcome == "rejected" and db.get_ledger(lid)["decision"] == "REJECT"
    assert db.payments() == [] and db.reviews()[0]["outcome"] == "rejected"


def test_uncertain_extraction_without_vendor_cannot_be_approved(db):
    res = process(db, "invoice_1002.txt")  # none mode: uncertain extraction, no vendor
    lid = ledger_id(res, 0)
    with pytest.raises(ReviewError, match="missing"):
        review_hold(db, lid, "approve", "pay it")
