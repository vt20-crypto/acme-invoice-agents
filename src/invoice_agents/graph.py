"""LangGraph pipeline: ingest -> resolve_items -> validate -> approve -> pay -> finalize."""

from __future__ import annotations

import operator
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .approval import ApprovalOutcome, code_verdict, run_approval
from .db import Database
from .ingestion.extractor import ExtractionResult, extract
from .ingestion.parsers import RawDocument, parse_file
from .llm.client import LLMClient
from .logs import jlog
from .models import Decision, Finding, Invoice, Reason
from .payments import pay_invoice
from .resolve import ResolveOutcome, resolve_items
from .validation import ValidationResult, number_key, validate, vendor_key


@dataclass
class Context:
    db: Database
    client: LLMClient | None
    llm_mode: str  # fixture | none | live
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    payer: Any = None  # injectable for tests; defaults to mock_payment


class PipelineState(TypedDict, total=False):
    path: str
    doc: RawDocument
    extraction: ExtractionResult
    invoice: Invoice | None
    resolve: ResolveOutcome
    validation: ValidationResult
    approval: ApprovalOutcome
    findings: Annotated[list[Finding], operator.add]
    steps: Annotated[list[dict[str, Any]], operator.add]
    llm_calls: Annotated[list[dict[str, Any]], operator.add]
    decision: Decision
    reasons: list[Reason]
    duplicate_of: int | None
    payment: dict[str, Any] | None
    ledger_id: int


def _step(name: str, lines: list[str]) -> list[dict[str, Any]]:
    return [{"step": name, "lines": lines}]


