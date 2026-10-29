"""Strict parser for IBKR transaction-history CSV files."""
import json
import math
import os
import re

import pandas as pd

from account_reconciliation import UnitReconciler, normalize_symbol_quantities
from nbp_service import NBPRateUnavailable
from tax_calculator import PIT38Calculator


PLACEHOLDERS = {"", "-", "NAN", "NONE", "NULL"}
CURRENCIES = {
    "EUR", "USD", "GBP", "CHF", "PLN", "AUD", "CAD", "JPY", "SEK", "NOK", "DKK", "CZK", "HUF",
    "CNH", "ILS", "KRW", "MYR", "MXN", "NZD", "SAR", "SGD", "ZAR", "TRY", "AED",
}
UNSUPPORTED_SECURITY_EVENT_MARKERS = (
    "transfer",
    "stock split",
    "split",
    "merger",
    "spin off",
    "spinoff",
    "reorganization",
    "reorg",
    "redemption",
    "tender",
    "cash in lieu",
    "return of capital",
    "rights issue",
    "rights distribution",
    "exercise",
    "assignment",
    "expiration",
    "conversion",
)
NEUTRAL_TRADE_TYPES = {"", "transaction", "trade", "execution", "trade execution", "fill", "trade fill"}


def _normalized_header(value):
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value).strip())
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def find_matching_column(columns, candidates):
    """Prefer exact normalized headers, then conservative partial matches."""
    normalized = {col: _normalized_header(col) for col in columns}
    for candidate in candidates:
        target = _normalized_header(candidate)
        for col in columns:
            if normalized[col] == target:
                return col
    for candidate in candidates:
        target = _normalized_header(candidate)
        for col in columns:
            if target and target in normalized[col]:
                return col
    return None


