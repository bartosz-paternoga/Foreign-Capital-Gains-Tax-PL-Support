"""Quantity reconciliation for IBKR security activity."""
from decimal import Decimal, InvalidOperation


ZERO = Decimal("0")
TOLERANCE = Decimal("0.00000001")


def _quantity(value, label):
    try:
        result = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"Invalid {label} quantity {value!r}.") from exc
    if not result.is_finite():
        raise ValueError(f"Invalid non-finite {label} quantity {value!r}.")
    return result


def parse_symbol_quantities(text, label):
    """Parse user entries such as ``EGLN=20`` or ``EGLN=20; IWDA=3.5``."""
    result = {}
    for item in str(text or "").replace("\n", ";").replace(",", ";").split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"{label}: use SYMBOL=quantity entries, for example EGLN=20.")
        symbol, raw_quantity = (part.strip() for part in item.split("=", 1))
        symbol = symbol.upper()
        if not symbol or not raw_quantity:
            raise ValueError(f"{label}: each entry needs both a symbol and a quantity.")
        if symbol in result:
            raise ValueError(f"{label}: {symbol} is listed more than once.")
        result[symbol] = _quantity(raw_quantity, label)
    return result


def normalize_symbol_quantities(values, label):
    result = {}
    for symbol, raw_quantity in (values or {}).items():
        symbol = str(symbol).strip().upper()
        if not symbol:
            raise ValueError(f"{label}: symbol cannot be blank.")
        if symbol in result:
            raise ValueError(f"{label}: {symbol} is listed more than once.")
        result[symbol] = _quantity(raw_quantity, label)
    return result


def _as_number(value):
    return None if value is None else float(value)