def build_graph(ctx: Context):
    db = ctx.db

    def audit(state: PipelineState, step: str, payload: dict[str, Any]) -> None:
        db.audit(ctx.run_id, state["path"], step, payload)
        jlog("step", run_id=ctx.run_id, file=state["path"], step=step, **payload)

    # --- nodes ---------------------------------------------------------------------------

    def ingest(state: PipelineState) -> dict[str, Any]:
        doc = parse_file(state["path"])
        ex = extract(doc, ctx.client)
        findings: list[Finding] = []
        if ex.uncertain:
            findings.append(
                Finding(
                    code="uncertain_extraction",
                    severity="hold",
                    message="Extraction is uncertain; a human must check the fields.",
                    evidence="; ".join(ex.reasons),
                )
            )
            if any("LLM unavailable" in r for r in ex.reasons):
                findings.append(
                    Finding(
                        code="llm_unavailable",
                        severity="hold",
                        message="LLM unavailable for extraction.",
                    )
                )
        inv = ex.invoice
        lines = [f"format={doc.format}  method={ex.method}  fingerprint={doc.key}"]
        if ex.provenance:
            lines.append(f"LLM provenance: {ex.provenance}  (calls: {ex.attempts})")
        if inv:
            lines += [
                f"vendor={inv.vendor!r}  invoice_number={inv.invoice_number!r}  "
                f"date={inv.invoice_date}  due={inv.due_date}  currency={inv.currency}",
                *(
                    f"item: {li.raw_name!r} qty={li.quantity:g} unit_price={li.unit_price}"
                    f" line_total={li.line_total}"
                    for li in inv.line_items
                ),
                *(f"charge: {c.kind} {c.amount:,.2f}" for c in inv.charges),
                f"subtotal={inv.subtotal}  tax={inv.tax}  total={inv.total}",
            ]
        if ex.uncertain:
            lines.append("EXTRACTION UNCERTAIN: " + "; ".join(ex.reasons))
        audit(
            state,
            "ingest",
            {
                "method": ex.method,
                "uncertain": ex.uncertain,
                "provenance": ex.provenance,
                "reasons": ex.reasons,
            },
        )
        return {
            "doc": doc,
            "extraction": ex,
            "invoice": inv,
            "findings": findings,
            "steps": _step("ingest", lines),
            "llm_calls": ex.llm_calls,
        }

    def resolve(state: PipelineState) -> dict[str, Any]:
        inv, doc = state["invoice"], state["doc"]
        assert inv is not None
        out = resolve_items(inv, db, ctx.client, doc.key)
        findings: list[Finding] = []
        lines = [
            f"method={out.method}" + (f"  provenance={out.provenance}" if out.provenance else "")
        ]
        for t in out.tool_trace:
            lines.append(f"tool call {t['tool']}({t['arguments']}) -> {t['result']}")
        for li, sku in zip(inv.line_items, out.proposed, strict=True):
            lines.append(f"{li.raw_name!r} -> {sku}")
        if out.error:
            lines.append(f"LLM resolution failed, used code normalization: {out.error}")
            findings.append(
                Finding(
                    code="llm_unavailable",
                    severity="hold",
                    message="LLM unavailable for item resolution.",
                    evidence=out.error,
                )
            )
        calls = (
            [{"agent": "resolve_items", "step": "tool_round", "provenance": out.provenance}]
            if out.provenance
            else []
        )
        audit(
            state,
            "resolve_items",
            {"method": out.method, "proposed": out.proposed, "tool_calls": len(out.tool_trace)},
        )
        return {
            "resolve": out,
            "findings": findings,
            "steps": _step("resolve_items", lines),
            "llm_calls": calls,
        }

    def validate_node(state: PipelineState) -> dict[str, Any]:
        inv = state["invoice"]
        assert inv is not None
        res = validate(inv, state["resolve"].proposed, db)
        lines = [f"[{f.severity}] {f.code}: {f.message}" for f in res.findings] or ["no findings"]
        lines.insert(0, f"content_hash={res.content_hash[:12] if res.content_hash else None}")
        audit(state, "validate", {"findings": [f.model_dump() for f in res.findings]})
        return {
            "validation": res,
            "findings": res.findings,
            "duplicate_of": res.duplicate_of,
            "steps": _step("validate", lines),
        }

    def approve(state: PipelineState) -> dict[str, Any]:
        inv, doc, val = state["invoice"], state["doc"], state["validation"]
        assert inv is not None
        out = run_approval(inv, state["findings"], val.facts, ctx.client, doc.key)
        lines = [f"decision={out.decision.value}", *out.notes]
        lines += [f"reason: {r.claim} [{r.fact_ref}]" for r in out.reasons]
        if out.critic_ran:
            lines.append(f"critic pass ran (revisions: {out.revisions})")
        lines += [f"[{f.severity}] {f.code}: {f.message}" for f in out.findings]
        for c in out.llm_calls:
            lines.append(f"LLM {c['agent']}/{c['step']}: provenance={c['provenance']}")
        audit(
            state,
            "approve",
            {"decision": out.decision.value, "reasons": [r.model_dump() for r in out.reasons]},
        )
        return {
            "approval": out,
            "decision": out.decision,
            "reasons": out.reasons,
            "findings": out.findings,
            "llm_calls": out.llm_calls,
            "steps": _step("approve", lines),
        }

    def pay(state: PipelineState) -> dict[str, Any]:
        inv = state["invoice"]
        assert inv is not None and inv.vendor and inv.total is not None
        key = f"{vendor_key(inv)}|{number_key(inv)}"
        res = pay_invoice(
            db,
            vendor=inv.vendor,
            amount=inv.total,
            currency=inv.currency,
            idempotency_key=key,
            payer=ctx.payer,
        )
        update: dict[str, Any] = {
            "payment": {
                "status": res.status,
                "detail": res.detail,
                "amount": inv.total,
                "vendor": inv.vendor,
            }
        }
        lines = [f"payment {res.status}: {res.detail}"]
        if res.status == "duplicate":
            update["decision"] = Decision.DUPLICATE
        elif res.status in ("refused", "failed"):
            update["decision"] = Decision.HOLD
            update["findings"] = [
                Finding(code=f"payment_{res.status}", severity="hold", message=res.detail)
            ]
        audit(state, "pay", update["payment"])
        update["steps"] = _step("pay", lines)
        return update

    def finalize(state: PipelineState) -> dict[str, Any]:
        inv, doc = state.get("invoice"), state["doc"]
        findings = state.get("findings", [])
        decision = state.get("decision")
        if decision is None:
            decision = Decision.DUPLICATE if state.get("duplicate_of") else code_verdict(findings)
            if state["extraction"].uncertain:
                decision = Decision.HOLD
        val = state.get("validation")
        ledger_id = db.record_invoice(
            source_file=state["path"],
            fingerprint=doc.fingerprint,
            vendor_norm=vendor_key(inv) if inv else None,
            invoice_number_norm=number_key(inv) if inv else None,
            content_hash=val.content_hash if val else None,
            decision=decision.value,
            total=inv.total if inv else None,
            currency=inv.currency if inv else None,
            duplicate_of=state.get("duplicate_of"),
            run_id=ctx.run_id,
        )
        lines = [f"final decision={decision.value}  ledger id={ledger_id}"]
        if decision is not Decision.APPROVE:
            lines += [
                f"[{f.severity}] {f.code}: {f.message}" for f in findings if f.severity != "note"
            ]
        audit(state, "finalize", {"decision": decision.value, "ledger_id": ledger_id})
        return {"decision": decision, "ledger_id": ledger_id, "steps": _step("finalize", lines)}

    # --- routing -------------------------------------------------------------------------

    def after_ingest(state: PipelineState) -> str:
        return (
            "finalize"
            if state["extraction"].uncertain or state.get("invoice") is None
            else "resolve"
        )

    def after_validate(state: PipelineState) -> str:
        return "finalize" if state.get("duplicate_of") else "approve"

    def after_approve(state: PipelineState) -> str:
        return "pay" if state["decision"] is Decision.APPROVE else "finalize"

    g = StateGraph(PipelineState)
    g.add_node("ingest", ingest)
    g.add_node("resolve", resolve)
    g.add_node("validate", validate_node)
    g.add_node("approve", approve)
    g.add_node("pay", pay)
    g.add_node("finalize", finalize)
    g.add_edge(START, "ingest")
    g.add_conditional_edges("ingest", after_ingest, ["resolve", "finalize"])
    g.add_edge("resolve", "validate")
    g.add_conditional_edges("validate", after_validate, ["approve", "finalize"])
    g.add_conditional_edges("approve", after_approve, ["pay", "finalize"])
    g.add_edge("pay", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


def process_invoice(path: str, ctx: Context) -> PipelineState:
    graph = build_graph(ctx)
    return graph.invoke({"path": path, "findings": [], "steps": [], "llm_calls": []})
