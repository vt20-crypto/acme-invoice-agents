from conftest import scripted_client

from invoice_agents.approval import code_verdict, run_approval, strictest
from invoice_agents.models import Decision, Finding, Invoice
from invoice_agents.payments import pay_invoice


def test_payment_is_idempotent(db):
    calls = []
    payer = lambda v, a: calls.append((v, a)) or {"status": "success"}  # noqa: E731
    kw = dict(
        vendor="Widgets Inc.",
        amount=5000.0,
        currency="USD",
        idempotency_key="widgetsinc|inv1001",
        payer=payer,
    )
    assert pay_invoice(db, **kw).status == "paid"
    assert pay_invoice(db, **kw).status == "duplicate"
    assert calls == [("Widgets Inc.", 5000.0)]
    assert [p["status"] for p in db.payments()] == ["sent"]


def test_payment_refuses_non_usd(db):
    calls = []
    res = pay_invoice(
        db,
        vendor="V",
        amount=1.0,
        currency="EUR",
        idempotency_key="k",
        payer=lambda v, a: calls.append(1),
    )
    assert res.status == "refused" and not calls and db.payments() == []


def test_payer_failure_is_recorded_and_key_stays_consumed(db):
    def boom(v, a):
        raise RuntimeError("bank down")

    kw = dict(vendor="V", amount=1.0, currency="USD", idempotency_key="k")
    assert pay_invoice(db, payer=boom, **kw).status == "failed"
    assert pay_invoice(db, payer=lambda v, a: {"status": "success"}, **kw).status == "duplicate"


FACTS = {"invoice.total": "5,000.00", "check.blockers": "none", "validation.items": "ok"}


def draft(ref="invoice.total", decision="APPROVE"):
    return {"decision": decision, "reasons": [{"claim": "fine", "fact_ref": ref}]}


PASS = {"verdict": "pass", "issues": []}


def inv(total):
    return Invoice(vendor="V", invoice_number="1", total=total)


def test_strictest_ordering():
    assert strictest(Decision.APPROVE, Decision.HOLD) is Decision.HOLD
    assert strictest(Decision.HOLD, Decision.REJECT, Decision.APPROVE) is Decision.REJECT


def test_gate_blocks_approve_even_if_llm_would_approve():
    client, transport = scripted_client({"vp_approval": [draft()]})
    blocker = Finding(code="stock_exceeded", severity="hold", message="x")
    out = run_approval(inv(5000), [blocker], FACTS, client, "k")
    assert out.decision is Decision.HOLD and transport.calls == []  # LLM never consulted
    rej = Finding(code="arithmetic", severity="reject", message="x")
    assert run_approval(inv(5000), [blocker, rej], FACTS, client, "k").decision is Decision.REJECT
    assert code_verdict([]) is Decision.APPROVE


def test_llm_can_make_decision_stricter_but_not_looser():
    client, _ = scripted_client({"vp_approval": [draft(decision="HOLD")]})
    out = run_approval(inv(5000), [], FACTS, client, "k")
    assert out.decision is Decision.HOLD and any(f.code == "vp_hold" for f in out.findings)


def test_vp_citing_unknown_fact_is_revised_once_then_approved():
    client, transport = scripted_client({"vp_approval": [draft("made.up.fact"), draft()]})
    out = run_approval(inv(5000), [], FACTS, client, "k")
    assert out.decision is Decision.APPROVE and out.revisions == 1
    assert "made.up.fact" in transport.calls[1][1][-1]["content"]


def test_unresolved_after_one_revision_holds():
    client, transport = scripted_client({"vp_approval": [draft("nope"), draft("nope")]})
    out = run_approval(inv(5000), [], FACTS, client, "k")
    assert out.decision is Decision.HOLD and out.revisions == 1 and len(transport.calls) == 2
    assert any(f.code == "critic_unresolved" for f in out.findings)


def test_over_threshold_runs_critic():
    client, transport = scripted_client({"vp_approval": [draft()], "critic": [PASS]})
    out = run_approval(inv(12000), [], FACTS, client, "k")
    assert out.decision is Decision.APPROVE and out.critic_ran
    assert [a for a, _ in transport.calls] == ["vp_approval", "critic"]


def test_critic_revise_triggers_one_revision():
    revise = {"verdict": "revise", "issues": ["justify the amount"]}
    client, _ = scripted_client({"vp_approval": [draft(), draft()], "critic": [revise, PASS]})
    out = run_approval(inv(12000), [], FACTS, client, "k")
    assert out.decision is Decision.APPROVE and out.revisions == 1


def test_llm_unavailable_during_approval_holds_with_a_system_code():
    client, _ = scripted_client({})
    out = run_approval(inv(5000), [], FACTS, client, "k")
    assert out.decision is Decision.HOLD and out.findings[0].code == "llm_unavailable"
    assert out.findings[0].kind == "execution"


def test_none_mode_thresholds():
    assert run_approval(inv(9999), [], FACTS, None, "k").decision is Decision.APPROVE
    over = run_approval(inv(10001), [], FACTS, None, "k")
    assert over.decision is Decision.HOLD and over.findings[0].code == "vp_requires_llm"
