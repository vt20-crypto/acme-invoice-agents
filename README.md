# Acme Invoice Agents

A multi-agent prototype for the Galatiq invoice-processing case. It takes a vendor invoice (PDF, TXT,
CSV, JSON or XML), extracts the fields, validates them against a SQLite inventory, runs a VP-style
approval with a critique loop, and makes a mock payment. The model never has payment authority:
code decides, and code pays once per invoice.

**This is a local prototype with mock payments, not a production payment system.**

## For reviewers

1. **No key needed offline.** Offline runs, cache replay (`--llm cache`) and rules only
   (`--llm none`), need no key. Every model response for the samples, the synthetic cases and the
   reviewer pack is recorded in `cache/llm/`, so `make test`, `make eval` and the UI work offline.
   Only live Grok needs an xAI key (or any OpenAI-compatible provider through `LLM_BASE_URL` and
   `LLM_MODEL`).
2. **Try the reviewer pack.** `make ui`, open Dashboard, Add invoices, upload the files in
   `data/reviewer_pack/` (all except `expected_results.csv`) and process them. Then open
   Check against expected and upload `data/reviewer_pack/expected_results.csv`: the table shows
   a check mark or a cross per file and "6 of 6 match". From the command line:
   `make eval-pack` (or `uv run python main.py eval --llm cache --expected data/reviewer_pack/expected_results.csv`).
3. **Use your own key (optional).** Copy `.env.example` to `.env` and set `LLM_API_KEY` (and,
   for a provider other than xAI, `LLM_BASE_URL` and `LLM_MODEL`). The UI has no key field: the
   key is read from the environment only. In Auto mode a file with no recorded response goes to
   the live model, and the response is recorded. A rejected key never falls back to the cache in
   `live` mode; see LLM provenance below.
4. **Process your own files.** Upload them on the Dashboard, then use Download results (CSV or
   JSON), or run `uv run python main.py --batch DIR --out results.csv`. To compare with your own
   expectations, make a CSV with the columns `file,expected_decision` (the Dashboard offers a
   template with your file names filled in) and upload it under Check against expected, or run
   `main.py eval --expected FILE.csv DIR`.

## Quick start

