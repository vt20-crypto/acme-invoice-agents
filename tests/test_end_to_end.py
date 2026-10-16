import sqlite3

from conftest import FIXTURES, INVOICES

from invoice_agents.cli import main

PATH = str(INVOICES / "invoice_1001.txt")


def run(tmp_path, capsys, *extra, llm="fixture"):
    rc = main(
        [
            f"--invoice_path={PATH}",
            f"--llm={llm}",
            f"--db={tmp_path / 'e2e.db'}",
            f"--fixtures={FIXTURES}",
            *extra,
        ]
    )
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def payments(tmp_path):
    return (
        sqlite3.connect(tmp_path / "e2e.db")
        .execute("SELECT amount, status FROM payments")
        .fetchall()
    )


def test_1001_fixture_mode_approves_and_pays_5000(tmp_path, capsys):
    rc, out, err = run(tmp_path, capsys, "--fresh")
    assert rc == 0
    assert "Decision: APPROVE" in out
    assert out.count("Paid 5000.0 to Widgets Inc.") == 1
    assert "extraction/initial: authored" in out and "vp_approval/draft: authored" in out
    assert "search_inventory" in out  # real tool calls ran
    assert payments(tmp_path) == [(5000.0, "sent")]
    assert '"event": "run_end"' in err  # structured logs on stderr


def test_second_run_is_duplicate_with_no_second_payment(tmp_path, capsys):
    run(tmp_path, capsys, "--fresh")
    rc, out, _ = run(tmp_path, capsys)
    assert rc == 0
    assert "Decision: DUPLICATE" in out and "Paid " not in out
    assert "already processed" in out and "--fresh" in out
    assert payments(tmp_path) == [(5000.0, "sent")]


def test_1001_none_mode_approves(tmp_path, capsys):
    rc, out, _ = run(tmp_path, capsys, "--fresh", llm="none")
    assert rc == 0
    assert "Decision: APPROVE" in out and "code-only run" in out
    assert payments(tmp_path) == [(5000.0, "sent")]


def test_missing_fixture_degrades_to_hold_not_crash(tmp_path, capsys):
    rc = main(
        [
            f"--invoice_path={INVOICES / 'invoice_1011.pdf'}",
            "--llm=fixture",
            f"--db={tmp_path / 'e2e.db'}",
            f"--fixtures={FIXTURES}",
            "--fresh",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0 and "Decision: HOLD" in out and "LLM unavailable" in out
    assert payments(tmp_path) == []


def test_reset_command(tmp_path, capsys):
    run(tmp_path, capsys, "--fresh")
    assert main(["reset", f"--db={tmp_path / 'e2e.db'}"]) == 0
    assert payments(tmp_path) == []
