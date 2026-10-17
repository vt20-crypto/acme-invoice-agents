from conftest import INVOICES, ROOT

from invoice_agents.batch import run_batch


def test_1004_approves_and_pays_once_then_revision_holds_without_payment(db):
    (orig, _), (rev, _) = run_batch(
        [INVOICES / "invoice_1004.json", INVOICES / "invoice_1004_revised.json"], db, None, "none"
    )
    assert orig["decision"].value == "APPROVE" and orig["payment"]["amount"] == 1890.0
    assert rev["decision"].value == "HOLD"  # a revision, not a duplicate
    assert rev["duplicate_of"] is None and rev.get("payment") is None
    revision = next(f for f in rev["findings"] if f.code == "revision")
    assert "+4,050.00" in revision.message and "original was paid" in revision.message
    assert len(db.payments()) == 1


def test_1005_is_evaluated_by_the_rules_stock_only(db):
    ((st, _),) = run_batch([INVOICES / "invoice_1005.json"], db, None, "none")
    assert st["decision"].value == "HOLD"
    assert {f.code for f in st["findings"]} == {"stock_exceeded"}


def test_1003_rejects_through_two_medium_rules(db):
    ((st, _),) = run_batch([INVOICES / "invoice_1003.txt"], db, None, "none")
    assert st["decision"].value == "REJECT"
    level = next(f for f in st["findings"] if f.code == "risk_level")
    assert level.severity == "reject"
    assert level.message == "Risk level high (pressure_language=medium, invalid_due_date=medium)"


def test_9002_injection_rejects_in_code_only_mode(db):
    ((st, _),) = run_batch([ROOT / "data/synthetic/invoice_9002.txt"], db, None, "none")
    assert st["decision"].value == "REJECT" and st.get("payment") is None
    assert "risk_prompt_injection" in {f.code for f in st["findings"]}


def test_clean_first_invoices_approve_without_a_first_time_vendor_penalty(db):
    for name in ("invoice_1006.csv", "invoice_1010.txt"):
        ((st, _),) = run_batch([INVOICES / name], db, None, "none")
        assert st["decision"].value == "APPROVE", name


def test_note_level_findings_do_not_block_approval():
    from invoice_agents.approval import code_verdict
    from invoice_agents.models import Finding

    notes = [Finding(code="risk_near_threshold", severity="note", message="n")]
    assert code_verdict(notes).value == "APPROVE"