Prerequisites: Python 3.11 or newer (`requires-python = ">=3.11"` in `pyproject.toml`),
[uv](https://docs.astral.sh/uv/), `make` and `git`. No API key is needed for the offline commands
below (see "For reviewers" above).

```bash
git clone https://github.com/vt20-crypto/acme-invoice-agents.git
cd acme-invoice-agents
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
| Payment | Code only: refuses unless vendor, invoice number (the idempotency key), an amount above zero and USD are all present; `mock_payment` called once | no |

A missing invoice number is a REJECT only when it is confidently absent (a structured file
without the field, or a complete text extraction with no number in the text); if the document
shows a number that extraction could not read, the invoice is held for a person
(`invoice_number_unreadable`). This is our policy, not a requirement of the brief
(`docs/DECISIONS.md` #17).

Rejections get a plain template message for the vendor (shown, not sent). Held invoices go to a
human review queue; approving one still passes the same payment guard.

**Human review.** A reviewer must leave a note and can always reject an open HOLD; approve is
allowed only when the same eligibility check finds no blocker (REJECT-severity findings, non-USD,
missing payment key, or an unverified system issue cannot be approved). Approval calls
`pay_invoice` with the key `vendor|invoice number`, and the invoice becomes APPROVE only if that
payment succeeds. Full rules: [docs/REVIEW_POLICY.md](docs/REVIEW_POLICY.md).

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
| Tests and error handling | `tests/` (246 tests, no network) |

## Results

Recorded Grok responses (model `grok-4.3`, recorded 2026-10-17) replayed in `cache` mode over all
40 documents (20 samples plus 20 synthetic cases). Expectations are in
`tests/regression/expected.yaml`, written before the run that checks them. Two mode-specific
matches, each scored against its own expectations (the two runs are not combined):

- Cache mode (`make eval`): **40/40** match the expected decisions, replaying recorded Grok responses.
- Rules only (`main.py eval --llm none`): **40/40** match the rules-only expectations, in which the
  cases that need a model (unreadable text, unmapped item names, VP review over $10,000) are
  expected to HOLD.
- Human-review (HOLD) counts: rules only **19**, with recorded Grok responses **12** (measured
  2026-10-17).
- **0 invalid payments** in each run. Scope: this only asserts that, over those 40 documents, no
  payment was made without an APPROVE decision; it is not a policy-safety guarantee and does not
  change the eval exit code.
- Tests: 246, no network.

Per-invoice table, methodology (authorization consistency vs policy safety), the model-call
bound and the cost totals are in [docs/EVALUATION.md](docs/EVALUATION.md).

## LLM provenance and recorded responses

- `auto` (default): per call, the recorded response if there is one, else a live Grok call if
  `LLM_API_KEY` is set (recorded for next time), else rules only. Each step reports its own
  provenance (recorded, live, or rules only). Tests and the regression run use strict `cache`.
- `live`: a real Grok call; the response is written to `cache/llm/<step>/<document>-<request-hash>.json`.
- `cache`: replays those recorded responses only. A missing entry is a HOLD marked as a system
  issue, never a vendor defect. The request hash covers the exact messages, schema, tools, model
  and settings, so changing a prompt means re-recording (`--llm live`).
- `none`: code only (parsers, rules, label-based extraction); anything needing a model holds.

Keys come from `LLM_API_KEY` in the environment or `.env` (see `.env.example`) and are never
logged, stored in the database or cache, or exported. `live` never reads the cache: if the key is
rejected the invoice is a HOLD marked as a system issue (`llm_unavailable`), not a recorded
result. `auto` replays a recorded response before it looks at the key (by design, so a recorded
file is served even with a bad key); on a miss it calls live, and a rejected key then gives the
same HOLD. Offline runs (`cache`, `none`) need no key.


Recorded versus fresh live: recorded responses are live Grok responses captured on 2026-10-17 and
replayed deterministically in cache mode. The 40-file evaluation, `make test` and CI replay those
recorded responses; none of them is a fresh live run. The only fresh live execution after the key
rotation is one smoke test covering one invoice, `data/synthetic/invoice_9011.pdf` (5 model
calls, all provenance `live`, decision APPROVE as expected); the other 39 documents were not
re-run live. Details: [docs/EVALUATION.md](docs/EVALUATION.md#recorded-versus-fresh-live).

## Key decisions

See [docs/DECISIONS.md](docs/DECISIONS.md): Responses API choice, the cache design, why the address
denylist was removed, risk levels, the two arithmetic rejects (1007, 1013), tiered item
resolution, due dates judged against the invoice date, the USD-only rail, duplicate vs revision.

## Detailed documentation

- [docs/EVALUATION.md](docs/EVALUATION.md): per-invoice results, methodology, model-call bound.
- [docs/REVIEW_POLICY.md](docs/REVIEW_POLICY.md): what a human reviewer can and cannot resolve.
- [docs/DECISIONS.md](docs/DECISIONS.md): design decisions and rationale.

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

### Known limitations of the prototype

- **Money is floating point.** Amounts are Python `float` (`models.py:15-36`, `payments.py:31`,
  stored as SQLite `REAL`), compared with a fixed tolerance of 0.01 (`TOLERANCE`, `config.py:4`;
  used in `validation.py:203,220,238`). There is no `Decimal` or integer-cents handling, so
  rounding behavior on large or many-line totals is not guaranteed.
- **Payment reconciliation is not implemented.** `mock_payment` is synchronous and assumed to
  succeed. If the payer raises or does not return `{"status": "success"}`, the payment row is set
  to `failed` and the invoice becomes HOLD with a `payment_failed` finding
  (`payments.py:56-61`, `graph.py:313-317`). The payment key stays used, so a later approval of
  the same invoice reports `duplicate` rather than retrying; a process crash after the row is
  inserted leaves it `pending`. There is no handling of pending or reversed payments, no retry
  and no reconciliation against an external ledger.

## Extending the prototype

- Policy constants: `src/invoice_agents/config.py`.
- Deterministic validation rules: `src/invoice_agents/validation.py`; add a focused
  rule test (`tests/test_validation.py`) and a regression case
  (`tests/regression/expected.yaml`) when changing behavior.
- New document formats: add a parser in `src/invoice_agents/ingestion/parsers.py` that
  returns the shared `RawDocument` and register it in `PARSERS`.
- Workflow changes: update the explicit graph in `src/invoice_agents/graph.py`.
- Payment integrations: replace the injected `payer` (the callable passed to
  `pay_invoice` in `src/invoice_agents/payments.py`, default `mock_payment`) only after
  defining external idempotency, reconciliation, and failure-recovery semantics.
- Model or prompt changes: re-record affected cached responses (`--llm live` writes to
  `cache/llm/`) and rerun evaluation (`make eval`, `uv run python main.py eval --llm none`).

This is a local prototype, not a production payment system.
