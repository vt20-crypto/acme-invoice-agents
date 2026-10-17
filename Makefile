.PHONY: install test lint run reset ui eval eval-pack

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

eval:
	uv run python main.py eval --llm cache

eval-pack:
	uv run python main.py eval --llm cache --expected data/reviewer_pack/expected_results.csv
