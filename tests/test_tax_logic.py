"""Offline regression tests for the IBKR parser and PIT calculations."""
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from ibkr_parser import inspect_ibkr_csv, process_and_calculate_file
from account_reconciliation import UnitReconciler, parse_symbol_quantities
from nbp_service import NBPRate, NBPRateUnavailable, get_nbp_rate
from tax_calculator import PIT38Calculator


CSV_HEADER = (
    "Transaction History,Date,Description,Transaction Type,Symbol,Quantity,Price,"
    "Price Currency,Gross Amount ,Commission,Net Amount\n"
)


def make_csv(rows, header=CSV_HEADER):
    content = header + "\n".join(rows) + "\n"
    handle = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", encoding="utf-8", delete=False)
    handle.write(content)
    handle.close()
    return Path(handle.name)


def make_raw_ibkr_report(rows):
    """Build a raw IBKR report with its four-column preamble and row tags."""
    content = io.StringIO()
    writer = csv.writer(content)
    writer.writerow(["Statement", "Header", "Field Name", "Field Value"])
    writer.writerow(["Transaction History", "Data", "Title", "Transaction History"])
    writer.writerow(["Summary", "Data", "Base Currency", "EUR"])
    writer.writerow([
        "Transaction History", "Header", "Date", "Account", "Description",
        "Transaction Type", "Symbol", "Quantity", "Price", "Price Currency",
        "Gross Amount ", "Commission", "Net Amount",
    ])
    for row in rows:
        writer.writerow(["Transaction History", "Data", *row])

    handle = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", encoding="utf-8", newline="", delete=False)
    handle.write(content.getvalue())
    handle.close()
    return Path(handle.name)


