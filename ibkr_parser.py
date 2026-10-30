"""Strict parser for IBKR transaction-history CSV files."""
import csv
import io
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

COLUMN_SPECS = {
    "date": {
        "label": "Date",
        "aliases": ("date", "trade date", "tradedate", "transaction date", "date/time", "datetime"),
        "required": True,
    },
    "transaction_type": {
        "label": "Transaction type / action",
        "aliases": ("transaction type", "transactiontype", "buy/sell", "buy sell", "action", "trade action", "transaction action", "side", "transaction side", "operation", "txn type", "trade type", "type"),
        "required": True,
    },
    "symbol": {
        "label": "Ticker / symbol",
        "aliases": ("symbol", "ticker", "ticker symbol", "security symbol", "instrument symbol", "asset", "instrument"),
        "required": True,
    },
    "description": {
        "label": "Description",
        "aliases": ("description", "details", "activity description", "transaction description"),
        "required": False,
    },
    "quantity": {
        "label": "Quantity",
        "aliases": ("quantity", "shares", "qty", "units"),
        "required": False,
    },
    "price": {
        "label": "Price",
        "aliases": ("trade price", "unit price", "unitprice", "t. price", "t_price", "price", "rate"),
        "required": False,
    },
    "currency": {
        "label": "Currency",
        "aliases": ("price currency", "listing currency", "currency", "curr", "ccy"),
        "required": False,
    },
    "commission": {
        "label": "Commission / fee",
        "aliases": ("commission", "comm/fee", "fee", "comm"),
        "required": False,
    },
    "gross_amount": {
        "label": "Gross amount / income amount",
        "aliases": ("gross amount", "gross proceeds", "gross value", "amount"),
        "required": False,
    },
    "net_amount": {
        "label": "Net amount",
        "aliases": ("net amount", "net cash", "net cash flow"),
        "required": False,
    },
    "flag": {
        "label": "IBKR flag",
        "aliases": ("flag",),
        "required": False,
    },
}


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
        if target in {"amount", "type", "price"}:
            continue
        for col in columns:
            if target and target in normalized[col]:
                return col
    return None


def _header_matches(columns, role):
    aliases = [_normalized_header(alias) for alias in COLUMN_SPECS[role]["aliases"]]
    normalized_columns = {
        column: _normalized_header(re.sub(r"\s+\[\d+\]$", "", str(column)))
        for column in columns
    }
    exact = [column for column in columns if normalized_columns[column] in aliases]
    if exact:
        return exact
    partial = [
        column for column in columns
        if any(
            len(alias) >= 5 and alias not in {"amount", "price"}
            and alias in normalized_columns[column]
            for alias in aliases
        )
    ]
    return partial


def _suggest_column_mapping(columns):
    suggestions = {}
    for role, spec in COLUMN_SPECS.items():
        matches = _header_matches(columns, role)
        suggestions[role] = {
            "label": spec["label"],
            "matches": matches,
            "suggested": matches[0] if len(matches) == 1 else None,
            "ambiguous": len(matches) > 1,
            "required": spec["required"],
        }
    return suggestions


def _unique_headers(headers):
    seen = {}
    unique = []
    for index, value in enumerate(headers, start=1):
        label = str(value).strip() or f"Unnamed column {index}"
        seen[label] = seen.get(label, 0) + 1
        unique.append(label if seen[label] == 1 else f"{label} [{seen[label]}]")
    return unique


