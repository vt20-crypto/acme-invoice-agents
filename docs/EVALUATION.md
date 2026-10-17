# Evaluation

Detailed evaluation results and methodology for the [README](../README.md). Decision rationale
is in [DECISIONS.md](DECISIONS.md); the human-review rules are in [REVIEW_POLICY.md](REVIEW_POLICY.md).

## Per-invoice results

Recorded Grok responses (model `grok-4.3`, recorded 2026-10-17) replayed in `cache` mode over all
40 documents: the 20 samples plus 20 synthetic cases (`invoice_9001` over $10K that should approve
through the VP and critic; `invoice_9002` with an injected instruction; and 18 edge cases,
`invoice_9010` to `invoice_9027`, one behaviour each, see `docs/DECISIONS.md` #14). Expectations
are in `tests/regression/expected.yaml`, written before the run that checks them.

| File | Expected | Actual | Why (plain language) |
|---|---|---|---|
| invoice_1001.txt | APPROVE | APPROVE ✓ | All checks passed. Payment sent. |
| invoice_1002.txt | HOLD | HOLD ✓ | Ordered 20 GadgetX, only 5 in stock. Not paid. |
| invoice_1003.txt | REJECT | REJECT ✓ | Risk is high: pressure or urgency wording, an invalid due date (+2 more). Not paid. |
| invoice_1004.json | APPROVE | APPROVE ✓ | All checks passed. Payment sent. |
| invoice_1004_revised.json | HOLD | HOLD ✓ | Revision of INV-1004: total changed from $1,890.00 to $5,940.00 (+$4,050.00); the original was already paid. Not paid. |
| invoice_1005.json | HOLD | HOLD ✓ | Ordered 8 GadgetX, only 5 in stock. Not paid. |
| invoice_1006.csv | APPROVE | APPROVE ✓ | All checks passed. Payment sent. |
| invoice_1007.csv | REJECT | REJECT ✓ | Invoice states $15,525.00 but lines, charges and tax add up to $15,635.00 (+2 more). Not paid. |
| invoice_1008.txt | REJECT | REJECT ✓ | None of the items exist in inventory: SuperGizmo, MegaSprocket. Not paid. |
| invoice_1009.json | REJECT | REJECT ✓ | The vendor name is missing (+3 more). Not paid. |
| invoice_1010.txt | APPROVE | APPROVE ✓ | All checks passed. Payment sent. |
| invoice_1011.pdf | APPROVE | APPROVE ✓ | All checks passed. Payment sent. |
| invoice_1011.txt | DUPLICATE | DUPLICATE ✓ | Already processed earlier as INV-1011 (approved and paid). Nothing to do. |
| invoice_1012.pdf | HOLD | HOLD ✓ | Risk is medium: the vendor name changed, total just under $10,000. Not paid. |
| invoice_1012.txt | DUPLICATE | DUPLICATE ✓ | Already processed earlier as INV-1012 (held for review). Nothing to do. |
| invoice_1013.json | REJECT | REJECT ✓ | Invoice states $22,562.80 but lines, charges and tax add up to $22,512.80 (+3 more). Not paid. |
| invoice_1013.pdf | DUPLICATE | DUPLICATE ✓ | Already processed earlier as INV-1013 (rejected). Nothing to do. |
| invoice_1014.xml | HOLD | HOLD ✓ | Invoice is in EUR; the payment rail is USD-only, so it is held (no currency conversion). Not paid. |
| invoice_1015.csv | APPROVE | APPROVE ✓ | All checks passed. Payment sent. |
| invoice_1016.json | HOLD | HOLD ✓ | Not found in inventory: WidgetC. Not paid. |
| invoice_9001.json | APPROVE | APPROVE ✓ | All checks passed. Payment sent. |
| invoice_9002.txt | REJECT | REJECT ✓ | Risk is high: instructions aimed at the system hidden in the invoice. Not paid. |
| invoice_9010.json | APPROVE | APPROVE ✓ | All checks passed. Payment sent. |
| invoice_9011.pdf | APPROVE | APPROVE ✓ | All checks passed. Payment sent ($11,550, so VP and critic review). |
| invoice_9012.txt | REJECT | REJECT ✓ | Invoice states $4,300.00 but lines, charges and tax add up to $4,000.00. Not paid. |
| invoice_9013.csv | HOLD | HOLD ✓ | Ordered 7 GadgetX, only 5 in stock. Not paid. |
| invoice_9014.xml | HOLD | HOLD ✓ | Invoice is in EUR; the payment rail is USD-only, so it is held (no currency conversion). Not paid. |
| invoice_9015.json | REJECT | REJECT ✓ | The invoice number is missing. Not paid. |
| invoice_9016.csv | HOLD | HOLD ✓ | Due date 2026-03-01 is before the invoice date 2026-03-10 (+1 more). Not paid. |
| invoice_9017.txt | DUPLICATE | DUPLICATE ✓ | Already processed earlier as NW-2301 (approved and paid). Nothing to do. |
| invoice_9018_revised.json | HOLD | HOLD ✓ | Revision of NW-2301: total changed from $2,700.00 to $3,240.00 (+$540.00); the original was already paid. Not paid. |
| invoice_9019.json | REJECT | REJECT ✓ | Risk is high: instructions aimed at the system hidden in the invoice. Not paid. |
| invoice_9020.txt | HOLD | HOLD ✓ | Risk is medium: pressure or urgency wording. Not paid. |
| invoice_9021.xml | APPROVE | APPROVE ✓ | All checks passed (total $9,975.00 carries the low-risk near-threshold note). Payment sent. |
| invoice_9022.json | APPROVE | APPROVE ✓ | Abbreviated item names mapped to WidgetA and WidgetB by inventory search. Payment sent. |
| invoice_9023.csv | REJECT | REJECT ✓ | None of the items exist in inventory: Titanium Flange Kit, Hydraulic Pump 3000. Not paid. |
| invoice_9024.csv | REJECT | REJECT ✓ | Quantity -2 for WidgetA is not a positive number. Not paid. |
| invoice_9025.txt | HOLD | HOLD ✓ | Ordered 12 WidgetB, only 10 in stock (two lines of 6). Not paid. |
| invoice_9026.txt | APPROVE | APPROVE ✓ | All checks passed. Payment sent. |
| invoice_9027.txt | APPROVE | APPROVE ✓ | Messy OCR-style text read correctly by the model. Payment sent. |

## Mode-specific matches

Two mode-specific matches over the same 40 documents (20 in `data/invoices`, 20 in
`data/synthetic`), each scored against its own expectations in `tests/regression/expected.yaml`:

- Cache mode (`make eval`): **40/40** match the expected decisions, replaying recorded Grok responses.
- Rules only (`main.py eval --llm none`): **40/40** match the rules-only expectations, in which the
  cases that need a model (unreadable text, unmapped item names, VP review over $10,000) are
  expected to HOLD. The two runs are not combined. Human-review (HOLD) counts: rules only 19,
  with recorded Grok responses 12 (measured 2026-10-17).

## Methodology

**Authorization consistency.** `make eval` and `main.py eval` count a document as an invalid
payment when it was paid but its final decision is not APPROVE, and print `0 invalid payments`
for each of the two runs above, in a throwaway database. That is all the number asserts: no
payment without an APPROVE decision, over those 40 documents. The count is printed, but it does
not change the exit code: `run_eval` in `src/invoice_agents/cli.py` exits non-zero only when a
decision differs from the expected one. Payment-safety failures are caught in CI by
`tests/test_blocking_conditions.py` and the regression tests, which fail on any payment for a
non-APPROVE decision. The idempotency-key check
(no key paid twice) is made by the regression test and the database's UNIQUE key, not by the
eval command.

**Policy safety** is a different claim: that each blocking condition actually stops payment. It
is evidenced by `tests/test_blocking_conditions.py`, which runs one minimal invoice per condition
through the whole graph (27 conditions: required fields, arithmetic, unknown items, stock
including per SKU, non-USD, due date, non-positive amounts, duplicate, revision, risk rules,
VP reject/hold, critic, model outage, cache miss, unreadable text). Each case, without using
`expected.yaml`, asserts the decision is not APPROVE and that there is no payment row, no payer
call and no `mock_payment` output, in rules-only mode and again with a model scripted to
approve everything (the code gate wins). A clean control invoice must be paid in the same
harness. Manual-approval eligibility is covered by `tests/test_review.py` and
`tests/test_release_gates.py`. This covers the conditions tested, not every possible input, and
is not a guarantee.

## Recorded versus fresh live

**Recorded versus fresh live.** "Recorded Grok responses" are live responses captured on
2026-10-17 and replayed deterministically in cache mode. The 40-file evaluation, `make test` and
CI replay those previously captured responses; none of them is a fresh live run. The only fresh
live execution after the key rotation is one smoke test on 2026-10-17 covering **one invoice**,
`data/synthetic/invoice_9011.pdf`, with explicit `--llm live` into an empty cache directory
(extraction, VP draft and revision, critic twice: 5 model calls, all provenance `live`, decision
APPROVE as expected). The other 39 documents were not re-run live.

## Model calls per invoice

**Model calls per invoice.** There is no application-level call limit and no counter: the bound
comes from the pipeline structure. Per invoice, at most 3 extraction calls (1 plus
`MAX_EXTRACT_RETRIES = 2`, `config.py:5`, loop at `ingestion/extractor.py:339`; text and PDF
only), at most 6 item-resolution turns (one tool-calling round per invoice for the lines code
could not resolve, `resolve.py:77`, capped by `MAX_TOOL_ITERATIONS = 6`, `config.py:7`,
`llm/client.py:375`), and in approval at most 2 VP drafts (the draft plus `MAX_VP_REVISIONS = 1`,
`config.py:6`, `approval.py:133`) and, for totals over $10,000, at most 2 critic calls
(`approval.py:145`). That is at most 13 model calls (11 without the critic), not counting the
OpenAI SDK's own default HTTP retries, which the repository does not configure. A full approval
path of an over-$10,000 PDF is 5 calls: extraction, VP draft, critic, VP revision, critic (the
`invoice_9011.pdf` smoke test). The "3" was only the release smoke-test budget, which that single
test exceeded because one full approval path makes 5 calls; it is not a limit in the application.

## What the model adds

**What the model adds when rules-only also "matches".** The rules-only run is scored against its
own expectations, in which every case that needs a model is expected to HOLD. Measured over the
same 40 documents: rules only routes **19** to a human (HOLD) and cache mode routes **12**. The
seven the model resolves:

| File | Rules only | With recorded model | What the model did |
|---|---|---|---|
| invoice_1008.txt | HOLD | REJECT | item search found none of the items in inventory |
| invoice_9022.json | HOLD | APPROVE | item search mapped abbreviated names to WidgetA and WidgetB |
| invoice_9023.csv | HOLD | REJECT | item search found none of the items in inventory |
| invoice_1012.txt | HOLD | DUPLICATE | read the text so it matched the earlier PDF |
| invoice_9027.txt | HOLD | APPROVE | read messy OCR-style text (Vndr, Inv#, Qty, Amt) |
| invoice_9001.json | HOLD | APPROVE | VP and critic review of a total over $10,000 |
| invoice_9011.pdf | HOLD | APPROVE | VP and critic review of a total over $10,000 |

So the model reduces the manual queue and reads messy text; it never decides on its own what is
paid, because code applies every blocker after it.

The first full live run matched 19 of 22 expectations. Three fixes followed (see
`docs/DECISIONS.md` #13): one grounding bug for quantities like "x12", and two prompt
clarifications (abbreviated labels such as "Amt:"; the critic only requests revisions for
substantive problems). The responses were then re-recorded. The 22/22 result is on the same
documents the prompts were adjusted against, so it is not a held-out evaluation.

## Sources and cost

The sample invoices come from the case repository
https://github.com/galatiq-ai/galatiq-case-invoices.

The regression test (`tests/regression/test_regression.py`) asserts every decision in cache mode
and in rules-only mode, that nothing but APPROVE is paid, and that no idempotency key is paid twice. It fails if a cached response is
missing. The cache's own spend estimate for everything recorded so far is about $0.14 by the grok-4.3
price list (the 18 synthetic cases added 20 calls, 14,677 input and 8,355 output tokens).
