# Decision log

Short, factual entries. Newest decisions are appended at the end.

## 1. First-time vendor carries weight 0
In a fresh ledger every vendor is first-time, so the signal fires on every invoice and carries no
information. It is computed and shown as informational but adds nothing to the risk score and
emits no finding. (Decision-log finding #1.)

## 2. Arithmetic rejects: invoice_1007 and invoice_1013.json
Total must equal the sum of lines plus charges plus tax, within 0.01.
- invoice_1007.csv: lines 14,750.00 + 6% tax 885.00 = 15,635.00, but the file states 15,525.00.
- invoice_1013.json: lines 21,040.00 + 7% tax 1,472.80 = 22,512.80, but the file states 22,562.80.

Both are REJECT on the arithmetic blocker (they also exceed stock). The brief's table does not
list these files as arithmetic errors; the data does not reconcile, so the rule stands.

## 3. Address denylist: added, then removed
Added in iteration 2 as a DEMONSTRATION LIST of well-known landmark or fictional addresses (for
example 1600 Pennsylvania Ave, 742 Evergreen Terrace), weight 6, so one match was a conclusive
REJECT. Removed entirely (code, risk signal and tests) because it was an unsupported rule: the MVP
has no principled data source for "an address that cannot be a real vendor". A real deployment would
use address verification plus the vendor master. No invoice-specific exceptions replace it.
Effect on outcomes versus PLAN section 4:
- invoice_1004.json: APPROVE (matches section 4; the denylist had briefly made it REJECT).
- invoice_1004_revised.json: HOLD, revision of a paid original, delta +4,050.00 (matches).
- invoice_1005.json: HOLD on `stock_exceeded` (GadgetX 8 > 5). Section 4 expected REJECT then audit
  on the strength of its White House address; without that signal no documented validation or
  risk rule justifies more than HOLD. This is a deliberate difference from section 4.

## 4. Tiered item resolution
Tier 1 is code: exact match after normalization (case, punctuation, spaces, parentheticals
stripped). Tier 2 is the LLM with a `search_inventory` tool, used only for names tier 1 cannot
resolve. The tier is logged per line. Whatever SKU the LLM proposes, validation re-checks it
exactly against the database; a SKU not in the database is treated as unknown.

## 5. Due-date validity is judged against the invoice date
A due date is invalid if it is unparseable, a word such as "yesterday", or earlier than the invoice
date. It is not compared with today's date: sample invoices are dated early 2026 and a wall-clock
comparison would make every sample invoice overdue.

## 6. USD-only payment rail
`mock_payment(vendor, amount)` has no currency field, so it is treated as a USD rail. Non-USD
invoices are held (no FX conversion), and the payment wrapper refuses any non-USD amount as a
second line of defence.

## 7. Duplicate versus revision
Same normalized vendor and invoice number with the same economic content (lines, charges, tax,
total, currency) is a DUPLICATE: linked to the original, no payment, whatever the original's
decision was. Same key with changed content is a revision: a HOLD blocker that shows the total
delta, never auto-paid. Other blockers still apply, so a revision that also fails another check
takes the stricter outcome.
