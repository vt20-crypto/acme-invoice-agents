import importlib.util
from pathlib import Path

from invoice_agents.models import Finding
from invoice_agents.presentation import (
    decision_badge,
    group_findings,
    key_codes,
    paid_text,
    provenance_label,
)

APP = Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py"


def test_decision_badges_have_icon_and_text():
    for d in ("APPROVE", "HOLD", "REJECT", "DUPLICATE"):
        badge = decision_badge(d)
        assert d in badge and badge.split()[0] != d  # icon precedes the text


def test_provenance_labels():
    assert "authored fixture" in provenance_label("authored")
    assert "not a model output" in provenance_label("authored")
    assert provenance_label("live").startswith("live")
    assert provenance_label("recorded").startswith("recorded")
    assert provenance_label(None).startswith("code")


def test_group_findings_orders_by_severity_with_evidence():
    fs = [
        Finding(code="a", severity="note", message="n"),
        Finding(code="b", severity="reject", message="r", evidence="why"),
    ]
    g = group_findings(fs)
    assert g["reject"] == ["r [b]"] and g["note"] == ["n [a]"] and g["hold"] == []


def test_paid_text_and_key_codes():
    assert paid_text({"payment": {"status": "paid", "amount": 5000.0}}) == "Yes (5,000.00)"
    assert paid_text({"payment": None}) == "No"
    assert key_codes({"codes": ["risk_pressure_language", "risk_score", "stock_exceeded"]}) == (
        "risk_score, stock_exceeded"
    )


def test_app_module_imports_without_running_ui():
    spec = importlib.util.spec_from_file_location("streamlit_app", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # __main__ guard: nothing renders on import
    assert callable(mod.main) and len(mod.sample_files()) == 22


def test_app_renders_with_streamlit_apptest(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Process", "Batch", "Ledger & review queue"]
    # every input has a label
    assert all(w.label for w in at.sidebar.selectbox) and all(w.label for w in at.text_input)


def test_app_processes_a_sample_end_to_end(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    at.sidebar.text_input[0].set_value(str(tmp_path / "ui.db")).run()
    run_btn = next(b for b in at.button if b.label == "Run")
    run_btn.click().run()
    assert not at.exception
    heads = [s.value for s in at.subheader]
    assert any("APPROVE" in h and h.startswith("Decision:") for h in heads), heads
    assert any("authored fixture" in m.value for m in at.markdown), "provenance label missing"


def test_cause_and_label_separate_system_issues_from_business_reasons():
    from invoice_agents.presentation import cause, decision_label, split_reasons

    sys_f = Finding(code="fixture_missing", severity="hold", message="no fixture")
    biz_f = Finding(code="stock_exceeded", severity="hold", message="too many")
    assert sys_f.kind == "execution" and biz_f.kind == "business"
    assert cause("HOLD", [sys_f]) == "system"
    assert cause("HOLD", [biz_f]) == "business"
    assert cause("HOLD", [sys_f, biz_f]) == "both"
    assert cause("APPROVE", []) == "-" and cause("DUPLICATE", [biz_f]) == "-"
    assert decision_label("HOLD", [sys_f]) == (
        "HOLD – system issue: no fixture for this document or step"
    )
    assert split_reasons([sys_f, biz_f]) == {
        "business": ["too many [stock_exceeded]"],
        "execution": [
            "No recorded or authored LLM fixture exists for this document and step"
            " [fixture_missing]"
        ],
    }


def test_ui_disables_approve_for_ineligible_hold_and_shows_reason(tmp_path, monkeypatch):
    from conftest import INVOICES
    from streamlit.testing.v1 import AppTest

    from invoice_agents.batch import run_batch
    from invoice_agents.db import Database

    db_path = tmp_path / "ui.db"
    db = Database(db_path)
    run_batch([INVOICES / "invoice_1014.xml", INVOICES / "invoice_1005.json"], db, None, "none")
    db.close()
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    at.sidebar.text_input[0].set_value(str(db_path)).run()
    assert not at.exception
    approve = [b for b in at.button if b.label == "Approve anyway"]
    assert len(approve) == 2
    by_disabled = sorted(b.disabled for b in approve)
    assert by_disabled == [False, True]  # EUR invoice disabled, USD stock hold enabled
    assert any("no FX" in w.value for w in at.warning)
