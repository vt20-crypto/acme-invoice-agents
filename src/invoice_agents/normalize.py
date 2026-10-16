"""Text and number normalization shared by grounding, matching and duplicate detection."""

from __future__ import annotations

import re

_CONFUSABLE = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1"})
_CURRENCY_CHARS = "$€£ "


def norm_text(s: str) -> str:
    """Casefold, turn punctuation into spaces, collapse whitespace."""
    s = re.sub(r"[^0-9a-z]+", " ", s.casefold())
    return s.strip()


def norm_key(s: str) -> str:
    """Compact key: casefolded alphanumerics only ('INV-1001' -> 'inv1001')."""
    return re.sub(r"[^0-9a-z]+", "", s.casefold())


def fold_confusables(s: str) -> str:
    """Map OCR look-alikes (O->0, l/I->1) so noisy and clean spellings compare equal."""
    return norm_key(s).translate(str.maketrans({"o": "0", "l": "1", "i": "1"}))


def canon_item_name(raw: str) -> str:
    """Item-name key for matching: drop parentheticals, then compact."""
    return norm_key(re.sub(r"\([^)]*\)", " ", raw))


def fix_ocr_numeric(token: str) -> str:
    """Repair a numeric token read with OCR confusables ('2O26' -> '2026')."""
    return token.translate(_CONFUSABLE)


def parse_number(s: str) -> float | None:
    """Parse '$3,500.O0' / '(500.00)' / '2O26' into a float, or None if not numeric."""
    t = s.strip().strip(_CURRENCY_CHARS)
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").strip(_CURRENCY_CHARS)
    if t.startswith("-"):
        neg, t = True, t[1:]
    t = fix_ocr_numeric(t.replace(",", ""))
    if not re.fullmatch(r"\d+(\.\d+)?", t):
        return None
    v = float(t)
    return -v if neg else v


_TOKEN = re.compile(r"(?<![A-Za-z0-9])[$€£]?[0-9OoIl][0-9OoIl,]*(?:\.[0-9OoIl]+)?(?![A-Za-z])")


def numbers_in_text(text: str) -> set[float]:
    """Every numeric token in the source, rounded to 2 decimals, OCR-repaired."""
    out: set[float] = set()
    for m in _TOKEN.finditer(text):
        tok = m.group(0)
        if not any(c.isdigit() for c in tok):
            continue
        v = parse_number(tok)
        if v is not None:
            out.add(round(v, 2))
    return out


def number_grounded(value: float, source_numbers: set[float]) -> bool:
    return round(abs(value), 2) in source_numbers or round(value, 2) in source_numbers


def text_grounded(value: str, source: str) -> bool:
    """True if value appears in source after normalization (spacing, case, punctuation, OCR)."""
    v = norm_key(value)
    if not v:
        return True
    if v in norm_key(source):
        return True
    return fold_confusables(value) in fold_confusables(source)