def _read_ibkr_transaction_rows(rows):
    """Extract a tagged IBKR table or a header-first transaction CSV."""
    raw_header_index = next((
        index for index, row in enumerate(rows)
        if len(row) >= 4
        and _normalized_header(row[0]) == "transaction history"
        and _normalized_header(row[1]) == "header"
    ), None)

    header_index = raw_header_index
    header_prefix = 2 if raw_header_index is not None else 0
    source_format = "IBKR raw report CSV" if header_prefix else "IBKR transaction table CSV"

    if header_index is None:
        scored_headers = []
        for index, row in enumerate(rows):
            candidate = [str(value).strip() for value in row]
            suggestions = _suggest_column_mapping(candidate)
            core_matches = sum(bool(suggestions[role]["matches"]) for role in ("date", "transaction_type", "symbol"))
            if core_matches >= 2:
                scored_headers.append((core_matches, -index, index, candidate))
        if scored_headers:
            _, _, header_index, _ = max(scored_headers)
        else:
            header_index = next((index for index, row in enumerate(rows) if any(str(value).strip() for value in row)), None)
            source_format = "IBKR transaction table CSV (header review required)"

    if header_index is None:
        raise ValueError("The CSV is empty.")

    raw_headers = [str(value).strip() for value in rows[header_index][header_prefix:]]
    headers = _unique_headers(raw_headers)
    if not headers:
        raise ValueError("The detected header row has no columns.")

    records = []
    source_lines = []
    for index in range(header_index + 1, len(rows)):
        line_number = index + 1
        cells = [str(value).strip() for value in rows[index]]
        if not cells or not any(cells):
            continue

        if header_prefix:
            if len(cells) < 2 or _normalized_header(cells[0]) != "transaction history":
                continue
            row_kind = _normalized_header(cells[1])
            if row_kind != "data":
                continue
            values = cells[2:]
        else:
            values = cells
            if len(values) == len(raw_headers) and [_normalized_header(value) for value in values] == [
                _normalized_header(value) for value in raw_headers
            ]:
                continue

        if len(values) != len(headers):
            raise ValueError(
                f"CSV row {line_number} has {len(values)} fields; the detected header has {len(headers)}."
            )
        records.append(values)
        source_lines.append(line_number)

    if not records:
        raise ValueError("The detected transaction table contains no data rows.")

    frame = pd.DataFrame(records, columns=headers)
    return frame, source_lines, source_format


def _read_ibkr_transaction_csv(file_path):
    """Read IBKR CSV files and retain source-row references."""
    with open(file_path, "r", encoding="utf-8-sig", newline="") as source:
        rows = list(csv.reader(source))
    return _read_ibkr_transaction_rows(rows)