class ParserCalculationTests(unittest.TestCase):
    def setUp(self):
        self.rate_patch = patch(
            "tax_calculator.get_nbp_rate",
            side_effect=lambda _currency, _date: NBPRate(1.0, "2025-12-31"),
        )
        self.rate_patch.start()
        self.addCleanup(self.rate_patch.stop)

    def calculate_rows(self, rows, **kwargs):
        path = make_csv(rows)
        self.addCleanup(path.unlink, missing_ok=True)
        defaults = {
            "tax_year": 2026,
            "cash_currency": "EUR",
            "cash_country": "IE",
            "source_country_by_symbol": {"EGLN": "IE"},
            "assume_zero_opening_units": True,
        }
        defaults.update(kwargs)
        return json.loads(process_and_calculate_file(str(path), **defaults))

    def test_sample_shape_uses_transaction_type_and_skips_blank_gross_amount(self):
        # Sell is intentionally before Buy in the export and has positive quantity.
        # The parser must use date ordering and the explicit Transaction Type.
        result = self.calculate_rows([
            "Transaction History,01/03/2026,ISHARES ETC,Sell,EGLN,2,12,EUR,24,-0.5,23.5",
            "Transaction History,01/02/2026,ISHARES ETC,Buy,EGLN,10,10,EUR,-100,-1,-101",
            "Transaction History,01/05/2026,Withholding on Credit Interest,Foreign Tax Withholding,-,-,-,-,-,-0.19,-0.19",
            "Transaction History,01/04/2026,Credit Interest for Jan-2026,Credit Interest,-,-,-,-,0.97,-,0.97",
        ])

        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["processed_records"], 4)
        calc = result["pit38_calculation"]
        section_c = calc["PIT38_Sekcja_C"]
        section_g = calc["PIT38_Sekcja_G_Art30a"]
        self.assertTrue(calc["Review_Required"])
        self.assertEqual(calc["Account_Reconciliation"]["Status"], "IBKR CLOSING UNITS NEEDED")
        self.assertEqual(section_c["Inne_przychody_Poz_22"], 24.0)
        self.assertEqual(section_c["Koszty_uzyskania_Poz_23"], 20.7)
        self.assertEqual(section_c["Dochod_Poz_28"], 3.3)
        self.assertEqual(section_c["Podstawa_Poz_31"], 3)
        self.assertEqual(section_c["Podatek_nalezny_Poz_35"], 1)
        self.assertEqual(section_g["Odsetki_brutto_PLN"], 0.97)
        self.assertEqual(section_g["Podatek_Art30a_Poz_47"], 0.19)
        self.assertEqual(section_g["Podatek_zagraniczny_uznany_Poz_48"], 0.19)
        self.assertEqual(section_g["Podatek_zagraniczny_pobrany_Art30a_PLN"], 0.19)
        self.assertEqual(section_g["Podatek_zagraniczny_nierozliczony_PLN"], 0.0)
        self.assertEqual(section_g["Roznica_Poz_49"], 0)
        self.assertEqual(calc["IBKR_Only_Illustrative_Tax_Estimate_Poz_35_and_49"], 1)

    def test_raw_ibkr_report_imports_embedded_table_and_audits_excluded_fx_rows(self):
        path = make_raw_ibkr_report([
            ["2026-01-02", "ACCOUNT", "ISHARES ETC", "Buy", "EGLN", "10", "10", "EUR", "-100", "-1", "-101"],
            ["2026-03-10", "ACCOUNT", "ISHARES ETC", "Sell", "EGLN", "-2", "12", "EUR", "24", "-0.5", "23.5"],
            ["2026-03-11", "ACCOUNT", "Net Amount in Base from Forex Trade: 0.2 EUR.PLN", "Forex Trade Component", "EUR.PLN", "1", "4", "PLN", "4", "-", "4"],
            ["2026-03-11", "ACCOUNT", "FX Translations P&L", "Adjustment", "-", "-", "-", "-", "-", "-", "-"],
            ["2026-03-12", "ACCOUNT", "Electronic Fund Transfer", "Deposit", "-", "-", "-", "-", "-", "-", "-"],
            ["2026-03-13", "ACCOUNT", "EUR Credit Interest for Mar-2026", "Credit Interest", "-", "-", "-", "-", "0.97", "-", "0.97"],
            ["2026-03-14", "ACCOUNT", "Withholding @ 20% on Credit Interest for Mar-2026", "Foreign Tax Withholding", "-", "-", "-", "-", "-", "-0.19", "-0.19"],
            ["2026-03-15", "ACCOUNT", "Electronic Fund Transfer", "Withdrawal", "-", "-", "-", "-", "-", "-", "-"],
        ])
        self.addCleanup(path.unlink, missing_ok=True)

        with patch("tax_calculator.get_nbp_rate", return_value=NBPRate(4.0, "2026-03-13")):
            result = json.loads(process_and_calculate_file(
                str(path),
                tax_year=2026,
                cash_currency="EUR",
                cash_country="IE",
                source_country_by_symbol={"EGLN": "IE"},
                opening_units_by_symbol={"EGLN": 0},
                ibkr_closing_units_by_symbol={"EGLN": 8},
            ))

        self.assertEqual(result["source_format"], "IBKR raw report CSV")
        self.assertEqual(result["processed_records"], 4)
        self.assertEqual(result["excluded_activity_summary"], {
            "total": 4,
            "by_category": {
                "FX conversion component": 1,
                "FX translation adjustment": 1,
                "Cash deposit": 1,
                "Cash withdrawal": 1,
            },
        })
        calc = result["pit38_calculation"]
        self.assertEqual(calc["Account_Reconciliation"]["Status"], "MATCH")
        self.assertEqual({row["Ticker"] for row in calc["Account_Reconciliation"]["Rows"]}, {"EGLN"})
        self.assertNotIn("EUR.PLN", {step["Ticker"] for step in calc["Calculation_Steps"]})
        fx_row = next(row for row in calc["Excluded_Activity"] if row["Category"] == "FX conversion component")
        self.assertEqual(fx_row["Source row"], 7)
        self.assertTrue(any("does not determine whether any actual currency-exchange gain" in note for note in calc["Review_Notes"]))

    def test_import_check_recognizes_raw_report_rows_and_previews_excluded_fx(self):
        path = make_raw_ibkr_report([
            ["2026-01-02", "ACCOUNT", "ISHARES ETC", "Buy", "EGLN", "10", "10", "EUR", "-100", "-1", "-101"],
            ["2026-03-10", "ACCOUNT", "ISHARES ETC", "Sell", "EGLN", "-2", "12", "EUR", "24", "-0.5", "23.5"],
            ["2026-03-11", "ACCOUNT", "Net Amount in Base from Forex Trade: 0.2 EUR.PLN", "Forex Trade Component", "EUR.PLN", "1", "4", "PLN", "4", "-", "4"],
            ["2026-03-12", "ACCOUNT", "Electronic Fund Transfer", "Deposit", "-", "-", "-", "-", "-", "-", "-"],
        ])
        self.addCleanup(path.unlink, missing_ok=True)

        check = inspect_ibkr_csv(path.read_bytes())

        self.assertEqual(check["status"], "ready")
        self.assertEqual(check["source_format"], "IBKR raw report CSV")
        self.assertEqual(check["row_count"], 4)
        self.assertEqual(
            check["excluded_activity_summary"],
            {"FX conversion component": 1, "Cash deposit": 1},
        )
        self.assertEqual(
            {item["Transaction type"]: item["Rows"] for item in check["action_counts"]},
            {"Buy": 1, "Sell": 1, "Forex Trade Component": 1, "Deposit": 1},
        )
        self.assertEqual(check["preview"].iloc[0]["Source row"], 5)

    def test_import_check_allows_manual_mapping_of_unfamiliar_headers(self):
        header = (
            "Trade Date,Txn Side,Ticker Code,Details,Units,Execution Price,CCY,"
            "Charges,Consideration,Cash Total\n"
        )
        path = make_csv([
            "2026-01-02,Buy,EGLN,ISHARES ETC,10,10,EUR,-1,-100,-101",
            "2026-03-10,Sell,EGLN,ISHARES ETC,2,12,EUR,-0.5,24,23.5",
        ], header=header)
        self.addCleanup(path.unlink, missing_ok=True)

        automatic_check = inspect_ibkr_csv(path.read_bytes())
        self.assertTrue(automatic_check["blocking_issues"])
        mapping = {
            "date": "Trade Date",
            "transaction_type": "Txn Side",
            "symbol": "Ticker Code",
            "description": "Details",
            "quantity": "Units",
            "price": "Execution Price",
            "currency": "CCY",
            "commission": "Charges",
            "gross_amount": "Consideration",
            "net_amount": "Cash Total",
            "flag": None,
        }
        reviewed_check = inspect_ibkr_csv(path.read_bytes(), mapping)
        self.assertEqual(reviewed_check["blocking_issues"], [])
        self.assertEqual(reviewed_check["status"], "ready")

        result = json.loads(process_and_calculate_file(
            str(path),
            tax_year=2026,
            cash_currency="EUR",
            cash_country="IE",
            source_country_by_symbol={"EGLN": "IE"},
            opening_units_by_symbol={"EGLN": 0},
            ibkr_closing_units_by_symbol={"EGLN": 8},
            column_mapping=mapping,
        ))
        self.assertEqual(result["processed_records"], 2)
        self.assertEqual(result["pit38_calculation"]["Account_Reconciliation"]["Status"], "MATCH")

    def test_import_check_blocks_rows_with_unreadable_dates(self):
        path = make_csv([
            "Transaction History,not-a-date,ISHARES ETC,Buy,EGLN,10,10,EUR,-100,-1,-101",
            "Transaction History,2026-01-03,ISHARES ETC,Buy,EGLN,10,10,EUR,-100,-1,-101",
        ])
        self.addCleanup(path.unlink, missing_ok=True)

        check = inspect_ibkr_csv(path.read_bytes())

        self.assertTrue(any(issue["Code"] == "bad_dates" for issue in check["issues"]))
        self.assertTrue(check["blocking_issues"])
        self.assertIn("2", check["issues"][-1]["Issue"])

    def test_import_check_does_not_use_cash_currency_for_missing_trade_currency(self):
        header = (
            "Transaction History,Date,Description,Transaction Type,Symbol,Quantity,"
            "Price,Gross Amount,Commission,Net Amount\n"
        )
        path = make_csv([
            "Transaction History,2026-01-02,ISHARES ETC,Buy,EGLN,10,10,-100,-1,-101",
        ], header=header)
        self.addCleanup(path.unlink, missing_ok=True)

        unresolved = inspect_ibkr_csv(path.read_bytes())
        resolved = inspect_ibkr_csv(path.read_bytes(), cash_currency="EUR")

        self.assertTrue(any(issue["Code"] == "row_currency_invalid" for issue in unresolved["issues"]))
        self.assertTrue(unresolved["blocking_issues"])
        self.assertTrue(any(issue["Code"] == "row_currency_invalid" for issue in resolved["issues"]))
        self.assertTrue(resolved["blocking_issues"])

    def test_import_check_allows_cash_currency_fallback_for_interest_rows(self):
        header = (
            "Transaction History,Date,Description,Transaction Type,Symbol,Quantity,"
            "Price,Price Currency,Gross Amount,Commission,Net Amount\n"
        )
        path = make_csv([
            "Transaction History,2026-01-02,Credit Interest,Credit Interest,-,-,-,-,0.97,-,0.97",
        ], header=header)
        self.addCleanup(path.unlink, missing_ok=True)

        unresolved = inspect_ibkr_csv(path.read_bytes())
        resolved = inspect_ibkr_csv(path.read_bytes(), cash_currency="EUR")

        self.assertTrue(any(issue["Code"] == "row_currency_invalid" for issue in unresolved["issues"]))
        self.assertEqual(resolved["blocking_issues"], [])

    def test_import_check_requires_choice_when_multiple_columns_match_one_role(self):
        header = (
            "Transaction History,Date,Description,Transaction Type,Symbol,Quantity,"
            "Price,Price Currency,Gross Amount,Amount,Commission,Net Amount\n"
        )
        path = make_csv([
            "Transaction History,2026-01-02,ISHARES ETC,Buy,EGLN,10,10,EUR,-100,-100,-1,-101",
        ], header=header)
        self.addCleanup(path.unlink, missing_ok=True)

        check = inspect_ibkr_csv(path.read_bytes())

        self.assertTrue(any(issue["Code"] == "ambiguous_column" and issue["Role"] == "gross_amount" for issue in check["issues"]))
        self.assertTrue(check["blocking_issues"])

    def test_unrecognized_forex_trade_component_is_not_silently_excluded(self):
        result = self.calculate_rows([
            "Transaction History,01/02/2026,Unknown FX product,Forex Trade Component,EURUSD,1,1,EUR,1,-,1",
            "Transaction History,01/03/2026,ISHARES ETC,Buy,EGLN,10,10,EUR,-100,-1,-101",
        ])
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["pit38_calculation"]["Excluded_Activity"], [])
        self.assertTrue(any("unsupported or unclear IBKR event type" in note for note in result["warnings"]))

    def test_ibkr_negative_sell_quantity_and_cashflow_commission_are_handled(self):
        result = self.calculate_rows([
            "Transaction History,01/02/2026,ISHARES ETC,Buy,EGLN,2,10,EUR,-20,-1,-21",
            "Transaction History,01/03/2026,ISHARES ETC,Sell,EGLN,-1,12,EUR,12,-0.5,11.5",
        ])

        self.assertEqual(result["status"], "needs_review")
        section_c = result["pit38_calculation"]["PIT38_Sekcja_C"]
        self.assertEqual(section_c["Inne_przychody_Poz_22"], 12.0)
        self.assertEqual(section_c["Koszty_uzyskania_Poz_23"], 11.0)
        self.assertEqual(result["pit38_calculation"]["Calculation_Steps"][-1]["Qty"], 1.0)

    def test_reported_ibkr_gross_and_net_amounts_are_checked_before_calculation(self):
        result = self.calculate_rows([
            "Transaction History,01/02/2026,ISHARES ETC,Buy,EGLN,2,10.25,EUR,-20.50,-0.12,-20.62",
            "Transaction History,01/03/2026,ISHARES ETC,Sell,EGLN,-1,11.50,EUR,11.50,-0.12,11.38",
        ])

        self.assertEqual(result["status"], "needs_review")
        section_c = result["pit38_calculation"]["PIT38_Sekcja_C"]
        self.assertEqual(section_c["Inne_przychody_Poz_22"], 11.5)
        self.assertEqual(section_c["Koszty_uzyskania_Poz_23"], 10.43)

    def test_etc_trade_uses_listed_security_calculation_and_flags_country_attribution(self):
        result = self.calculate_rows([
            "Transaction History,01/02/2026,ISHARES PHYSICAL GOLD ETC,Buy,EGLN,1,10,EUR,-10,-0.1,-10.1",
        ])

        self.assertEqual(result["status"], "needs_review")
        warnings = " ".join(result["warnings"])
        self.assertIn("listed-security FIFO (Art. 30b) calculation", warnings)
        self.assertIn("Confirm PIT/ZG source-country attribution separately", warnings)
        self.assertNotIn("confirm the Polish PIT-38 category", warnings)

    def test_mismatched_ibkr_gross_amount_stops_calculation(self):
        result = self.calculate_rows([
            "Transaction History,01/02/2026,ISHARES ETC,Buy,EGLN,2,10.25,EUR,-20.00,-0.12,-20.12",
        ])

        self.assertEqual(result["status"], "error")
        self.assertNotIn("pit38_calculation", result)
        self.assertIn("Gross Amount (-20) does not match quantity × price", " ".join(result["warnings"]))

    def test_multiple_tickers_currencies_transfers_and_corporate_actions_reconcile(self):
        for tax_year in (2025, 2026):
            with self.subTest(tax_year=tax_year):
                rows = [
                    f"Transaction History,01/02/{tax_year},ISHARES ETC,Buy,EGLN,10,10,EUR,-100,-0.1,-100.1",
                    f"Transaction History,01/05/{tax_year},ISHARES ETC,Sell,EGLN,-2,12,EUR,24,-0.1,23.9",
                    f"Transaction History,01/06/{tax_year},Position transfer in,Transfer,EGLN,3,0,EUR,0,0,0",
                    f"Transaction History,01/07/{tax_year},Forward split,Corporate Action,EGLN,1,0,EUR,0,0,0",
                    f"Transaction History,01/02/{tax_year},ACME,Buy,ACME,5,50,USD,-250,-0.5,-250.5",
                    f"Transaction History,01/05/{tax_year},ACME,Sell,ACME,-1,60,USD,60,-0.5,59.5",
                ]
                rates = {
                    ("EUR", f"{tax_year}-01-02"): 4.0,
                    ("EUR", f"{tax_year}-01-05"): 4.1,
                    ("USD", f"{tax_year}-01-02"): 3.5,
                    ("USD", f"{tax_year}-01-05"): 3.6,
                }
                with patch(
                    "tax_calculator.get_nbp_rate",
                    side_effect=lambda currency, date: NBPRate(rates[(currency, date)], f"{tax_year - 1}-12-31"),
                ):
                    result = self.calculate_rows(
                        rows,
                        tax_year=tax_year,
                        opening_units_by_symbol={"EGLN": 4, "ACME": 2},
                        ibkr_closing_units_by_symbol={"EGLN": 16, "ACME": 6},
                        source_country_by_symbol={"EGLN": "IE", "ACME": "US"},
                    )

                calculation = result["pit38_calculation"]
                section_c = calculation["PIT38_Sekcja_C"]
                self.assertEqual(section_c["Inne_przychody_Poz_22"], 314.4)
                self.assertEqual(section_c["Koszty_uzyskania_Poz_23"], 257.64)
                reconciliation = calculation["Account_Reconciliation"]
                self.assertEqual(reconciliation["Status"], "MATCH")
                by_ticker = {row["Ticker"]: row for row in reconciliation["Rows"]}
                self.assertEqual(by_ticker["EGLN"]["Calculated closing units"], 16.0)
                self.assertEqual(by_ticker["EGLN"]["Transfers in"], 3.0)
                self.assertEqual(by_ticker["EGLN"]["Corporate action units (net)"], 1.0)
                self.assertEqual(by_ticker["ACME"]["Calculated closing units"], 6.0)
                self.assertEqual(len(reconciliation["Unit events"]), 6)
                self.assertEqual(result["status"], "needs_review")

    def test_ibkr_reporting_activity_codes_and_camel_case_headers(self):
        header = (
            "TradeDate,Description,TransactionType,Symbol,Quantity,UnitPrice,Currency,"
            "GrossAmount,Commission,Flag\n"
        )
        path = make_csv([
            "20260102,ISHARES ETC,BUY,EGLN,10,10,EUR,-100,-1,",
            "20260103,ISHARES ETC,SELL,EGLN,-2,12,EUR,24,-0.5,",
            "20260104,Receipt of securities,REC,EGLN,3,0,EUR,0,0,",
            "20260105,Forward split,CORP,EGLN,1,0,EUR,0,0,FS",
        ], header=header)
        self.addCleanup(path.unlink, missing_ok=True)
        result = json.loads(process_and_calculate_file(
            str(path),
            tax_year=2026,
            source_country_by_symbol={"EGLN": "IE"},
            opening_units_by_symbol={"EGLN": 4},
            ibkr_closing_units_by_symbol={"EGLN": 16},
        ))

        reconciliation = result["pit38_calculation"]["Account_Reconciliation"]
        self.assertEqual(reconciliation["Status"], "MATCH")
        self.assertEqual(reconciliation["Rows"][0]["Calculated closing units"], 16.0)
        self.assertEqual(reconciliation["Rows"][0]["Transfers in"], 3.0)
        self.assertEqual(reconciliation["Rows"][0]["Corporate action units (net)"], 1.0)

    def test_opening_and_closing_snapshot_mismatch_is_reported(self):
        result = self.calculate_rows([
            "Transaction History,01/02/2026,ISHARES ETC,Buy,EGLN,5,10,EUR,-50,-0.1,-50.1",
            "Transaction History,01/03/2026,ISHARES ETC,Sell,EGLN,-2,12,EUR,24,-0.1,23.9",
        ], opening_units_by_symbol={"EGLN": 10}, ibkr_closing_units_by_symbol={"EGLN": 12})

        reconciliation = result["pit38_calculation"]["Account_Reconciliation"]
        row = reconciliation["Rows"][0]
        self.assertEqual(reconciliation["Status"], "MISMATCH")
        self.assertEqual(row["Calculated closing units"], 13.0)
        self.assertEqual(row["Difference (calculated - IBKR)"], 1.0)
        self.assertIn("IBKR unit mismatch for EGLN", " ".join(result["warnings"]))

    def test_tax_year_switch_changes_only_target_year_tax_figures(self):
        rows = [
            "Transaction History,01/02/2025,ISHARES ETC,Buy,EGLN,10,10,EUR,-100,0,-100",
            "Transaction History,01/03/2025,ISHARES ETC,Sell,EGLN,-2,12,EUR,24,0,24",
            "Transaction History,01/05/2026,ISHARES ETC,Sell,EGLN,-3,15,EUR,45,0,45",
            "Transaction History,01/06/2026,Forward split,Corporate Action,EGLN,5,0,EUR,0,0,0",
        ]
        common = {
            "opening_units_by_symbol": {"EGLN": 0},
            "ibkr_closing_units_by_symbol": {"EGLN": 10},
        }
        result_2025 = self.calculate_rows(rows, tax_year=2025, **common)
        result_2026 = self.calculate_rows(rows, tax_year=2026, **common)

        self.assertEqual(result_2025["pit38_calculation"]["PIT38_Sekcja_C"]["Inne_przychody_Poz_22"], 24.0)
        self.assertEqual(result_2025["pit38_calculation"]["PIT38_Sekcja_C"]["Koszty_uzyskania_Poz_23"], 20.0)
        self.assertEqual(result_2026["pit38_calculation"]["PIT38_Sekcja_C"]["Inne_przychody_Poz_22"], 45.0)
        self.assertEqual(result_2026["pit38_calculation"]["PIT38_Sekcja_C"]["Koszty_uzyskania_Poz_23"], 30.0)
        for result in (result_2025, result_2026):
            reconciliation = result["pit38_calculation"]["Account_Reconciliation"]
            self.assertEqual(reconciliation["Status"], "MATCH")
            self.assertEqual(reconciliation["Rows"][0]["Corporate action units (net)"], 5.0)

    def test_missing_nbprate_stops_tax_result_but_keeps_unit_reconciliation(self):
        for tax_year in (2025, 2026):
            with self.subTest(tax_year=tax_year):
                path = make_csv([
                    f"Transaction History,01/02/{tax_year},ACME,Buy,ACME,5,50,USD,-250,-0.5,-250.5",
                ])
                self.addCleanup(path.unlink, missing_ok=True)
                with patch("tax_calculator.get_nbp_rate", side_effect=NBPRateUnavailable("USD rate missing")):
                    result = json.loads(process_and_calculate_file(
                        str(path),
                        tax_year=tax_year,
                        opening_units_by_symbol={"ACME": 2},
                        ibkr_closing_units_by_symbol={"ACME": 7},
                    ))

                self.assertEqual(result["status"], "error")
                self.assertNotIn("pit38_calculation", result)
                self.assertEqual(result["account_reconciliation"]["Rows"][0]["Calculated closing units"], 7.0)
                self.assertIn("USD rate missing", " ".join(result["warnings"]))

    def test_failed_buy_rate_does_not_create_cascading_insufficient_fifo_errors(self):
        path = make_csv([
            "Transaction History,01/02/2026,ISHARES ETC,Buy,EGLN,2,10,EUR,-20,-0.1,-20.1",
            "Transaction History,01/03/2026,ISHARES ETC,Sell,EGLN,-1,12,EUR,12,-0.1,11.9",
        ])
        self.addCleanup(path.unlink, missing_ok=True)
        with patch("tax_calculator.get_nbp_rate", side_effect=NBPRateUnavailable("EUR rate missing")):
            result = json.loads(process_and_calculate_file(
                str(path),
                tax_year=2026,
                source_country_by_symbol={"EGLN": "IE"},
            ))

        warnings = " ".join(result["warnings"])
        self.assertEqual(result["status"], "error")
        self.assertIn("EUR rate missing", warnings)
        self.assertIn("FIFO for EGLN cannot continue", warnings)
        self.assertNotIn("Insufficient FIFO inventory", warnings)

    def test_source_row_and_nbp_table_are_kept_in_transaction_ledger(self):
        result = self.calculate_rows([
            "Transaction History,01/02/2026,ISHARES ETC,Buy,EGLN,2,10,EUR,-20,-1,-21",
            "Transaction History,01/03/2026,ISHARES ETC,Sell,EGLN,-1,12,EUR,12,-0.5,11.5",
        ])
        steps = result["pit38_calculation"]["Calculation_Steps"]
        self.assertEqual(steps[0]["Source row"], 2)
        self.assertEqual(steps[1]["Source row"], 3)
        self.assertIsNone(steps[0]["NBP Table"])
        self.assertIn("2025-12-31", steps[0]["NBP Source"])
        self.assertIn("source row 2", steps[1]["Details_EN"])

    def test_ibkr_cancel_or_correction_row_stops_tax_calculation(self):
        result = self.calculate_rows([
            "Transaction History,01/02/2026,ISHARES ETC,Buy,EGLN,10,10,EUR,-100,-1,-101",
            "Transaction History,01/03/2026,Cancel buy,CA,EGLN,10,10,EUR,100,1,101",
        ])

        self.assertEqual(result["status"], "error")
        self.assertNotIn("pit38_calculation", result)
        self.assertIn("cancel/correction", " ".join(result["warnings"]).lower())

    def test_oversell_returns_error_and_no_partial_tax_result(self):
        result = self.calculate_rows([
            "Transaction History,01/02/2026,ISHARES ETC,Buy,EGLN,1,10,EUR,-10,-0.1,-10.1",
            "Transaction History,01/03/2026,ISHARES ETC,Sell,EGLN,2,12,EUR,24,-0.1,23.9",
        ])
        self.assertEqual(result["status"], "error")
        self.assertIn("Insufficient FIFO inventory", " ".join(result["warnings"]))
        self.assertNotIn("pit38_calculation", result)

    def test_cash_row_without_currency_is_rejected(self):
        result = self.calculate_rows([
            "Transaction History,01/04/2026,Credit Interest for Jan-2026,Credit Interest,-,-,-,-,0.97,-,0.97",
        ], cash_currency=None)
        self.assertEqual(result["status"], "error")
        self.assertIn("cash-row currency is missing", " ".join(result["warnings"]).lower())

    def test_invalid_source_country_mapping_is_structured_error(self):
        result = self.calculate_rows([
            "Transaction History,01/04/2026,EUR Credit Interest for Jan-2026,Credit Interest,-,-,-,-,0.97,-,0.97",
        ], source_country_by_symbol={"EGLN": "Ireland"})
        self.assertEqual(result["status"], "error")
        self.assertIn("two-letter ISO code", result["error"])

    def test_security_dividend_does_not_inherit_cash_interest_country(self):
        result = self.calculate_rows([
            "Transaction History,01/04/2026,EGLN Dividend,Dividend,EGLN,-,1,EUR,1,-,1",
        ], source_country_by_symbol={})
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("source country for dividend", " ".join(result["warnings"]).lower())
        self.assertEqual(result["pit38_calculation"]["PIT_ZG_Attachments"], [])

    def test_dividend_and_its_withholding_are_separate_art30a_items(self):
        for tax_year in (2025, 2026):
            with self.subTest(tax_year=tax_year):
                result = self.calculate_rows([
                    f"Transaction History,01/04/{tax_year},EGLN dividend,Dividend,EGLN,-,1,EUR,100,-,100",
                    f"Transaction History,01/05/{tax_year},Withholding on EGLN Dividend,Foreign Tax Withholding,EGLN,-,-,-,-10,-,-10",
                ], tax_year=tax_year)

                self.assertEqual(result["status"], "success")
                self.assertEqual(result["processed_records"], 2)
                calculation = result["pit38_calculation"]
                section_g = calculation["PIT38_Sekcja_G_Art30a"]
                self.assertEqual(section_g["Dywidendy_brutto_PLN"], 100.0)
                self.assertEqual(section_g["Podatek_Art30a_Poz_47"], 19.0)
                self.assertEqual(section_g["Podatek_zagraniczny_uznany_Poz_48"], 10.0)
                self.assertEqual(section_g["Roznica_Poz_49"], 9)
                self.assertEqual(calculation["PIT_ZG_Attachments"][0]["Dywidendy_Art30a"], 100.0)

    def test_passive_income_reversals_and_net_only_dividends_are_not_counted_as_gross_income(self):
        invalid_rows = [
            (
                "Transaction History,01/04/2026,Credit Interest Reversal,Credit Interest,-,-,-,-,-0.50,-,-0.50",
                "Negative or reversed interest",
            ),
            (
                "Transaction History,01/04/2026,EGLN dividend reversal,Dividend,EGLN,-,1,EUR,-10,-,-10",
                "Negative or reversed dividend",
            ),
            (
                "Transaction History,01/05/2026,Withholding refund on Credit Interest,Foreign Tax Withholding,-,-,-,-,0.19,-,0.19",
                "Positive withholding may be a tax refund or reversal",
            ),
            (
                "Transaction History,01/04/2026,EGLN dividend,Dividend,EGLN,-,1,EUR,-,0,90",
                "Missing dividend amount",
            ),
        ]

        for row, expected_warning in invalid_rows:
            with self.subTest(expected_warning=expected_warning):
                result = self.calculate_rows([row])
                self.assertEqual(result["status"], "error")
                self.assertIn(expected_warning.lower(), " ".join(result["warnings"]).lower())
                self.assertNotIn("pit38_calculation", result)

    def test_transfers_and_corporate_actions_reconcile_units_but_flag_tax_basis_review(self):
        result = self.calculate_rows([
            "Transaction History,01/02/2026,ISHARES ETC,Buy,EGLN,1,10,EUR,-10,-0.1,-10.1",
            "Transaction History,01/03/2026,Position transfer in,Transfer,EGLN,5,11,EUR,55,0,55",
            "Transaction History,01/04/2026,Stock split 2 for 1,Corporate Action,EGLN,2,10,EUR,20,0,20",
        ])

        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["processed_records"], 1)
        self.assertEqual(result["pit38_calculation"]["PIT38_Sekcja_C"]["Koszty_uzyskania_Poz_23"], 0.0)
        warnings = " ".join(result["warnings"]).lower()
        self.assertIn("transfer units were included in account reconciliation", warnings)
        self.assertIn("corporate-action units were included as reported", warnings)
        rec = result["pit38_calculation"]["Account_Reconciliation"]
        self.assertEqual(rec["Rows"][0]["Transfers in"], 5.0)
        self.assertEqual(rec["Rows"][0]["Corporate action units (net)"], 2.0)
        self.assertEqual(rec["Rows"][0]["Calculated closing units"], 8.0)

    def test_sale_of_transferred_in_shares_fails_without_original_cost_basis(self):
        result = self.calculate_rows([
            "Transaction History,01/02/2026,ISHARES ETC,Buy,EGLN,1,10,EUR,-10,-0.1,-10.1",
            "Transaction History,01/03/2026,Position transfer in,Transfer,EGLN,5,11,EUR,55,0,55",
            "Transaction History,01/04/2026,ISHARES ETC,Sell,EGLN,2,12,EUR,24,-0.1,23.9",
        ])

        self.assertEqual(result["status"], "error")
        warnings = " ".join(result["warnings"])
        self.assertIn("Insufficient FIFO inventory for EGLN", warnings)
        self.assertNotIn("pit38_calculation", result)

    def test_non_finite_csv_number_is_rejected(self):
        result = self.calculate_rows([
            "Transaction History,01/02/2026,ISHARES ETC,Buy,EGLN,inf,10,EUR,-10,-0.1,-10.1",
        ])
        self.assertEqual(result["status"], "error")
        self.assertIn("non-finite quantity", " ".join(result["warnings"]))


