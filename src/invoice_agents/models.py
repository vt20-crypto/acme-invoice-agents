"""Core data models. Schemas stay simple so they work as structured-output schemas."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class LineItem(BaseModel):
    raw_name: str
    sku: str | None = None
    quantity: float
    unit_price: float | None = None
    line_total: float | None = None


class Charge(BaseModel):
    kind: str  # shipping | fee | discount | other
    description: str
    amount: float  # discounts are negative


class Invoice(BaseModel):
    vendor: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    currency: str = "USD"
    line_items: list[LineItem] = Field(default_factory=list)
    charges: list[Charge] = Field(default_factory=list)
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None
    notes: list[str] = Field(default_factory=list)


class Decision(StrEnum):
    APPROVE = "APPROVE"
    HOLD = "HOLD"
    REJECT = "REJECT"
    DUPLICATE = "DUPLICATE"


# Execution findings describe the system failing to do its job (no LLM, missing cache entry,
# extraction or mapping not completed). They are never a vendor or invoice defect.
EXECUTION_CODES = frozenset(
    {
        "llm_unavailable",
        "cache_missing",
        "extraction_failed",
        "item_needs_mapping",
        "critic_unresolved",
        "vp_requires_llm",
        "payment_failed",
    }
)


class Finding(BaseModel):
    code: str
    severity: Literal["reject", "hold", "note"]
    message: str
    evidence: str = ""
    kind: Literal["business", "execution"] = "business"
    stage: str = ""  # extraction | resolve | approval | payment (set on execution findings)
    data: dict[str, Any] = Field(default_factory=dict)  # facts for plain-language rendering

    @model_validator(mode="after")
    def _classify(self) -> Finding:
        if self.code in EXECUTION_CODES:
            self.kind = "execution"
        return self


# --- LLM response schemas -------------------------------------------------------------


class ResolvedItem(BaseModel):
    raw_name: str
    sku: str | None = None


class ResolutionResult(BaseModel):
    items: list[ResolvedItem]


class Reason(BaseModel):
    claim: str
    fact_ref: str


class VPDraft(BaseModel):
    decision: Literal["APPROVE", "HOLD", "REJECT"]
    reasons: list[Reason]


class Critique(BaseModel):
    verdict: Literal["pass", "revise"]
    issues: list[str] = Field(default_factory=list)
