# Acme Invoice Agents

A multi-agent prototype for the Galatiq invoice-processing case. It takes a vendor invoice (PDF, TXT,
CSV, JSON or XML), extracts the fields, validates them against a SQLite inventory, runs a VP-style
approval with a critique loop, and makes a mock payment. The model never has payment authority:
code decides, and code pays once per invoice.

## Quick start

```bash
make install                      # uv sync
make test                         # no API key needed: tests replay recorded responses
make run                          # invoice_1001, code-only, fresh database
make ui                           # Streamlit app (4 tabs)
uv run python main.py --invoice_path=data/invoices/invoice_1002.txt --llm cache --fresh
uv run python main.py --batch data/invoices data/synthetic --llm cache --fresh
```

CLI: `--invoice_path`, `--batch DIR...`, `--fresh`, `--db`, `--cache`, and
`--llm live|cache|none`. Structured JSON logs go to stderr; the readable summary to stdout.
`python main.py reset` wipes and reseeds the database.

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
| Tests and error handling | `tests/` (118 tests, no network) |

## Results

Recorded Grok responses (model `grok-4.3`, recorded 2026-10-17) replayed in `cache` mode over all
22 documents: the 20 samples plus two synthetic cases (`invoice_9001` over $10K that should
approve through the VP and critic; `invoice_9002` with an injected instruction). Expectations are
in `tests/regression/expected.yaml`, written before the first live run.

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

**22/22 match**

The first full live run matched 19 of 22 expectations. Three fixes followed (see
`docs/DECISIONS.md` #13): one grounding bug for quantities like "x12", and two prompt
clarifications (abbreviated labels such as "Amt:"; the critic only requests revisions for
substantive problems). The responses were then re-recorded. The 22/22 result is on the same
documents the prompts were adjusted against, so it is not a held-out evaluation.

The sample invoices come from the case repository
https://github.com/galatiq-ai/galatiq-case-invoices.

The regression test (`tests/regression/test_regression.py`) asserts every decision, that nothing
but APPROVE is paid, and that no idempotency key is paid twice. It fails if a cached response is
missing. About 39k input and 20k output tokens were spent building the cache and prompts, roughly
$0.10 by the grok-4.3 price list.

## LLM provenance

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
- A larger synthetic test suite
- Fixture manifests and stamping tooling
- mypy, pip-audit, coverage gates and branch protection
- Knowledge-transfer and operating-model documents
