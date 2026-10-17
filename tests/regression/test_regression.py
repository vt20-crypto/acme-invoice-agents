"""The regression test: every sample and synthetic document in cache mode (recorded Grok
responses, no key needed) against tests/regression/expected.yaml.
It fails, never skips, if a cached response is missing."""

import pytest

from invoice_agents.evaluation import evaluate, load_expected


@pytest.fixture(scope="module")
def run():
    return evaluate("cache")


@pytest.mark.parametrize("entry", load_expected(), ids=lambda e: e["file"].split("/")[-1])
def test_decision_matches_expected(entry, run):
    row = next(r for r in run["rows"] if r["file"] == entry["file"].split("/")[-1])
    assert "cache_missing" not in row["codes"], "a cached LLM response is missing; re-record it"
    assert row["actual"] == entry["decision"], f"{row['reason']} (expected: {entry['why']})"


def test_all_documents_ran_and_none_is_a_system_hold(run):
    assert run["total"] == len(load_expected())
    assert not [r["file"] for r in run["rows"] if r["cause"] in ("system", "both")]


def test_no_payment_for_anything_but_approve_and_one_per_key(run):
    assert not [r["file"] for r in run["rows"] if r["paid"] and r["actual"] != "APPROVE"]
    approved = [r for r in run["rows"] if r["actual"] == "APPROVE"]
    assert len(run["payments"]) == len(approved)
    keys = [p["idempotency_key"] for p in run["payments"]]
    assert len(keys) == len(set(keys))
    assert all(p["status"] == "sent" for p in run["payments"])


def test_rules_only_run_matches_expected_without_any_model():
    run = evaluate("none")
    assert run["total"] == len(load_expected())
    assert not [(r["file"], r["actual"], r["expected"]) for r in run["rows"] if not r["match"]]
    assert not [r["file"] for r in run["rows"] if r["paid"] and r["actual"] != "APPROVE"]
