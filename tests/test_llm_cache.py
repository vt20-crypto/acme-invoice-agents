import json

import pytest
from conftest import scripted_client

from invoice_agents.llm.client import (
    BUDGET_STOP_USD,
    BudgetExceeded,
    CacheMiss,
    CacheTransport,
    LiveTransport,
    LLMClient,
    ModelTurn,
    Tool,
    cache_spend,
    cost_usd,
    request_hash,
)
from invoice_agents.models import Critique, ResolutionResult

MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def write_entry(root, agent, key, rhash, response, usage=None):
    d = root / agent
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key}-{rhash}.json").write_text(
        json.dumps(
            {"provenance": "recorded", "model": "m", "usage": usage or {}, "response": response}
        )
    )


def test_request_hash_changes_with_messages_schema_tools_and_model():
    base = request_hash("critic", MSGS, Critique, None, "m")
    assert base == request_hash("critic", MSGS, Critique, None, "m")
    assert base != request_hash(
        "critic", [*MSGS, {"role": "user", "content": "x"}], Critique, None, "m"
    )
    assert base != request_hash("critic", MSGS, ResolutionResult, None, "m")
    assert base != request_hash("critic", MSGS, Critique, None, "other-model")
    tool = Tool("t", "d", {"type": "object"}, lambda: 1)
    assert base != request_hash("critic", MSGS, Critique, [tool], "m")
    assert len(base) == 12


def test_cache_hit_returns_recorded_response_with_recorded_provenance(tmp_path):
    rhash = request_hash("critic", MSGS, Critique, None, "m")
    write_entry(tmp_path, "critic", "abcd", rhash, {"content": '{"verdict": "pass", "issues": []}'})
    client = LLMClient(CacheTransport(tmp_path, "m"))
    obj, prov = client.structured("critic", MSGS, Critique, key="abcd")
    assert obj.verdict == "pass" and prov == "recorded"


def test_cache_miss_is_an_error_not_a_silent_fallback(tmp_path):
    client = LLMClient(CacheTransport(tmp_path, "m"))
    with pytest.raises(CacheMiss):
        client.structured("critic", MSGS, Critique, key="abcd")
    # a changed prompt is a different request, hence a miss
    rhash = request_hash("critic", MSGS, Critique, None, "m")
    write_entry(tmp_path, "critic", "abcd", rhash, {"content": "{}"})
    with pytest.raises(CacheMiss):
        client.structured(
            "critic", [*MSGS, {"role": "user", "content": "changed"}], Critique, key="abcd"
        )


def test_tool_round_executes_tools_and_feeds_results_back():
    calls = []
    tool = Tool(
        "search_inventory", "d", {"type": "object"}, lambda query: calls.append(query) or ["hit"]
    )
    turn1 = ModelTurn(
        None, [{"id": "c1", "name": "search_inventory", "arguments": '{"query": "gadget"}'}]
    )
    final = {"items": [{"raw_name": "GX gadget", "sku": "GadgetX"}]}
    client, transport = scripted_client({"resolve_items": [turn1, final]})
    res = client.tool_call_round("resolve_items", MSGS, [tool], ResolutionResult, key="k")
    assert calls == ["gadget"] and res.obj.items[0].sku == "GadgetX"
    assert res.tool_trace[0]["result"] == ["hit"]
    second_call_messages = transport.calls[1][1]
    assert second_call_messages[-1] == {"role": "tool", "tool_call_id": "c1", "content": '["hit"]'}


def test_tool_round_is_capped():
    tool = Tool("t", "d", {"type": "object"}, lambda: 1)
    loop = ModelTurn(None, [{"id": "c", "name": "t", "arguments": "{}"}])
    client, _ = scripted_client({"resolve_items": [loop] * 5})
    with pytest.raises(Exception, match="tool iterations"):
        client.tool_call_round("resolve_items", MSGS, [tool], ResolutionResult, max_iterations=3)


def test_cost_estimate_and_budget_stop(tmp_path):
    assert cost_usd({"input_tokens": 1_000_000, "output_tokens": 1_000_000}) == 3.75
    write_entry(tmp_path, "x", "k", "h", {}, usage={"cost_usd_estimate": BUDGET_STOP_USD})
    assert cache_spend(tmp_path) == BUDGET_STOP_USD
    live = LiveTransport("not-a-real-key", "https://api.x.ai/v1", "m", tmp_path)
    with pytest.raises(BudgetExceeded):
        live.call("critic", "k", MSGS, Critique, None)  # stops before any network call


def test_cache_entries_never_contain_secrets_fields(tmp_path):
    # the entry format written by LiveTransport has no header/key fields
    import inspect

    src = inspect.getsource(LiveTransport.call)
    assert "api_key" not in src and "Authorization" not in src


def test_api_key_argument_enables_live_for_this_client_only(monkeypatch):
    # An explicit key (the UI's per-session key) turns on live calls in auto mode without
    # touching the environment.
    from invoice_agents.llm.client import AutoTransport, make_client

    monkeypatch.setenv("LLM_API_KEY", "")
    assert make_client("auto").transport.live is None
    client = make_client("auto", api_key="test-key")
    assert isinstance(client.transport, AutoTransport) and client.transport.live is not None
    import os

    assert os.environ["LLM_API_KEY"] == ""
