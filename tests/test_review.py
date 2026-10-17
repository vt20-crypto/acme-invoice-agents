import json

import pytest
from conftest import INVOICES

from invoice_agents.batch import run_batch
from invoice_agents.review import (
    ReviewError,
    approval_eligibility,
    reasons_for,
    review_hold,
    review_queue,
)

# invoice_1005 in none mode is a clean business HOLD: stock_exceeded only, USD, vendor present.
HOLD_FILE = "invoice_1005.json"


def process(db, *names):
    return run_batch([INVOICES / n for n in names], db, None, "none")


def lid_of(results, i=0):
    return results[i][0]["ledger_id"]


def spy():
    calls = []
    return calls, lambda v, a: calls.append((v, a)) or {"status": "success"}


def set_findings(db, lid, findings):
    db.conn.execute(
        "UPDATE invoices SET findings_json = ? WHERE id = ?", (json.dumps(findings), lid)
    )
    db.conn.commit()


def finding(code, severity="hold", kind="business", stage=""):
    return {"code": code, "severity": severity, "message": "m", "evidence": "", "kind": kind,
            "stage": stage}  # fmt: skip


def test_queue_lists_holds_with_reasons(db):
    res = process(db, HOLD_FILE, "invoice_1001.txt")
    q = review_queue(db)
    assert [r["id"] for r in q] == [lid_of(res, 0)]
    assert any(f["code"] == "stock_exceeded" for f in reasons_for(q[0]))
    assert approval_eligibility(q[0]).ok


def test_approve_hold_pays_through_the_guard_once(db):
    lid = lid_of(process(db, HOLD_FILE))
    calls, payer = spy()
    out = review_hold(db, lid, "approve", "stock arrives Monday", "ann", payer=payer)
    assert out.outcome == "approved_paid"
    assert calls == [("Global Supply Chain Partners", 15225.0)]
    row = db.get_ledger(lid)
    assert row["decision"] == "APPROVE" and row["review_status"] == "approved_by_human"
    assert [p["status"] for p in db.payments()] == ["sent"]
    assert (
        db.reviews()[0]["note"] == "stock arrives Monday" and db.reviews()[0]["reviewer"] == "ann"
    )
    with pytest.raises(ReviewError, match="not an open HOLD"):
        review_hold(db, lid, "approve", "again", payer=payer)
    assert len(calls) == 1


def test_idempotency_key_blocks_second_payment_for_same_invoice(db):
    lid = lid_of(process(db, HOLD_FILE))
    row = db.get_ledger(lid)
    db.insert_payment(f"{row['vendor_norm']}|{row['invoice_number_norm']}", 15225.0, "USD")
    calls, payer = spy()
    out = review_hold(db, lid, "approve", "ok", payer=payer)
    assert out.outcome == "approved_not_paid" and calls == []


def test_non_usd_hold_cannot_be_approved_and_no_payment_exists(db):
    lid = lid_of(process(db, "invoice_1014.xml"))
    assert db.get_ledger(lid)["decision"] == "HOLD"
    elig = approval_eligibility(db.get_ledger(lid))
    assert not elig.ok and any("EUR" in r and "no FX" in r for r in elig.reasons)
    calls, payer = spy()
    with pytest.raises(ReviewError, match="Currency EUR"):
        review_hold(db, lid, "approve", "treasury says fine", payer=payer)
    assert calls == [] and db.payments() == []
    # rejecting stays possible
    assert review_hold(db, lid, "reject", "wrong currency").outcome == "rejected"


def test_reject_decision_rows_cannot_be_approved(db):
    lid = lid_of(process(db, "invoice_1007.csv"))
    assert db.get_ledger(lid)["decision"] == "REJECT"
    with pytest.raises(ReviewError, match="not an open HOLD"):
        review_hold(db, lid, "approve", "please")


def test_reject_severity_finding_blocks_even_on_a_hold_row(db):
    lid = lid_of(process(db, HOLD_FILE))
    set_findings(db, lid, [finding("arithmetic", severity="reject")])
    calls, payer = spy()
    with pytest.raises(ReviewError, match="REJECT blocker"):
        review_hold(db, lid, "approve", "override", payer=payer)
    assert calls == [] and db.payments() == []


@pytest.mark.parametrize(
    "column,value,fragment",
    [
        ("vendor", None, "Vendor is missing"),
        ("total", None, "total is missing"),
        ("total", 0.0, "total is missing"),
        ("total", -5.0, "total is missing"),
        ("invoice_number_norm", None, "Invoice number is missing"),
        ("currency", "GBP", "Currency GBP"),
    ],
)
def test_ineligible_when_required_fields_are_bad(db, column, value, fragment):
    lid = lid_of(process(db, HOLD_FILE))
    db.conn.execute(f"UPDATE invoices SET {column} = ? WHERE id = ?", (value, lid))
    db.conn.commit()
    calls, payer = spy()
    with pytest.raises(ReviewError, match=fragment):
        review_hold(db, lid, "approve", "n", payer=payer)
    assert calls == [] and db.payments() == []


def test_failed_extraction_cannot_be_approved(db):
    lid = lid_of(process(db, "invoice_1002.txt"))  # none mode: extraction_failed
    row = db.get_ledger(lid)
    assert row["decision"] == "HOLD"
    elig = approval_eligibility(row)
    assert not elig.ok and any("extraction_failed" in r for r in elig.reasons)
    with pytest.raises(ReviewError):
        review_hold(db, lid, "approve", "pay it")


def test_unmapped_items_cannot_be_approved(db):
    lid = lid_of(process(db, "invoice_1016.json"))  # none mode: item_needs_mapping
    elig = approval_eligibility(db.get_ledger(lid))
    assert not elig.ok and any("item_needs_mapping" in r for r in elig.reasons)


def test_execution_failure_in_approval_stage_leaves_nothing_unverified(db):
    lid = lid_of(process(db, HOLD_FILE))
    set_findings(db, lid, [finding("fixture_missing", kind="execution", stage="approval")])
    calls, payer = spy()
    assert review_hold(db, lid, "approve", "I am the VP", payer=payer).outcome == "approved_paid"


def test_note_is_required_and_reject_action_is_recorded(db):
    lid = lid_of(process(db, HOLD_FILE))
    with pytest.raises(ReviewError, match="note"):
        review_hold(db, lid, "approve", "   ")
    out = review_hold(db, lid, "reject", "vendor sent wrong item")
    assert out.outcome == "rejected" and db.get_ledger(lid)["decision"] == "REJECT"
    assert db.payments() == [] and db.reviews()[0]["outcome"] == "rejected"
