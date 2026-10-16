# Acme Invoice Agents

A multi-agent prototype that takes a vendor invoice (TXT, PDF, CSV, JSON or XML) from arrival
to a decision: extract the fields, validate them against an inventory database, run a VP-style
approval with a critique loop, and make a guarded mock payment. Payment authority lives in code,
never in a model.

## How to run

```bash
make install
make test
make run          # processes data/invoices/invoice_1001.txt from a fresh database
make reset        # wipe and reseed the local database
```

Direct use:

```bash
uv run python main.py --invoice_path=data/invoices/invoice_1001.txt [--llm fixture|none|live] [--db acme.db] [--fresh]
```

## Batch mode

```bash
uv run python main.py --batch data/invoices data/synthetic --llm none --fresh --out results.jsonl
```

Order is defined: directories as given, then files within each sorted by file name with Python
`sorted()` (byte order, so `invoice_1004.json` comes before `invoice_1004_revised.json`; shell or
locale ordering may differ). A file that looks like a revision but has no original in the ledger
logs a warning. `--out` writes one JSON line per document (overwritten each run).

## LLM modes

- `--llm fixture` (default): replays JSON fixtures from `fixtures/llm/`. Fixtures are hand-written
  and labelled `authored`; they are not model output. A captured real response would be labelled
  `recorded`. Authored fixtures exist only for a small demonstration set: invoice_1001, invoice_1002 (messy text),
  invoice_1012.pdf (OCR noise) and the synthetic data/synthetic/invoice_9001.json (over $10K, runs
  the critic). Any other text/PDF file in fixture mode HOLDs with "LLM unavailable".
- `--llm none`: code-only paths, no network.
- `--llm live`: calls the model configured in `.env` (see `.env.example`). Not yet exercised.

Every LLM result is logged with its provenance (`live`, `authored` or `recorded`).

## Status

Work in progress. This is the first vertical slice.
