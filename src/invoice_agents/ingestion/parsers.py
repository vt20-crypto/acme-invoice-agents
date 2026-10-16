"""One parser per file format. Each returns a RawDocument; none of them interpret the content."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pdfplumber
from defusedxml import ElementTree


@dataclass
class RawDocument:
    source_file: str
    format: str
    raw_text: str
    structured: dict[str, Any] | None
    fingerprint: str  # sha256 of the file bytes

    @property
    def key(self) -> str:
        """Short fingerprint used to name fixtures."""
        return self.fingerprint[:16]


def _read(path: Path) -> tuple[bytes, str]:
    data = path.read_bytes()
    return data, data.decode("utf-8", errors="replace")


def parse_txt(path: Path) -> RawDocument:
    data, text = _read(path)
    return RawDocument(str(path), "txt", text, None, hashlib.sha256(data).hexdigest())


def parse_json(path: Path) -> RawDocument:
    data, text = _read(path)
    return RawDocument(str(path), "json", text, json.loads(text), hashlib.sha256(data).hexdigest())


def _csv_structured(text: str) -> dict[str, Any]:
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        return {"layout": "empty"}
    header = [c.strip() for c in rows[0]]
    if [h.lower() for h in header[:2]] == ["field", "value"]:
        # key/value layout; 'item' starts a new line item, quantity/unit_price attach to it
        fields: dict[str, str] = {}
        items: list[dict[str, str]] = []
        for r in rows[1:]:
            if len(r) < 2:
                continue
            k, v = r[0].strip().lower(), r[1].strip()
            if k == "item":
                items.append({"item": v})
            elif k in ("quantity", "unit_price") and items:
                items[-1][k] = v
            else:
                fields[k] = v
        return {"layout": "kv", "fields": fields, "items": items}
    # tabular layout with footer rows ("...,Subtotal:,14750.00")
    items = []
    meta: dict[str, str] = {}
    footer: dict[str, str] = {}
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        row = {header[i]: r[i].strip() for i in range(len(header))}
        if row.get(header[0]):
            items.append(row)
            for k in header[:4]:
                meta.setdefault(k, row[k])
        else:
            label = next((c.strip() for c in r[:-1][::-1] if c.strip()), "")
            if label:
                footer[label.rstrip(":").strip().lower()] = r[-1].strip()
    return {"layout": "table", "header": header, "meta": meta, "items": items, "footer": footer}


def parse_csv(path: Path) -> RawDocument:
    data, text = _read(path)
    return RawDocument(
        str(path), "csv", text, _csv_structured(text), hashlib.sha256(data).hexdigest()
    )


def _xml_to_obj(el: Any) -> Any:
    children = list(el)
    if not children:
        return (el.text or "").strip()
    out: dict[str, Any] = {}
    for c in children:
        v = _xml_to_obj(c)
        if c.tag in out:
            if not isinstance(out[c.tag], list):
                out[c.tag] = [out[c.tag]]
            out[c.tag].append(v)
        else:
            out[c.tag] = v
    return out


def parse_xml(path: Path) -> RawDocument:
    data, text = _read(path)
    root = ElementTree.fromstring(data)  # defusedxml: no entity expansion / external entities
    return RawDocument(
        str(path), "xml", text, {root.tag: _xml_to_obj(root)}, hashlib.sha256(data).hexdigest()
    )


def parse_pdf(path: Path) -> RawDocument:
    data = path.read_bytes()
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    return RawDocument(str(path), "pdf", text, None, hashlib.sha256(data).hexdigest())


PARSERS = {
    ".txt": parse_txt,
    ".json": parse_json,
    ".csv": parse_csv,
    ".xml": parse_xml,
    ".pdf": parse_pdf,
}


def parse_file(path: str | Path) -> RawDocument:
    p = Path(path)
    parser = PARSERS.get(p.suffix.lower())
    if parser is None:
        raise ValueError(f"Unsupported file type: {p.suffix}")
    return parser(p)
