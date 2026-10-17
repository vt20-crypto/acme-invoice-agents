import csv
import io
import json

import pytest
from conftest import ROOT

from invoice_agents.batch import collect_files, run_batch
from invoice_agents.cli import main
from invoice_agents.export import (
    RESULT_COLUMNS,
    ai_provenance,
    compare_expected,
    expected_template,
    parse_expected,
    result_rows,
    summary_line,
    to_csv,
    to_json,
)
from invoice_agents.llm.client import make_client

PACK = ROOT / "data" / "reviewer_pack"


@pytest.fixture
def pack_rows(db):
    run_batch(collect_files([PACK]), db, make_client("cache", ROOT / "cache" / "llm"), "cache")
    return result_rows(db.ledger(), db.payments())


def test_reviewer_pack_is_collected_without_its_answer_key():
    names = [p.name for p in collect_files([PACK])]
    assert "expected_results.csv" not in names and len(names) == 6


def test_result_rows_have_the_documented_columns_and_paid_flags(pack_rows):
    assert list(pack_rows[0]) == RESULT_COLUMNS
    by_file = {r["file"]: r for r in pack_rows}
    assert by_file["invoice_9010.json"]["paid"] == "yes"
    assert by_file["invoice_9010.json"]["ai_provenance"] == "recorded"
    assert by_file["invoice_9017.txt"]["decision"] == "DUPLICATE"
    assert by_file["invoice_9017.txt"]["paid"] == "no"  # shares the original's payment key
    assert by_file["invoice_9014.xml"]["paid"] == "no"
    assert by_file["invoice_9014.xml"]["ai_provenance"] == "code"
    assert "USD-only" in by_file["invoice_9014.xml"]["reasons"]
    assert by_file["invoice_9012.txt"]["processed_at"].startswith("20")  # ISO timestamp


def test_csv_and_json_carry_the_same_rows(pack_rows):
    from_csv = list(csv.DictReader(io.StringIO(to_csv(pack_rows))))
    from_json = json.loads(to_json(pack_rows))
    assert [r["file"] for r in from_csv] == [r["file"] for r in from_json]
    assert from_json[0]["total"] == 2700.0 and from_csv[0]["total"] == "2700.0"


def test_ai_provenance_summary():
    assert ai_provenance([]) == "code"
    assert ai_provenance([{"provenance": "recorded"}, {"provenance": "recorded"}]) == "recorded"
    assert ai_provenance([{"provenance": "recorded"}, {"provenance": "live"}]) == "live+recorded"
    assert ai_provenance([{"agent": "x", "outcome": "rules_only"}]) == "rules_only"


PROCESSED = [
    {"file": "data/in/Invoice_1.TXT", "decision": "APPROVE"},
    {"file": "invoice_2.json", "decision": "HOLD"},
    {"file": "invoice_3.csv", "decision": "REJECT"},
]


def test_compare_matches_by_basename_ignoring_case_and_spacing():
    text = "file,expected_decision\ninvoice_1.txt, approve \nINVOICE_2.JSON,Reject\n"
    cmp = compare_expected(PROCESSED, text)
    assert [(r["file"], r["match"]) for r in cmp["rows"]] == [
        ("invoice_1.txt", True),
        ("INVOICE_2.JSON", False),
    ]
    assert (cmp["matched"], cmp["total"], summary_line(cmp)) == (1, 2, "1 of 2 match")
    assert cmp["not_processed"] == []


def test_compare_lists_expected_files_that_were_not_processed():
    cmp = compare_expected(
        PROCESSED, "file,expected_decision\nghost.pdf,APPROVE\ninvoice_3.csv,REJECT\n"
    )
    assert cmp["not_processed"] == ["ghost.pdf"] and (cmp["matched"], cmp["total"]) == (1, 1)


def test_compare_accepts_extra_columns_bom_and_header_spacing():
    text = "﻿ File , Expected_Decision ,note\ninvoice_2.json,HOLD,because\n"
    assert compare_expected(PROCESSED, text)["matched"] == 1


def test_compare_rejects_a_csv_without_the_required_columns():
    with pytest.raises(ValueError, match="file and expected_decision"):
        parse_expected("name,decision\nx,APPROVE\n")
    with pytest.raises(ValueError, match="missing: expected_decision"):
        parse_expected("file,note\nx,y\n")


def test_blank_expected_decision_never_matches():
    cmp = compare_expected(PROCESSED, expected_template(["invoice_1.txt"]))
    assert cmp["rows"][0]["match"] is False and cmp["rows"][0]["expected"] == ""


def test_template_lists_processed_file_names_with_blank_decisions():
    rows = list(csv.DictReader(io.StringIO(expected_template(["a.txt", "b.json"]))))
    assert rows == [
        {"file": "a.txt", "expected_decision": ""},
        {"file": "b.json", "expected_decision": ""},
    ]


def test_cli_out_writes_csv_or_json_for_this_run(tmp_path, capsys):
    base = ["--batch", str(PACK), "--llm", "cache", "--fresh", f"--db={tmp_path / 'x.db'}"]
    assert main([*base, "--out", str(tmp_path / "r.csv")]) == 0
    assert main([*base, "--out", str(tmp_path / "r.json")]) == 0
    csv_rows = list(csv.DictReader((tmp_path / "r.csv").open()))
    json_rows = json.loads((tmp_path / "r.json").read_text())
    assert len(csv_rows) == len(json_rows) == 6
    assert [r["decision"] for r in csv_rows] == [r["decision"] for r in json_rows]
    assert "Results written to" in capsys.readouterr().out


def test_cli_eval_expected_reports_per_file_and_summary(capsys):
    rc = main(["eval", "--llm", "cache", "--expected", str(PACK / "expected_results.csv")])
    out = capsys.readouterr().out
    assert rc == 0 and "6 of 6 match" in out and out.count("✓") == 6


def test_cli_eval_expected_flags_a_mismatch_and_a_missing_file(tmp_path, capsys):
    exp = tmp_path / "exp.csv"
    exp.write_text("file,expected_decision\ninvoice_9010.json,REJECT\nnope.txt,APPROVE\n")
    rc = main(["eval", "--llm", "cache", "--expected", str(exp), str(PACK)])
    out = capsys.readouterr().out
    assert rc == 1 and "✗" in out and "0 of 1 match" in out
    assert "Expected but not processed: nope.txt" in out


def test_cli_eval_expected_rejects_a_csv_with_wrong_columns(tmp_path, capsys):
    exp = tmp_path / "bad.csv"
    exp.write_text("name,decision\nx,APPROVE\n")
    assert main(["eval", "--llm", "cache", "--expected", str(exp), str(PACK)]) == 2
    assert "file and expected_decision" in capsys.readouterr().err
