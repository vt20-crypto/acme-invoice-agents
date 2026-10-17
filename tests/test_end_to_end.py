import sqlite3

from conftest import INVOICES

from invoice_agents.cli import main


def run(tmp_path, capsys, name="invoice_1001.txt", *extra, llm="none"):
    rc = main(
        [
            f"--invoice_path={INVOICES / name}",
            f"--llm={llm}",
            f"--db={tmp_path / 'e2e.db'}",
            f"--cache={tmp_path / 'empty_cache'}",
            *extra,
        ]
    )
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def payments(tmp_path):
    con = sqlite3.connect(tmp_path / "e2e.db")
    return con.execute("SELECT amount, status FROM payments").fetchall()


def test_1001_none_mode_approves_pays_once_and_keeps_output_in_order(tmp_path, capsys):
    rc, out, err = run(tmp_path, capsys, "invoice_1001.txt", "--fresh")
    assert rc == 0 and out.startswith("=== Invoice:")
    assert "Decision: APPROVE" in out and "Payment: paid $5,000.00 to Widgets Inc." in out
    assert "code-only run" in out and "(exact match after normalization)" in out
    assert payments(tmp_path) == [(5000.0, "sent")]
    assert '"event": "run_end"' in err  # structured logs on stderr


def test_second_run_is_duplicate_with_no_second_payment(tmp_path, capsys):
    run(tmp_path, capsys, "invoice_1001.txt", "--fresh")
    rc, out, _ = run(tmp_path, capsys, "invoice_1001.txt")
    assert rc == 0 and "Decision: DUPLICATE" in out and "Payment: paid" not in out
    assert "already processed" in out and "--fresh" in out
    assert payments(tmp_path) == [(5000.0, "sent")]


def test_cache_mode_with_no_cache_entry_holds_as_a_system_issue(tmp_path, capsys):
    rc, out, _ = run(tmp_path, capsys, "invoice_1012.txt", "--fresh", llm="cache")
    assert rc == 0
    assert "Decision: HOLD – system issue: no recorded LLM response" in out
    assert "System issues (not vendor or invoice defects):" in out
    assert payments(tmp_path) == []


def test_rejection_prints_a_template_vendor_message(tmp_path, capsys):
    rc, out, _ = run(tmp_path, capsys, "invoice_1007.csv", "--fresh")
    assert "Decision: REJECT" in out and "Vendor message (template, not sent)" in out
    assert "Dear MegaWidgets Corp" in out and "$15,635.00" in out


def test_live_mode_without_a_key_is_a_clean_error(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.chdir(tmp_path)  # so no .env is picked up
    rc, _, err = run(tmp_path, capsys, "invoice_1001.txt", "--fresh", llm="live")
    assert rc == 2 and "LLM_API_KEY is not set" in err


def test_reset_command(tmp_path, capsys):
    run(tmp_path, capsys, "invoice_1001.txt", "--fresh")
    assert main(["reset", f"--db={tmp_path / 'e2e.db'}"]) == 0
    assert payments(tmp_path) == []
