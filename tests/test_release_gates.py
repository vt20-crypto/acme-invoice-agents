"""Release gates: no payment without required data, the UI key never persists or leaks, and live
mode never falls back to the cache. The model API is a local stub (no network)."""

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import INVOICES, ROOT, scripted_client
from streamlit.testing.v1 import AppTest

from invoice_agents.approval import ApprovalOutcome
from invoice_agents.db import Database
from invoice_agents.export import result_rows, to_csv, to_json
from invoice_agents.graph import Context, process_invoice
from invoice_agents.ingestion.extractor import extract
from invoice_agents.ingestion.parsers import parse_file
from invoice_agents.llm.client import LiveTransport, make_client
from invoice_agents.models import Critique, Decision
from invoice_agents.payments import pay_invoice
from invoice_agents.review import ReviewError, review_hold

CACHE = ROOT / "cache" / "llm"
APP = ROOT / "app" / "streamlit_app.py"


# --- local stand-in for the model API -----------------------------------------------------


class Stub:
    def __init__(self, status: int):
        self.status, self.hits, self.auth = status, 0, []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                stub.hits += 1
                stub.auth.append(self.headers.get("Authorization", ""))
                if stub.status == 401:  # worst case: the server echoes the credential back
                    body = {"error": {"message": f"bad credential {stub.auth[-1]}", "type": "x"}}
                else:
                    text = json.dumps({"verdict": "pass", "issues": []})
                    body = {
                        "id": "resp_1",
                        "object": "response",
                        "created_at": 0,
                        "model": "m",
                        "status": "completed",
                        "parallel_tool_calls": False,
                        "tool_choice": "auto",
                        "tools": [],
                        "output": [
                            {
                                "type": "message",
                                "id": "m1",
                                "status": "completed",
                                "role": "assistant",
                                "content": [
                                    {"type": "output_text", "text": text, "annotations": []}
                                ],
                            }
                        ],
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "total_tokens": 2,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens_details": {"reasoning_tokens": 0},
                        },
                    }
                data = json.dumps(body).encode()
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def stub401(monkeypatch):
    s = Stub(401)
    monkeypatch.setenv("LLM_BASE_URL", s.url)
    monkeypatch.setenv("LLM_MODEL", "grok-4.3")
    yield s
    s.close()


def run_file(db, path, mode, client, payer=None):
    return process_invoice(str(path), Context(db=db, client=client, llm_mode=mode, payer=payer))


def provenances(state):
    return {c.get("provenance") for c in state.get("llm_calls", [])}


def codes(state):
    return {f.code for f in state["findings"]}


# --- policy: missing invoice number ------------------------------------------------------

BASE_TXT = (INVOICES / "invoice_1001.txt").read_text()


