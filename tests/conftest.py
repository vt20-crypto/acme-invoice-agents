from pathlib import Path

import pytest

from invoice_agents.db import Database

ROOT = Path(__file__).resolve().parent.parent
INVOICES = ROOT / "data" / "invoices"
FIXTURES = ROOT / "fixtures" / "llm"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    yield d
    d.close()
