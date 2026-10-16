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

## 3. Address denylist and invoice_1004
Category: well-known landmark or fictional addresses that cannot belong to a real vendor
(for example 1600 Pennsylvania Ave, 742 Evergreen Terrace, 221B Baker Street). It is a
DEMONSTRATION LIST in code, weight 6, so one match is a conclusive REJECT.
invoice_1004.json and invoice_1004_revised.json list 742 Evergreen Terrace (the fictional
Springfield address), so invoice_1004.json is now REJECT. This contradicts the brief's README
table, which treats 1004 as a normal invoice. We follow the rule over the table: a vendor address
that cannot exist is a stronger signal than the clean line items. A production system would use a
vendor master and address validation instead of a list.

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