def inspect_ibkr_csv(file_bytes, column_mapping=None, cash_currency=None):
    """Preflight an uploaded CSV without performing tax calculations."""
    try:
        text = file_bytes.decode("utf-8-sig") if isinstance(file_bytes, bytes) else str(file_bytes)
        rows = list(csv.reader(io.StringIO(text)))
        frame, source_lines, source_format = _read_ibkr_transaction_rows(rows)
    except Exception as exc:
        return {"status": "error", "error": f"Could not inspect this CSV: {exc}"}

    columns = list(frame.columns)
    suggestions = _suggest_column_mapping(columns)
    mapping = {
        role: (column_mapping[role] if column_mapping is not None and role in column_mapping else item["suggested"])
        for role, item in suggestions.items()
    }
    issues = []
    blocking_issues = []
    for role, spec in COLUMN_SPECS.items():
        selected = mapping[role]
        if selected is not None and selected not in columns:
            message = f"Mapped {spec['label']} column is not present in this file."
            issues.append({"Severity": "Error", "Code": "mapped_column_missing", "Role": role, "Issue": message})
            blocking_issues.append(message)
            mapping[role] = None
        if spec["required"] and selected is None:
            message = f"Select the column for {spec['label']}."
            issues.append({"Severity": "Required", "Code": "select_required_column", "Role": role, "Issue": message})
            blocking_issues.append(message)
        elif suggestions[role]["ambiguous"] and (column_mapping is None or role not in column_mapping):
            message = f"More than one column could be {spec['label']}; choose the correct one."
            issues.append({
                "Severity": "Review",
                "Code": "ambiguous_column",
                "Role": role,
                "Issue": message,
            })
            blocking_issues.append(message)

    action_col = mapping.get("transaction_type")
    symbol_col = mapping.get("symbol")
    quantity_col = mapping.get("quantity")
    date_col = mapping.get("date")
    action_values = frame[action_col].fillna("").astype(str).str.strip() if action_col else pd.Series([""] * len(frame))
    action_counts = action_values.replace("", "(blank)").value_counts().to_dict()
    trade_rows = []
    passive_rows = []
    unknown_rows = []
    excluded_counts = {}
    bad_date_rows = []

    for position, (_, row) in enumerate(frame.iterrows()):
        source_row = int(source_lines[position])
        action_value = _clean_value(row[action_col]) if action_col else ""
        symbol_value = _clean_value(row[symbol_col]).upper() if symbol_col else ""
        description_col = mapping.get("description")
        description = _clean_value(row[description_col]) if description_col else ""
        quantity = None
        if quantity_col and _clean_value(row[quantity_col]).upper() not in PLACEHOLDERS:
            try:
                quantity = _parse_number(row[quantity_col], "quantity")
            except ValueError:
                pass
        action = _action_from_row(action_value, quantity or 0.0)
        full_text = f"{action_value} {description}".lower()
        is_passive_row = any(marker in full_text for marker in (
            "withholding", "foreign tax", "credit interest", "cash interest", "dividend",
        ))
        excluded_event = _excluded_activity_classification(action_value, description, symbol_value)
        if excluded_event:
            category = excluded_event[0]
            excluded_counts[category] = excluded_counts.get(category, 0) + 1
        if action in {"BUY", "SELL"}:
            trade_rows.append((source_row, action, symbol_value, row))
        if is_passive_row:
            passive_rows.append((source_row, full_text, row))
        unit_event = _security_unit_event(action_value, description)
        if (
            action not in {"BUY", "SELL"}
            and not is_passive_row
            and not unit_event
            and not excluded_event
            and action_value
        ):
            unknown_rows.append({
                "Source row": source_row,
                "Transaction type": action_value,
                "Symbol": symbol_value,
                "Description": description,
            })

        if date_col:
            date_value = _clean_value(row[date_col])
            if date_value and pd.isna(pd.to_datetime(date_value, errors="coerce")):
                bad_date_rows.append(source_row)
            elif not date_value and (action_value or symbol_value):
                bad_date_rows.append(source_row)

    has_trades = bool(trade_rows) or any(_normalized_header(value) in {"buy", "sell", "bot", "sld"} for value in action_values)
    if has_trades:
        for role in ("quantity", "price"):
            if mapping.get(role) is None:
                message = f"Trade rows are present, so a {COLUMN_SPECS[role]['label']} column must be mapped."
                issues.append({"Severity": "Required for trades", "Code": "trade_column_missing", "Role": role, "Issue": message})
                blocking_issues.append(message)
        if mapping.get("quantity") is not None:
            for source_row, action, symbol_value, row in trade_rows:
                if not symbol_value or symbol_value in PLACEHOLDERS:
                    message = f"Row {source_row}: {action.lower()} has no ticker or symbol."
                    issues.append({
                        "Severity": "Error", "Code": "row_symbol_missing",
                        "Row": source_row, "Action": action, "Issue": message,
                    })
                    blocking_issues.append(message)
                for role in ("quantity", "price"):
                    column = mapping.get(role)
                    if column is None or _clean_value(row[column]).upper() in PLACEHOLDERS:
                        message = f"Row {source_row}: {action.lower()} is missing {COLUMN_SPECS[role]['label'].lower()}."
                        issues.append({
                            "Severity": "Error", "Code": "row_value_missing",
                            "Role": role, "Row": source_row, "Action": action, "Issue": message,
                        })
                        blocking_issues.append(message)
                numeric_values = {}
                for role in ("quantity", "price", "commission", "gross_amount", "net_amount"):
                    column = mapping.get(role)
                    if column is None or _clean_value(row[column]).upper() in PLACEHOLDERS:
                        continue
                    try:
                        numeric_values[role] = _parse_number(row[column], COLUMN_SPECS[role]["label"].lower())
                    except ValueError:
                        message = f"Row {source_row}: {COLUMN_SPECS[role]['label']} is not a valid number."
                        issues.append({
                            "Severity": "Error", "Code": "row_value_invalid",
                            "Role": role, "Row": source_row, "Action": action, "Issue": message,
                        })
                        blocking_issues.append(message)
                if "quantity" in numeric_values and "price" in numeric_values and "gross_amount" in numeric_values:
                    gross = numeric_values["gross_amount"]
                    gross_sign = -1 if action == "BUY" else 1
                    expected_gross = abs(numeric_values["quantity"]) * numeric_values["price"]
                    if gross * gross_sign < 0 or abs(abs(gross) - expected_gross) > 0.010000001:
                        message = f"Row {source_row}: reported gross amount does not agree with quantity × price."
                        issues.append({
                            "Severity": "Error", "Code": "gross_amount_mismatch",
                            "Row": source_row, "Issue": message,
                        })
                        blocking_issues.append(message)
                    if "net_amount" in numeric_values:
                        expected_net = gross - abs(numeric_values.get("commission", 0.0))
                        if abs(numeric_values["net_amount"] - expected_net) > 0.010000001:
                            message = f"Row {source_row}: reported net amount does not agree with gross amount minus commission."
                            issues.append({
                                "Severity": "Error", "Code": "net_amount_mismatch",
                                "Row": source_row, "Issue": message,
                            })
                            blocking_issues.append(message)

    gross_income_rows = [
        item for item in passive_rows
        if not ("withholding" in item[1] or "foreign tax" in item[1])
    ]
    withholding_rows = [
        item for item in passive_rows
        if "withholding" in item[1] or "foreign tax" in item[1]
    ]
    if gross_income_rows and mapping.get("gross_amount") is None:
        message = "Interest or dividend rows are present, so map the gross income amount column."
        issues.append({"Severity": "Required for income", "Code": "income_amount_missing", "Role": "gross_amount", "Issue": message})
        blocking_issues.append(message)
    if withholding_rows and mapping.get("gross_amount") is None and mapping.get("net_amount") is None:
        message = "Withholding rows are present, so map a gross amount or net amount column."
        issues.append({"Severity": "Required for withholding", "Code": "withholding_amount_missing", "Role": "gross_amount", "Issue": message})
        blocking_issues.append(message)
    if passive_rows and mapping.get("description") is None and any(
        "withholding" in full_text or "foreign tax" in full_text for _, full_text, _ in passive_rows
    ):
        message = "A description column is required to identify what the foreign withholding relates to."
        issues.append({"Severity": "Required for withholding", "Code": "withholding_description_missing", "Role": "description", "Issue": message})
        blocking_issues.append(message)

    currency_col = mapping.get("currency")
    description_col = mapping.get("description")
    currency_rows = [
        (source_row, f"{action_value} {_clean_value(row[description_col]) if description_col else ''}", row, False)
        for source_row, _, symbol_value, row in trade_rows
        for action_value in [_clean_value(row[action_col]) if action_col else ""]
    ] + [
        (source_row, full_text, row, True)
        for source_row, full_text, row in passive_rows
    ]
    for source_row, text, row, is_cash_row in currency_rows:
        try:
            # The account cash currency is valid fallback data for interest
            # and withholding, but not for securities that may trade in other
            # currencies in the same IBKR account.
            currency = _currency_for_row(
                row, currency_col, text, cash_currency if is_cash_row else None
            )
            if not currency:
                raise ValueError("Currency missing.")
        except ValueError:
            message = f"Row {source_row}: currency is missing, invalid, or ambiguous."
            issues.append({
                "Severity": "Error", "Code": "row_currency_invalid",
                "Row": source_row, "Issue": message,
            })
            blocking_issues.append(message)

    for source_row, full_text, row in passive_rows:
        is_withholding = "withholding" in full_text or "foreign tax" in full_text
        amount_columns = (
            [mapping.get("gross_amount"), mapping.get("net_amount")]
            if is_withholding else [mapping.get("gross_amount")]
        )
        amount_columns = [column for column in amount_columns if column is not None]
        amount = next(
            (column for column in amount_columns if _clean_value(row[column]).upper() not in PLACEHOLDERS),
            None,
        )
        if amount is None:
            code = "withholding_amount_missing" if is_withholding else "row_amount_missing"
            message = f"Row {source_row}: the income or withholding amount is blank."
            issues.append({"Severity": "Error", "Code": code, "Row": source_row, "Issue": message})
            blocking_issues.append(message)
        else:
            try:
                _parse_number(row[amount], "income or withholding amount")
            except ValueError:
                message = f"Row {source_row}: the income or withholding amount is not a valid number."
                issues.append({"Severity": "Error", "Code": "row_amount_invalid", "Row": source_row, "Issue": message})
                blocking_issues.append(message)

    if bad_date_rows:
        message = f"{len(bad_date_rows)} row(s) have a missing or unreadable date: {', '.join(map(str, bad_date_rows[:10]))}."
        issues.append({"Severity": "Error", "Code": "bad_dates", "Count": len(bad_date_rows), "Rows": bad_date_rows, "Issue": message})
        blocking_issues.append(message)
    if unknown_rows:
        for item in unknown_rows:
            item["Code"] = "unrecognized_transaction_type"
        issues.append({
            "Severity": "Review",
            "Code": "unknown_transaction_types",
            "Count": len(unknown_rows),
            "Issue": f"{len(unknown_rows)} security row(s) use an unrecognized transaction type; review before continuing.",
        })

    sample_columns = list(dict.fromkeys(column for column in mapping.values() if column is not None))
    preview = frame[sample_columns].head(5).copy() if sample_columns else frame.head(5).copy()
    preview.insert(0, "Source row", source_lines[:len(preview)])
    type_counts = [{"Transaction type": str(kind), "Rows": int(count)} for kind, count in action_counts.items()]
    return {
        "status": "needs_review" if issues else "ready",
        "source_format": source_format,
        "row_count": len(frame),
        "columns": columns,
        "suggestions": suggestions,
        "column_mapping": mapping,
        "action_counts": type_counts,
        "excluded_activity_summary": excluded_counts,
        "unknown_rows": unknown_rows,
        "issues": issues,
        "blocking_issues": list(dict.fromkeys(blocking_issues)),
        "preview": preview,
    }