class CalculatorValidationTests(unittest.TestCase):
    def test_passive_foreign_tax_excess_is_explained_without_adding_polish_tax(self):
        rates = {
            "2026-10-05": NBPRate(4.3745, "2026-10-02"),
            "2026-10-07": NBPRate(4.3699, "2026-10-06"),
        }
        with patch("tax_calculator.get_nbp_rate", side_effect=lambda _currency, day: rates[day]):
            calc = PIT38Calculator(target_tax_year=2026)
            calc.add_interest("2026-10-05", 0.97, "EUR", country="IE")
            calc.add_withholding_tax("2026-10-07", 0.19, "EUR", "interest", country="IE")
            result = calc.calculate_pit38()

        section_g = result["PIT38_Sekcja_G_Art30a"]
        self.assertEqual(section_g["Podatek_zagraniczny_pobrany_Art30a_PLN"], 0.83)
        self.assertEqual(section_g["Podatek_zagraniczny_uznany_Poz_48"], 0.81)
        self.assertEqual(section_g["Podatek_zagraniczny_nierozliczony_PLN"], 0.02)
        self.assertEqual(section_g["Roznica_Poz_49"], 0)
        self.assertIn("does not increase Polish tax due", " ".join(result["Review_Notes"]))

    def test_prior_loss_requires_explicit_review_confirmation(self):
        with patch("tax_calculator.get_nbp_rate", return_value=NBPRate(1.0, "2025-12-31")):
            calc = PIT38Calculator(target_tax_year=2026, loss_carryforward=100.0)
            self.assertTrue(calc.calculate_pit38()["Review_Required"])
            calc = PIT38Calculator(
                target_tax_year=2026,
                loss_carryforward=100.0,
                loss_carryforward_verified=True,
            )
            self.assertFalse(calc.calculate_pit38()["Review_Required"])

    def test_oversell_does_not_consume_available_fifo_inventory(self):
        with patch("tax_calculator.get_nbp_rate", return_value=NBPRate(1.0, "2025-12-31")):
            calc = PIT38Calculator(target_tax_year=2026)
            calc.add_transaction("2026-01-02", "EGLN", "BUY", 1, 10, "EUR")
            with self.assertRaisesRegex(ValueError, "Insufficient FIFO inventory"):
                calc.add_transaction("2026-01-03", "EGLN", "SELL", 2, 12, "EUR")
            self.assertAlmostEqual(calc.buy_inventory["EGLN"][0]["quantity"], 1)


