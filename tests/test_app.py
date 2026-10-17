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
    assert "Approved" in html_blocks and "&#36;5,000.00" in html_blocks


def test_evaluation_tab_shows_summary_in_cache_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("ACME_UI_DB", str(tmp_path / "ui.db"))
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(b for b in at.button if b.label == "Run evaluation (cache mode)").click().run()
    assert not at.exception
    assert any("22/22 match" in m.value for m in at.markdown)


def test_css_does_not_hide_or_recolour_streamlit_containers():
    import importlib.util
    import re
    import tomllib

    spec = importlib.util.spec_from_file_location("streamlit_app_css", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    css = mod.CSS
    assert css.count("<style>") == 1 and css.count("</style>") == 1
    assert not re.search(r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0\b", css)
    # No rules on Streamlit's own containers; theme colours come from .streamlit/config.toml.
    assert not re.search(r"\.stApp|\[data-testid|\[class\*=|(^|\s)(html|body)\s*[{,]", css)
    cfg = tomllib.loads((APP.parent.parent / ".streamlit" / "config.toml").read_text())
    assert cfg["theme"]["base"] == "light"


def test_injected_html_is_balanced(tmp_path, monkeypatch):
    from html.parser import HTMLParser

    class Balance(HTMLParser):
        VOID = {"br", "hr", "img", "input"}

        def __init__(self):
            super().__init__()
            self.stack = []

        def handle_starttag(self, tag, attrs):
            if tag not in self.VOID:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            assert self.stack and self.stack.pop() == tag, f"unbalanced </{tag}>"

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("ACME_UI_DB", str(tmp_path / "ui.db"))
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    next(s for s in at.sidebar.selectbox if s.label == "LLM mode").select("none").run()
    next(b for b in at.button if b.label == "Run").click().run()
    snippets = [m.value for m in at.markdown if "<" in m.value and "<style>" not in m.value]
    assert snippets
    for snippet in snippets:
        p = Balance()
        p.feed(snippet)
        assert not p.stack, f"unclosed tags {p.stack} in {snippet[:80]}"
