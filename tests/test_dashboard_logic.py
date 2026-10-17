from datetime import UTC, timedelta, timezone
from pathlib import Path

from conftest import ROOT

from invoice_agents.batch import collect_files, run_new
from invoice_agents.llm.client import make_client
from invoice_agents.presentation import (
    filter_rows,
    format_local,
    sort_rows,
    usd_bounds,
)

SAMPLES = collect_files([ROOT / "data" / "invoices", ROOT / "data" / "synthetic"])


def test_loading_samples_twice_skips_files_already_in_the_ledger(db):
    client = make_client("cache", ROOT / "cache" / "llm")
    first, skipped = run_new(SAMPLES, db, client, "cache")
    assert len(first) == 22 and skipped == 0
    decisions = [s["decision"].value for s, _ in first]
    # different files that are the same business invoice are still recorded as duplicates
    assert decisions.count("DUPLICATE") == 3
    ledger_rows, payments = len(db.ledger()), len(db.payments())
    assert ledger_rows == 22

    second, skipped = run_new(SAMPLES, db, client, "cache")
    assert len(second) == 0 and skipped == 22
    assert len(db.ledger()) == ledger_rows and len(db.payments()) == payments


def test_an_identical_upload_is_still_a_duplicate_submission(db, tmp_path):
    from invoice_agents.graph import Context, process_invoice

    copy = tmp_path / "again.txt"
    copy.write_bytes((ROOT / "data" / "invoices" / "invoice_1001.txt").read_bytes())
    ctx = Context(db=db, client=None, llm_mode="none")
    process_invoice(str(ROOT / "data" / "invoices" / "invoice_1001.txt"), ctx)
    state = process_invoice(str(copy), Context(db=db, client=None, llm_mode="none"))
    assert state["decision"].value == "DUPLICATE" and len(db.ledger()) == 2


def row(i, vendor, total, decision="APPROVE", currency="USD", number="INV-1"):
    return {
        "id": i, "vendor": vendor, "total": total, "decision": decision, "currency": currency,
        "invoice_number": number, "review_status": None, "findings_json": "[]",
    }  # fmt: skip


ROWS = [
    row(1, "Beta Co", 500.0),
    row(2, "alpha Ltd", 12000.0, "HOLD"),
    row(3, "Gamma", 4125.0, "HOLD", "EUR"),
    row(4, None, None, "REJECT"),
    row(5, "Delta", 75.5, "REJECT"),
]


def ids(rows):
    return [r["id"] for r in rows]


def test_sort_orders():
    assert ids(sort_rows(ROWS, "Newest first")) == [5, 4, 3, 2, 1]
    assert ids(sort_rows(ROWS, "Oldest first")) == [1, 2, 3, 4, 5]
    assert ids(sort_rows(ROWS, "Amount: high to low")) == [2, 3, 1, 5, 4]
    assert ids(sort_rows(ROWS, "Amount: low to high")) == [5, 1, 3, 2, 4]
    assert ids(sort_rows(ROWS, "Vendor A–Z")) == [2, 1, 5, 3, 4]  # case-insensitive, blanks last


def test_amount_filter_bounds_and_combination_with_segment_and_search():
    assert usd_bounds(ROWS) == (75, 12000) and usd_bounds([ROWS[2]]) is None
    bounds = (75, 12000)
    # untouched range keeps everything, including the EUR row and the row with no amount
    assert ids(filter_rows(ROWS, "all", "", (75, 12000), bounds)) == [1, 2, 3, 4, 5]
    # a narrowed range keeps only USD rows inside it
    assert ids(filter_rows(ROWS, "all", "", (100, 1000), bounds)) == [1]
    assert ids(filter_rows(ROWS, "all", "", (75, 600), bounds)) == [1, 5]
    # it combines with the decision segment and the search text
    assert ids(filter_rows(ROWS, "REJECT", "", (75, 600), bounds)) == [5]
    assert ids(filter_rows(ROWS, "all", "beta", (75, 600), bounds)) == [1]
    assert ids(filter_rows(ROWS, "HOLD", "", (75, 12000), bounds)) == [2, 3]


def test_format_local_converts_utc_and_is_total():
    ts = "2026-10-17T04:16:30+00:00"
    assert format_local(ts, timezone(timedelta(hours=-4))) == "Oct 17, 2026 · 12:16 AM"
    assert format_local(ts, UTC) == "Oct 17, 2026 · 4:16 AM"
    assert format_local("2026-10-17T16:05:00+00:00", UTC) == "Oct 17, 2026 · 4:05 PM"
    assert format_local("2026-10-17T04:16:30", UTC).endswith("4:16 AM")  # naive = UTC
    assert format_local(None) == format_local("not a date") == "—"


def test_processed_at_is_stored_as_iso_utc(db):
    client = make_client("cache", Path(ROOT) / "cache" / "llm")
    run_new(SAMPLES[:1], db, client, "cache")
    ts = db.ledger()[0]["processed_at"]
    assert ts.endswith("+00:00") and format_local(ts) != "—"
