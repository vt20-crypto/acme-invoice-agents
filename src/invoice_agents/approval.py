"""VP approval with a critique loop, behind a code-enforced gate.

The code verdict comes from blockers (see ``code_verdict``). If any blocker exists the LLM is not
even asked. If there are none, a VP agent drafts a decision with reasons that cite fact_refs from
a fact sheet; totals over the review threshold also go through a critic. The final decision is
the strictest of the code verdict and the LLM verdict, so a model can never turn a blocked
invoice into an APPROVE.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .config import MAX_VP_REVISIONS, VP_REVIEW_THRESHOLD
from .llm.client import LLMClient, LLMUnavailable, SchemaValidationFailed, failure_code
from .models import Critique, Decision, Finding, Invoice, Reason, VPDraft

_RANK = {Decision.APPROVE: 0, Decision.HOLD: 1, Decision.REJECT: 2}

VP_SYSTEM = (
    "You are the VP of Finance at Acme Corp reviewing one invoice that already passed automated "
    "checks. Decide APPROVE, HOLD or REJECT. Totals over $10,000 need a stricter standard. Give "
    "reasons as {claim, fact_ref}: every fact_ref must be a key of the fact sheet and the claim "
    "must be supported by that fact. Reply with JSON matching the schema."
)
CRITIC_SYSTEM = (
    "You are a compliance critic. Check the VP's draft decision against the fact sheet and "
    "policy (over $10,000 requires a strict standard). Every reason must cite a fact_ref that "
    "exists and supports the claim. Reply verdict 'pass' or 'revise' with a list of issues."
)


def strictest(*decisions: Decision) -> Decision:
    return max(decisions, key=lambda d: _RANK[d])


def code_verdict(findings: list[Finding]) -> Decision:
    if any(f.severity == "reject" for f in findings):
        return Decision.REJECT
    if any(f.severity == "hold" for f in findings):
        return Decision.HOLD
    return Decision.APPROVE


@dataclass
class ApprovalOutcome:
    decision: Decision
    reasons: list[Reason] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)  # added by this step
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    revisions: int = 0
    critic_ran: bool = False
    notes: list[str] = field(default_factory=list)


def check_draft(draft: VPDraft, facts: dict[str, str]) -> list[str]:
    """Deterministic part of the critique: reasons exist and cite real facts."""
    issues = []
    if not draft.reasons:
        issues.append("The draft gives no reasons.")
    for r in draft.reasons:
        if r.fact_ref not in facts:
            issues.append(f"fact_ref {r.fact_ref!r} is not in the fact sheet.")
        if not r.claim.strip():
            issues.append(f"Reason citing {r.fact_ref!r} has an empty claim.")
    return issues


def run_approval(
    invoice: Invoice,
    findings: list[Finding],
    facts: dict[str, str],
    client: LLMClient | None,
    key: str,
) -> ApprovalOutcome:
    verdict = code_verdict(findings)
    if verdict is not Decision.APPROVE:
        blockers = [f.code for f in findings if f.severity in ("reject", "hold")]
        return ApprovalOutcome(
            verdict, notes=[f"Blocked by code ({', '.join(blockers)}); VP agent not consulted."]
        )
    total = invoice.total or 0.0
    over = total > VP_REVIEW_THRESHOLD

    if client is None:  # --llm none: rules only
        if over:
            f = Finding(
                code="vp_requires_llm",
                severity="hold",
                message=(
                    f"Total {total:,.2f} exceeds {VP_REVIEW_THRESHOLD:,.0f}; "
                    "VP review requires LLM."
                ),
                stage="approval",
            )
            return ApprovalOutcome(Decision.HOLD, findings=[f], notes=["Rules only (no LLM)."])
        return ApprovalOutcome(
            Decision.APPROVE,
            reasons=[
                Reason(
                    claim="No blockers and total within VP threshold (rules only).",
                    fact_ref="check.blockers",
                )
            ],
            notes=["Rules only (no LLM)."],
        )

    out = ApprovalOutcome(Decision.HOLD)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": VP_SYSTEM},
        {"role": "user", "content": json.dumps({"fact_sheet": facts}, indent=1)},
    ]
    step = ""
    try:
        for rnd in range(MAX_VP_REVISIONS + 1):
            draft, prov = client.structured("vp_approval", messages, VPDraft, key=key, step=step)
            out.llm_calls.append(
                {"agent": "vp_approval", "step": step or "draft", "provenance": prov}
            )
            issues = check_draft(draft, facts)
            if over:
                critique, cprov = client.structured(
                    "critic",
                    [
                        {"role": "system", "content": CRITIC_SYSTEM},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {"fact_sheet": facts, "draft": draft.model_dump()}
                            ),
                        },
                    ],
                    Critique,
                    key=key,
                    step=f"critique{rnd}",
                )
                out.critic_ran = True
                out.llm_calls.append(
                    {"agent": "critic", "step": f"critique{rnd}", "provenance": cprov}
                )
                if critique.verdict == "revise":
                    issues += critique.issues or ["Critic requested a revision."]
            if not issues:
                out.reasons = draft.reasons
                llm_decision = Decision(draft.decision)
                out.decision = strictest(verdict, llm_decision)
                if llm_decision is not Decision.APPROVE:
                    out.findings.append(
                        Finding(
                            code=f"vp_{llm_decision.value.lower()}",
                            severity="reject" if llm_decision is Decision.REJECT else "hold",
                            message="VP agent did not approve: "
                            + "; ".join(r.claim for r in draft.reasons),
                        )
                    )
                return out
            if rnd == MAX_VP_REVISIONS:
                break
            out.revisions += 1
            step = f"revise{out.revisions}"
            messages += [
                {"role": "assistant", "content": draft.model_dump_json()},
                {"role": "user", "content": "Revise the draft. Issues:\n- " + "\n- ".join(issues)},
            ]
        out.findings.append(
            Finding(
                code="critic_unresolved",
                severity="hold",
                message=f"VP draft still had issues after {MAX_VP_REVISIONS} revisions.",
                evidence="; ".join(issues),
                stage="approval",
            )
        )
    except (LLMUnavailable, SchemaValidationFailed) as exc:
        out.findings.append(
            Finding(
                code=failure_code(exc),
                severity="hold",
                message=f"System issue: the LLM approval step could not run: {exc}",
                stage="approval",
            )
        )
    out.decision = Decision.HOLD
    return out
