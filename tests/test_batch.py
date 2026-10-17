from conftest import INVOICES

from invoice_agents.batch import collect_files, run_batch


def test_batch_order_is_byte_sorted_by_file_name():
    names = [p.name for p in collect_files([INVOICES])]
    assert names == sorted(names)
    assert names.index("invoice_1004.json") < names.index("invoice_1004_revised.json")
    assert names.index("invoice_1013.json") < names.index("invoice_1013.pdf")


def test_1013_pdf_is_duplicate_of_the_rejected_json(db):
    (js, _), (pdf, _) = run_batch(
        [INVOICES / "invoice_1013.json", INVOICES / "invoice_1013.pdf"], db, None, "none"
    )
    assert js["decision"].value == "REJECT"
    assert pdf["decision"].value == "DUPLICATE" and pdf["duplicate_of"] == js["ledger_id"]


def test_revision_processed_first_is_flagged_with_a_warning(db, capsys):
    paths = [INVOICES / "invoice_1004_revised.json", INVOICES / "invoice_1004.json"]
    (first, _), (second, _) = run_batch(paths, db, None, "none")
    assert "revision_before_original" in {f.code for f in first["findings"]}
    assert '"event": "warning"' in capsys.readouterr().err
    assert second["decision"].value != "APPROVE" and second.get("payment") is None
