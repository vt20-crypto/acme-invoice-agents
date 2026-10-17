import json
from collections import Counter

import pytest
import yaml
from conftest import ROOT

from invoice_agents.evaluation import classify, counts, evaluate, load_baseline

REG = ROOT / "tests" / "regression"


def load(name):
    return yaml.safe_load((REG / name).read_text())


def test_baseline_json_matches_the_yaml_plan4_fields():
    base = load_baseline()
    for name in ("expected_none.yaml", "expected_fixture.yaml"):
        for e in load(name):
            assert (base[e["file"]] or "none") == e["plan4"], (name, e["file"])
    assert base["data/synthetic/invoice_9003.txt"] is None and len(base) == 22


@pytest.mark.parametrize(
    "mode,yaml_name", [("none", "expected_none.yaml"), ("fixture", "expected_fixture.yaml")]
)
def test_live_evaluation_reproduces_the_yaml_snapshot_and_classes(mode, yaml_name):
    rows = evaluate(mode)
    expected = load(yaml_name)
    assert [r["file"] for r in rows] == [e["file"].split("/")[-1] for e in expected]
    for r, e in zip(rows, expected, strict=True):
        assert r["actual"] == e["decision"], r["file"]
        assert r["class"] == e["class"], (r["file"], r["class"], e["class"])
    assert sum(counts(rows).values()) == 22
    assert counts(rows) == dict(Counter(e["class"] for e in expected))


def test_section4_counts_per_mode():
    assert counts(evaluate("none")) == {"match": 15, "expected exception": 6, "no baseline": 1}
    assert counts(evaluate("fixture")) == {"match": 10, "missing fixture": 11, "no baseline": 1}


def test_mismatch_is_flagged_not_hidden():
    rec = {"codes": [], "cause": "business", "execution_failures": [], "decision": "APPROVE"}
    assert classify("none", rec, "REJECT") == "mismatch"
    assert classify("none", rec, None) == "no baseline"


def test_live_mode_cannot_be_evaluated_offline():
    with pytest.raises(ValueError):
        evaluate("live")
    assert json.loads((ROOT / "docs" / "baseline_plan4.json").read_text())
