"""Core data models. Schemas stay simple so they work as structured-output schemas."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


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


class Finding(BaseModel):
    code: str
    severity: Literal["reject", "hold", "note"]
    message: str
    evidence: str = ""


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
