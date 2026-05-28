"""Pure AdSense ``payments.list`` -> repository-input mapping (no I/O, no DB).

Splits Google ``Payment[]`` into paid settlements and retained balances,
derives the settlement month, enforces the resource-name-date / ``Payment.date``
agreement, and parses the formatted amount string into ``(Decimal, ISO)`` with a
fail-closed currency allowlist. ``parse_amount`` is called by the sync service
for OPEN-month settlements only, never inside ``classify_payments``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

# accounts/{account}/payments/{suffix}; the suffix carries the type/status.
_RESOURCE_NAME_RE = re.compile(
    r"^accounts/(?P<account>[^/]+)/payments/(?P<suffix>.+)$"
)
# Paid settlement suffix: [youtube-]YYYY-MM-DD.
_DATE_SUFFIX_RE = re.compile(r"^(?:youtube-)?(?P<date>\d{4}-\d{2}-\d{2})$")
# Running balance suffix: [youtube-]unpaid (no date).
_BALANCE_SUFFIX_RE = re.compile(r"^(?:youtube-)?unpaid$")
# An explicit ISO 4217 code anywhere in the string wins over symbols.
_ISO_CODE_RE = re.compile(r"\b([A-Z]{3})\b")
# Only unambiguous symbols are accepted; $, ¥, kr, etc. are intentionally absent.
_SYMBOL_CURRENCIES: dict[str, str] = {"£": "GBP", "€": "EUR"}
# Plain decimal: optional 3-digit thousands groups, optional fractional part.
_NUMBER_RE = re.compile(r"^(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?$")


class AdSensePaymentMappingError(ValueError):
    """Raised when a Payment cannot be safely mapped (fail-closed)."""


@dataclass(frozen=True)
class SkippedBalance:
    resource_name: str
    raw_amount: str
    reason: str  # "no_payment_date" for unpaid / youtube-unpaid balances


@dataclass(frozen=True)
class PaidSettlement:
    source_account_id: str
    month: str          # YYYY-MM derived from Payment.date
    payment_name: str   # raw resource-name suffix, e.g. "2026-04-21"
    payment_date: date
    raw_amount: str     # raw formatted string, preserved into raw_payload
    resource_name: str  # full "accounts/{account}/payments/{suffix}"


@dataclass(frozen=True)
class ClassifiedPayments:
    paid: list[PaidSettlement]
    skipped_balances: list[SkippedBalance]


# ============================================================================
# Purpose: Classify each Google Payment into a paid settlement or a retained
#   balance, deriving the settlement month and enforcing fail-closed identity
#   and date-agreement invariants before any persistence.
# Database/ORM: None (pure mapping; no I/O).
# Standards: Typed AdSensePaymentMappingError on every unsafe shape; no silent
#   skips of dated settlements; raw formatted amount preserved for raw_payload.
# Blast Radius: Feeds the AdSense payment sync service inputs only. A drift here
#   would mis-attribute or drop a real payment from the source of truth.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/adsense_payment_sync.py
#     -> consumes ClassifiedPayments and calls parse_amount for open months.
# ============================================================================
def classify_payments(
    response: dict[str, object], *, account_id: str
) -> ClassifiedPayments:
    """Split payments into paid settlements vs retained balances (fail-closed)."""
    if not isinstance(response, dict):
        raise AdSensePaymentMappingError("payments response must be an object")
    raw_payments = response.get("payments")
    if not isinstance(raw_payments, list):
        raise AdSensePaymentMappingError("payments field must be a list")

    paid: list[PaidSettlement] = []
    skipped: list[SkippedBalance] = []
    for entry in raw_payments:
        if not isinstance(entry, dict):
            raise AdSensePaymentMappingError("each payment must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise AdSensePaymentMappingError("payment.name must be a non-empty string")
        raw_amount = entry.get("amount")
        if not isinstance(raw_amount, str):
            raise AdSensePaymentMappingError(
                f"payment.amount must be a string for {name!r}"
            )
        suffix = _resource_suffix(name, account_id)

        if _BALANCE_SUFFIX_RE.fullmatch(suffix):
            # unpaid / youtube-unpaid carry no settlement date -> never a row.
            skipped.append(
                SkippedBalance(
                    resource_name=name,
                    raw_amount=raw_amount,
                    reason="no_payment_date",
                )
            )
            continue

        date_match = _DATE_SUFFIX_RE.fullmatch(suffix)
        if date_match is None:
            raise AdSensePaymentMappingError(
                f"unrecognized payment name form: {name!r}"
            )
        payment_date = _parse_google_date(entry.get("date"), name)
        suffix_date = _parse_iso_date(date_match.group("date"), name)
        if suffix_date != payment_date:
            raise AdSensePaymentMappingError(
                f"resource-name date {suffix_date} disagrees with "
                f"Payment.date {payment_date} for {name!r}"
            )
        paid.append(
            PaidSettlement(
                source_account_id=account_id,
                month=f"{payment_date.year:04d}-{payment_date.month:02d}",
                payment_name=suffix,
                payment_date=payment_date,
                raw_amount=raw_amount,
                resource_name=name,
            )
        )

    return ClassifiedPayments(paid=paid, skipped_balances=skipped)


# ============================================================================
# Purpose: Parse a Google formatted amount string into a non-negative Decimal
#   and an ISO 4217 currency, fail-closed. Explicit ISO code wins; otherwise an
#   unambiguous allowlisted symbol; bare ambiguous symbols ($, ¥, kr, ...) fail.
# Database/ORM: None.
# Standards: No global "$ -> USD" assumption; deterministic Decimal; negatives
#   and malformed/multi-separator numbers fail closed.
# Blast Radius: Determines the stored amount/currency of a real payment.
# ============================================================================
def parse_amount(raw_amount: str) -> tuple[Decimal, str]:
    """Return ``(Decimal amount, ISO currency)`` or raise (fail-closed)."""
    if not isinstance(raw_amount, str):
        raise AdSensePaymentMappingError(
            f"amount must be a string, got {type(raw_amount).__name__}"
        )
    text = raw_amount.strip()
    if not text:
        raise AdSensePaymentMappingError("amount string is empty")
    if "-" in text:
        # Negative settlements are not valid paid rows; reject before parsing.
        raise AdSensePaymentMappingError(
            f"amount must be non-negative: {raw_amount!r}"
        )

    iso = _ISO_CODE_RE.search(text)
    if iso is not None:
        currency = iso.group(1)
        remainder = (text[: iso.start()] + text[iso.end():]).strip()
        # FIX: The explicit ISO code is authoritative. Strip at most ONE leading
        # currency symbol (e.g. the "¥" in "¥1,235 JPY") plus surrounding
        # whitespace -- do NOT delete embedded characters. The previous
        # re.sub(r"[^0-9.,]", "", remainder) deleted every non-numeric char, so
        # junk inside the number was silently dropped and a fabricated amount
        # was returned ("1e3 GBP" -> 13, "100x50 GBP" -> 10050, "1 234 GBP" ->
        # 1234). Leaving the junk in place forces it through _NUMBER_RE below,
        # which fails closed on anything that is not a clean decimal token.
        number = re.sub(r"^[^\d.,\s]?\s*", "", remainder)
    else:
        currency = ""
        number = ""
        for symbol, code in _SYMBOL_CURRENCIES.items():
            if text.startswith(symbol):
                currency = code
                number = text[len(symbol):].strip()
                break
        if not currency:
            # No ISO code and no allowlisted symbol -> ambiguous ($, ¥, kr, ...).
            raise AdSensePaymentMappingError(
                f"unresolved/ambiguous currency in amount: {raw_amount!r}"
            )

    if not _NUMBER_RE.fullmatch(number):
        raise AdSensePaymentMappingError(
            f"unparseable amount number: {raw_amount!r}"
        )
    try:
        amount = Decimal(number.replace(",", ""))
    except InvalidOperation as exc:
        raise AdSensePaymentMappingError(
            f"unparseable amount: {raw_amount!r}"
        ) from exc
    if not amount.is_finite() or amount < 0:
        raise AdSensePaymentMappingError(
            f"amount must be a finite value >= 0: {raw_amount!r}"
        )
    return amount, currency


def _resource_suffix(name: str, account_id: str) -> str:
    """Return the resource-name suffix after a fail-closed account match."""
    match = _RESOURCE_NAME_RE.fullmatch(name)
    if match is None:
        raise AdSensePaymentMappingError(
            f"payment.name is not a valid resource name: {name!r}"
        )
    if match.group("account") != account_id:
        # The resource name's account must equal the account we pulled for,
        # else the row's identity cannot be trusted.
        raise AdSensePaymentMappingError(
            f"payment.name account {match.group('account')!r} "
            f"!= requested account {account_id!r}"
        )
    return match.group("suffix")


def _parse_google_date(value: object, name: str) -> date:
    """Parse the ``google.type.Date`` object ({year,month,day}) for a settlement."""
    if not isinstance(value, dict):
        raise AdSensePaymentMappingError(
            f"paid settlement {name!r} is missing Payment.date"
        )
    try:
        return date(int(value["year"]), int(value["month"]), int(value["day"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise AdSensePaymentMappingError(
            f"invalid Payment.date for {name!r}"
        ) from exc


def _parse_iso_date(text: str, name: str) -> date:
    """Parse the YYYY-MM-DD resource-name date suffix."""
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise AdSensePaymentMappingError(
            f"invalid date suffix for {name!r}"
        ) from exc
