"""Row-level validation, price parsing and article-code normalization.

A row is validated independently of every other row: failures are collected
into a Rejection and never abort the file. All reject reasons for one row are
joined into the rejection message; the first reason becomes the primary
reason_code.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .models import PriceRow, Rejection
from .profiles import CodeNormalization, DecimalFormat, SupplierProfile
from .readers import NumericCell

MISSING_ARTICLE_CODE = "missing_article_code"
INVALID_ARTICLE_CODE = "invalid_article_code"
MISSING_PRICE = "missing_price"
INVALID_PRICE = "invalid_price"
AMBIGUOUS_PRICE = "ambiguous_price"
NEGATIVE_PRICE = "negative_price"
INVALID_CURRENCY = "invalid_currency"

_PLAIN_NUMBER_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")
_INTEGER_RE = re.compile(r"^[+-]?\d+$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def normalize_code(raw: str, rules: CodeNormalization) -> str:
    code = raw.strip()
    if rules.uppercase:
        code = code.upper()
    if rules.strip_prefix and code.startswith(rules.strip_prefix):
        code = code[len(rules.strip_prefix):].strip()
    if rules.strip_whitespace:
        code = re.sub(r"\s+", "", code)
    return code


class AmbiguousPriceError(ValueError):
    """A textual price whose separators cannot be resolved unambiguously."""


def _thousands_grouped_re(separator: str) -> re.Pattern:
    return re.compile(rf"^[+-]?\d{{1,3}}({re.escape(separator)}\d{{3}})+$")


def _to_decimal(text: str, raw: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {raw!r}") from exc
    if not value.is_finite():
        raise ValueError(f"not a finite number: {raw!r}")
    return value


def parse_price(raw: str, fmt: DecimalFormat) -> Decimal:
    """Parse a price according to the profile's decimal format.

    A value coming from a numeric Excel cell (marked ``NumericCell`` by the
    reader) is already machine-formatted and accepted as a plain number.
    Supplier-authored *text* is instead interpreted strictly through the
    profile: the declared decimal separator marks the fraction, and the
    declared thousands separator is accepted only in strict 3-digit groups
    (``1.234`` under a ``.``-thousands profile means 1234). A text value whose
    only punctuation is a separator the profile assigns a different role —
    e.g. ``12.90`` when ``.`` is the thousands separator and no fraction is
    present — is rejected as ambiguous instead of being silently reinterpreted
    as a far smaller or larger price. When both separators appear, the
    thousands separator is accepted only in strict 3-digit groups in the
    integer part: ``1.2.3,45`` under a ``.``-thousands ``,``-decimal profile
    is rejected as ambiguous instead of becoming 123.45. The fractional part
    is validated before any separator is removed too: ``12,3.45`` and
    ``1.234,5.6`` are rejected as ambiguous instead of silently becoming
    12.345 or 1234.56, while ``1.234,56`` still parses as 1234.56.
    """

    text = raw.strip()
    if not text:
        raise ValueError("empty price")

    if isinstance(raw, NumericCell):
        if not _PLAIN_NUMBER_RE.match(text):
            raise ValueError(f"numeric cell is not a plain number: {raw!r}")
        return _to_decimal(text, raw)

    if fmt.decimal_separator in text:
        integer_part, _, fractional_part = text.rpartition(fmt.decimal_separator)
        if fmt.thousands_separator and fmt.thousands_separator in text:
            # Grouping is validated *before* any separator is removed: the
            # integer part must be in strict 3-digit thousands groups, or a
            # malformed supplier value like "1.2.3,45" would be silently
            # re-interpreted as 123.45.
            if not re.fullmatch(
                rf"[+-]?\d{{1,3}}({re.escape(fmt.thousands_separator)}\d{{3}})*",
                integer_part,
            ):
                raise AmbiguousPriceError(
                    f"ambiguous price {raw!r}: {fmt.thousands_separator!r} is the "
                    "profile's thousands separator, but the integer part is not "
                    "in strict 3-digit groups"
                )
        if fmt.thousands_separator and fmt.thousands_separator in fractional_part:
            # The fractional part must be validated before any separator is
            # removed: "12,3.45" or "1.234,5.6" would otherwise silently
            # become 12.345 or 1234.56 once the thousands separator inside
            # the fraction is stripped.
            raise AmbiguousPriceError(
                f"ambiguous price {raw!r}: {fmt.thousands_separator!r} is the "
                "profile's thousands separator, but it appears in the "
                "fractional part"
            )
        converted = text
        if fmt.thousands_separator:
            converted = converted.replace(fmt.thousands_separator, "")
        if fmt.decimal_separator != ".":
            converted = converted.replace(fmt.decimal_separator, ".")
        if not _PLAIN_NUMBER_RE.match(converted):
            raise ValueError(f"not a number: {raw!r}")
        return _to_decimal(converted, raw)

    if fmt.thousands_separator and fmt.thousands_separator in text:
        rest = text.replace(fmt.thousands_separator, "")
        if _INTEGER_RE.match(rest):
            if _thousands_grouped_re(fmt.thousands_separator).match(text):
                return _to_decimal(rest, raw)
            raise AmbiguousPriceError(
                f"ambiguous price {raw!r}: {fmt.thousands_separator!r} is the "
                "profile's thousands separator, but the value is not in strict "
                "thousands groups and carries no decimal separator"
            )

    if fmt.decimal_separator != "." and "." in text:
        if _INTEGER_RE.match(text.replace(".", "")):
            raise AmbiguousPriceError(
                f"ambiguous price {raw!r}: '.' is not the profile's decimal "
                f"separator ({fmt.decimal_separator!r}); write the fraction with "
                "the profile's separator or add explicit thousands groups"
            )

    if not _PLAIN_NUMBER_RE.match(text):
        raise ValueError(f"not a number: {raw!r}")
    return _to_decimal(text, raw)


def validate_row(
    row_number: int, raw: dict[str, str | None], profile: SupplierProfile
) -> tuple[PriceRow | None, Rejection | None]:
    """Validate one raw row. Returns (PriceRow, None) or (None, Rejection)."""

    errors: list[tuple[str, str]] = []

    code_raw = raw.get("article_code")
    if code_raw is None or not code_raw.strip():
        errors.append((MISSING_ARTICLE_CODE, "article code is missing"))
        code = ""
    else:
        code = normalize_code(code_raw, profile.code_normalization)
        if not code:
            errors.append(
                (MISSING_ARTICLE_CODE, "article code is empty after normalization")
            )
        elif profile.code_pattern and not re.fullmatch(profile.code_pattern, code):
            errors.append(
                (
                    INVALID_ARTICLE_CODE,
                    f"article code {code!r} does not match pattern "
                    f"{profile.code_pattern!r}",
                )
            )

    price_raw = raw.get("price")
    price: Decimal | None = None
    if price_raw is None or not price_raw.strip():
        errors.append((MISSING_PRICE, "price is missing"))
    else:
        try:
            price = parse_price(price_raw, profile.decimal)
        except AmbiguousPriceError as exc:
            errors.append((AMBIGUOUS_PRICE, str(exc)))
        except ValueError as exc:
            errors.append((INVALID_PRICE, f"invalid price {price_raw!r}: {exc}"))
        else:
            if price < 0:
                errors.append((NEGATIVE_PRICE, f"negative price {price}"))

    currency_raw = raw.get("currency")
    if currency_raw is None or not currency_raw.strip():
        currency = profile.defaults.get("currency")
        if currency is None:
            errors.append(
                (
                    INVALID_CURRENCY,
                    "currency is missing and no defaults.currency is configured",
                )
            )
            currency = ""
    else:
        currency = currency_raw.strip().upper()
    if currency and not _CURRENCY_RE.match(currency):
        errors.append(
            (INVALID_CURRENCY, f"currency {currency!r} is not a 3-letter code")
        )

    if errors:
        message = "; ".join(message for _, message in errors)
        return None, Rejection(
            row_number=row_number,
            reason_code=errors[0][0],
            message=message,
            raw=dict(raw),
        )

    description = raw.get("description")
    if description is not None:
        description = description.strip() or None

    return (
        PriceRow(
            row_number=row_number,
            article_code=code,
            description=description,
            price=price,
            currency=currency,
        ),
        None,
    )
