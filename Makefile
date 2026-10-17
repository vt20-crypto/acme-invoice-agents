.PHONY: install test lint run reset ui

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .

run:
	uv run python main.py --invoice_path=data/invoices/invoice_1001.txt --llm none --fresh

reset:
	uv run python main.py reset

ui:
	uv run streamlit run app/streamlit_app.py
