"""Fixture maintenance.

    python -m invoice_agents.record stamp   # bind the hand-written (authored) fixtures to the
                                            # current prompts/schemas/inputs; rewrite the manifest
    python -m invoice_agents.record live    # needs LLM_API_KEY: run the demonstration set against
                                            # the real model and write `recorded` fixtures

``stamp`` runs offline. ``live`` has never been executed (no key was available when it was
written). Both operate on the same required set, listed in ``DEFAULT_FILES``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .db import Database
from .graph import Context, process_invoice
from .llm.client import FixtureClient, LiveClient, LLMClient, ToolRoundResult
from .llm.compat import compat_meta
from .logs import jlog

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_FILES = [
    "data/invoices/invoice_1001.txt",
    "data/invoices/invoice_1002.txt",
    "data/invoices/invoice_1012.pdf",
    "data/synthetic/invoice_9001.json",
    "data/synthetic/invoice_9003.txt",
]
NOTE_RECORDED = "Recorded live model response"


def write_manifest(path: Path, entries: list[dict[str, Any]]) -> None:
    entries = sorted(entries, key=lambda e: (e["source_file"], e["path"]))
    path.write_text(json.dumps({"version": 1, "fixtures": entries}, indent=2) + "\n")


def stamp(files: list[str], fixtures: Path, manifest: Path) -> list[dict[str, Any]]:
    client = FixtureClient(fixtures, stamp=True)
    db = Database(":memory:")
    entries: list[dict[str, Any]] = []
    for f in files:
        client.current_source = f
        client.stamped.clear()
        process_invoice(str(ROOT / f), Context(db=db, client=client, llm_mode="fixture"))
        entries += [dict(e) for e in client.stamped]
    db.close()
    write_manifest(manifest, entries)
    return entries


class RecordingClient(LLMClient):
    """Wraps a live client and writes every response as a ``recorded`` fixture.
    NOT EXERCISED: requires a real key. Tool rounds are stored as turn0 (all tool calls seen)
    and turn1 (the final answer), which is a simplification of multi-turn exchanges."""

    def __init__(self, live: LiveClient, fixtures: Path):
        self.live, self.root = live, fixtures
        self.current_source: str | None = None
        self.entries: list[dict[str, Any]] = []

    def _write(self, agent, key, suffix, body, messages, schema_model, prompt) -> None:
        meta = compat_meta(agent, messages, schema_model, prompt, self.live.model)
        d = self.root / agent
        d.mkdir(parents=True, exist_ok=True)
        data = {"provenance": "recorded", "note": NOTE_RECORDED, "meta": meta, **body}
        (d / f"{key}{suffix}.json").write_text(json.dumps(data, indent=2) + "\n")
        self.entries.append(
            {"path": f"{agent}/{key}{suffix}.json", "agent": agent,
             "step": suffix.lstrip("_") or "initial", "source_file": self.current_source,
             "provenance": "recorded", **meta}
        )  # fmt: skip

    def structured(self, agent, messages, schema_model, *, key=None, step="", prompt_hash=None):
        obj, prov = self.live.structured(agent, messages, schema_model, key=key, step=step)
        suffix = f"__{step}" if step else ""
        self._write(agent, key, suffix, {"response": obj.model_dump(mode="json")},
                    messages, schema_model, prompt_hash)  # fmt: skip
        return obj, prov

    def tool_call_round(self, agent, messages, tools, schema_model, **kw) -> ToolRoundResult:
        kw.pop("prompt_hash", None)
        prompt = None
        res = self.live.tool_call_round(agent, messages, tools, schema_model, **kw)
        calls = [{"name": t["tool"], "arguments": t["arguments"]} for t in res.tool_trace]
        key = kw.get("key")
        self._write(agent, key, "__turn0", {"tool_calls": calls}, messages, schema_model, prompt)
        self._write(
            agent, key, "__turn1", {"response": _dump(res.obj)}, messages, schema_model, prompt
        )
        return res


def _dump(obj: BaseModel) -> dict[str, Any]:
    return obj.model_dump(mode="json")


def record_live(files: list[str], fixtures: Path, manifest: Path) -> None:
    client = RecordingClient(LiveClient.from_env(), fixtures)
    db = Database(":memory:")
    for f in files:
        client.current_source = f
        process_invoice(str(ROOT / f), Context(db=db, client=client, llm_mode="live"))
    write_manifest(manifest, client.entries)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m invoice_agents.record")
    p.add_argument("command", choices=["stamp", "live"])
    p.add_argument("--files", nargs="+", default=DEFAULT_FILES)
    p.add_argument("--fixtures", default=str(ROOT / "fixtures" / "llm"))
    p.add_argument("--manifest", default=str(ROOT / "fixtures" / "manifest.json"))
    a = p.parse_args(argv)
    if a.command == "stamp":
        entries = stamp(a.files, Path(a.fixtures), Path(a.manifest))
    else:
        record_live(a.files, Path(a.fixtures), Path(a.manifest))
        entries = []
    jlog("record_done", command=a.command, fixtures=len(entries))
    print(f"{a.command}: manifest written to {a.manifest} ({len(entries)} fixture entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
