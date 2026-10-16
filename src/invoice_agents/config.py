"""Policy constants. Kept in one place so rules are easy to find and change."""

VP_REVIEW_THRESHOLD = 10_000.0  # totals above this get the critic pass
TOLERANCE = 0.01  # arithmetic tolerance in currency units
MAX_EXTRACT_RETRIES = 2  # validation/grounding retries after the first extraction attempt
MAX_VP_REVISIONS = 2
MAX_TOOL_ITERATIONS = 6
PAYABLE_CURRENCY = "USD"  # mock_payment has no currency field, so it is a USD-only rail
