import re

from conftest import INVOICES, ROOT

from invoice_agents.batch import run_batch
from invoice_agents.cli import main
from invoice_agents.llm.client import FixtureClient
from invoice_agents.payments import pay_invoice
from invoice_agents.presentation import display_path, finding_line, money, plain

ABS = re.compile(r"/Users/|/private/|/tmp/|/home/")


def test_money_formats_display_only():
    assert money(1890) == "$1,890.00"
    assert money(4125, "EUR") == "€4,125.00"
    assert money(-250, "USD") == "-$250.00"
    assert money(10, "CHF") == "CHF 10.00"
    assert money(None) == "n/a"


def test_display_path_is_relative_or_bare():
    assert display_path(INVOICES / "invoice_1001.txt") == "data/invoices/invoice_1001.txt"
    assert display_path("/somewhere/else/x.txt") == "x.txt"
    assert display_path("data/invoices/a.txt") == "data/invoices/a.txt"


def test_plain_sentences_for_key_findings(db):
    ((st, _),) = run_batch([INVOICES / "invoice_1005.json"], db, None, "none")
    assert plain(st["findings"][0]) == "Ordered 8 GadgetX, only 5 in stock"
    assert finding_line(st["findings"][0]) == "Ordered 8 GadgetX, only 5 in stock [stock_exceeded]"
    ((st, _),) = run_batch([INVOICES / "invoice_1007.csv"], db, None, "none")
    arith = next(f for f in st["findings"] if f.code == "arithmetic")
    assert plain(arith) == (
        "Invoice states $15,525.00 but lines, charges and tax add up to $15,635.00"
    )
    ((st, _),) = run_batch([INVOICES / "invoice_1014.xml"], db, None, "none")
    assert "EUR" in plain(st["findings"][0]) and "no currency conversion" in plain(
        st["findings"][0]
    )


def test_every_finding_code_emitted_by_the_samples_has_a_human_sentence(db):
    from invoice_agents.batch import collect_files
    from invoice_agents.evaluation import ROOT as EROOT  # noqa: F401

    files = collect_files([ROOT / "data/invoices", ROOT / "data/synthetic"])
    for state, _ in run_batch(files, db, None, "none"):
        for f in state["findings"]:
            text = plain(f)
            assert text and not text.startswith(f.code), (f.code, text)


def test_revision_sentence_mentions_delta_and_paid(db):
    res = run_batch(
        [INVOICES / "invoice_1004.json", INVOICES / "invoice_1004_revised.json"], db, None, "none"
    )
    rev = next(f for f in res[1][0]["findings"] if f.code == "revision")
    assert plain(rev) == (
        "Revision of invoice_1004.json: total changed from $1,890.00 to $5,940.00 (+4,050.00); "
        "the original was already paid"
    )


def test_mock_payment_print_is_captured_not_leaked(db, capsys):
    res = pay_invoice(db, vendor="V", amount=12.5, currency="USD", idempotency_key="k")
    assert res.status == "paid" and res.output == "Paid 12.5 to V"
    assert capsys.readouterr().out == ""


def test_cli_hides_absolute_paths_unless_verbose(tmp_path, capsys):
    base = [f"--invoice_path={INVOICES / 'invoice_1001.txt'}", "--llm=none", "--fresh"]
    main([*base, f"--db={tmp_path / 'p.db'}"])
    out = capsys.readouterr().out
    assert not ABS.search(out) and "data/invoices/invoice_1001.txt" in out
    assert "Developer details" not in out
    main([*base, f"--db={tmp_path / 'p.db'}", "--verbose"])
    out = capsys.readouterr().out
    assert "--- Developer details ---" in out and str(ROOT) in out


def test_jsonl_and_findings_have_no_absolute_paths(db, tmp_path):
    out = tmp_path / "r.jsonl"
    main(
        [
            f"--batch={INVOICES}",
            "--llm=none",
            f"--db={tmp_path / 'b.db'}",
            f"--out={out}",
            "--fresh",
        ]
    )
    assert not ABS.search(out.read_text())


def test_missing_fixture_message_has_no_absolute_path(db):
    ((st, _),) = run_batch(
        [INVOICES / "invoice_1012.txt"], db, FixtureClient(ROOT / "fixtures/llm"), "fixture"
    )
    text = " ".join(f.evidence + f.message for f in st["findings"])
    assert "fixture" in text and not ABS.search(text)


def test_resolution_lines_show_sku_and_how(db):
    ((st, _),) = run_batch(
        [ROOT / "data/synthetic/invoice_9003.txt"],
        db,
        FixtureClient(ROOT / "fixtures/llm"),
        "fixture",
    )
    lines = next(s for s in st["steps"] if s["step"] == "resolve_items")["lines"]
    assert "'Widget-A assembly unit' → WidgetA  (via inventory search)" in lines
    ((st, _),) = run_batch([INVOICES / "invoice_1010.txt"], db, None, "none")
    lines = next(s for s in st["steps"] if s["step"] == "resolve_items")["lines"]
    assert "'WidgetA (rush order)' → WidgetA  (exact match after normalization)" in lines