class UnitReconciliationTests(unittest.TestCase):
    def test_symbol_quantity_input_supports_multiple_fractional_positions(self):
        values = parse_symbol_quantities("egln=91\nIWDA=12.5", "Opening units")
        self.assertEqual(values["EGLN"], 91)
        self.assertEqual(values["IWDA"], 12.5)

    def test_symbol_quantity_input_rejects_duplicates_and_bad_rows(self):
        with self.assertRaisesRegex(ValueError, "listed more than once"):
            parse_symbol_quantities("EGLN=91; egln=92", "Opening units")
        with self.assertRaisesRegex(ValueError, "SYMBOL=quantity"):
            parse_symbol_quantities("EGLN", "Opening units")

    def test_unquantified_corporate_action_marks_reconciliation_incomplete(self):
        reconciliation = UnitReconciler(
            opening_units={"EGLN": 10},
            broker_closing_units={"EGLN": 20},
        )
        reconciliation.record("EGLN", "CORPORATE_ACTION", None, "2026-01-01", 2, "Split")
        result = reconciliation.build()
        self.assertEqual(result["Status"], "INCOMPLETE ACTIVITY")
        self.assertIsNone(result["Rows"][0]["Difference (calculated - IBKR)"])

    def test_assume_zero_only_reports_symbols_whose_opening_was_inferred(self):
        reconciliation = UnitReconciler(
            opening_units={"EGLN": 0},
            broker_closing_units={"EGLN": 1},
            assume_zero_opening=True,
        )
        reconciliation.record("EGLN", "BUY", 1, "2026-01-01", 2)
        result = reconciliation.build()
        self.assertEqual(result["Status"], "MATCH")
        self.assertEqual(result["Issues"], [])


