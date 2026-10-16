.PHONY: install test lint run reset

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .

run:
	uv run python main.py --invoice_path=data/invoices/invoice_1001.txt --fresh

reset:
	uv run python main.py reset
