# Acme Invoice Agents

A multi-agent prototype for the Galatiq invoice-processing case. It takes a vendor invoice (PDF, TXT,
CSV, JSON or XML), extracts the fields, validates them against a SQLite inventory, runs a VP-style
approval with a critique loop, and makes a mock payment. The model never has payment authority:
code decides, and code pays once per invoice.

## For reviewers

1. **No key needed.** Every model response for the samples, the synthetic cases and the reviewer
   pack is recorded in `cache/llm/`, so `make test`, `make eval` and the UI work offline.
2. **Try the reviewer pack.** `make ui`, open Dashboard, Add invoices, upload the files in
   `data/reviewer_pack/` (all except `expected_results.csv`) and process them. Then open
   Check against expected and upload `data/reviewer_pack/expected_results.csv`: the table shows
   a check mark or a cross per file and "6 of 6 match". From the command line:
   `make eval-pack` (or `uv run python main.py eval --llm cache --expected data/reviewer_pack/expected_results.csv`).
3. **Use your own key.** Paste an xAI key into the Settings popover (kept in this browser
   session only, never saved), or copy `.env.example` to `.env` and set `LLM_API_KEY`. Any
   OpenAI-compatible provider works through `LLM_BASE_URL` and `LLM_MODEL` in `.env`.
   In Auto mode a file with no recorded response goes to the live model, and the response is
   recorded.
4. **Process your own files.** Upload them on the Dashboard, then use Download results (CSV or
   JSON), or run `uv run python main.py --batch DIR --out results.csv`. To compare with your own
   expectations, make a CSV with the columns `file,expected_decision` (the Dashboard offers a
   template with your file names filled in) and upload it under Check against expected, or run
   `main.py eval --expected FILE.csv DIR`.

## Quick start

```bash
make install                      # uv sync
make test                         # no API key needed: tests replay recorded responses
make run                          # invoice_1001, code-only, fresh database
make ui                           # Streamlit app (Process invoice, Dashboard, Review queue)
make eval                         # expected vs actual decisions for all samples (cache mode)
uv run python main.py --invoice_path=data/invoices/invoice_1002.txt --llm cache --fresh
uv run python main.py --batch data/invoices data/synthetic --llm cache --fresh
uv run python main.py --batch data/reviewer_pack --llm cache --fresh --out results.csv
make eval-pack                    # the reviewer pack against its expected_results.csv
```

CLI: `--invoice_path`, `--batch DIR...`, `--fresh`, `--db`, `--cache`, `--out PATH` (write the
results of the run as CSV, or JSON if the path ends in `.json`), and
`--llm auto|live|cache|none` (default `auto`). Structured JSON logs go to stderr; the readable summary to stdout.
`python main.py reset` wipes and reseeds the database. `python main.py eval` prints expected vs
actual for every sample with the match summary (`make eval`); `python main.py eval --expected
FILE.csv [DIR...]` does the same for your own files and expectations.

## Architecture

LangGraph pipeline (`src/invoice_agents/graph.py`):

```
ingest -> resolve items -> validate (+ risk rules) -> approve (VP + critic) -> pay -> ledger
              |                  |                        |
              +--------- early exit to the ledger on: extraction failed, duplicate, any blocker
```

| Agent | Job | LLM? |
|---|---|---|
| Ingestion | Parse the file; structured formats are mapped by code; text and PDF go to Grok (structured output, schema validation, up to 2 retries with the exact error, then a normalized grounding check against the source) | text/PDF only |
| Item resolution | Code normalization first; names it cannot resolve go to Grok with a `search_inventory` tool (real tool calling) | only for the remainder |
| Validation | Pure code: required fields, arithmetic including shipping and fees, item exists, stock aggregated per item, duplicate vs revision, currency, risk rules | no |
| Approval | VP agent drafts a decision with reasons that cite a fact sheet; totals over $10,000 also go through a critic; at most 1 revision. Final = strictest of code and model, so a model can never approve a blocked invoice | yes |
| Payment | Code only: idempotency key (vendor + invoice number), USD only, `mock_payment` called once | no |

