"""Deterministic risk signals with fixed rubric weights.

Weights were fixed before any sample-wide run. Score >= 6 -> REJECT, 3-5 -> HOLD, below 3 -> note.
Validation facts (unknown items, stock) are NOT counted here; they are blockers on their own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import Finding, Invoice

REJECT_SCORE = 6
HOLD_SCORE = 3

WEIGHTS = {
    "pressure_language": 3,
    "vendor_identity_change": 3,
    "prompt_injection": 6,
    "invalid_due_date": 2,
    "near_threshold": 1,
    "round_total": 1,
    "first_time_vendor": 0,  # informational: true for every vendor in a fresh ledger
}

PRESSURE = re.compile(
    r"\b(urgent|immediately|asap|act now|final notice|avoid penalt\w*|wire transfer)\b", re.I
)
IDENTITY_CHANGE = re.compile(r"\bformerly\b|\bf/k/a\b|\bnow known as\b", re.I)
INJECTION = re.compile(
    r"ignore (all |any )?(the )?(previous|prior|above) (instructions|rules)|system prompt|"
    r"you are now|disregard (the )?(above|previous)|approve this invoice|do not flag|"
    r"override (the )?(policy|rules|checks)",
    re.I,
)
BAD_DUE_WORDS = re.compile(
    r"\bdue(?:\s*date)?\s*:\s*(yesterday|immediate\w*|asap|now|today)\b", re.I
)

NEAR_THRESHOLD = (9_000.0, 10_000.0)  # [9,000.00, 9,999.99]


@dataclass
class Signal:
    name: str
    weight: int
    evidence: str


@dataclass
class RiskResult:
    score: int = 0
    signals: list[Signal] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def assess(invoice: Invoice, text: str, *, known_vendor: bool = False) -> RiskResult:
    sigs: list[Signal] = []

    def hit(name: str, evidence: str) -> None:
        sigs.append(Signal(name, WEIGHTS[name], evidence))

    if m := PRESSURE.search(text):
        hit("pressure_language", f"found {m.group(0)!r}")
    if m := IDENTITY_CHANGE.search(text):
        hit("vendor_identity_change", f"found {m.group(0)!r}")
    if m := INJECTION.search(text):
        hit("prompt_injection", f"found {m.group(0)!r}")

    bad_due = None
    if m := BAD_DUE_WORDS.search(text):
        bad_due = f"due date reads {m.group(1)!r}"
    elif any("due_date not parseable" in n for n in invoice.notes):
        bad_due = next(n for n in invoice.notes if "due_date not parseable" in n)
    elif invoice.due_date and invoice.invoice_date and invoice.due_date < invoice.invoice_date:
        bad_due = f"due {invoice.due_date} is before invoice date {invoice.invoice_date}"
    if bad_due:
        hit("invalid_due_date", bad_due)

    t = invoice.total
    if t is not None and NEAR_THRESHOLD[0] <= t < NEAR_THRESHOLD[1]:
        hit("near_threshold", f"total {t:,.2f} is just under the 10,000 review threshold")
    if t is not None and t >= 10_000 and round(t, 2) % 10_000 == 0:
        hit("round_total", f"total {t:,.2f} is an exact multiple of 10,000")
    if not known_vendor:
        hit("first_time_vendor", "vendor not seen in the ledger (informational, weight 0)")

    res = RiskResult(score=sum(s.weight for s in sigs), signals=sigs)
    for s in sigs:
        if s.weight > 0:
            res.findings.append(
                Finding(
                    code=f"risk_{s.name}",
                    severity="note",
                    message=f"Risk signal {s.name} (+{s.weight}): {s.evidence}",
                    data={"weight": s.weight, "evidence": s.evidence},
                )
            )
    sev: Any
    if res.score >= REJECT_SCORE:
        sev = "reject"
    elif res.score >= HOLD_SCORE:
        sev = "hold"
    else:
        sev = "note"
    if res.score > 0:
        res.findings.append(
            Finding(
                code="risk_score",
                severity=sev,
                message=f"Risk score {res.score} ("
                + ", ".join(f"{s.name}={s.weight}" for s in sigs if s.weight)
                + ")",
                data={"score": res.score, "signals": {s.name: s.weight for s in sigs if s.weight}},
            )
        )
    return res
