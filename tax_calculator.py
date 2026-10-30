"""Tax-year ledger and PIT-38 calculation primitives."""
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from math import isfinite
from typing import Dict, List

from nbp_service import get_nbp_rate


CENT = Decimal("0.01")
WHOLE_ZLOTY = Decimal("1")
CAPITAL_GAINS_RATE = Decimal("0.19")
PASSIVE_INCOME_RATE = Decimal("0.19")
EPSILON = 1e-9


def _money(value) -> float:
    return float(Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP))


def _ceil_grosz(value) -> float:
    return float(Decimal(str(value)).quantize(CENT, rounding=ROUND_CEILING))


def _whole_zloty(value) -> int:
    return int(Decimal(str(value)).quantize(WHOLE_ZLOTY, rounding=ROUND_HALF_UP))


def _pln_rate(rate) -> float:
    value = float(rate.value)
    if not isfinite(value) or value <= 0:
        raise ValueError("NBP rate must be finite and positive.")
    return value


def _nbp_source_url(currency: str, effective_date: str | None) -> str:
    if currency == "PLN" or not effective_date:
        return ""
    return (
        "https://api.nbp.pl/api/exchangerates/rates/a/"
        f"{currency.lower()}/{effective_date}/?format=json"
    )


class PIT38Calculator:
    """FIFO securities ledger with separate PIT-38 Art. 30b and Art. 30a totals."""

    def __init__(
        self,
        target_tax_year: int = 2026,
        loss_carryforward: float = 0.0,
        loss_carryforward_verified: bool = False,
    ):
        self.target_tax_year = int(target_tax_year)
        self.loss_carryforward = max(0.0, _money(loss_carryforward))
        self.loss_carryforward_verified = bool(loss_carryforward_verified)
        self.reset()

    def reset(self):
        self.buy_inventory: Dict[str, List[Dict]] = {}
        self.total_capital_revenue_pln = 0.0
        self.total_capital_cost_pln = 0.0
        self.total_interest_pln = 0.0
        self.total_dividend_gross_pln = 0.0
        self.foreign_tax_by_type_and_country: Dict[str, Dict[str, float]] = {
            "capital_gains": {},
            "interest": {},
            "dividend": {},
        }
        self.unallocated_foreign_tax_by_type: Dict[str, float] = {
            "capital_gains": 0.0,
            "interest": 0.0,
            "dividend": 0.0,
        }
        self.source_income_by_country: Dict[str, Dict[str, float]] = {}
        self.calculation_steps: List[Dict] = []
        self.review_notes: List[str] = []

        if self.loss_carryforward > 0 and not self.loss_carryforward_verified:
            self.add_review_note(
                "Prior-year loss entered manually: confirm it is from the same PIT-38 source, "
                "still within its five-year period, and within the applicable annual deduction limit."
            )

    def add_review_note(self, note: str):
        if note not in self.review_notes:
            self.review_notes.append(note)

    @staticmethod
    def _validate_date(date: str) -> int:
        try:
            return datetime.strptime(date, "%Y-%m-%d").year
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid transaction date {date!r}; expected YYYY-MM-DD.") from exc

    @staticmethod
    def _validate_country(country):
        if country is None:
            return None
        country = str(country).strip().upper()
        if not country:
            return None
        if len(country) != 2 or not country.isalpha():
            raise ValueError(f"Source country must be a two-letter ISO code, got {country!r}.")
        return country

    def _source_bucket(self, country: str):
        if country not in self.source_income_by_country:
            self.source_income_by_country[country] = {
                "capital_revenue": 0.0,
                "capital_cost": 0.0,
                "interest": 0.0,
                "dividend": 0.0,
                "foreign_tax": 0.0,
            }
        return self.source_income_by_country[country]

    def _quote(self, currency: str, date: str):
        quote = get_nbp_rate(currency, date)
        if not hasattr(quote, "value") or not hasattr(quote, "effective_date"):
            raise ValueError("NBP service returned an unverifiable rate quote.")
        return quote

    def add_transaction(
        self,
        date: str,
        ticker: str,
        action: str,
        quantity: float,
        price_fx: float,
        currency: str,
        commission_fx: float = 0.0,
        country: str | None = None,
        source_row: int | None = None,
    ):
        tx_year = self._validate_date(date)
        action = str(action).strip().upper()
        ticker = str(ticker).strip().upper()
        quantity = float(quantity)
        price_fx = float(price_fx)
        commission_fx = abs(float(commission_fx))
        currency = str(currency).strip().upper()
        country = self._validate_country(country)

        if action not in {"BUY", "SELL"}:
            raise ValueError(f"Unsupported trade action {action!r} for {ticker}.")
        if not ticker or ticker in {"NAN", "-", "TOTAL"}:
            raise ValueError("Trade is missing a usable ticker or instrument identifier.")
        if not all(isfinite(value) for value in (quantity, price_fx, commission_fx)):
            raise ValueError(f"Non-finite quantity, price, or commission for {ticker} on {date}.")
        if quantity <= 0 or price_fx <= 0 or commission_fx < 0:
            raise ValueError(f"Invalid quantity, price, or commission for {ticker} on {date}.")

        inventory = self.buy_inventory.get(ticker, [])
        if action == "SELL":
            available = sum(float(layer["quantity"]) for layer in inventory)
            if available + EPSILON < quantity:
                raise ValueError(
                    f"Insufficient FIFO inventory for {ticker} on {date}: "
                    f"requested {quantity:g}, available {available:g}. "
                    "Provide the complete acquisition history or correct the transaction."
                )

        quote = self._quote(currency, date)
        rate = _pln_rate(quote)
        commission_pln = _money(commission_fx * rate)
        nbp_date = quote.effective_date or "PLN"

        if action == "BUY":
            unit_cost_pln = price_fx * rate + commission_pln / quantity
            self.buy_inventory.setdefault(ticker, []).append({
                "quantity": quantity,
                "original_quantity": quantity,
                "unit_cost_pln": unit_cost_pln,
                "date": date,
                "price_fx": price_fx,
                "currency": currency,
                "rate": rate,
                "nbp_date": nbp_date,
                "nbp_table": getattr(quote, "table_number", None),
                "source_row": source_row,
                "commission_fx": commission_fx,
                "commission_pln": commission_pln,
            })
            total_buy_cost = _money(quantity * price_fx * rate + commission_pln)
            self.calculation_steps.append({
                "Year": tx_year,
                "Date": date,
                "NBP Date": nbp_date,
                "NBP Table": getattr(quote, "table_number", None),
                "NBP Source": _nbp_source_url(currency, quote.effective_date),
                "Source row": source_row,
                "Source country": country,
                "Unit cost (PLN)": unit_cost_pln,
                "Action_EN": "BUY (FIFO inventory)",
                "Action_PL": "KUPNO (magazyn FIFO)",
                "Ticker": ticker,
                "Curr": currency,
                "Qty": quantity,
                "Price (FX)": f"{price_fx:.8f}",
                "NBP Rate": f"{rate:.6f}",
                "Comm (FX)": f"{commission_fx:.8f}",
                "Comm (PLN)": commission_pln,
                "Revenue (PLN)": 0.0,
                "Cost Basis (PLN)": total_buy_cost,
                "Net (PLN)": 0.0,
                "Details_EN": (
                    f"Inventory cost = {quantity:g} × {price_fx:g} × {rate:g} "
                    f"+ commission {commission_pln:.2f} PLN"
                ),
                "Details_PL": (
                    f"Koszt magazynu = {quantity:g} × {price_fx:g} × {rate:g} "
                    f"+ prowizja {commission_pln:.2f} PLN"
                ),
            })
            return

        # The full quantity was checked above, so matching cannot partially mutate
        # inventory and then fail.
        sale_revenue = _money(quantity * price_fx * rate)
        remaining = quantity
        sale_cost = commission_pln
        matched_layers = []
        matched_layers_pl = []
        fifo_matches = []
        while remaining > EPSILON:
            layer = inventory[0]
            matched_quantity = min(float(layer["quantity"]), remaining)
            unit_cost_pln = float(layer["unit_cost_pln"])
            allocated_cost_pln = matched_quantity * unit_cost_pln
            sale_cost += allocated_cost_pln
            fifo_matches.append({
                "Buy date": layer["date"],
                "Buy source row": layer.get("source_row"),
                "Matched quantity": matched_quantity,
                "Original buy quantity": layer.get("original_quantity"),
                "Buy price (FX)": layer.get("price_fx"),
                "Buy currency": layer.get("currency"),
                "Buy NBP rate": layer.get("rate"),
                "Buy NBP date": layer.get("nbp_date"),
                "Buy NBP table": layer.get("nbp_table"),
                "Buy commission (PLN)": layer.get("commission_pln"),
                "Unit cost (PLN)": unit_cost_pln,
                "Allocated cost (PLN)": allocated_cost_pln,
            })
            matched_layers.append(
                f"{matched_quantity:g} from {layer['date']} at {unit_cost_pln:.4f} PLN/unit"
                f" (source row {layer.get('source_row') or 'not recorded'})"
            )
            matched_layers_pl.append(
                f"{matched_quantity:g} z zakupu {layer['date']} po {unit_cost_pln:.4f} PLN/szt."
                f" (wiersz zrodlowy {layer.get('source_row') or 'niezapisany'})"
            )
            layer["quantity"] -= matched_quantity
            remaining -= matched_quantity
            if layer["quantity"] <= EPSILON:
                inventory.pop(0)

        sale_cost = _money(sale_cost)
        net_pln = _money(sale_revenue - sale_cost)
        if tx_year == self.target_tax_year:
            self.total_capital_revenue_pln += sale_revenue
            self.total_capital_cost_pln += sale_cost
            if country:
                bucket = self._source_bucket(country)
                bucket["capital_revenue"] += sale_revenue
                bucket["capital_cost"] += sale_cost
            else:
                self.add_review_note(
                    f"Source country for {ticker} is unresolved; verify PIT/ZG attribution."
                )

        self.calculation_steps.append({
            "Year": tx_year,
            "Date": date,
            "NBP Date": nbp_date,
            "NBP Table": getattr(quote, "table_number", None),
            "NBP Source": _nbp_source_url(currency, quote.effective_date),
            "Source row": source_row,
            "Source country": country,
            "Income type": "capital_gains",
            "Action_EN": "SELL (target year)" if tx_year == self.target_tax_year else f"SELL (prior year {tx_year})",
            "Action_PL": "SPRZEDAŻ (rok docelowy)" if tx_year == self.target_tax_year else f"SPRZEDAŻ (rok ubiegły {tx_year})",
            "Ticker": ticker,
            "Curr": currency,
            "Qty": quantity,
            "Price (FX)": f"{price_fx:.8f}",
            "NBP Rate": f"{rate:.6f}",
            "Comm (FX)": f"{commission_fx:.8f}",
            "Comm (PLN)": commission_pln,
            "Revenue (PLN)": sale_revenue,
            "Cost Basis (PLN)": sale_cost,
            "Net (PLN)": net_pln,
            "FIFO_Matches": fifo_matches,
            "Details_EN": f"Proceeds = {quantity:g} × {price_fx:g} × {rate:g}; FIFO: " + "; ".join(matched_layers),
            "Details_PL": f"Przychod = {quantity:g} × {price_fx:g} × {rate:g}; FIFO: " + "; ".join(matched_layers_pl),
        })

    def add_interest(
        self, date: str, gross_fx: float, currency: str, country: str | None = None,
        source_row: int | None = None,
    ):
        self._add_passive_income("interest", date, gross_fx, currency, country, source_row=source_row)

    def add_dividend(
        self, date: str, gross_fx: float, currency: str, country: str | None = None,
        ticker: str = "DIV", source_row: int | None = None,
    ):
        self._add_passive_income("dividend", date, gross_fx, currency, country, ticker=ticker, source_row=source_row)

    def _add_passive_income(self, income_type, date, gross_fx, currency, country, ticker=None, source_row=None):
        tx_year = self._validate_date(date)
        gross_fx = float(gross_fx)
        currency = str(currency).strip().upper()
        country = self._validate_country(country)
        if not isfinite(gross_fx) or gross_fx < 0:
            raise ValueError("Passive income amount must be finite and non-negative; review reversals manually.")
        quote = self._quote(currency, date)
        rate = _pln_rate(quote)
        gross_pln = _money(gross_fx * rate)

        if tx_year == self.target_tax_year:
            if income_type == "interest":
                self.total_interest_pln += gross_pln
            else:
                self.total_dividend_gross_pln += gross_pln

            if country:
                self._source_bucket(country)[income_type] += gross_pln
            else:
                self.add_review_note(
                    f"Source country for {income_type} income on {date} is unresolved; verify PIT/ZG attribution."
                )

        label_en = "INTEREST" if income_type == "interest" else "DIVIDEND"
        label_pl = "ODSETKI" if income_type == "interest" else "DYWIDENDA"
        self.calculation_steps.append({
            "Year": tx_year,
            "Date": date,
            "NBP Date": quote.effective_date or "PLN",
            "NBP Table": getattr(quote, "table_number", None),
            "NBP Source": _nbp_source_url(currency, quote.effective_date),
            "Source row": source_row,
            "Source country": country,
            "Income type": income_type,
            "Action_EN": label_en,
            "Action_PL": label_pl,
            "Ticker": ticker or ("INT" if income_type == "interest" else "DIV"),
            "Curr": currency,
            "Qty": 1.0,
            "Price (FX)": f"{gross_fx:.8f}",
            "NBP Rate": f"{rate:.6f}",
            "Comm (FX)": "0.00",
            "Comm (PLN)": 0.0,
            "Revenue (PLN)": gross_pln,
            "Cost Basis (PLN)": 0.0,
            "Net (PLN)": gross_pln,
            "Details_EN": f"{label_en.title()} = {gross_fx:g} {currency} × NBP {rate:g} = {gross_pln:.2f} PLN",
            "Details_PL": f"{label_pl.title()} = {gross_fx:g} {currency} × NBP {rate:g} = {gross_pln:.2f} PLN",
        })

    def add_withholding_tax(
        self,
        date: str,
        tax_paid_fx: float,
        currency: str,
        income_type: str,
        country: str | None = None,
        source_row: int | None = None,
    ):
        tx_year = self._validate_date(date)
        income_type = str(income_type).strip().lower()
        if income_type not in self.foreign_tax_by_type_and_country:
            raise ValueError(f"Withholding tax needs a known income type, got {income_type!r}.")
        tax_paid_fx = abs(float(tax_paid_fx))
        currency = str(currency).strip().upper()
        country = self._validate_country(country)
        if not isfinite(tax_paid_fx):
            raise ValueError("Foreign tax amount must be finite.")
        quote = self._quote(currency, date)
        rate = _pln_rate(quote)
        tax_pln = _money(tax_paid_fx * rate)

        if tx_year == self.target_tax_year:
            if country:
                amounts = self.foreign_tax_by_type_and_country[income_type]
                amounts[country] = amounts.get(country, 0.0) + tax_pln
                self._source_bucket(country)["foreign_tax"] += tax_pln
            else:
                self.unallocated_foreign_tax_by_type[income_type] += tax_pln
                self.add_review_note(
                    "Foreign withholding has no confirmed source country and is not claimed as a Polish tax credit."
                )

        self.calculation_steps.append({
            "Year": tx_year,
            "Date": date,
            "NBP Date": quote.effective_date or "PLN",
            "NBP Table": getattr(quote, "table_number", None),
            "NBP Source": _nbp_source_url(currency, quote.effective_date),
            "Source row": source_row,
            "Source country": country,
            "Income type": income_type,
            "Action_EN": f"FOREIGN TAX ({income_type})",
            "Action_PL": f"PODATEK ZAGRANICZNY ({income_type})",
            "Ticker": "TAX",
            "Curr": currency,
            "Qty": 1.0,
            "Price (FX)": f"{tax_paid_fx:.8f}",
            "NBP Rate": f"{rate:.6f}",
            "Comm (FX)": "0.00",
            "Comm (PLN)": 0.0,
            "Revenue (PLN)": 0.0,
            "Cost Basis (PLN)": 0.0,
            "Net (PLN)": 0.0,
            "Foreign tax (PLN)": tax_pln,
            "Details_EN": f"Foreign tax = {tax_paid_fx:g} {currency} × NBP {rate:g} = {tax_pln:.2f} PLN",
            "Details_PL": f"Podatek zagraniczny = {tax_paid_fx:g} {currency} × NBP {rate:g} = {tax_pln:.2f} PLN",
        })

    def calculate_pit38(self) -> Dict:
        revenue = _money(self.total_capital_revenue_pln)
        costs = _money(self.total_capital_cost_pln)
        raw_net = _money(revenue - costs)
        raw_income = max(0.0, raw_net)
        raw_loss = max(0.0, -raw_net)
        loss_deducted = min(raw_income, self.loss_carryforward)
        income_after_loss = _money(raw_income - loss_deducted)
        tax_base = _whole_zloty(income_after_loss)
        capital_tax = _money(tax_base * float(CAPITAL_GAINS_RATE))
        capital_tax_paid = _money(sum(self.foreign_tax_by_type_and_country["capital_gains"].values()))
        capital_tax_due = _whole_zloty(max(0.0, capital_tax - capital_tax_paid))

        passive_gross = _money(self.total_interest_pln + self.total_dividend_gross_pln)
        passive_tax = _ceil_grosz(passive_gross * float(PASSIVE_INCOME_RATE))
        passive_tax_by_country = {}
        for country, data in self.source_income_by_country.items():
            country_gross = _money(data["interest"] + data["dividend"])
            if country_gross:
                passive_tax_by_country[country] = _ceil_grosz(country_gross * float(PASSIVE_INCOME_RATE))

        passive_foreign_tax_paid = {}
        for income_type in ("interest", "dividend"):
            for country, amount in self.foreign_tax_by_type_and_country[income_type].items():
                passive_foreign_tax_paid[country] = passive_foreign_tax_paid.get(country, 0.0) + amount
        passive_credit = sum(
            min(amount, passive_tax_by_country.get(country, 0.0))
            for country, amount in passive_foreign_tax_paid.items()
        )
        passive_credit = _money(min(passive_credit, passive_tax))
        passive_foreign_tax_paid_total = _money(
            sum(passive_foreign_tax_paid.values())
            + self.unallocated_foreign_tax_by_type["interest"]
            + self.unallocated_foreign_tax_by_type["dividend"]
        )
        passive_foreign_tax_uncredited = _money(
            max(0.0, passive_foreign_tax_paid_total - passive_credit)
        )
        passive_credit_audit_rows = []
        for country in sorted(set(passive_tax_by_country) | set(passive_foreign_tax_paid)):
            country_income = self.source_income_by_country.get(country, {})
            country_passive_income = _money(country_income.get("interest", 0.0) + country_income.get("dividend", 0.0))
            country_polish_tax_cap = passive_tax_by_country.get(country, 0.0)
            country_foreign_tax = _money(passive_foreign_tax_paid.get(country, 0.0))
            passive_credit_audit_rows.append({
                "Country": country,
                "Interest income (PLN)": _money(country_income.get("interest", 0.0)),
                "Dividend income (PLN)": _money(country_income.get("dividend", 0.0)),
                "Passive income (PLN)": country_passive_income,
                "Polish tax cap for country (PLN)": country_polish_tax_cap,
                "Foreign tax paid (PLN)": country_foreign_tax,
                "Credit before overall cap (PLN)": _money(min(country_foreign_tax, country_polish_tax_cap)),
            })
        unallocated_passive_tax = _money(
            self.unallocated_foreign_tax_by_type["interest"]
            + self.unallocated_foreign_tax_by_type["dividend"]
        )
        if unallocated_passive_tax:
            passive_credit_audit_rows.append({
                "Country": "UNALLOCATED",
                "Interest income (PLN)": 0.0,
                "Dividend income (PLN)": 0.0,
                "Passive income (PLN)": 0.0,
                "Polish tax cap for country (PLN)": 0.0,
                "Foreign tax paid (PLN)": unallocated_passive_tax,
                "Credit before overall cap (PLN)": 0.0,
            })
        if passive_foreign_tax_uncredited > 0:
            self.add_review_note(
                f"Passive foreign tax paid: {passive_foreign_tax_paid_total:.2f} PLN; allowed Polish credit "
                f"(Poz. 48): {passive_credit:.2f} PLN; uncredited: "
                f"{passive_foreign_tax_uncredited:.2f} PLN. The uncredited amount does not increase Polish tax due. "
                "Check whether it can be reclaimed abroad."
            )
        passive_tax_due = _whole_zloty(max(0.0, passive_tax - passive_credit))

        pit_zg = []
        for country, data in sorted(self.source_income_by_country.items()):
            country_net = _money(data["capital_revenue"] - data["capital_cost"])
            pit_zg.append({
                "Kraj": country,
                "Przychod_Art30b": _money(data["capital_revenue"]),
                "Koszty_Art30b": _money(data["capital_cost"]),
                "Dochod_Art30b": max(0.0, country_net),
                "Strata_Art30b": max(0.0, -country_net),
                "Odsetki_Art30a": _money(data["interest"]),
                "Dywidendy_Art30a": _money(data["dividend"]),
                "Podatek_Zagraniczny": _money(data["foreign_tax"]),
            })

        # PIT/ZG(8), section C.3, positions 29-30 concern foreign Art. 30b
        # income and tax paid on that income. Keep the capital-gains withholding
        # separate from Art. 30a dividend/interest withholding in this table.
        # These are IBKR-only candidate contributions based on the user's
        # source-country mappings, not a completed official form.
        capital_gains_tax_by_country = self.foreign_tax_by_type_and_country["capital_gains"]
        pit_zg_position_countries = {
            country for country, data in self.source_income_by_country.items()
            if data["capital_revenue"] or data["capital_cost"]
        } | set(capital_gains_tax_by_country)
        pit_zg_position_summary = []
        for country in sorted(pit_zg_position_countries):
            data = self.source_income_by_country.get(country, {})
            country_net = _money(data.get("capital_revenue", 0.0) - data.get("capital_cost", 0.0))
            pit_zg_position_summary.append({
                "Kraj": country,
                "Poz_29_Dochod_Art30b_5a_5b_PLN": _money(max(0.0, country_net)),
                "Poz_30_Podatek_zagraniczny_od_Poz_29_PLN": _money(
                    capital_gains_tax_by_country.get(country, 0.0)
                ),
            })

        section_c = {
            "Inne_przychody_Poz_22": revenue,
            "Koszty_uzyskania_Poz_23": costs,
            "Przychody_Razem_Poz_26": revenue,
            "Koszty_Razem_Poz_27": costs,
            "Dochod_Poz_28": raw_income,
            "Strata_Poz_29": raw_loss,
            "Strata_z_lat_ubieglych_Poz_30": _money(loss_deducted),
            "Podstawa_Poz_31": tax_base,
            "Stawka_Poz_32": float(CAPITAL_GAINS_RATE),
            "Podatek_Poz_33": capital_tax,
            "Podatek_zagraniczny_Poz_34": capital_tax_paid,
            "Podatek_nalezny_Poz_35": capital_tax_due,
        }
        section_g = {
            "Podatek_Art30a_Poz_47": passive_tax,
            "Podatek_zagraniczny_uznany_Poz_48": passive_credit,
            "Roznica_Poz_49": passive_tax_due,
            "Odsetki_brutto_PLN": _money(self.total_interest_pln),
            "Dywidendy_brutto_PLN": _money(self.total_dividend_gross_pln),
            "Podatek_zagraniczny_pobrany_Art30a_PLN": passive_foreign_tax_paid_total,
            "Podatek_zagraniczny_nierozliczony_PLN": passive_foreign_tax_uncredited,
        }
        imported_items_tax_due = capital_tax_due + passive_tax_due

        return {
            "Target_Tax_Year": self.target_tax_year,
            "PIT38_Sekcja_C": section_c,
            "PIT38_Sekcja_G_Art30a": section_g,
            "PIT38_Sekcja_D_Art30b": {
                "Podstawa_Poz_31": tax_base,
                "Stawka_Poz_32": float(CAPITAL_GAINS_RATE),
                "Podatek_Poz_33": capital_tax,
                "Podatek_zagraniczny_Poz_34": capital_tax_paid,
                "Podatek_nalezny_Poz_35": capital_tax_due,
            },
            "PIT_ZG_Attachments": pit_zg,
            "PIT_ZG_Position_Summary": pit_zg_position_summary,
            "Passive_Tax_Credit_Audit": {
                "Formula": "By source country: credit before total cap = min(foreign tax paid, Polish tax attributable to that country's passive income); total Poz. 48 is then capped at Poz. 47.",
                "Formula_PL": "W tym kalkulatorze limit dla kraju = 19% dochodow pasywnych z tego kraju, zaokraglone w gore do grosza. Odliczenie przed limitem lacznym = mniejsza z kwot: podatek zagraniczny zaplacony lub ten limit. Suma odliczen nie moze przekroczyc poz. 47.",
                "By country": passive_credit_audit_rows,
                "Poz. 47 Polish tax (PLN)": passive_tax,
                "Foreign tax paid (PLN)": passive_foreign_tax_paid_total,
                "Poz. 48 allowed credit (PLN)": passive_credit,
                "Uncredited foreign tax (PLN)": passive_foreign_tax_uncredited,
            },
            "PIT_ZG_Unresolved": sorted({
                note for note in self.review_notes
                if "Source country" in note or "source country" in note
            }),
            "Review_Notes": list(self.review_notes),
            "Review_Required": bool(self.review_notes),
            "IBKR_Only_Illustrative_Tax_Estimate_Poz_35_and_49": imported_items_tax_due,
            "Calculation_Steps": self.calculation_steps,
        }
