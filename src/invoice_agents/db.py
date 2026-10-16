"""SQLite persistence: inventory, invoice ledger, payments and the audit trail."""

from __future__ import annotations

import difflib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .normalize import canon_item_name

# README rows; prices are our extension (the brief's schema has stock only).
SEED_INVENTORY = [
    ("WidgetA", 15, 250.0),
    ("WidgetB", 10, 500.0),
    ("GadgetX", 5, 750.0),
    ("FakeItem", 0, None),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    item TEXT PRIMARY KEY, stock INTEGER, unit_price REAL
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT, fingerprint TEXT, vendor_norm TEXT, invoice_number_norm TEXT,
    content_hash TEXT, decision TEXT, total REAL, currency TEXT, duplicate_of INTEGER,
    processed_at TEXT, run_id TEXT
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT UNIQUE, amount REAL, currency TEXT, status TEXT, paid_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT, source_file TEXT, step TEXT, payload TEXT, ts TEXT
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path = "acme.db"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()
        if self.conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == 0:
            self.seed()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def seed(self) -> None:
        self.conn.executemany("INSERT INTO inventory VALUES (?, ?, ?)", SEED_INVENTORY)
        self.conn.commit()

    def reset(self) -> None:
        """Wipe every table and reseed inventory."""
        self.conn.executescript(
            "DROP TABLE IF EXISTS inventory; DROP TABLE IF EXISTS invoices;"
            "DROP TABLE IF EXISTS payments; DROP TABLE IF EXISTS audit_events;"
        )
        self.init_schema()
        self.seed()

    def close(self) -> None:
        self.conn.close()

    # --- inventory ---------------------------------------------------------------------

    def get_item(self, sku: str) -> dict[str, Any] | None:
        """Exact (case-sensitive) lookup. This is the authoritative SKU check."""
        row = self.conn.execute(
            "SELECT item, stock, unit_price FROM inventory WHERE item = ?", (sku,)
        ).fetchone()
        return dict(row) if row else None

    def inventory_all(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT item, stock, unit_price FROM inventory ORDER BY item")
        return [dict(r) for r in rows]

    def search_inventory(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Normalized + fuzzy match of a free-text item name against inventory."""
        q = canon_item_name(query)
        if not q:
            return []
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in self.inventory_all():
            c = canon_item_name(row["item"])
            if q == c:
                score = 1.0
            elif q in c or c in q:
                score = 0.85
            else:
                score = difflib.SequenceMatcher(None, q, c).ratio()
            if score >= 0.7:
                scored.append((score, row))
        scored.sort(key=lambda t: (-t[0], t[1]["item"]))
        return [r for _, r in scored[:limit]]

    # --- ledger ------------------------------------------------------------------------

    def find_original(self, vendor_norm: str, invoice_number_norm: str) -> dict[str, Any] | None:
        """Earliest non-duplicate ledger row for this (vendor, invoice number) key."""
        row = self.conn.execute(
            "SELECT * FROM invoices WHERE vendor_norm = ? AND invoice_number_norm = ? "
            "AND duplicate_of IS NULL ORDER BY id LIMIT 1",
            (vendor_norm, invoice_number_norm),
        ).fetchone()
        return dict(row) if row else None

    def find_by_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM invoices WHERE fingerprint = ? ORDER BY id LIMIT 1", (fingerprint,)
        ).fetchone()
        return dict(row) if row else None

    def record_invoice(
        self,
        *,
        source_file: str,
        fingerprint: str,
        vendor_norm: str | None,
        invoice_number_norm: str | None,
        content_hash: str | None,
        decision: str,
        total: float | None,
        currency: str | None,
        duplicate_of: int | None,
        run_id: str,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO invoices (source_file, fingerprint, vendor_norm, invoice_number_norm, "
            "content_hash, decision, total, currency, duplicate_of, processed_at, run_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_file,
                fingerprint,
                vendor_norm,
                invoice_number_norm,
                content_hash,
                decision,
                total,
                currency,
                duplicate_of,
                _now(),
                run_id,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    # --- payments ----------------------------------------------------------------------

    def insert_payment(self, key: str, amount: float, currency: str) -> int:
        """Insert a payment row; raises sqlite3.IntegrityError if the key was used before."""
        with self.conn:  # transaction
            cur = self.conn.execute(
                "INSERT INTO payments (idempotency_key, amount, currency, status, paid_at) "
                "VALUES (?,?,?,?,?)",
                (key, amount, currency, "pending", _now()),
            )
        return int(cur.lastrowid or 0)

    def set_payment_status(self, payment_id: int, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE payments SET status = ? WHERE id = ?", (status, payment_id))

    def payment_exists(self, key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM payments WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return row is not None

    def payments(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM payments ORDER BY id")]

    # --- audit -------------------------------------------------------------------------

    def audit(self, run_id: str, source_file: str, step: str, payload: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO audit_events (run_id, source_file, step, payload, ts) "
                "VALUES (?,?,?,?,?)",
                (run_id, source_file, step, json.dumps(payload, default=str), _now()),
            )

    def audit_events(self, run_id: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM audit_events", ()
        if run_id:
            q, args = q + " WHERE run_id = ?", (run_id,)
        return [dict(r) for r in self.conn.execute(q + " ORDER BY id", args)]
