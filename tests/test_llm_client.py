import json

import pytest
from conftest import FIXTURES

from invoice_agents.llm.client import FixtureClient, FixtureError, LLMUnavailable, Tool
from invoice_agents.models import Critique, ResolutionResult


def test_fixture_without_provenance_is_refused(tmp_path):
    d = tmp_path / "critic"
    d.mkdir()
    (d / "k.json").write_text(json.dumps({"response": {"verdict": "pass", "issues": []}}))
    with pytest.raises(FixtureError, match="provenance"):
        FixtureClient(tmp_path, verify=False).structured("critic", [], Critique, key="k")


def test_fixture_with_unknown_provenance_is_refused(tmp_path):
    d = tmp_path / "critic"
    d.mkdir()
    (d / "k.json").write_text(
        json.dumps({"provenance": "live", "response": {"verdict": "pass", "issues": []}})
    )
    with pytest.raises(FixtureError):
        FixtureClient(tmp_path, verify=False).structured("critic", [], Critique, key="k")


def test_recorded_provenance_is_supported(tmp_path):
    d = tmp_path / "critic"
    d.mkdir()
    (d / "k.json").write_text(
        json.dumps({"provenance": "recorded", "response": {"verdict": "pass", "issues": []}})
    )
    obj, prov = FixtureClient(tmp_path, verify=False).structured("critic", [], Critique, key="k")
    assert prov == "recorded" and obj.verdict == "pass"


def test_missing_fixture_raises_unavailable(tmp_path):
    with pytest.raises(LLMUnavailable, match="no fixture"):
        FixtureClient(tmp_path, verify=False).structured("critic", [], Critique, key="nope")


def test_every_shipped_fixture_is_labelled_authored():
    files = list(FIXTURES.rglob("*.json"))
    assert files
    for f in files:
        data = json.loads(f.read_text())
        assert data["provenance"] == "authored", f
        assert data["note"] == "Hand-written fixture, not a model output", f


def test_tool_round_executes_tools_from_fixture_turns(tmp_path):
    d = tmp_path / "resolve_items"
    d.mkdir()
    lab = {"provenance": "authored", "note": "Hand-written fixture, not a model output"}
    (d / "k__turn0.json").write_text(
        json.dumps({**lab, "tool_calls": [{"name": "echo", "arguments": {"query": "Widget A"}}]})
    )
    (d / "k__turn1.json").write_text(
        json.dumps({**lab, "response": {"items": [{"raw_name": "Widget A", "sku": "WidgetA"}]}})
    )
    calls = []
    tool = Tool("echo", "d", {"type": "object"}, lambda query: calls.append(query) or ["hit"])
    res = FixtureClient(tmp_path, verify=False).tool_call_round(
        "resolve_items", [], [tool], ResolutionResult, key="k"
    )
    assert calls == ["Widget A"]
    assert res.tool_trace[0]["result"] == ["hit"] and res.provenance == "authored"
    assert res.obj.items[0].sku == "WidgetA"


def test_tool_round_is_capped(tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    lab = {"provenance": "authored", "note": "n"}
    for i in range(3):
        (d / f"k__turn{i}.json").write_text(
            json.dumps({**lab, "tool_calls": [{"name": "t", "arguments": {}}]})
        )
    tool = Tool("t", "d", {"type": "object"}, lambda: 1)
    with pytest.raises(LLMUnavailable, match="tool iterations"):
        FixtureClient(tmp_path, verify=False).tool_call_round(
            "a", [], [tool], ResolutionResult, key="k", max_iterations=3
        )