def txt(tmp_path, text, name="inv.txt"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_number_absent_in_structured_source_is_rejected():
    res = extract(parse_file(ROOT / "data" / "synthetic" / "invoice_9015.json"), None)
    assert not res.uncertain and res.invoice.invoice_number is None


def test_confidently_absent_number_in_text_is_rejected(db, tmp_path):
    p = txt(tmp_path, BASE_TXT.replace("Invoice Number: INV-1001\n", ""))
    state = run_file(db, p, "none", None)
    assert state["decision"] is Decision.REJECT and "missing_invoice_number" in codes(state)
    assert db.payments() == []


def test_blank_label_counts_as_absent(db, tmp_path):
    p = txt(tmp_path, BASE_TXT.replace("INV-1001", ""))
    assert run_file(db, p, "none", None)["decision"] is Decision.REJECT


def test_number_shown_but_unread_is_held_not_rejected(db, tmp_path):
    p = txt(tmp_path, BASE_TXT.replace("Invoice Number: INV-1001", "Invoice Number - INV-1001"))
    state = run_file(db, p, "none", None)
    assert state["decision"] is Decision.HOLD
    assert "invoice_number_unreadable" in codes(state) and "missing_invoice_number" not in codes(
        state
    )
    f = next(f for f in state["findings"] if f.code == "invoice_number_unreadable")
    assert f.kind == "execution" and "a person must" in f.message
    assert db.payments() == []
    row = db.get_ledger(state["ledger_id"])  # and a reviewer cannot pay it either
    with pytest.raises(ReviewError):
        review_hold(db, row["id"], "approve", "paid", payer=lambda v, a: {"status": "success"})
    assert db.payments() == []


GOOD = {
    "vendor": "Widgets Inc.",
    "invoice_number": None,
    "invoice_date": "2026-01-15",
    "due_date": "2026-02-01",
    "currency": "USD",
    "line_items": [
        {"raw_name": "WidgetA", "quantity": 10, "unit_price": 250.0},
        {"raw_name": "WidgetB", "quantity": 5, "unit_price": 500.0},
    ],
    "charges": [],
    "subtotal": 5000.0,
    "tax": 0.0,
    "total": 5000.0,
    "notes": [],
}


def test_llm_null_number_follows_the_same_policy(db, tmp_path):
    shown = txt(
        tmp_path, BASE_TXT, "shown.txt"
    )  # the text has a number the model returned null for
    client, _ = scripted_client({"extraction": [GOOD]})
    res = extract(parse_file(shown), client)
    assert res.uncertain and res.failure_code == "invoice_number_unreadable"
    absent = txt(tmp_path, BASE_TXT.replace("Invoice Number: INV-1001\n", ""), "absent.txt")
    client, _ = scripted_client({"extraction": [GOOD]})
    res = extract(parse_file(absent), client)
    assert not res.uncertain and res.invoice.invoice_number is None


# --- G1: no payment without required data -------------------------------------------------


@pytest.mark.parametrize(
    "kw,why",
    [
        (dict(vendor=None), "vendor"),
        (dict(vendor="  "), "vendor"),
        (dict(idempotency_key="widgetsinc|"), "invoice number"),
        (dict(idempotency_key="|inv1"), "vendor|invoice number"),
        (dict(amount=None), "amount"),
        (dict(amount=0.0), "amount"),
        (dict(amount=-5.0), "amount"),
        (dict(currency="EUR"), "USD"),
    ],
)
def test_pay_invoice_refuses_incomplete_payment_data(db, capsys, kw, why):
    calls = []
    args = dict(vendor="Widgets Inc.", amount=10.0, currency="USD", idempotency_key="w|1")
    args.update(kw)
    res = pay_invoice(db, payer=lambda v, a: calls.append(1) or {"status": "success"}, **args)
    assert res.status == "refused" and calls == [] and db.payments() == []
    assert "Paid" not in capsys.readouterr().out


def approving_vp(monkeypatch):
    """Force the approval step to say APPROVE regardless of blockers, as if a model had."""
    monkeypatch.setattr(
        "invoice_agents.graph.run_approval",
        lambda *a, **k: ApprovalOutcome(Decision.APPROVE),
    )


def edited_json(tmp_path, **changes):
    d = json.loads((INVOICES / "invoice_1005.json").read_text())
    d.update(changes)
    d["invoice_number"] = d.get("invoice_number", "INV-X")
    p = tmp_path / "x.json"
    p.write_text(json.dumps({k: v for k, v in d.items() if v is not ABSENT}))
    return p


ABSENT = object()


@pytest.mark.parametrize(
    "changes",
    [
        {"vendor": ABSENT},
        {"invoice_number": ABSENT},
        {"total": ABSENT},
        {"currency": "EUR"},
    ],
)
def test_graph_never_pays_incomplete_invoice_even_if_vp_approves(
    db, tmp_path, capsys, monkeypatch, changes
):
    p = edited_json(tmp_path, **changes)
    calls = []
    payer = lambda v, a: calls.append(1) or {"status": "success"}  # noqa: E731
    # (a) real approval step with a scripted VP that says APPROVE
    client, t = scripted_client(
        {"vp_approval": [{"decision": "APPROVE", "reasons": [{"claim": "ok", "fact_ref": "x"}]}]}
    )
    state = run_file(db, p, "cache", client, payer)
    assert state["decision"] is not Decision.APPROVE and state.get("payment") is None
    assert t.calls == [] or all(a != "vp_approval" for a, _ in t.calls)
    assert calls == [] and db.payments() == []
    # (b) the approval step forced to APPROVE: the payment guard itself must refuse
    approving_vp(monkeypatch)
    db2 = Database(tmp_path / "second.db")
    state = run_file(db2, p, "none", None, payer)
    assert state["decision"] is Decision.HOLD and state["payment"]["status"] == "refused"
    assert calls == [] and db2.payments() == []
    assert "Paid" not in capsys.readouterr().out
    db2.close()


def test_graph_refuses_non_positive_amount_even_if_approved(db, tmp_path, monkeypatch):
    p = edited_json(tmp_path, total=0, subtotal=0, items=[], line_items=[])
    approving_vp(monkeypatch)
    state = run_file(db, p, "none", None, lambda v, a: {"status": "success"})
    assert state["decision"] is Decision.HOLD and db.payments() == []


@pytest.mark.parametrize(
    "column,value",
    [
        ("invoice_number_norm", None),
        ("vendor", None),
        ("total", None),
        ("total", 0.0),
        ("currency", "EUR"),
    ],
)
def test_manual_approval_backend_rechecks_eligibility(db, capsys, column, value):
    state = run_file(db, INVOICES / "invoice_1005.json", "none", None)  # clean business HOLD
    lid = state["ledger_id"]
    db.conn.execute(f"UPDATE invoices SET {column} = ? WHERE id = ?", (value, lid))
    db.conn.commit()
    calls = []
    with pytest.raises(ReviewError, match="Cannot approve"):
        review_hold(
            db, lid, "approve", "ok", payer=lambda v, a: calls.append(1) or {"status": "success"}
        )
    assert calls == [] and db.payments() == []
    assert "Paid" not in capsys.readouterr().out  # the default payer was not reached either


# --- G3: live mode never falls back to the cache ------------------------------------------


def test_the_sample_response_is_in_the_cache():
    state = run_file(Database(":memory:"), INVOICES / "invoice_1001.txt", "cache",
                     make_client("cache", CACHE))  # fmt: skip
    assert state["decision"] is Decision.APPROVE and "recorded" in provenances(state)


def test_live_mode_with_a_bad_key_holds_instead_of_using_the_cache(db, stub401, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "invalid-key")
    client = make_client("live", CACHE)
    state = run_file(db, INVOICES / "invoice_1001.txt", "live", client)
    assert stub401.hits >= 1
    assert state["decision"] is Decision.HOLD and "llm_unavailable" in codes(state)
    assert "recorded" not in provenances(state) and "live" not in provenances(state)
    assert db.payments() == []
    f = next(f for f in state["findings"] if f.code == "llm_unavailable")
    assert f.kind == "execution" and "invalid-key" not in json.dumps(f.model_dump())


def test_auto_mode_cache_miss_with_bad_key_holds(db, stub401, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "invalid-key")
    p = txt(tmp_path, BASE_TXT.replace("Widgets Inc.", "Widgets Incorporated"))  # not in the cache
    state = run_file(db, p, "auto", make_client("auto", CACHE))
    assert stub401.hits >= 1
    assert state["decision"] is Decision.HOLD and "llm_unavailable" in codes(state)
    assert db.payments() == []


def test_auto_mode_cache_hit_is_served_without_calling_the_api(db, stub401, monkeypatch):
    """By design: a recorded response is replayed in auto mode and the key is never used."""
    monkeypatch.setenv("LLM_API_KEY", "invalid-key")
    state = run_file(db, INVOICES / "invoice_1001.txt", "auto", make_client("auto", CACHE))
    assert state["decision"] is Decision.APPROVE and "recorded" in provenances(state)
    assert stub401.hits == 0


# --- G2: the key never persists or leaks --------------------------------------------------


def all_bytes(*roots: Path) -> bytes:
    out = b""
    for r in roots:
        files = [r] if r.is_file() else sorted(p for p in r.rglob("*") if p.is_file())
        out += b"".join(p.read_bytes() for p in files)
    return out


def test_key_from_the_environment_does_not_persist_or_leak_through_the_ui(
    tmp_path, monkeypatch, capfd, caplog
):
    sentinel = "xai-SENTINEL-" + secrets.token_hex(12)
    stub = Stub(401)
    monkeypatch.setenv("LLM_API_KEY", sentinel)  # the only way a key reaches the app
    monkeypatch.setenv("LLM_BASE_URL", stub.url)
    monkeypatch.setenv("ACME_UI_DB", str(tmp_path / "ui.db"))
    cache_before = {p: p.read_bytes() for p in CACHE.rglob("*") if p.is_file()}
    try:
        at = AppTest.from_file(str(APP), default_timeout=90).run()
        assert not [t for t in at.text_input if "key" in t.label.lower()]  # no key field
        at.radio(key="engine").set_value("live").run()
        next(b for b in at.button if b.label == "Run").click().run()
        assert not at.exception
        assert stub.hits >= 1 and sentinel in stub.auth[0]  # the key did reach the client
        shown = " ".join(
            str(e.value)
            for kind in (at.markdown, at.error, at.caption, at.warning, at.info, at.text)
            for e in kind
        )
        assert sentinel not in shown and sentinel[-12:] not in shown
        # 1. database file(s)
        assert sentinel.encode() not in all_bytes(*tmp_path.glob("ui.db*"))
        # 2. cache directory: unchanged, and no sentinel anywhere in it
        after = {p: p.read_bytes() for p in CACHE.rglob("*") if p.is_file()}
        assert after == cache_before and sentinel.encode() not in b"".join(after.values())
        # 3. logs
        out, err = capfd.readouterr()
        assert sentinel not in out + err + caplog.text
        # 4. exports
        db = Database(tmp_path / "ui.db")
        rows = result_rows(db.ledger(), db.payments())
        db.close()
        assert rows and sentinel not in to_csv(rows) + to_json(rows)
        # 5. URL
        assert sentinel not in str(dict(at.query_params))
    finally:
        stub.close()


def test_recorded_live_response_has_no_key_in_request_hash_or_entry(tmp_path):
    sentinel = "xai-SENTINEL-" + secrets.token_hex(12)
    stub = Stub(200)
    try:
        t = LiveTransport(sentinel, stub.url, "grok-4.3", tmp_path / "cache")
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        turn = t.call("critic", "fp", msgs, Critique, None)
        assert json.loads(turn.content)["verdict"] == "pass" and sentinel in stub.auth[0]
        files = list((tmp_path / "cache").rglob("*.json"))
        assert len(files) == 1 and sentinel not in files[0].name
        assert sentinel.encode() not in all_bytes(tmp_path / "cache")
    finally:
        stub.close()


def test_exception_text_from_a_rejected_key_carries_no_credential(stub401):
    from invoice_agents.llm.client import LLMUnavailable

    sentinel = "xai-SENTINEL-" + secrets.token_hex(12)
    t = LiveTransport(sentinel, stub401.url, "grok-4.3", ROOT / "does-not-matter")
    with pytest.raises(LLMUnavailable) as ei:
        t.call("critic", "k", [{"role": "user", "content": "u"}], Critique, None)
    assert sentinel not in str(ei.value) and sentinel[-12:] not in str(ei.value)
