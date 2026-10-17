from pathlib import Path

import pytest

from invoice_agents.db import Database

ROOT = Path(__file__).resolve().parent.parent
INVOICES = ROOT / "data" / "invoices"
FIXTURES = ROOT / "fixtures" / "llm"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


class ScriptedTransport:
    """Test double for the model transport: returns queued turns per agent and records calls.
    Used only in unit tests; real runs use LiveTransport or CacheTransport."""

    provenance = "recorded"
    model = "test-model"

    def __init__(self, script):
        # script: {agent: [content-str | dict | ModelTurn, ...]}
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def call(self, agent, key, messages, schema_model, tools):
        import json as _json

        from invoice_agents.llm.client import LLMUnavailable, ModelTurn

        self.calls.append((agent, messages))
        queue = self.script.get(agent) or []
        if not queue:
            raise LLMUnavailable(f"no scripted answer for {agent}")
        item = queue.pop(0)
        if isinstance(item, ModelTurn):
            return item
        return ModelTurn(item if isinstance(item, str) else _json.dumps(item))


def scripted_client(script):
    from invoice_agents.llm.client import LLMClient

    t = ScriptedTransport(script)
    return LLMClient(t), t