class UnitReconciler:
    """Tracks units independently from the tax-basis/FIFO calculation."""

    def __init__(self, opening_units=None, broker_closing_units=None, assume_zero_opening=False):
        self.opening_units = normalize_symbol_quantities(opening_units, "Opening units")
        self.broker_closing_units = normalize_symbol_quantities(broker_closing_units, "IBKR closing units")
        self.assume_zero_opening = bool(assume_zero_opening)
        self.movements = {}
        self.unit_events = []
        self.incomplete_by_symbol = {}
        self.general_issues = []

    def _bucket(self, symbol):
        return self.movements.setdefault(symbol, {
            "buys": ZERO,
            "sells": ZERO,
            "transfers_in": ZERO,
            "transfers_out": ZERO,
            "corporate_action_delta": ZERO,
            "buy_rows": 0,
            "sell_rows": 0,
            "transfer_in_rows": 0,
            "transfer_out_rows": 0,
            "corporate_action_rows": 0,
        })

    def record(self, symbol, event_type, quantity, date, row_number, description=""):
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            self.general_issues.append(f"Row {row_number}: {event_type} has no symbol; account reconciliation is incomplete.")
            return
        if quantity is None:
            self.mark_incomplete(
                symbol,
                f"Row {row_number}: {event_type} for {symbol} has no usable unit quantity.",
            )
            self.unit_events.append({
                "Date": date,
                "Ticker": symbol,
                "Event": event_type,
                "Quantity": None,
                "Description": description,
                "Row": row_number,
                "Included": False,
            })
            return

        quantity = _quantity(quantity, f"row {row_number}")
        bucket = self._bucket(symbol)
        if event_type == "BUY":
            bucket["buys"] += abs(quantity)
            bucket["buy_rows"] += 1
        elif event_type == "SELL":
            bucket["sells"] += abs(quantity)
            bucket["sell_rows"] += 1
        elif event_type == "TRANSFER_IN":
            bucket["transfers_in"] += abs(quantity)
            bucket["transfer_in_rows"] += 1
        elif event_type == "TRANSFER_OUT":
            bucket["transfers_out"] += abs(quantity)
            bucket["transfer_out_rows"] += 1
        elif event_type == "CORPORATE_ACTION":
            bucket["corporate_action_delta"] += quantity
            bucket["corporate_action_rows"] += 1
        else:
            raise ValueError(f"Unsupported unit event type {event_type!r}.")

        self.unit_events.append({
            "Date": date,
            "Ticker": symbol,
            "Event": event_type,
            "Quantity": _as_number(quantity),
            "Description": description,
            "Row": row_number,
            "Included": True,
        })

    def mark_incomplete(self, symbol, message):
        symbol = str(symbol or "").strip().upper()
        if symbol:
            self.incomplete_by_symbol.setdefault(symbol, []).append(message)
        else:
            self.general_issues.append(message)

    def build(self):
        symbols = sorted(
            set(self.opening_units)
            | set(self.broker_closing_units)
            | set(self.movements)
            | set(self.incomplete_by_symbol)
        )
        rows = []
        has_mismatch = False
        has_incomplete_activity = bool(self.general_issues)
        missing_opening = False
        missing_closing = False
        for symbol in symbols:
            movement = self._bucket(symbol)
            if symbol in self.opening_units:
                opening = self.opening_units[symbol]
                opening_source = "Provided"
            elif self.assume_zero_opening:
                opening = ZERO
                opening_source = "Assumed zero"
            else:
                opening = None
                opening_source = "Not provided"
                missing_opening = True

            calculated_close = None
            if opening is not None:
                calculated_close = (
                    opening
                    + movement["buys"]
                    - movement["sells"]
                    + movement["transfers_in"]
                    - movement["transfers_out"]
                    + movement["corporate_action_delta"]
                )

            reported_close = self.broker_closing_units.get(symbol)
            if reported_close is None:
                missing_closing = True
            incomplete_notes = self.incomplete_by_symbol.get(symbol, [])
            has_incomplete_activity = has_incomplete_activity or bool(incomplete_notes)
            difference = None
            if calculated_close is not None and reported_close is not None and not incomplete_notes:
                difference = calculated_close - reported_close
                if abs(difference) > TOLERANCE:
                    has_mismatch = True
                    status = "MISMATCH"
                else:
                    status = "MATCH"
            elif incomplete_notes:
                status = "INCOMPLETE ACTIVITY"
            elif calculated_close is None:
                status = "OPENING UNITS NEEDED"
            else:
                status = "IBKR CLOSING UNITS NEEDED"

            rows.append({
                "Ticker": symbol,
                "Opening units": _as_number(opening),
                "Opening units source": opening_source,
                "Buys": _as_number(movement["buys"]),
                "Buy rows": movement["buy_rows"],
                "Sells": _as_number(movement["sells"]),
                "Sell rows": movement["sell_rows"],
                "Transfers in": _as_number(movement["transfers_in"]),
                "Transfer-in rows": movement["transfer_in_rows"],
                "Transfers out": _as_number(movement["transfers_out"]),
                "Transfer-out rows": movement["transfer_out_rows"],
                "Corporate action units (net)": _as_number(movement["corporate_action_delta"]),
                "Corporate-action rows": movement["corporate_action_rows"],
                "Calculated closing units": _as_number(calculated_close),
                "IBKR closing units": _as_number(reported_close),
                "Difference (calculated - IBKR)": _as_number(difference),
                "Status": status,
                "Issues": " | ".join(incomplete_notes),
            })

        if has_incomplete_activity:
            overall_status = "INCOMPLETE ACTIVITY"
        elif has_mismatch:
            overall_status = "MISMATCH"
        elif missing_opening:
            overall_status = "OPENING UNITS NEEDED"
        elif missing_closing:
            overall_status = "IBKR CLOSING UNITS NEEDED"
        elif rows:
            overall_status = "MATCH"
        else:
            overall_status = "NO SECURITY ACTIVITY"

        notes = list(self.general_issues)
        for messages in self.incomplete_by_symbol.values():
            notes.extend(messages)
        if any(row["Opening units source"] == "Assumed zero" for row in rows):
            notes.append("Opening units default to zero for tickers omitted from the opening-units input.")
        return {
            "Status": overall_status,
            "Formula": "Opening + buys - sells + transfers in - transfers out + corporate-action unit change = calculated closing units",
            "Rows": rows,
            "Unit events": self.unit_events,
            "Issues": list(dict.fromkeys(notes)),
            "Opening units assumed zero when omitted": self.assume_zero_opening,
        }
