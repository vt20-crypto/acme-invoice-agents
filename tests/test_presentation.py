from invoice_agents.presentation import display_number, plain


def _f(code, **data):
    return {"code": code, "severity": "note", "kind": "business", "message": "raw", "data": data}


def test_duplicate_message_uses_invoice_number_and_plain_decision():
    msg = plain(
        _f(
            "duplicate",
            original_file="invoice_1001.txt",
            original_number="INV-1001",
            original_decision="APPROVE",
        )
    )
    assert msg == "Already processed earlier as INV-1001 (approved and paid). Nothing to do."


def test_revision_message_has_number_signed_money_and_no_filename():
    msg = plain(
        _f(
            "revision",
            original_file="invoice_1004.json",
            original_number="INV-1004",
            original_decision="APPROVE",
            original_total=1890.0,
            new_total=5940.0,
            paid=True,
        )
    )
    assert msg == (
        "Revision of INV-1004: total changed from $1,890.00 to $5,940.00 (+$4,050.00); "
        "the original was already paid"
    )


def test_no_user_facing_message_leaks_filenames_or_decision_codes():
    for held in ("HOLD", "REJECT", "APPROVE"):
        msg = plain(
            _f("duplicate", original_file="x.pdf", original_number=None, original_decision=held)
        )
        assert ".pdf" not in msg and held not in msg


def test_display_number_normalizes():
    assert display_number("1002") == "INV-1002"
    assert display_number(" inv-1002 ") == "INV-1002"
    assert display_number("INV 1012") == "INV-1012"
    assert display_number("OF-4471") == "OF-4471"
    assert display_number(None) == "—"
