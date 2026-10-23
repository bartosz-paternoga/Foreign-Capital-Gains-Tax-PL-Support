# tax_calculator.py
from dataclasses import dataclass
from typing import List, Dict
from datetime import datetime, timedelta
from nbp_service import get_nbp_rate

def get_preceding_business_day(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        dt -= timedelta(days=1)
        while dt.weekday() >= 5:  # Saturday or Sunday
            dt -= timedelta(days=1)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return date_str

class PIT38Calculator:
    def __init__(self, target_tax_year: int = 2026, loss_carryforward: float = 0.0):
        self.target_tax_year = target_tax_year
        self.loss_carryforward = loss_carryforward
        self.reset()

    def reset(self):
        self.buy_inventory: Dict[str, List[Dict]] = {}
        self.total_revenue_pln = 0.0
        self.total_cost_pln = 0.0
        
        self.foreign_capital_by_country: Dict[str, Dict[str, float]] = {
            "Irlandia (IE)": {"revenue": 0.0, "cost": 0.0}
        }

        self.total_dividend_gross_pln = 0.0
        self.total_dividend_tax_paid_pln = 0.0
        self.total_interest_pln = 0.0
        self.total_interest_tax_paid_pln = 0.0
        
        self.calculation_steps: List[Dict] = []

    def add_transaction(self, date: str, ticker: str, action: str, quantity: float, price_fx: float, currency: str, commission_fx: float = 0.0, country: str = "Irlandia (IE)"):
        nbp_date = get_preceding_business_day(date)
        rate = get_nbp_rate(currency, date)
        action = action.upper()
        ticker = ticker.upper()
        
        if country not in self.foreign_capital_by_country:
            self.foreign_capital_by_country[country] = {"revenue": 0.0, "cost": 0.0}

        tx_year = int(date.split("-")[0])
        comm_pln = round(commission_fx * rate, 2)

        if action == "BUY":
            if ticker not in self.buy_inventory:
                self.buy_inventory[ticker] = []
            
            comm_pln_per_unit = (comm_pln / quantity) if quantity > 0 else 0
            unit_cost_pln = (price_fx * rate) + comm_pln_per_unit
            
            batch = {
                "quantity": quantity,
                "unit_cost_pln": unit_cost_pln,
                "date": date,
                "price_fx": price_fx,
                "currency": currency,
                "rate": rate,
                "commission_fx": commission_fx
            }
            self.buy_inventory[ticker].append(batch)

            self.calculation_steps.append({
                "Year": tx_year,
                "Date": date,
                "NBP Date": nbp_date,
                "Action_EN": "BUY (Inventory)",
                "Action_PL": "KUPNO (Magazyn FIFO)",
                "Ticker": ticker,
                "Curr": currency,
                "Qty": quantity,
                "Price (FX)": f"{price_fx:.2f}",
                "NBP Rate": f"{rate:.4f}",
                "Comm (FX)": f"{commission_fx:.2f}",
                "Comm (PLN)": comm_pln,
                "Revenue (PLN)": 0.0,
                "Cost Basis (PLN)": round((quantity * price_fx * rate) + comm_pln, 2),
                "Net (PLN)": 0.0,
                "Details_EN": f"UnitCost=({price_fx}*{rate})+({commission_fx}/{quantity}*{rate})={unit_cost_pln:.2f}PLN",
                "Details_PL": f"KosztJedn=({price_fx}*{rate})+({commission_fx}/{quantity}*{rate})={unit_cost_pln:.2f}PLN"
            })

        elif action == "SELL":
            sell_price_pln = price_fx * rate
            rev = round(quantity * sell_price_pln, 2)
            cost_total = comm_pln

            remaining_to_sell = quantity
            if ticker not in self.buy_inventory or not self.buy_inventory[ticker]:
                raise ValueError(f"Selling {ticker} on {date} without a preceding buy record in inventory.")

            matched_layers_en = []
            matched_layers_pl = []

            while remaining_to_sell > 0 and self.buy_inventory[ticker]:
                first_batch = self.buy_inventory[ticker][0]
                matched_qty = min(first_batch["quantity"], remaining_to_sell)
                batch_cost = matched_qty * first_batch["unit_cost_pln"]
                cost_total += batch_cost
                
                matched_layers_en.append(f"Match {matched_qty}u from {first_batch['date']}@{first_batch['unit_cost_pln']:.2f}PLN")
                matched_layers_pl.append(f"Dopasowano {matched_qty}szt z {first_batch['date']}@{first_batch['unit_cost_pln']:.2f}PLN")

                if first_batch["quantity"] <= remaining_to_sell:
                    remaining_to_sell -= first_batch["quantity"]
                    self.buy_inventory[ticker].pop(0)
                else:
                    first_batch["quantity"] -= remaining_to_sell
                    remaining_to_sell = 0

            cost_total = round(cost_total, 2)
            net_pln = round(rev - cost_total, 2)

            if tx_year == self.target_tax_year:
                self.total_revenue_pln += rev
                self.total_cost_pln += cost_total
                self.foreign_capital_by_country[country]["revenue"] += rev
                self.foreign_capital_by_country[country]["cost"] += cost_total

            action_label_en = "SELL (Target)" if tx_year == self.target_tax_year else f"SELL (Prior {tx_year})"
            action_label_pl = "SPRZEDAŻ (Rok Docelowy)" if tx_year == self.target_tax_year else f"SPRZEDAŻ (Ubiegły {tx_year})"

            self.calculation_steps.append({
                "Year": tx_year,
                "Date": date,
                "NBP Date": nbp_date,
                "Action_EN": action_label_en,
                "Action_PL": action_label_pl,
                "Ticker": ticker,
                "Curr": currency,
                "Qty": quantity,
                "Price (FX)": f"{price_fx:.2f}",
                "NBP Rate": f"{rate:.4f}",
                "Comm (FX)": f"{commission_fx:.2f}",
                "Comm (PLN)": comm_pln,
                "Revenue (PLN)": rev,
                "Cost Basis (PLN)": cost_total,
                "Net (PLN)": net_pln,
                "Details_EN": f"Rev={quantity}*{price_fx}*{rate}={rev}. " + "; ".join(matched_layers_en),
                "Details_PL": f"Przych={quantity}*{price_fx}*{rate}={rev}. " + "; ".join(matched_layers_pl)
            })

    def add_withholding_tax(self, date: str, tax_paid_fx: float, currency: str):
        tx_year = int(date.split("-")[0])
        if tx_year == self.target_tax_year:
            nbp_date = get_preceding_business_day(date)
            rate = get_nbp_rate(currency, date)
            tax_pln = round(tax_paid_fx * rate, 2)
            
            # Accumulate into foreign tax paid (used as credit against Polish tax liability)
            self.total_interest_tax_paid_pln += tax_pln

            self.calculation_steps.append({
                "Year": tx_year,
                "Date": date,
                "NBP Date": nbp_date,
                "Action_EN": "WITHHOLDING TAX",
                "Action_PL": "ZAPŁACONY PODATEK U ŹRÓDŁA",
                "Ticker": "TAX",
                "Curr": currency,
                "Qty": 1.0,
                "Price (FX)": f"{tax_paid_fx:.2f}",
                "NBP Rate": f"{rate:.4f}",
                "Comm (FX)": "0.00",
                "Comm (PLN)": 0.0,
                "Revenue (PLN)": 0.0,
                "Cost Basis (PLN)": 0.0,
                "Net (PLN)": 0.0,
                "Details_EN": f"Foreign Withholding Tax = {tax_paid_fx} {currency} * NBP {rate} = {tax_pln} PLN (Credited against PL tax)",
                "Details_PL": f"Zagraniczny podatek u źródła = {tax_paid_fx} {currency} * NBP {rate} = {tax_pln} PLN (Zaliczone na poczet podatku w PL)"
            })

    def add_interest(self, date: str, gross_fx: float, currency: str, country: str = "Irlandia (IE)"):
        tx_year = int(date.split("-")[0])
        if tx_year == self.target_tax_year:
            nbp_date = get_preceding_business_day(date)
            rate = get_nbp_rate(currency, date)
            interest_pln = round(gross_fx * rate, 2)
            
            self.total_interest_pln += interest_pln
            self.total_revenue_pln += interest_pln

            if country not in self.foreign_capital_by_country:
                self.foreign_capital_by_country[country] = {"revenue": 0.0, "cost": 0.0}
            self.foreign_capital_by_country[country]["revenue"] += interest_pln

            self.calculation_steps.append({
                "Year": tx_year,
                "Date": date,
                "NBP Date": nbp_date,
                "Action_EN": "INTEREST",
                "Action_PL": "ODSETKI",
                "Ticker": "INT",
                "Curr": currency,
                "Qty": 1.0,
                "Price (FX)": f"{gross_fx:.2f}",
                "NBP Rate": f"{rate:.4f}",
                "Comm (FX)": "0.00",
                "Comm (PLN)": 0.0,
                "Revenue (PLN)": interest_pln,
                "Cost Basis (PLN)": 0.0,
                "Net (PLN)": interest_pln,
                "Details_EN": f"Interest = {gross_fx} {currency} * NBP {rate} = {interest_pln} PLN",
                "Details_PL": f"Odsetki = {gross_fx} {currency} * NBP {rate} = {interest_pln} PLN"
            })

    def calculate_pit38(self) -> Dict:
        revenue = round(self.total_revenue_pln, 2)
        costs = round(self.total_cost_pln, 2)
        raw_income = max(0.0, round(revenue - costs, 2))
        raw_loss = max(0.0, round(costs - revenue, 2))
        
        loss_deducted = min(raw_income, self.loss_carryforward)
        final_income_after_loss = max(0.0, round(raw_income - loss_deducted, 2))

        total_foreign_tax = self.total_dividend_tax_paid_pln + self.total_interest_tax_paid_pln
        total_passive_gross = self.total_dividend_gross_pln + self.total_interest_pln
        
        pl_tax_due_passive = total_passive_gross * 0.19
        remaining_passive_tax = max(0.0, pl_tax_due_passive - total_foreign_tax)

        pit_zg_list = []
        for country, data in self.foreign_capital_by_country.items():
            c_rev = round(data["revenue"], 2)
            c_cost = round(data["cost"], 2)
            c_inc = max(0.0, round(c_rev - c_cost, 2))
            c_loss = max(0.0, round(c_cost - c_rev, 2))
            if c_rev > 0 or c_cost > 0:
                pit_zg_list.append({
                    "Kraj": country,
                    "Przychód": c_rev,
                    "Koszty": c_cost,
                    "Dochód": c_inc,
                    "Strata": c_loss
                })

        return {
            "Target_Tax_Year": self.target_tax_year,
            "PIT38_Sekcja_C": {
                "Inne_przychody_Poz_22": revenue,
                "Koszty_uzyskania_Poz_23": costs,
                "Dochod_przed_odliczeniem_strat_Poz_24": raw_income,
                "Strata_za_dany_rok_Poz_25": raw_loss,
                "Strata_z_lat_ubieglych_odliczona_Poz_26": round(loss_deducted, 2),
                "Dochod_po_odliczeniu_strat": final_income_after_loss
            },
            "PIT38_Sekcja_G_Dividends": {
                "Zwolniony_podatek_zaplacony_za_granica": round(total_foreign_tax, 2),
                "Podatek_do_zaplaty_w_PL": round(remaining_passive_tax, 2)
            },
            "PIT_ZG_Attachments": pit_zg_list,
            "Calculation_Steps": self.calculation_steps
        }