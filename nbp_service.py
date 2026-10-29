"""Strict access to NBP Table A exchange rates used for PIT conversions."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite

import requests


class NBPRateUnavailable(RuntimeError):
    """Raised when a published NBP rate cannot be verified."""


@dataclass(frozen=True)
class NBPRate:
    value: float
    effective_date: str | None
    table_number: str | None = None


def get_nbp_rate(currency: str, transaction_date: str) -> NBPRate:
    """Return the latest Table A rate published before the transaction date.

    The date included in the returned quote is the NBP effective date, not a
    weekday guessed from the calendar. No estimated fallback rates are used.
    """
    code = str(currency or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise NBPRateUnavailable(f"Invalid currency code: {currency!r}")

    try:
        tx_date = datetime.strptime(str(transaction_date).split(" ")[0], "%Y-%m-%d").date()
    except ValueError as exc:
        raise NBPRateUnavailable(f"Invalid transaction date: {transaction_date!r}") from exc

    if code == "PLN":
        return NBPRate(1.0, None)

    last_allowed_date = tx_date - timedelta(days=1)
    first_allowed_date = last_allowed_date - timedelta(days=14)
    url = (
        "https://api.nbp.pl/api/exchangerates/rates/a/"
        f"{code.lower()}/{first_allowed_date:%Y-%m-%d}/"
        f"{last_allowed_date:%Y-%m-%d}/?format=json"
    )

    try:
        response = requests.get(url, timeout=8)
    except requests.RequestException as exc:
        raise NBPRateUnavailable(
            f"Could not reach NBP for {code} before {tx_date}: {exc}"
        ) from exc

    if response.status_code != 200:
        detail = "no published rate in the requested period" if response.status_code == 404 else f"HTTP {response.status_code}"
        raise NBPRateUnavailable(f"NBP rate unavailable for {code} before {tx_date}: {detail}")

    try:
        rates = response.json()["rates"]
        eligible = [
            row for row in rates
            if row.get("effectiveDate") and row["effectiveDate"] <= last_allowed_date.isoformat()
        ]
        if not eligible:
            raise ValueError("NBP response contained no eligible rate")
        quote = max(eligible, key=lambda row: row["effectiveDate"])
        value = float(quote["mid"])
        if not isfinite(value) or value <= 0:
            raise ValueError("NBP returned a non-finite or non-positive rate")
        return NBPRate(value, quote["effectiveDate"], quote.get("no"))
    except (KeyError, TypeError, ValueError) as exc:
        raise NBPRateUnavailable(f"Invalid NBP response for {code} before {tx_date}: {exc}") from exc