Rejections get a plain template message for the vendor (shown, not sent). Held invoices go to a
human review queue; approving one still passes the same payment guard.

## How the brief is met

| Requirement | Where |
|---|---|
| Ingestion from messy PDF/text | Grok structured extraction with retry and grounding check (`ingestion/`) |
| Validation against SQLite | `db.py`, `validation.py` (stock, unknown items, arithmetic, duplicates) |
| VP approval with scrutiny over $10K and a critique loop | `approval.py` (critic over $10K, 1 revision, code gate) |
| Payment or logged rejection | `payments.py`; every decision is written to the ledger and audit trail |
| Grok, function calling, structured outputs, self-correction | Responses API, `search_inventory` tool, Pydantic schemas, retry loops |
| Multi-agent orchestration | LangGraph |
| `python main.py --invoice_path=...` with logs and results | `cli.py` |
| Tests and error handling | `tests/` (176 tests, no network) |

## Results

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

**40/40 match** (`make eval`, cache mode)

Rules only (`--llm none`) is a separate run and is not combined with the above: 40/40 match its own
expectations, which differ for the files that need a model (`none` entries in `expected.yaml`:
unreadable text, unmapped item names and VP review over $10,000 hold instead).

The first full live run matched 19 of 22 expectations. Three fixes followed (see
`docs/DECISIONS.md` #13): one grounding bug for quantities like "x12", and two prompt
clarifications (abbreviated labels such as "Amt:"; the critic only requests revisions for
substantive problems). The responses were then re-recorded. The 22/22 result is on the same
documents the prompts were adjusted against, so it is not a held-out evaluation.

The sample invoices come from the case repository
https://github.com/galatiq-ai/galatiq-case-invoices.

The regression test (`tests/regression/test_regression.py`) asserts every decision in cache mode
and in rules-only mode, that nothing but APPROVE is paid, and that no idempotency key is paid twice. It fails if a cached response is
missing. The cache's own spend estimate for everything recorded so far is about $0.14 by the grok-4.3
price list (the 18 synthetic cases added 20 calls, 14,677 input and 8,355 output tokens).

## LLM provenance

- `auto` (default): per call, the recorded response if there is one, else a live Grok call if
  `LLM_API_KEY` is set (recorded for next time), else rules only. Each step reports its own
  provenance (recorded, live, or rules only). Tests and the regression run use strict `cache`.
- `live`: a real Grok call; the response is written to `cache/llm/<step>/<document>-<request-hash>.json`.
- `cache`: replays those recorded responses only. A missing entry is a HOLD marked as a system
  issue, never a vendor defect. The request hash covers the exact messages, schema, tools, model
  and settings, so changing a prompt means re-recording (`--llm live`).
- `none`: code only (parsers, rules, label-based extraction); anything needing a model holds.

Keys come from `.env` (see `.env.example`) and are never logged or cached.

## Key decisions

See [docs/DECISIONS.md](docs/DECISIONS.md): Responses API choice, the cache design, why the address
denylist was removed, risk levels, the two arithmetic rejects (1007, 1013), tiered item
resolution, due dates judged against the invoice date, the USD-only rail, duplicate vs revision.

## Assumptions

- The brief validates against **stock on hand**. Real accounts payable does a three-way match
  against the purchase order and goods receipt. We implement what was asked and do not decrement
  stock after payment.
- The brief's `grok-3` has been retired; the project uses `grok-4.3`, set in `.env`.
- `mock_payment` has no currency, so it is a USD rail; other currencies hold.
- "No internet" is read as: the LLM API is the only network call. Everything else is local.

## Next steps (cut from this MVP)

- Checkpoints, resume, retry-failed and a run journal for large batches
- A weighted risk rubric and richer fraud signals (split invoices, near-duplicates, price deviation)
- A separate LLM risk reviewer
- A Communication agent that drafts and sends vendor messages
- FX conversion for non-USD invoices
- Fixture manifests and stamping tooling
- mypy, pip-audit, coverage gates and branch protection
- Knowledge-transfer and operating-model documents