class NBPServiceTests(unittest.TestCase):
    def test_returns_actual_effective_date_from_api(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "rates": [
                {"effectiveDate": "2026-01-01", "mid": 4.20, "no": "001/A/NBP/2026"},
                {"effectiveDate": "2026-01-02", "mid": 4.21, "no": "002/A/NBP/2026"},
            ]
        }
        with patch("nbp_service.requests.get", return_value=response) as get:
            quote = get_nbp_rate("eur", "2026-01-05")
        self.assertEqual(quote, NBPRate(4.21, "2026-01-02", "002/A/NBP/2026"))
        self.assertIn("/eur/2025-12-21/2026-01-04/", get.call_args.args[0])

    def test_network_failure_has_no_estimated_fallback(self):
        with patch("nbp_service.requests.get", side_effect=requests.ConnectionError("offline")):
            with self.assertRaisesRegex(NBPRateUnavailable, "Could not reach NBP"):
                get_nbp_rate("EUR", "2026-01-05")

    def test_non_finite_api_rate_is_rejected(self):
        response = Mock(status_code=200)
        response.json.return_value = {"rates": [{"effectiveDate": "2026-01-02", "mid": float("nan")}]}
        with patch("nbp_service.requests.get", return_value=response):
            with self.assertRaisesRegex(NBPRateUnavailable, "non-finite"):
                get_nbp_rate("EUR", "2026-01-05")


if __name__ == "__main__":
    unittest.main()
