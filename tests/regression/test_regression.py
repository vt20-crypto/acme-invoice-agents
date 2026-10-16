import sys
from pathlib import Path

import pytest
import yaml
from conftest import FIXTURES, ROOT

from invoice_agents.batch import run_batch
from invoice_agents.db import Database
from invoice_agents.llm.client import FixtureClient

HERE = Path(__file__).parent


def load(name):
    return yaml.safe_load((HERE / name).read_text())


def run(tmp_path, entries, client, mode):
    db = Database(tmp_path / "reg.db")
    paths = [ROOT / e["file"] for e in entries]
    results = run_batch(paths, db, client, mode)
    return db, {e["file"]: state for e, (state, _) in zip(entries, results, strict=True)}


@pytest.fixture(scope="module")
def none_run(tmp_path_factory):
    entries = load("expected_none.yaml")
    db, states = run(tmp_path_factory.mktemp("none"), entries, None, "none")
    yield db, states
    db.close()


@pytest.fixture(scope="module")
def fixture_run(tmp_path_factory):
    entries = load("expected_fixture.yaml")
    db, states = run(tmp_path_factory.mktemp("fx"), entries, FixtureClient(FIXTURES), "fixture")
    yield db, states
    db.close()


@pytest.mark.parametrize("entry", load("expected_none.yaml"), ids=lambda e: Path(e["file"]).name)
def test_none_mode(entry, none_run):
    _, states = none_run
    state = states[entry["file"]]
    assert state["decision"].value == entry["decision"]
    assert sorted({f.code for f in state["findings"]}) == sorted(entry["codes"])


def test_none_mode_covers_all_20_samples_plus_9001():
    files = {Path(e["file"]).name for e in load("expected_none.yaml")}
    on_disk = {p.name for p in (ROOT / "data/invoices").iterdir()} | {"invoice_9001.json"}
    assert files == on_disk and len(files) == 21


def test_none_mode_payments_match_approvals(none_run):
    db, states = none_run
    approved = [s for s in states.values() if s["decision"].value == "APPROVE"]
    assert len(db.payments()) == len(approved) == 6
    assert all(p["status"] == "sent" for p in db.payments())


@pytest.mark.parametrize("entry", load("expected_fixture.yaml"), ids=lambda e: Path(e["file"]).name)
def test_fixture_mode(entry, fixture_run):
    _, states = fixture_run
    state = states[entry["file"]]
    assert state["decision"].value == entry["decision"]
    assert sorted({f.code for f in state["findings"]}) == sorted(entry["codes"])
    if "paid" in entry:
        assert state["payment"]["amount"] == entry["paid"]
    if entry.get("critic"):
        assert any(c["agent"] == "critic" for c in state["llm_calls"])
    assert all(c.get("provenance") in (None, "authored") for c in state["llm_calls"]), (
        "fixture runs must only report authored provenance"
    )


def test_fixture_mode_file_without_fixtures_holds_llm_unavailable(tmp_path):
    entries = [{"file": "data/invoices/invoice_1012.txt"}]
    _, states = run(tmp_path, entries, FixtureClient(FIXTURES), "fixture")
    st = states["data/invoices/invoice_1012.txt"]
    assert st["decision"].value == "HOLD"
    assert {"uncertain_extraction", "llm_unavailable"} <= {f.code for f in st["findings"]}


sys.dont_write_bytecode = True
