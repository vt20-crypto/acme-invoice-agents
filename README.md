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

## LLM modes

- `--llm fixture` (default): replays JSON fixtures from `fixtures/llm/`. Fixtures are hand-written
  and labelled `authored`; they are not model output. A captured real response would be labelled
  `recorded`. Only invoice_1001 has fixtures so far.
- `--llm none`: code-only paths, no network.
- `--llm live`: calls the model configured in `.env` (see `.env.example`). Not yet exercised.

Every LLM result is logged with its provenance (`live`, `authored` or `recorded`).

## Status

Work in progress. This is the first vertical slice.
