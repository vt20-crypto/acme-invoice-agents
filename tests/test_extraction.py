import json

from conftest import INVOICES, scripted_client

from invoice_agents.ingestion.extractor import extract, extract_labelled, extract_with_llm
from invoice_agents.ingestion.parsers import parse_file

GOOD = {
    "vendor": "Widgets Inc.",
    "invoice_number": "INV-1001",
    "invoice_date": "2026-01-15",
    "due_date": "2026-02-01",
    "currency": "USD",
    "line_items": [
        {"raw_name": "WidgetA", "quantity": 10, "unit_price": 250.0},
        {"raw_name": "WidgetB", "quantity": 5, "unit_price": 500.0},
    ],
    "charges": [],
    "subtotal": 5000.0,
    "tax": 0.0,
    "total": 5000.0,
    "notes": [],
}


def test_label_fallback_extracts_1001():
    res = extract_labelled(parse_file(INVOICES / "invoice_1001.txt"))
    assert not res.uncertain
    inv = res.invoice
    assert inv.vendor == "Widgets Inc." and inv.invoice_number == "INV-1001"
    assert str(inv.due_date) == "2026-02-01"
    assert [(i.raw_name, i.quantity, i.unit_price) for i in inv.line_items] == [
        ("WidgetA", 10, 250.0),
        ("WidgetB", 5, 500.0),
    ]
    assert inv.subtotal == 5000.0 and inv.total == 5000.0 and inv.tax == 0.0


def test_label_fallback_handles_shipping_line_1010():
    inv = extract_labelled(parse_file(INVOICES / "invoice_1010.txt")).invoice
    assert len(inv.line_items) == 4
    assert [(c.kind, c.amount) for c in inv.charges] == [("shipping", 150.0)]
    assert inv.total == 7185.0


def test_label_fallback_is_uncertain_on_unlabelled_text(tmp_path):
    f = tmp_path / "messy.txt"
    f.write_text("hey pls pay us for the stuff we sent last week, thx\n")
    res = extract_labelled(parse_file(f))
    assert res.uncertain and res.reasons


def test_structured_formats_map_without_llm():
    for name, vendor, total in [
        ("invoice_1004.json", "Precision Parts Ltd.", 1890.0),
        ("invoice_1006.csv", "Acme Industrial Supplies", 2750.0),
        ("invoice_1007.csv", "MegaWidgets Corp", 15525.0),
        ("invoice_1014.xml", "TechParts International", 4125.0),
    ]:
        res = extract(parse_file(INVOICES / name), None)
        assert res.method == "code" and not res.uncertain
        assert res.invoice.vendor == vendor and res.invoice.total == total
    assert extract(parse_file(INVOICES / "invoice_1014.xml"), None).invoice.currency == "EUR"


def test_pydantic_retry_path_feeds_back_exact_error():
    doc = parse_file(INVOICES / "invoice_1001.txt")
    bad = json.loads(json.dumps(GOOD))
    bad["line_items"][0]["quantity"] = "ten"  # fails schema validation
    client, transport = scripted_client({"extraction": [bad, GOOD]})
    res = extract_with_llm(doc, client)
    assert not res.uncertain and res.attempts == 2 and res.invoice.total == 5000.0
    retry_msg = transport.calls[1][1][-1]["content"]
    assert "failed schema validation" in retry_msg and "line_items.0.quantity" in retry_msg
    assert [c["outcome"] for c in res.llm_calls] == ["schema_error", "grounded"]


def test_extraction_gives_up_after_two_retries():
    doc = parse_file(INVOICES / "invoice_1001.txt")
    client, transport = scripted_client({"extraction": [{"vendor": 1}] * 3})
    res = extract_with_llm(doc, client)
    assert res.uncertain and res.attempts == 3 and len(transport.calls) == 3
    assert res.failure_code == "extraction_failed"


def test_grounding_failure_retries_then_marks_uncertain():
    doc = parse_file(INVOICES / "invoice_1001.txt")
    invented = {**GOOD, "vendor": "Gadgets LLC"}
    client, _ = scripted_client({"extraction": [invented] * 3})
    res = extract_with_llm(doc, client)
    assert res.uncertain and res.attempts == 3
    assert any("grounding failed" in r and "Gadgets LLC" in r for r in res.reasons)


def test_grounding_recovers_on_retry():
    doc = parse_file(INVOICES / "invoice_1001.txt")
    client, _ = scripted_client({"extraction": [{**GOOD, "total": 5100.0}, GOOD]})
    res = extract_with_llm(doc, client)
    assert not res.uncertain and res.attempts == 2


def test_llm_unavailable_is_uncertain_with_a_system_code():
    doc = parse_file(INVOICES / "invoice_1001.txt")
    client, _ = scripted_client({})
    res = extract_with_llm(doc, client)
    assert res.uncertain and res.failure_code == "llm_unavailable"
