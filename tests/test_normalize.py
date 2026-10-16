from invoice_agents.ingestion.extractor import check_grounding
from invoice_agents.models import Invoice, LineItem
from invoice_agents.normalize import (
    fix_ocr_numeric,
    number_grounded,
    numbers_in_text,
    parse_number,
    text_grounded,
)


def test_ocr_confusables_in_numbers():
    assert fix_ocr_numeric("2O26") == "2026"
    assert parse_number("2O26") == 2026
    assert parse_number("$3,500.O0") == 3500.0
    assert parse_number("(500.00)") == -500.0
    assert parse_number("abc") is None


def test_numbers_in_text_repairs_ocr_tokens():
    nums = numbers_in_text("Date: 2O26-01-15  Total: $3,500.O0  Invoice INV-1001")
    assert 2026.0 in nums and 3500.0 in nums and 1001.0 in nums
    assert number_grounded(3500.0, nums)
    assert not number_grounded(3600.0, nums)


def test_words_are_not_read_as_numbers():
    assert numbers_in_text("Invoice Oil Illinois") == set()


def test_text_grounding_spacing_case_punctuation():
    assert text_grounded("Widget A", "Items: WidgetA qty 3")
    assert text_grounded("widgets inc", "Vendor: Widgets Inc.")
    assert text_grounded("INV-1012", "Invoice # INV-1O12")  # OCR confusable
    assert not text_grounded("Gadgets LLC", "Vendor: Widgets Inc.")


def test_check_grounding_flags_invented_numbers():
    src = "Vendor: Widgets Inc.\nWidgetA 10 $250.00\nTotal: $2,500.00"
    good = Invoice(
        vendor="Widgets Inc.",
        line_items=[LineItem(raw_name="WidgetA", quantity=10, unit_price=250.0)],
        total=2500.0,
    )
    assert check_grounding(good, src) == []
    bad = good.model_copy(update={"total": 2600.0})
    assert any("total" in p for p in check_grounding(bad, src))