def _clean_value(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def _parse_number(value, label):
    raw = _clean_value(value)
    if raw.upper() in PLACEHOLDERS:
        raise ValueError(f"Missing {label}.")
    raw = raw.replace(",", "").replace("−", "-").replace(" ", "")
    try:
        number = float(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid {label}: {raw!r}.") from exc
    if not math.isfinite(number):
        raise ValueError(f"Invalid non-finite {label}: {raw!r}.")
    return number


def _column_value(row, col):
    return _clean_value(row[col]) if col is not None else ""


def _first_amount(row, columns, candidates, label):
    seen = set()
    for candidate in candidates:
        target = _normalized_header(candidate)
        col = next((item for item in columns if _normalized_header(item) == target), None)
        # "Amount" must not partially match "Net Amount" after a blank gross
        # amount. For passive income that would silently tax the net cash flow
        # as if it were gross income.
        if col is None and target not in {"amount", "total"}:
            col = next((item for item in columns if target and target in _normalized_header(item)), None)
        if col is None or col in seen:
            continue
        seen.add(col)
        if _clean_value(row[col]).upper() in PLACEHOLDERS:
            continue
        return _parse_number(row[col], label)
    if not seen:
        raise ValueError(f"Could not find a {label} column.")
    raise ValueError(f"Missing {label}.")


def _currency_for_row(row, currency_col, text, cash_currency):
    value = _column_value(row, currency_col).upper()
    if value not in PLACEHOLDERS:
        if len(value) == 3 and value.isalpha():
            return value
        raise ValueError(f"Unrecognized currency code {value!r}.")

    tokens = set(re.findall(r"\b[A-Z]{3}\b", text.upper()))
    found = sorted(tokens & CURRENCIES)
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise ValueError(f"Ambiguous currency in description: {', '.join(found)}.")
    return (cash_currency or "").strip().upper()


def _action_from_row(action_value, quantity):
    raw = _normalized_header(action_value)
    tokens = set(raw.split())
    if tokens & {"sell", "sld", "sold", "zbycie", "sprzedaz"}:
        return "SELL"
    if tokens & {"buy", "bot", "bought", "nabycie", "kupno"}:
        return "BUY"
    # Some IBKR reports encode direction only in the signed quantity. Only
    # infer it when the action field is blank or a generic trade label; an
    # explicit unknown type must not turn transfers/actions into trades.
    if raw not in NEUTRAL_TRADE_TYPES:
        return None
    if quantity < 0:
        return "SELL"
    if quantity > 0:
        return "BUY"
    return None


def _security_unit_event(action_value, description, flag_value=""):
    """Return a reconciliation event category, keeping tax treatment separate."""
    action = _normalized_header(action_value)
    text = _normalized_header(f"{action_value} {description}")
    flag = _normalized_header(flag_value).upper()

    if action in {"rec", "receipt", "dvpin"} or any(
        marker in text for marker in ("transfer in", "incoming transfer", "receipt of securities", "securities received")
    ):
        return "TRANSFER_IN"
    if action in {"del", "delivery", "dvpout"} or any(
        marker in text for marker in ("transfer out", "outgoing transfer", "delivery of securities", "securities delivered")
    ):
        return "TRANSFER_OUT"
    if "transfer" in text:
        return "TRANSFER_SIGNED"

    corporate_markers = tuple(
        marker for marker in UNSUPPORTED_SECURITY_EVENT_MARKERS if marker != "transfer"
    )
    is_corporate_action = action in {"corp", "corporate action", "divr"} or any(
        marker in text for marker in corporate_markers
    )
    if is_corporate_action:
        # IBKR labels ordinary cash dividends with CORP + flag CD in some
        # Activity exports. They affect cash/income, not share units.
        if flag == "CD" or "cash dividend" in text or "payment in lieu of dividend" in text:
            return None
        return "CORPORATE_ACTION"
    return None


def _country_map(mapping):
    cleaned = {}
    for ticker, country in (mapping or {}).items():
        ticker = str(ticker).strip().upper()
        country = str(country).strip().upper()
        if ticker and country:
            if len(country) != 2 or not country.isalpha():
                raise ValueError(f"Country for {ticker} must be a two-letter ISO code.")
            cleaned[ticker] = country
    return cleaned


def process_and_calculate_file(
    file_path: str,
    tax_year: int = 2026,
    loss_carryforward: float = 0.0,
    loss_carryforward_verified: bool = False,
    cash_currency: str | None = None,
    cash_country: str | None = None,
    source_country_by_symbol: dict | None = None,
    opening_units_by_symbol: dict | None = None,
    ibkr_closing_units_by_symbol: dict | None = None,
    assume_zero_opening_units: bool = False,
) -> str:
    """Parse a CSV and calculate only after all recognized rows validate.

    Cash-row currencies, cash-source country, and security-source countries
    must be supplied explicitly when the export does not contain them.
    """
    if not file_path or not os.path.isfile(file_path):
        return json.dumps({
            "status": "error",
            "error": f"File {file_path!r} was not found.",
            "warnings": [],
        }, ensure_ascii=False)

    try:
        df = pd.read_csv(file_path, low_memory=False)
        df.columns = [str(col).strip() for col in df.columns]
        if not any(_normalized_header(col) in {"symbol", "ticker"} for col in df.columns):
            for idx, row in df.iterrows():
                values = [_clean_value(value) for value in row.values]
                row_text = " ".join(values).lower()
                if "symbol" in row_text or "ticker" in row_text:
                    df.columns = [str(value).strip() for value in row.values]
                    df = df.iloc[idx + 1:].copy()
                    break
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "error": f"Failed to read CSV: {exc}",
            "warnings": [],
        }, ensure_ascii=False)

    df.columns = [str(col).strip() for col in df.columns]
    cols = list(df.columns)
    date_col = find_matching_column(cols, ["date", "trade date", "tradedate", "transaction date", "date/time", "datetime"])
    ticker_col = find_matching_column(cols, ["symbol", "ticker", "asset", "instrument"])
    description_col = find_matching_column(cols, ["description"])
    # Description is deliberately last: IBKR exports commonly put it before
    # Transaction Type, and it must not shadow an explicit Buy/Sell column.
    action_col = find_matching_column(cols, ["transaction type", "transactiontype", "buy/sell", "action", "side", "operation", "type", "description"])
    qty_col = find_matching_column(cols, ["quantity", "shares", "qty", "units"])
    price_col = find_matching_column(cols, ["trade price", "unit price", "unitprice", "t. price", "t_price", "price", "rate"])
    currency_col = find_matching_column(cols, ["price currency", "listing currency", "currency", "curr", "ccy"])
    commission_col = find_matching_column(cols, ["commission", "comm/fee", "fee", "comm"])
    gross_amount_col = find_matching_column(cols, ["gross amount", "gross proceeds", "gross value"])
    net_amount_col = find_matching_column(cols, ["net amount", "net cash", "net cash flow"])
    flag_col = find_matching_column(cols, ["flag"])

    if date_col is None:
        return json.dumps({
            "status": "error",
            "error": "Critical error: could not find a Date column.",
            "warnings": [],
        }, ensure_ascii=False)

    # Keep the original CSV record index after removing any IBKR preamble rows,
    # so every exported calculation can be tied back to its source record.
    df["__source_row"] = [int(index) + 2 for index in df.index]
    df["__parsed_date"] = pd.to_datetime(df[date_col], errors="coerce")
    cash_currency = (cash_currency or "").strip().upper()
    cash_country = (cash_country or "").strip().upper() or None
    try:
        country_map = _country_map(source_country_by_symbol)
        opening_units = normalize_symbol_quantities(opening_units_by_symbol, "Opening units")
        closing_units = normalize_symbol_quantities(ibkr_closing_units_by_symbol, "IBKR closing units")
        if cash_country and (len(cash_country) != 2 or not cash_country.isalpha()):
            raise ValueError("Cash source country must be a two-letter ISO code.")
    except ValueError as exc:
        return json.dumps({
            "status": "error",
            "error": str(exc),
            "warnings": [],
        }, ensure_ascii=False)
    calculator = PIT38Calculator(
        target_tax_year=tax_year,
        loss_carryforward=loss_carryforward,
        loss_carryforward_verified=loss_carryforward_verified,
    )
    errors = []
    review_notes = []
    etc_reviewed = set()
    processed_count = 0
    fifo_blocked_by_ticker = {}
    reconciler = UnitReconciler(
        opening_units=opening_units,
        broker_closing_units=closing_units,
        assume_zero_opening=assume_zero_opening_units,
    )

    # A stable sort preserves the export's order for fills on the same day.
    df = df.sort_values(
        by=["__parsed_date", "__source_row"],
        ascending=True,
        kind="stable",
        na_position="last",
    )

    for _, row in df.iterrows():
        row_num = int(row["__source_row"])
        if pd.isna(row["__parsed_date"]):
            values = [_clean_value(row[col]) for col in cols]
            if any(value for value in values):
                errors.append(f"Row {row_num}: date could not be parsed; row was excluded.")
                bad_ticker = _column_value(row, ticker_col).upper()
                reconciler.mark_incomplete(
                    bad_ticker if bad_ticker not in PLACEHOLDERS else None,
                    f"Row {row_num}: date could not be parsed, so unit activity could not be ordered.",
                )
            continue

        date = row["__parsed_date"].strftime("%Y-%m-%d")
        # If Description is the only available action-like column, do not treat
        # its prose as an explicit transaction type. Signed quantities remain
        # usable for trade-only exports that omit a direction column.
        action_value = "" if action_col == description_col else _column_value(row, action_col)
        description = _column_value(row, description_col)
        full_text = f"{action_value} {description}".lower()
        flag_value = _column_value(row, flag_col)
        ticker = _column_value(row, ticker_col).upper()
        ticker = "" if ticker in PLACEHOLDERS else ticker
        action_code = _normalized_header(action_value)

        if ticker and ticker not in etc_reviewed and re.search(r"\bETC\b", description, flags=re.IGNORECASE):
            etc_reviewed.add(ticker)
            review_notes.append(
                f"{ticker} is described as an ETC and is included in the listed-security FIFO (Art. 30b) "
                "calculation. Confirm PIT/ZG source-country attribution separately; the ticker and country "
                "mapping alone do not establish the source country."
            )

        try:
            unit_event = _security_unit_event(action_value, description, flag_value)
            if unit_event:
                raw_quantity = None
                if qty_col is not None and _column_value(row, qty_col).upper() not in PLACEHOLDERS:
                    try:
                        raw_quantity = _parse_number(row[qty_col], "unit quantity")
                    except ValueError:
                        raw_quantity = None

                if unit_event == "TRANSFER_SIGNED":
                    if raw_quantity is None or raw_quantity == 0:
                        resolved_event = "TRANSFER_IN"
                        reconciler.record(ticker, resolved_event, None, date, row_num, description)
                        review_notes.append(
                            f"Row {row_num}: transfer direction or quantity for {ticker or 'unknown security'} "
                            "is unresolved and cannot be reconciled."
                        )
                    else:
                        resolved_event = "TRANSFER_IN" if raw_quantity > 0 else "TRANSFER_OUT"
                        reconciler.record(ticker, resolved_event, raw_quantity, date, row_num, description)
                else:
                    reconciler.record(ticker, unit_event, raw_quantity, date, row_num, description)
                    if unit_event == "CORPORATE_ACTION" and raw_quantity == 0:
                        reconciler.mark_incomplete(
                            ticker,
                            f"Row {row_num}: corporate action for {ticker or 'unknown security'} reports zero units; "
                            "confirm whether the action changed the position.",
                        )

                if unit_event.startswith("TRANSFER"):
                    review_notes.append(
                        f"Row {row_num}: transfer units were included in account reconciliation, but the original "
                        "tax basis is not imported. Verify basis before relying on any later sale."
                    )
                else:
                    review_notes.append(
                        f"Row {row_num}: corporate-action units were included as reported, but their tax-basis "
                        "treatment is not calculated; review the event before using tax figures."
                    )
                continue

            if action_code in {"ca", "cancel", "co", "correct"}:
                message = (
                    f"Row {row_num}: IBKR cancel/correction record ({action_value}) requires matching the original "
                    "trade; tax calculation stopped to avoid counting a reversed trade."
                )
                reconciler.mark_incomplete(ticker or None, message)
                errors.append(message)
                continue

            if action_code in {"divacc", "intacc"} or (
                "accrual" in full_text and ("dividend" in full_text or "interest" in full_text)
            ):
                message = (
                    f"Row {row_num}: accrual activity was not treated as paid income; verify its tax timing separately."
                )
                review_notes.append(message)
                continue

            if "withholding" in full_text or "foreign tax" in full_text or action_code == "frtax":
                if "interest" in full_text:
                    income_type = "interest"
                elif "dividend" in full_text:
                    income_type = "dividend"
                elif "capital gain" in full_text or "sale" in full_text:
                    income_type = "capital_gains"
                else:
                    raise ValueError(
                        "Withholding row does not identify whether the tax relates to interest, "
                        "dividends, or capital gains."
                    )
                amount = _first_amount(
                    row, cols,
                    ["gross amount", "amount", "net amount", "proceeds", "total"],
                    "withholding amount",
                )
                if amount > 0:
                    raise ValueError(
                        "Positive withholding may be a tax refund or reversal; review it manually instead of claiming it as tax paid."
                    )
                currency = _currency_for_row(row, currency_col, full_text, cash_currency)
                if not currency:
                    raise ValueError("Cash-row currency is missing; select the account currency.")
                if income_type == "interest":
                    source_country = cash_country
                else:
                    source_country = country_map.get(ticker) if ticker else None
                calculator.add_withholding_tax(
                    date=date,
                    tax_paid_fx=abs(amount),
                    currency=currency,
                    income_type=income_type,
                    country=source_country,
                    source_row=row_num,
                )
                processed_count += 1
                continue

            if "credit interest" in full_text or "cash interest" in full_text or action_code == "cint":
                amount = _first_amount(
                    row, cols,
                    ["gross amount", "amount"],
                    "interest amount",
                )
                if amount < 0:
                    raise ValueError("Negative or reversed interest needs manual review; it was not treated as positive income.")
                currency = _currency_for_row(row, currency_col, full_text, cash_currency)
                if not currency:
                    raise ValueError("Cash-row currency is missing; select the account currency.")
                calculator.add_interest(
                    date=date,
                    gross_fx=amount,
                    currency=currency,
                    country=cash_country,
                    source_row=row_num,
                )
                processed_count += 1
                continue

            if "dividend" in full_text or action_code in {"div", "pil"}:
                amount = _first_amount(
                    row, cols,
                    ["gross amount", "amount"],
                    "dividend amount",
                )
                if amount < 0:
                    raise ValueError("Negative or reversed dividend needs manual review; it was not treated as positive income.")
                currency = _currency_for_row(row, currency_col, full_text, cash_currency)
                if not currency:
                    raise ValueError("Dividend currency is missing; select the account currency.")
                calculator.add_dividend(
                    date=date,
                    gross_fx=amount,
                    currency=currency,
                    country=country_map.get(ticker),
                    ticker=ticker or "DIV",
                    source_row=row_num,
                )
                processed_count += 1
                continue

            if ticker:
                if qty_col is None:
                    raise ValueError("Trade row has no Quantity column.")
                try:
                    quantity_raw = _parse_number(row[qty_col], "quantity")
                except ValueError:
                    reconciler.mark_incomplete(
                        ticker,
                        f"Row {row_num}: trade quantity for {ticker} could not be read.",
                    )
                    raise
                action = _action_from_row(action_value, quantity_raw)
                if action is None:
                    event_type = action_value or "missing transaction type"
                    message = (
                        f"Row {row_num}: unsupported or unclear IBKR event type ({event_type}) for {ticker} "
                        "was excluded from trades and FIFO; review it before relying on the calculation."
                    )
                    reconciler.mark_incomplete(ticker, message)
                    review_notes.append(message)
                    continue
                quantity = abs(quantity_raw)
                reconciler.record(ticker, action, quantity_raw, date, row_num, description)
                prior_failure_row = fifo_blocked_by_ticker.get(ticker)
                if prior_failure_row is not None:
                    message = (
                        f"Row {row_num}: FIFO for {ticker} cannot continue because an earlier trade on row "
                        f"{prior_failure_row} failed validation or currency conversion. Correct that row "
                        "before relying on any later sale calculation."
                    )
                    reconciler.mark_incomplete(ticker, message)
                    errors.append(message)
                    continue

                try:
                    price = _parse_number(row[price_col], "price") if price_col else 0.0
                    commission_raw = _column_value(row, commission_col)
                    commission = abs(_parse_number(commission_raw, "commission")) if commission_raw.upper() not in PLACEHOLDERS else 0.0
                    # IBKR exports provide redundant cash-flow totals. Cross-check
                    # them against quantity × price and commission before using
                    # our independently calculated PLN amounts. This catches
                    # truncated exports, unexpected fees, or column mismatches.
                    expected_gross = quantity * price
                    gross_raw = _column_value(row, gross_amount_col)
                    if gross_raw.upper() not in PLACEHOLDERS:
                        reported_gross = _parse_number(gross_raw, "gross amount")
                        gross_sign = -1 if action == "BUY" else 1
                        if reported_gross * gross_sign < 0:
                            raise ValueError(
                                f"Gross Amount sign does not match {action.lower()} cash flow "
                                f"({reported_gross:g})."
                            )
                        if abs(abs(reported_gross) - expected_gross) > 0.010000001:
                            raise ValueError(
                                f"Gross Amount ({reported_gross:g}) does not match quantity × price "
                                f"({expected_gross:.8f}); check the source row and currency."
                            )

                        net_raw = _column_value(row, net_amount_col)
                        if net_raw.upper() not in PLACEHOLDERS:
                            reported_net = _parse_number(net_raw, "net amount")
                            expected_net = reported_gross - commission
                            if abs(reported_net - expected_net) > 0.010000001:
                                raise ValueError(
                                    f"Net Amount ({reported_net:g}) does not match Gross Amount "
                                    f"minus commission ({expected_net:.8f}); check for additional fees "
                                    "or a mismatched amount currency."
                                )
                    currency = _currency_for_row(row, currency_col, full_text, cash_currency)
                    if not currency:
                        raise ValueError("Trade currency is missing.")
                    calculator.add_transaction(
                        date=date,
                        ticker=ticker,
                        action=action,
                        quantity=quantity,
                        price_fx=price,
                        currency=currency,
                        commission_fx=commission,
                        country=country_map.get(ticker),
                        source_row=row_num,
                    )
                except Exception:
                    fifo_blocked_by_ticker[ticker] = row_num
                    raise
                processed_count += 1
                continue

            # Empty balance/header rows are harmless. Any other unrecognized
            # row is surfaced, never silently treated as a clean parse.
            nonempty = [f"{col}={_clean_value(row[col])}" for col in cols if _clean_value(row[col])]
            if nonempty:
                review_notes.append(
                    f"Row {row_num}: unrecognized activity was not included: "
                    + ", ".join(nonempty[:4])
                )
        except (ValueError, NBPRateUnavailable) as exc:
            errors.append(f"Row {row_num}: {exc}")
        except Exception as exc:
            errors.append(f"Row {row_num}: calculation failed: {exc}")

    account_reconciliation = reconciler.build()
    if account_reconciliation["Rows"]:
        mismatches = [row for row in account_reconciliation["Rows"] if row["Status"] == "MISMATCH"]
        for row in mismatches:
            review_notes.append(
                f"IBKR unit mismatch for {row['Ticker']}: calculated closing units "
                f"{row['Calculated closing units']:g}, IBKR reported "
                f"{row['IBKR closing units']:g} (difference {row['Difference (calculated - IBKR)']:+g})."
            )
        missing_opening = [row["Ticker"] for row in account_reconciliation["Rows"] if row["Opening units"] is None]
        if missing_opening:
            review_notes.append(
                "Unit reconciliation needs opening quantities before the earliest imported activity for: "
                + ", ".join(missing_opening) + "."
            )
        missing_closing = [row["Ticker"] for row in account_reconciliation["Rows"] if row["IBKR closing units"] is None]
        if missing_closing:
            review_notes.append(
                "Unit reconciliation needs IBKR-reported closing quantities as of the end of the imported activity for: "
                + ", ".join(missing_closing) + "."
            )

    if errors:
        return json.dumps({
            "status": "error",
            "error": "Calculation stopped because one or more rows could not be safely processed.",
            "processed_records": processed_count,
            "warnings": errors + review_notes,
            "account_reconciliation": account_reconciliation,
        }, ensure_ascii=False)

    if processed_count == 0:
        return json.dumps({
            "status": "error",
            "error": "No supported transactions or income rows were found.",
            "processed_records": 0,
            "warnings": review_notes,
            "account_reconciliation": account_reconciliation,
        }, ensure_ascii=False)

    for note in review_notes:
        calculator.add_review_note(note)
    results = calculator.calculate_pit38()
    results["Account_Reconciliation"] = account_reconciliation
    status = "needs_review" if results["Review_Required"] else "success"
    return json.dumps({
        "status": status,
        "file_processed": os.path.basename(file_path),
        "processed_records": processed_count,
        "warnings": results["Review_Notes"],
        "pit38_calculation": results,
    }, indent=2, ensure_ascii=False)
