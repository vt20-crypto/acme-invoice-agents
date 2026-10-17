from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py"


def app(tmp_path, monkeypatch, engine="none", timeout=90):
    # An empty value is "set", so load_dotenv() will not pull a real key from .env: no network.
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("ACME_UI_DB", str(tmp_path / "ui.db"))
    at = AppTest.from_file(str(APP), default_timeout=timeout).run()
    at.radio(key="engine").set_value(engine).run()
    assert not at.exception
    return at


def click(at, label):
    next(b for b in at.button if b.label == label).click().run()
    assert not at.exception


def test_app_has_three_tabs_and_processes_a_sample_in_rules_only_mode(tmp_path, monkeypatch):
    at = app(tmp_path, monkeypatch, "none")
    assert [t.label for t in at.tabs] == ["Process invoice", "Dashboard", "Review queue"]
    assert not at.sidebar.children  # no sidebar content: settings live in a popover
    click(at, "Run")
    html_blocks = " ".join(m.value for m in at.markdown)
    assert "Approved" in html_blocks and "&#36;5,000.00" in html_blocks
    assert "Processed " in html_blocks and " \u00b7 " in html_blocks  # local-time stamp on the card


def test_settings_offer_engines_and_hide_live_without_a_key(tmp_path, monkeypatch):
    at = app(tmp_path, monkeypatch)
    options = at.radio(key="engine").options
    assert options[0] == "Auto (recommended)" and len(options) == 3
    assert "Live Grok" not in options  # no key configured in this test


def test_dashboard_is_empty_until_samples_are_loaded_from_the_ledger(tmp_path, monkeypatch):
    at = app(tmp_path, monkeypatch, "cache")
    assert any("The ledger is empty" in m.value for m in at.markdown)
    click(at, "Load sample invoices")
    blocks = " ".join(m.value for m in at.markdown)
    assert '<p class="v">22</p>' in blocks  # processed
    assert "INV-1002" in blocks and "Already processed earlier as INV-1011" in blocks


def test_review_queue_empty_state_then_cards_after_samples(tmp_path, monkeypatch):
    at = app(tmp_path, monkeypatch, "cache")
    assert any("Nothing waiting for review" in m.value for m in at.markdown)
    click(at, "Load sample invoices")
    labels = [b.label for b in at.button]
    assert labels.count("Approve and pay") == 6 and labels.count("Reject") == 6
    disabled = [b for b in at.button if b.label == "Approve and pay" and b.disabled]
    assert len(disabled) == 1  # INV-1014 is EUR: the payment rail is USD-only
    assert any("Approve is unavailable" in c.value for c in at.caption)


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
    theme = cfg["theme"]
    assert "base" not in theme  # not pinned: Light, Dark and System must all work
    assert theme["light"]["backgroundColor"] != theme["dark"]["backgroundColor"]
    # Our own HTML must not hardcode opaque backgrounds or text colours that break in one theme.
    assert not re.search(r"background:\s*#|(^|[;{\s])color:\s*#(?!2e9e4f|d98a00|d64545)", css)


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

    at = app(tmp_path, monkeypatch, "none")
    click(at, "Run")
    snippets = [m.value for m in at.markdown if "<" in m.value and "<style>" not in m.value]
    assert snippets
    for snippet in snippets:
        p = Balance()
        p.feed(snippet)
        assert not p.stack, f"unclosed tags {p.stack} in {snippet[:80]}"
