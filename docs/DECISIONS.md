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

## 13. What the first live run changed
The first full live batch matched 19 of 22 expectations. The three differences were fixed in code
and prompts, not in `expected.yaml`:
- invoice_1008: the grounding check could not see quantities written like "x12" (a digit glued to a
  letter), so a correct extraction was rejected. The number matcher now accepts them.
- invoice_1002: the model left the total null because the source labels it "Amt:". The extraction
  prompt now says labels may be abbreviated or misspelled and a single stated amount is the total.
- invoice_9001: the critic asked for rewording twice, so the one allowed revision ran out and the
  invoice held. The critic prompt now asks for a revision only for substantive problems, and the
  fact sheet gives the risk level and the over-$10,000 policy as citable facts.

Those prompt changes meant discarding the first cache and re-recording once. The second live batch
matched 22 of 22. Total live spend for the smoke test and both batches is about $0.10.

## 14. Synthetic edge-case suite (invoice_9010 to invoice_9027)
Eighteen invoices, one behaviour each, over the seeded inventory (WidgetA 15 at 250, WidgetB 10 at
500, GadgetX 5 at 750). Every number reconciles to the cent except where the case intends
otherwise. The expected decision and a one-line reason per case were derived from the policy in
#4 to #11 and committed before any run. Formats: JSON 5, TXT 6, CSV 4, XML 2, PDF 1 (the PDF is
a text PDF written by a small throwaway script, as no PDF writer is a dependency).

Results, each mode on its own:
- Rules only (`--llm none`, scratch database): first run 38/40. The two differences:
  invoice_9015 (a defect, see #15) and invoice_1012.txt (its expected DUPLICATE needs the OCR-style
  PDF to be read first, which rules only cannot do, so it holds with extraction_failed; a `none`
  entry was added for it, with that reason). After the fix, 40/40.
- Live (`--llm auto` on data/synthetic, which records missing responses and replays the rest): all
  20 documents matched their expectations. 20 live calls were made (budget cap was 80), 14,677
  input and 8,355 output tokens, about $0.04 by the cache's own estimate. invoice_9001 and
  invoice_9002 were already recorded.
- Strict replay (`make eval`, cache mode): 40/40 match, no cache misses. No prompt changed, so
  nothing existing had to be re-recorded.
The suite was written to test the system, but its documents are few and the model saw them only
once, so it is a regression set, not a held-out benchmark.

`expected.yaml` entries may carry a `none` decision for the rules-only run. 1008 and 9001 got
theirs before the first run (item mapping and VP review need a model); 1012.txt after it, as
described above. No `decision` of an existing entry was changed.

## 15. A missing invoice number is a REJECT
The suite found a real defect. An invoice with no invoice number passed validation and was paid
under the idempotency key `<vendor>|None` (rules-only run, invoice_9015, before the fix). That
contradicts the payment design (key = vendor + invoice number) and #11 (a reviewer cannot approve
an invoice without a number either). Validation now raises `missing_invoice_number` as a REJECT,
like a missing vendor, with a unit test. The expectation was written from the policy and was not
changed.

## 16. Export and comparison
Result rows (file, invoice number, vendor, total, currency, decision, reasons, processed_at,
ai_provenance, paid) come from one function in `export.py` used by the CLI `--out` and the UI
downloads. `ai_provenance` is stored in the ledger details at decision time (recorded, live,
rules_only or code). `paid` is yes only for a row whose final decision is APPROVE and whose payment
key was sent, so a duplicate never shows as paid. The expected-results comparison matches by file
name (basename, case-insensitive), ignores case and spacing in decisions, rejects a CSV without
the `file` and `expected_decision` columns, and lists expected files that were not processed. A
file called `expected_results.csv` is never collected as an invoice. (An earlier version had a
session-only API key field in the UI; see #18, it was removed.)

## 17. Missing invoice number: reject only when confidently absent
Our policy, not a requirement of the brief. Without a number there is no idempotency key
(vendor + number), so the same invoice could be paid twice. So an invoice with no number never
pays. Whether it is a REJECT or a HOLD depends on how sure we are that the number is absent:
- REJECT (`missing_invoice_number`): confidently absent. A JSON, CSV or XML file without the field
  or with it empty (invoice_9015), or a text/PDF extraction that is otherwise complete and whose
  text carries no invoice-number label with a value (including a label left blank).
- HOLD (`invoice_number_unreadable`, a system issue): the text shows an invoice-number label with
  a value, but extraction found no number (a label style the rules cannot read, or a model that
  returned null). A person must check the document and confirm the number. A reviewer still
  cannot approve and pay it, because no payment key can be formed.
Extraction that is uncertain for any other reason already holds (`extraction_failed`). Tests for
both branches and for the blank-label case are in `tests/test_release_gates.py`. The check found
a latent bug: the label-based extractor read across lines, so `Invoice Number:` left blank took
the next line's word ("Date") as the number; it now stays on the line.

## 18. Release gates
- Payment guard: `pay_invoice` itself refuses without vendor, invoice number (a complete key),
  an amount above zero, or USD, whatever the approval step said. Before this check the graph's
  pay step asserted vendor and total only and would have used a key like `vendor|None` if the
  approval step were ever forced to APPROVE. The manual path re-checks `approval_eligibility`
  inside `review_hold`, not only in the button state.
- Live mode never reads the cache; a rejected key is a HOLD with `llm_unavailable`. Auto mode
  replays a recorded response without using the key (by design) and holds on a miss with a
  rejected key. xAI answers an invalid key with HTTP 400, not 401; every API failure is handled
  the same way and only the exception type name is kept.
- The session-only API key field in the UI was removed. One live attempt from the UI with the
  owner's key was rejected by the provider, so the feature could not be shown to work. The key
  now comes from the environment or `.env` only. Leak tests run the UI against a local stub that
  echoes the credential and assert it appears in no database file, cache file, log, export or
  page text.
- Scope of "0 invalid payments": see #19 and the README Results section.

## 19. Authorization consistency is not policy safety

`0 invalid payments` in the eval means only that no document in the run was paid unless its final
decision was APPROVE, over the 40 sample and synthetic documents, once per mode. It is an
authorization-consistency check; it does not show that each blocking condition stops payment
(a document that is wrongly APPROVEd and paid would also count as consistent). Policy safety is
evidenced separately by `tests/test_blocking_conditions.py`: 27 conditions, each a minimal
invoice run through the whole graph, asserting a non-APPROVE decision and no payment row, payer
call or `mock_payment` output, independent of `expected.yaml`, in rules-only mode and with a
model scripted to approve. The eval prints the invalid-payment count but does not use it for its
exit code: `run_eval` (`src/invoice_agents/cli.py`) exits non-zero only on a decision mismatch, so payment safety is enforced by `tests/test_blocking_conditions.py` and the regression tests, not by the eval exit status. Limits: tested inputs only, not a guarantee.

Likewise, "recorded Grok responses" (captured live on 2026-10-17, replayed in cache mode by tests,
CI and the eval) are distinct from fresh live execution. The only fresh live run since is one smoke
test of `data/synthetic/invoice_9011.pdf` (5 model calls, APPROVE as expected).
There is no 3-call application limit: the "3" was only the smoke-test budget, exceeded because one full approval path makes 5 calls. The per-invoice bound comes from the pipeline structure (max 3 extraction, 6 resolution turns, 2 VP drafts, 2 critic calls = 13; max 1 revision), not from a counter.
