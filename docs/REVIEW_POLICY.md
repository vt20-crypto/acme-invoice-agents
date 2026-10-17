# Human review policy

Detail for the human-review queue described in the [README](../README.md#architecture).
Held invoices go to a review queue; approving one still passes the same payment guard.

## What a reviewer can resolve

A reviewer must leave a note and can always **reject** an open
HOLD (`review_hold()` in `src/invoice_agents/review.py`; the invoice becomes REJECT,
`rejected_by_human`). **Approve** is allowed only when `approval_eligibility()` (same file) finds
no blocker. Approve is refused for:

- a ledger row that is not an open HOLD (already decided or already reviewed);
- a missing vendor, a missing invoice number (no payment key can be formed), or a total that is
  missing or not positive;
- a currency other than USD (the rail is USD-only, no FX conversion);
- any finding with severity `reject` ("A REJECT blocker cannot be overridden"), for example
  `missing_vendor`, `missing_invoice_number`, `missing_total`, `missing_items`, `bad_quantity`,
  `line_arithmetic`, `subtotal_mismatch`, `arithmetic`, `all_items_unknown`, a `vp_reject`, or a
  high `risk_level`;
- a system issue that left fields unverified: an execution-kind finding from the extraction or
  item-resolution stage (`stage` of `extraction` or `resolve`; for example `llm_unavailable`,
  `cache_missing`, `extraction_failed`, `invoice_number_unreadable`); the reviewer is told to re-run once it
  is fixed.

Any other HOLD can be approved, for example `invalid_due_date`, `arithmetic_unverifiable`,
`item_needs_mapping`, `unknown_items`, `stock_exceeded`, `revision`, a `vp_hold`, and the
approval-stage system issues `vp_requires_llm`, `critic_unresolved` and an LLM failure in the VP
step (the system-issue block applies only to the extraction and item-resolution stages).

Approval then calls `pay_invoice` with the key `vendor|invoice number`. If the guard refuses or
the payer fails, the invoice stays on HOLD with the reason recorded. If a payment record with that
key already exists, nothing is sent again and the invoice is marked
`approved_by_human_already_paid`. That status only means the payment key already exists; it does
not independently verify that an earlier payment succeeded. Because a failed or pending attempt
also keeps its key (see [Known limitations](../README.md#known-limitations-of-the-prototype)), such cases need manual investigation of the payment
record. Only a successful payment in this review makes the invoice APPROVE (`approved_by_human`).
