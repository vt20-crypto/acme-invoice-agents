"""Every fixture the manifest requires must exist and be compatible with the CURRENT prompts,
schemas and inputs. This test fails when a prompt, schema or input changes without re-stamping
(`python -m invoice_agents.record stamp`) or re-recording (`... record live`)."""

import json

import pytest
from conftest import FIXTURES, ROOT

from invoice_agents.batch import run_batch
from invoice_agents.db import Database
from invoice_agents.llm.client import FixtureClient, FixtureStale
from invoice_agents.llm.compat import compat_meta, prompt_hash
from invoice_agents.models import Critique

MANIFEST = ROOT / "fixtures" / "manifest.json"


def entries():
    return json.loads(MANIFEST.read_text())["fixtures"]


def test_manifest_lists_required_fixtures_and_each_file_matches_it():
    es = entries()
    assert es, "manifest is empty"
    for e in es:
        path = FIXTURES / e["path"]
        assert path.exists(), f"required fixture missing: {e['path']}"
        data = json.loads(path.read_text())
        assert data["provenance"] == e["provenance"], e["path"]
        for k in ("prompt_hash", "schema_hash", "input_hash", "model", "settings"):
            assert data["meta"][k] == e[k], f"{e['path']}: manifest {k} differs from the file"
        assert (ROOT / e["source_file"]).exists()


def test_all_manifest_fixtures_are_current_against_prompts_and_schemas(tmp_path):
    """Re-run the pipeline over every manifest document with strict verification."""
    sources = sorted({e["source_file"] for e in entries()})
    client = FixtureClient(FIXTURES)  # verify=True
    db = Database(tmp_path / "m.db")
    results = run_batch([ROOT / s for s in sources], db, client, "fixture")
    db.close()
    for state, _ in results:
        stale = [f for f in state["findings"] if f.code in ("fixture_missing", "fixture_stale")]
        assert not stale, (state["path"], [(f.code, f.evidence or f.message) for f in stale])
    required = {e["path"] for e in entries()}
    assert required <= set(client.loaded), f"unused/unreachable: {required - set(client.loaded)}"


def _write(root, meta, provenance="authored"):
    d = root / "critic"
    d.mkdir(parents=True)
    (d / "k.json").write_text(
        json.dumps(
            {
                "provenance": provenance,
                "note": "n",
                "meta": meta,
                "response": {"verdict": "pass", "issues": []},
            }  # fmt: skip
        )
    )


MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
PH = prompt_hash("s", "tmpl")


def call(root, messages=MSGS, ph=PH, **kw):
    return FixtureClient(root, **kw).structured(
        "critic", messages, Critique, key="k", prompt_hash=ph
    )


def test_matching_fixture_is_used(tmp_path):
    _write(tmp_path, compat_meta("critic", MSGS, Critique, PH, "authored"))
    obj, prov = call(tmp_path)
    assert obj.verdict == "pass" and prov == "authored"


@pytest.mark.parametrize(
    "what",
    ["prompt", "input", "schema", "settings", "no_meta"],
)
def test_stale_fixture_is_refused_not_silently_used(tmp_path, what, monkeypatch):
    meta = compat_meta("critic", MSGS, Critique, PH, "authored")
    messages, ph = MSGS, PH
    if what == "prompt":
        ph = prompt_hash("a different system prompt", "tmpl")
    elif what == "input":
        messages = [*MSGS, {"role": "user", "content": "extra"}]
    elif what == "schema":
        meta["schema_hash"] = "0" * 64
    elif what == "settings":
        meta["settings"] = {"reasoning_effort": "high"}
    elif what == "no_meta":
        meta = None
    _write(tmp_path, meta)
    with pytest.raises(FixtureStale):
        call(tmp_path, messages, ph)


def test_recorded_fixture_for_another_model_is_stale(tmp_path):
    _write(tmp_path, compat_meta("critic", MSGS, Critique, PH, "grok-4.3"), "recorded")
    assert call(tmp_path, expected_model="grok-4.3")[1] == "recorded"
    with pytest.raises(FixtureStale, match="model"):
        call(tmp_path, expected_model="grok-5")


def test_pipeline_turns_a_stale_fixture_into_an_execution_hold(tmp_path, monkeypatch):
    from invoice_agents.ingestion import extractor

    monkeypatch.setattr(extractor, "EXTRACTION_SYSTEM", extractor.EXTRACTION_SYSTEM + " changed")
    db = Database(tmp_path / "s.db")
    ((state, _),) = run_batch(
        [ROOT / "data/invoices/invoice_1001.txt"], db, FixtureClient(FIXTURES), "fixture"
    )
    db.close()
    assert state["decision"].value == "HOLD"
    stale = [f for f in state["findings"] if f.code == "fixture_stale"]
    assert stale and stale[0].kind == "execution" and stale[0].stage == "extraction"
