# Decision log

Short, factual entries.

## 1. LLM API: Responses API with structured outputs
A one-call smoke test against the live xAI API (grok-4.3) confirmed the Responses API accepts a
Pydantic JSON schema (`text.format = json_schema`), `reasoning.effort` of `none` and `low`, and
function tools with `function_call_output` follow-ups. Chat Completions is marked deprecated by
xAI, so the Responses API is used. `presence_penalty`, `frequency_penalty` and `stop` are never
sent. Extraction uses `reasoning_effort=none`; resolve, VP and critic use `low`.

## 2. LLM cache replaces fixtures
Every live response is written to `cache/llm/<step>/<doc-fingerprint16>-<request-hash12>.json`
(provenance `recorded`, model, timestamp, token usage, response; no secrets). The request hash
covers the exact messages, schema, tools, model and settings, so a changed prompt or input is a
different, missing entry. Modes: `live` calls Grok and records, `cache` replays only (a miss is a
HOLD with a system issue), `none` is code only. The regression test runs in cache mode, needs no
key and fails on any missing entry.

## 3. Address denylist: added, then removed
It flagged landmark or fictional addresses. Removed because there is no principled data source for
"an address that cannot be a real vendor" in this MVP. A real deployment would use address
verification plus the vendor master. No invoice-specific exceptions replace it, so invoice_1004
(742 Evergreen Terrace) is evaluated on its content and approves.

## 4. Risk is a simple level, not a score
Five rules: prompt injection (high); pressure language, vendor identity change ("formerly"),
invalid due date (medium each); total in 9,000-9,999.99 (low). Overall: high if any high rule or two
or more medium; medium if exactly one medium; else low. High is REJECT, medium is HOLD, low is a
note. Unknown items and stock are validation blockers and are not counted as risk. There is no
weighting, no round-total rule and no first-time-vendor rule (it fires on every invoice in a fresh
ledger, so it carries no information).

## 5. Arithmetic rejects: invoice_1007 and invoice_1013.json
Total must equal lines plus charges plus tax within 0.01.
- invoice_1007.csv: 14,750.00 + 6% tax 885.00 = 15,635.00, but the file states 15,525.00.
- invoice_1013.json: 21,040.00 + 7% tax 1,472.80 = 22,512.80, but the file states 22,562.80.

## 6. Tiered item resolution
Code first: exact match after normalization (case, punctuation, spaces, parentheticals).
Unresolved names go to the LLM with a `search_inventory` tool. Whatever SKU the LLM proposes,
validation re-checks it exactly in the database. All items unknown is a REJECT only when a search
actually ran and found nothing; without a search (`--llm none`, or the LLM step failed) the finding
is `item_needs_mapping`, a HOLD and a system issue.

## 7. Due-date validity is judged against the invoice date
A due date is invalid if unparseable, a word such as "yesterday", or earlier than the invoice
date. Today's date is not used: the samples are dated early 2026 and would all look overdue.

## 8. USD-only payment rail, no FX
`mock_payment(vendor, amount)` has no currency field, so it is treated as a USD rail. Non-USD
invoices are held, and the payment wrapper refuses non-USD as a second guard. There is no
conversion and no one-click non-USD approval anywhere.

## 9. Duplicate versus revision
Same normalized vendor and invoice number with the same economic content (lines, charges, tax,
total, currency) is a DUPLICATE: linked to the original, never paid again. Same key with changed
content is a revision: a HOLD that shows the total delta. A revision is never auto-paid.

## 10. System issues are separate from business findings
Findings are `business` (vendor or invoice defects) or `execution` (no LLM, cache miss, extraction
or item mapping not possible). The UI and CLI show "HOLD - system issue" separately. A missing
cache entry is never reported as a vendor defect.

## 11. Human review and eligibility
A reviewer can approve or reject a held invoice with a note. Approve is refused (backend and UI)
when vendor, invoice number or a positive total is missing, the currency is not USD, a REJECT
blocker exists, or a system issue left fields unverified. Payment still goes through the code
guard (idempotency key, USD only).

## 12. Baseline
`tests/regression/expected.yaml` was written before the first live run and records the documented
decision for each of the 22 documents. A live result that differs is reported, not edited away.
