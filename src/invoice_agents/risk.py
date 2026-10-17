"""Plain risk rules with a simple low / medium / high level.

Rules: prompt injection (high); pressure or urgency language (medium); vendor identity change
(medium); invalid due date (medium); total just under the review threshold (low).
Overall level: high if any high rule or two or more medium rules; medium if exactly one medium
rule; otherwise low. High -> REJECT, medium -> HOLD, low -> a note only.
Validation facts (unknown items, stock) are not risk rules; they are blockers on their own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import Finding, Invoice

RULE_LEVEL = {
    "prompt_injection": "high",
    "pressure_language": "medium",
    "vendor_identity_change": "medium",
    "invalid_due_date": "medium",
    "near_threshold": "low",
}

PRESSURE = re.compile(
    r"\b(urgent|immediately|asap|act now|final notice|avoid penalt\w*|wire transfer)\b", re.I
)
IDENTITY_CHANGE = re.compile(r"\bformerly\b|\bf/k/a\b|\bnow known as\b", re.I)
INJECTION = re.compile(
    r"ignore (all |any )?(the )?(previous|prior|above) (instructions|rules)|system prompt|"
    r"system note|you are now|disregard (the )?(above|previous)|approve this invoice|"
    r"do not flag|override (the )?(policy|rules|checks)",
    re.I,
)
BAD_DUE_WORDS = re.compile(
    r"\bdue(?:\s*date)?\s*:\s*(yesterday|immediate\w*|asap|now|today)\b", re.I
)

NEAR_THRESHOLD = (9_000.0, 10_000.0)  # [9,000.00, 9,999.99]


@dataclass
class Signal:
    name: str
    level: str
    evidence: str


@dataclass
class RiskResult:
    level: str = "low"
    signals: list[Signal] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def overall_level(levels: list[str]) -> str:
    if "high" in levels or levels.count("medium") >= 2:
        return "high"
    return "medium" if levels.count("medium") == 1 else "low"


def assess(invoice: Invoice, text: str) -> RiskResult:
    sigs: list[Signal] = []

    def hit(name: str, evidence: str) -> None:
        sigs.append(Signal(name, RULE_LEVEL[name], evidence))

    if m := INJECTION.search(text):
        hit("prompt_injection", f"found {m.group(0)!r}")
    if m := PRESSURE.search(text):
        hit("pressure_language", f"found {m.group(0)!r}")
    if m := IDENTITY_CHANGE.search(text):
        hit("vendor_identity_change", f"found {m.group(0)!r}")

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

    level = overall_level([s.level for s in sigs])
    res = RiskResult(level=level, signals=sigs)
    for s in sigs:
        res.findings.append(
            Finding(
                code=f"risk_{s.name}",
                severity="note",
                message=f"Risk rule {s.name} ({s.level}): {s.evidence}",
                data={"level": s.level, "evidence": s.evidence},
            )
        )
    if sigs:
        sev: Any = {"high": "reject", "medium": "hold", "low": "note"}[level]
        res.findings.append(
            Finding(
                code="risk_level",
                severity=sev,
                message=f"Risk level {level} ("
                + ", ".join(f"{s.name}={s.level}" for s in sigs)
                + ")",
                data={"level": level, "rules": {s.name: s.level for s in sigs}},
            )
        )
    return res