def _excluded_activity_classification(action_value, description, ticker):
    """Recognize only explicit raw-report cash/FX rows outside PIT-38 security math."""
    action = _normalized_header(action_value)
    description_normalized = _normalized_header(description)
    symbol = str(ticker or "").strip().upper()

    if (
        action == "forex trade component"
        and re.fullmatch(r"[A-Z]{3}\.[A-Z]{3}", symbol)
        and "net amount in base from forex trade" in description_normalized
        and symbol in str(description or "").upper()
    ):
        return (
            "FX conversion component",
            "IBKR currency-conversion component; excluded from this PIT-38 calculation. "
            "Separate tax treatment is not determined by this report.",
        )

    if action == "adjustment" and description_normalized == "fx translations p l":
        return (
            "FX translation adjustment",
            "IBKR FX translation adjustment; not treated as a security trade or cash-income row in this PIT-38 calculation.",
        )

    if action in {"deposit", "withdrawal"}:
        return (
            "Cash deposit" if action == "deposit" else "Cash withdrawal",
            "IBKR account-funding cash movement; excluded from security proceeds and investment income.",
        )

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


def _resolve_column(columns, column_mapping, role, candidates):
    if column_mapping is not None and role in column_mapping:
        selected = column_mapping[role]
        return selected if selected in columns else None
    return find_matching_column(columns, candidates)


