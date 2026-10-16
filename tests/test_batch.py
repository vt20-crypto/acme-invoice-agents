import json
from pathlib import Path

from conftest import INVOICES

from invoice_agents.batch import collect_files, run_batch, write_results
from invoice_agents.cli import main
from invoice_agents.ingestion.extractor import extract_labelled
from invoice_agents.ingestion.parsers import parse_file
from invoice_agents.validation import content_hash


def test_batch_order_is_byte_sorted_by_file_name():
    names = [p.name for p in collect_files([INVOICES])]
    assert names == sorted(names)
    assert names.index("invoice_1004.json") < names.index("invoice_1004_revised.json")
    assert names.index("invoice_1013.json") < names.index("invoice_1013.pdf")


def test_revision_processed_first_logs_warning_and_is_flagged(db, capsys):
    paths = [INVOICES / "invoice_1004_revised.json", INVOICES / "invoice_1004.json"]
    (first, _), (second, _) = run_batch(paths, db, None, "none")
    assert "revision_before_original" in {f.code for f in first["findings"]}
    assert '"event": "warning"' in capsys.readouterr().err
    # the original then looks like a revision of the one processed first; never auto-paid
    assert "revision" in {f.code for f in second["findings"]}
    assert second["decision"].value != "APPROVE" and second.get("payment") is None


def test_1013_pdf_extracts_to_same_content_as_json():
    pdf = extract_labelled(parse_file(INVOICES / "invoice_1013.pdf"))
    js = parse_file(INVOICES / "invoice_1013.json")
    from invoice_agents.ingestion.extractor import extract_structured

    assert not pdf.uncertain
    assert content_hash(pdf.invoice) == content_hash(extract_structured(js))
    assert (
        pdf.invoice.vendor == "Atlas Industrial Supply" and pdf.invoice.invoice_number == "INV-1013"
    )


def test_1013_pdf_is_duplicate_of_rejected_json(db):
    (js, _), (pdf, _) = run_batch(
        [INVOICES / "invoice_1013.json", INVOICES / "invoice_1013.pdf"], db, None, "none"
    )
    assert js["decision"].value == "REJECT"
    assert pdf["decision"].value == "DUPLICATE" and pdf["duplicate_of"] == js["ledger_id"]
    arith = next(f for f in js["findings"] if f.code == "arithmetic")
    assert "22,562.80 != lines 21,040.00" in arith.message and "22,512.80" in arith.message


def test_extraction_is_uncertain_when_lines_do_not_sum_to_subtotal(tmp_path):
    f = tmp_path / "i.txt"
    f.write_text(
        "Vendor: V\nInvoice Number: INV-1\nItems:\n  WidgetA qty: 1 unit price: $10.00\n"
        "Subtotal: $30.00\nTotal: $30.00\n"
    )
    res = extract_labelled(parse_file(f))
    assert res.uncertain and "subtotal" in res.reasons[0]


def test_results_jsonl_one_line_per_document(db, tmp_path):
    out = tmp_path / "r.jsonl"
    rc = main(
        [
            f"--batch={INVOICES}",
            "--llm=none",
            f"--db={tmp_path / 'b.db'}",
            f"--out={out}",
            "--fresh",
        ]
    )
    assert rc == 0
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(lines) == 20 and lines[0]["file"].endswith("invoice_1001.txt")
    assert {"decision", "codes", "findings", "payment", "llm_calls"} <= set(lines[0])
    write_results(tmp_path / "x.jsonl", [lines[0]])
    assert len(Path(tmp_path / "x.jsonl").read_text().splitlines()) == 1
