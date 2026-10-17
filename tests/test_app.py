from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py"


def test_app_renders_four_tabs_and_processes_a_sample_in_none_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("ACME_UI_DB", str(tmp_path / "ui.db"))
    monkeypatch.setattr("invoice_agents.presentation.__name__", "invoice_agents.presentation")
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    assert [t.label for t in at.tabs] == [
        "Process invoice",
        "Dashboard",
        "Review queue",
        "Evaluation",
    ]
    next(s for s in at.sidebar.selectbox if s.label == "LLM mode").select("none").run()
    next(b for b in at.button if b.label == "Run").click().run()
    assert not at.exception
    html_blocks = " ".join(m.value for m in at.markdown)
    assert "Approved" in html_blocks and "$5,000.00" in html_blocks


def test_evaluation_tab_shows_summary_in_cache_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("ACME_UI_DB", str(tmp_path / "ui.db"))
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(b for b in at.button if b.label == "Run evaluation (cache mode)").click().run()
    assert not at.exception
    assert any("22/22 match" in m.value for m in at.markdown)