def _first_amount(row, columns, candidates, label, preferred_col=None):
    seen = set()
    ordered_candidates = ([preferred_col] if preferred_col else []) + list(candidates)
    for candidate in ordered_candidates:
        if candidate in columns:
            if candidate in seen:
                continue
            seen.add(candidate)
            if _clean_value(row[candidate]).upper() not in PLACEHOLDERS:
                return _parse_number(row[candidate], label)
            continue
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
    column_mapping: dict | None = None,
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
        df, source_lines, source_format = _read_ibkr_transaction_csv(file_path)
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "error": f"Failed to read CSV: {exc}",
            "warnings": [],
        }, ensure_ascii=False)

    df.columns = [str(col).strip() for col in df.columns]
    cols = list(df.columns)
    date_col = _resolve_column(cols, column_mapping, "date", list(COLUMN_SPECS["date"]["aliases"]))
    ticker_col = _resolve_column(cols, column_mapping, "symbol", list(COLUMN_SPECS["symbol"]["aliases"]))
    description_col = _resolve_column(cols, column_mapping, "description", list(COLUMN_SPECS["description"]["aliases"]))
    # Description is deliberately last: IBKR exports commonly put it before
    # Transaction Type, and it must not shadow an explicit Buy/Sell column.
    action_col = _resolve_column(cols, column_mapping, "transaction_type", list(COLUMN_SPECS["transaction_type"]["aliases"]) + ["description"])
    qty_col = _resolve_column(cols, column_mapping, "quantity", list(COLUMN_SPECS["quantity"]["aliases"]))
    price_col = _resolve_column(cols, column_mapping, "price", list(COLUMN_SPECS["price"]["aliases"]))
    currency_col = _resolve_column(cols, column_mapping, "currency", list(COLUMN_SPECS["currency"]["aliases"]))
    commission_col = _resolve_column(cols, column_mapping, "commission", list(COLUMN_SPECS["commission"]["aliases"]))
    gross_amount_col = _resolve_column(cols, column_mapping, "gross_amount", list(COLUMN_SPECS["gross_amount"]["aliases"]))
    net_amount_col = _resolve_column(cols, column_mapping, "net_amount", list(COLUMN_SPECS["net_amount"]["aliases"]))
    flag_col = _resolve_column(cols, column_mapping, "flag", list(COLUMN_SPECS["flag"]["aliases"]))
    withholding_amount_candidates = (
        [net_amount_col] if column_mapping is not None and net_amount_col
        else [] if column_mapping is not None
        else ["gross amount", "amount", "net amount", "proceeds", "total"]
    )
    income_amount_candidates = [] if column_mapping is not None else ["gross amount", "amount"]

    if date_col is None:
        return json.dumps({
            "status": "error",
            "error": "Critical error: could not find a Date column.",
            "warnings": [],
        }, ensure_ascii=False)

    # Keep physical source line numbers so raw IBKR report rows can be traced
    # through the summary preamble to the original CSV.
    df["__source_row"] = source_lines
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
    excluded_activity = []
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

        exclusion = _excluded_activity_classification(action_value, description, ticker)
        if exclusion:
            category, reason = exclusion
            excluded_activity.append({
                "Date": date,
                "Source row": row_num,
                "IBKR transaction type": action_value,
                "Category": category,
                "Symbol": ticker,
                "Description": description,
                "Reason excluded from PIT-38": reason,
            })
            continue

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
                    withholding_amount_candidates,
                    "withholding amount",
                    preferred_col=gross_amount_col,
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
                    income_amount_candidates,
                    "interest amount",
                    preferred_col=gross_amount_col,
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
                    income_amount_candidates,
                    "dividend amount",
                    preferred_col=gross_amount_col,
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
                    currency = _currency_for_row(row, currency_col, full_text, None)
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

    excluded_summary = {}
    for item in excluded_activity:
        category = item["Category"]
        excluded_summary[category] = excluded_summary.get(category, 0) + 1
    excluded_activity_summary = {
        "total": len(excluded_activity),
        "by_category": excluded_summary,
    }
    fx_conversion_count = excluded_summary.get("FX conversion component", 0)
    if fx_conversion_count:
        review_notes.append(
            f"Excluded {fx_conversion_count} explicit IBKR Forex Trade Component rows from this PIT-38 calculation. "
            "They are retained in the excluded-activity audit trail. This does not determine whether any actual "
            "currency-exchange gain needs separate PIT reporting; verify the treatment for your facts."
        )

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
            "source_format": source_format,
            "excluded_activity": excluded_activity,
            "excluded_activity_summary": excluded_activity_summary,
        }, ensure_ascii=False)

    if processed_count == 0:
        return json.dumps({
            "status": "error",
            "error": "No supported transactions or income rows were found.",
            "processed_records": 0,
            "warnings": review_notes,
            "account_reconciliation": account_reconciliation,
            "source_format": source_format,
            "excluded_activity": excluded_activity,
            "excluded_activity_summary": excluded_activity_summary,
        }, ensure_ascii=False)

    for note in review_notes:
        calculator.add_review_note(note)
    results = calculator.calculate_pit38()
    results["Account_Reconciliation"] = account_reconciliation
    results["Source_Format"] = source_format
    results["Excluded_Activity"] = excluded_activity
    results["Excluded_Activity_Summary"] = excluded_activity_summary
    status = "needs_review" if results["Review_Required"] else "success"
    return json.dumps({
        "status": status,
        "file_processed": os.path.basename(file_path),
        "source_format": source_format,
        "processed_records": processed_count,
        "excluded_activity": excluded_activity,
        "excluded_activity_summary": excluded_activity_summary,
        "warnings": results["Review_Notes"],
        "pit38_calculation": results,
    }, indent=2, ensure_ascii=False)
