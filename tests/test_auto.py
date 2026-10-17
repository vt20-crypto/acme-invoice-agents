"""Auto mode: recorded response first, then live (fake transport here, never the network), then
rules only. Regression tests keep using strict cache mode."""

import pytest
from conftest import INVOICES

from invoice_agents.cli import main
from invoice_agents.graph import Context, process_invoice
from invoice_agents.llm.client import (
    AutoTransport,
    CacheMiss,
    LLMClient,
    ModelTurn,
    NoRecordedAI,
    Transport,
)
from invoice_agents.models import Decision, Invoice


class Fake(Transport):
    def __init__(self, provenance, content=None):
        self.provenance, self.model, self.content, self.calls = provenance, "m", content, 0

    def call(self, agent, key, messages, schema_model, tools):
        self.calls += 1
        if self.content is None:
            raise CacheMiss("miss")
        return ModelTurn(self.content)


INV = Invoice(vendor="V", invoice_number="1", line_items=[], total=1.0).model_dump_json()


def ask(client):
    return client.structured("extraction", [{"role": "user", "content": "x"}], Invoice, key="k")


def test_recorded_response_wins_and_live_is_not_called():
    cache, live = Fake("recorded", INV), Fake("live", INV)
    _, prov = ask(LLMClient(AutoTransport(cache, live)))
    assert prov == "recorded" and live.calls == 0


def test_cache_miss_falls_through_to_live_and_reports_live():
    cache, live = Fake("recorded"), Fake("live", INV)
    _, prov = ask(LLMClient(AutoTransport(cache, live)))
    assert prov == "live" and live.calls == 1


def test_provenance_follows_each_call():
    cache, live = Fake("recorded", INV), Fake("live", INV)
    client = LLMClient(AutoTransport(cache, live))
    assert ask(client)[1] == "recorded"
    cache.content = None
    assert ask(client)[1] == "live"


def test_miss_with_no_key_raises_no_recorded_ai():
    with pytest.raises(NoRecordedAI):
        ask(LLMClient(AutoTransport(Fake("recorded"), None)))
    assert issubclass(NoRecordedAI, CacheMiss)


def test_auto_with_nothing_recorded_and_no_key_processes_with_rules_only(tmp_path, db):
    client = LLMClient(AutoTransport(Fake("recorded"), None))
    ctx = Context(db=db, client=client, llm_mode="auto")
    state = process_invoice(str(INVOICES / "invoice_1001.txt"), ctx)
    assert state["decision"] is Decision.APPROVE
    assert state["extraction"].method == "label_fallback"
    outcomes = {(c["agent"], c.get("outcome")) for c in state["llm_calls"]}
    assert ("extraction", "rules_only") in outcomes and ("vp_approval", "rules_only") in outcomes
    assert not any(c.get("provenance") for c in state["llm_calls"])


def test_eval_command_prints_expected_vs_actual_and_summary(capsys, tmp_path):
    rc = main(["eval", "--llm", "cache", f"--db={tmp_path / 'x.db'}"])
    out = capsys.readouterr().out
    assert rc == 0 and "40/40 match" in out and "✓" in out and "✗" not in out
