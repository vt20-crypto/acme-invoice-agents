import hashlib

from conftest import INVOICES

from invoice_agents.ingestion.parsers import parse_file


def test_txt():
    doc = parse_file(INVOICES / "invoice_1001.txt")
    assert doc.format == "txt" and doc.structured is None
    assert "Widgets Inc." in doc.raw_text
    assert (
        doc.fingerprint == hashlib.sha256((INVOICES / "invoice_1001.txt").read_bytes()).hexdigest()
    )
    assert doc.key == doc.fingerprint[:16]


def test_json():
    doc = parse_file(INVOICES / "invoice_1004.json")
    assert doc.structured["invoice_number"] == "INV-1004"
    assert len(doc.structured["line_items"]) == 2


def test_csv_key_value_layout():
    doc = parse_file(INVOICES / "invoice_1006.csv")
    s = doc.structured
    assert s["layout"] == "kv"
    assert s["fields"]["vendor"] == "Acme Industrial Supplies"
    assert s["items"] == [
        {"item": "WidgetA", "quantity": "5", "unit_price": "250.00"},
        {"item": "WidgetB", "quantity": "3", "unit_price": "500.00"},
    ]


def test_csv_tabular_layout_with_footer_rows():
    doc = parse_file(INVOICES / "invoice_1007.csv")
    s = doc.structured
    assert s["layout"] == "table"
    assert len(s["items"]) == 3
    assert s["meta"]["Vendor"] == "MegaWidgets Corp"
    assert s["footer"]["subtotal"] == "14750.00"
    assert s["footer"]["total"] == "15525.00"


def test_xml():
    doc = parse_file(INVOICES / "invoice_1014.xml")
    inv = doc.structured["invoice"]
    assert inv["header"]["currency"] == "EUR"
    assert len(inv["line_items"]["item"]) == 2


def test_xml_rejects_entity_expansion(tmp_path):
    import pytest
    from defusedxml.common import DefusedXmlException

    bad = tmp_path / "evil.xml"
    bad.write_text(
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "boom">]><invoice><v>&a;</v></invoice>'
    )
    with pytest.raises(DefusedXmlException):
        parse_file(bad)


def test_pdf():
    doc = parse_file(INVOICES / "invoice_1011.pdf")
    assert doc.format == "pdf"
    assert "INV-1011" in doc.raw_text and "Summit Manufacturing" in doc.raw_text
