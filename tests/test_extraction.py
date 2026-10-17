import json

from conftest import INVOICES

from invoice_agents.ingestion.extractor import extract, extract_labelled, extract_with_llm
from invoice_agents.ingestion.parsers import parse_file
from invoice_agents.llm.client import FixtureClient, LLMClient, LLMUnavailable
from invoice_agents.models import Invoice

LABEL = {"provenance": "authored", "note": "Hand-written fixture, not a model output"}

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


def write(root, key, step, response):
    d = root / "extraction"
    d.mkdir(parents=True, exist_ok=True)
    name = f"{key}__{step}.json" if step else f"{key}.json"
    (d / name).write_text(json.dumps({**LABEL, "response": response}))


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


def test_pydantic_retry_path_feeds_back_exact_error(tmp_path):
    doc = parse_file(INVOICES / "invoice_1001.txt")
    bad = json.loads(json.dumps(GOOD))
    bad["line_items"][0]["quantity"] = "ten"  # fails schema validation
    write(tmp_path, doc.key, "", bad)
    write(tmp_path, doc.key, "retry1", GOOD)
    seen = []

    class Spy(FixtureClient):
        def structured(self, agent, messages, schema_model, **kw):
            seen.append(messages[-1]["content"])
            return super().structured(agent, messages, schema_model, **kw)

    res = extract_with_llm(doc, Spy(tmp_path, verify=False))
    assert not res.uncertain and res.attempts == 2
    assert res.invoice.total == 5000.0
    assert "failed schema validation" in seen[1] and "line_items.0.quantity" in seen[1]
    assert [c["outcome"] for c in res.llm_calls] == ["schema_error", "grounded"]


def test_grounding_failure_retries_then_marks_uncertain(tmp_path):
    doc = parse_file(INVOICES / "invoice_1001.txt")
    invented = {**GOOD, "vendor": "Gadgets LLC"}
    for step in ("", "retry1", "retry2"):
        write(tmp_path, doc.key, step, invented)
    res = extract_with_llm(doc, FixtureClient(tmp_path, verify=False))
    assert res.uncertain and res.attempts == 3
    assert any("grounding failed" in r and "Gadgets LLC" in r for r in res.reasons)


def test_grounding_recovers_on_retry(tmp_path):
    doc = parse_file(INVOICES / "invoice_1001.txt")
    write(tmp_path, doc.key, "", {**GOOD, "total": 5100.0})
    write(tmp_path, doc.key, "retry1", GOOD)
    res = extract_with_llm(doc, FixtureClient(tmp_path, verify=False))
    assert not res.uncertain and res.attempts == 2


def test_missing_fixture_is_uncertain_llm_unavailable(tmp_path):
    doc = parse_file(INVOICES / "invoice_1001.txt")
    res = extract_with_llm(doc, FixtureClient(tmp_path, verify=False))
    assert res.uncertain and "LLM unavailable" in res.reasons[0]


def test_llm_client_is_abstract():
    assert issubclass(FixtureClient, LLMClient) and issubclass(LLMUnavailable, RuntimeError)
    assert Invoice.model_json_schema()["properties"]["line_items"]
