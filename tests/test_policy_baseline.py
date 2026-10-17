from conftest import FIXTURES, INVOICES, ROOT

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


def test_1005_is_held_for_stock_only(db):
    ((st, _),) = run_batch([INVOICES / "invoice_1005.json"], db, None, "none")
    assert st["decision"].value == "HOLD"
    assert {f.code for f in st["findings"]} == {"stock_exceeded"}
    assert "GadgetX: invoice needs 8 but only 5" in st["findings"][0].message


def test_1003_rejects_through_documented_risk_signals(db):
    ((st, _),) = run_batch([INVOICES / "invoice_1003.txt"], db, None, "none")
    assert st["decision"].value == "REJECT"
    score = next(f for f in st["findings"] if f.code == "risk_score")
    assert score.severity == "reject"
    assert score.message == (
        "Risk score 6 (pressure_language=3, invalid_due_date=2, round_total=1)"
    )


def test_first_time_vendor_weight_zero_lets_clean_first_invoices_approve(db):
    for name in ("invoice_1006.csv", "invoice_1010.txt"):
        ((st, _),) = run_batch([INVOICES / name], db, None, "none")
        assert st["decision"].value == "APPROVE", name
        assert not [f for f in st["findings"] if f.severity != "note"]


def test_note_level_findings_do_not_block_approval():
    from invoice_agents.approval import code_verdict
    from invoice_agents.models import Finding

    notes = [Finding(code="risk_round_total", severity="note", message="n")]
    assert code_verdict(notes).value == "APPROVE"


def test_9003_in_none_mode_is_a_coverage_hold_not_a_reject(db):
    ((st, _),) = run_batch([ROOT / "data/synthetic/invoice_9003.txt"], db, None, "none")
    assert st["decision"].value == "HOLD"
    codes = {f.code: f for f in st["findings"]}
    assert set(codes) == {"item_needs_mapping"} and codes["item_needs_mapping"].kind == "execution"
    assert "all_items_unknown" not in codes


def test_9001_approves_in_fixture_mode_with_note_findings_not_blocking(db):
    from invoice_agents.llm.client import FixtureClient

    ((st, _),) = run_batch(
        [ROOT / "data/synthetic/invoice_9001.json"], db, FixtureClient(FIXTURES), "fixture"
    )
    assert st["decision"].value == "APPROVE" and st["payment"]["amount"] == 10750.0
    assert any(c["agent"] == "critic" for c in st["llm_calls"])
