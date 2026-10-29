import csv
import hashlib
import io
import unittest
import zipfile

from audit_exports import generate_csv, generate_steps_csv
from tax_dossier import generate_audit_package


def audit_result():
    return {
        "PIT38_Sekcja_C": {"Inne_przychody_Poz_22": 120.0},
        "PIT38_Sekcja_D_Art30b": {"Podatek_nalezny_Poz_35": 2.0},
        "PIT38_Sekcja_G_Art30a": {
            "Podatek_Art30a_Poz_47": 19.0,
            "Podatek_zagraniczny_pobrany_Art30a_PLN": 10.0,
            "Podatek_zagraniczny_uznany_Poz_48": 10.0,
            "Podatek_zagraniczny_nierozliczony_PLN": 0.0,
        },
        "Passive_Tax_Credit_Audit": {
            "Formula": "Per country cap, followed by total Poz. 47 cap.",
            "By country": [{
                "Country": "IE",
                "Interest income (PLN)": 10.0,
                "Dividend income (PLN)": 0.0,
                "Passive income (PLN)": 10.0,
                "Polish tax cap for country (PLN)": 1.9,
                "Foreign tax paid (PLN)": 2.0,
                "Credit before overall cap (PLN)": 1.9,
            }],
            "Poz. 47 Polish tax (PLN)": 19.0,
            "Foreign tax paid (PLN)": 10.0,
            "Poz. 48 allowed credit (PLN)": 10.0,
            "Uncredited foreign tax (PLN)": 0.0,
        },
        "IBKR_Only_Illustrative_Tax_Estimate_Poz_35_and_49": 2.0,
        "PIT_ZG_Attachments": [],
        "Review_Notes": ["Verify this security's tax-country mapping."],
        "Account_Reconciliation": {
            "Status": "MISMATCH",
            "Formula": "Opening + buys - sells = calculated close",
            "Rows": [{
                "Ticker": "EGLN",
                "Opening units": 10.0,
                "Buys": 5.0,
                "Sells": 2.0,
                "Transfers in": 1.0,
                "Transfers out": 0.0,
                "Corporate action units (net)": 0.0,
                "Calculated closing units": 14.0,
                "IBKR closing units": 13.0,
                "Difference (calculated - IBKR)": 1.0,
                "Status": "MISMATCH",
            }],
            "Unit events": [{
                "Date": "2026-01-02",
                "Ticker": "EGLN",
                "Event": "TRANSFER_IN",
                "Quantity": 1.0,
                "Description": "Receipt of securities",
                "Row": 3,
                "Included": True,
            }],
            "Issues": [],
        },
        "Calculation_Steps": [{
            "Year": 2026,
            "Date": "2026-01-03",
            "NBP Date": "2026-01-02",
            "NBP Table": "001/A/NBP/2026",
            "NBP Source": "https://api.nbp.pl/api/exchangerates/rates/a/eur/2026-01-02/?format=json",
            "Source row": 3,
            "Action_EN": "SELL (target year)",
            "Action_PL": "SPRZEDAŻ",
            "Ticker": "EGLN",
            "Curr": "EUR",
            "Qty": 2.0,
            "Price (FX)": "12.00000000",
            "NBP Rate": "4.200000",
            "Comm (PLN)": 0.42,
            "Revenue (PLN)": 100.8,
            "Cost Basis (PLN)": 80.5,
            "Net (PLN)": 20.3,
            "Details_EN": "Proceeds = 2 × 12 × 4.2; FIFO: 2 from 2025-01-01",
            "Details_PL": "Przychód 2 × 12 × 4,2",
        }],
    }


class AuditExportTests(unittest.TestCase):
    def test_summary_csv_contains_reconciliation_credit_cap_and_assumptions(self):
        rows = list(csv.DictReader(io.StringIO(generate_csv(audit_result()).decode("utf-8"))))
        values = {(row["Category"], row["Field"]): row["Value"] for row in rows}

        self.assertEqual(values[("Account unit reconciliation", "Status")], "MISMATCH")
        self.assertEqual(values[("Unit reconciliation (EGLN)", "Difference (calculated - IBKR)")], "1.0")
        self.assertEqual(values[("Unit event (EGLN)", "Event")], "TRANSFER_IN")
        credit_rule = next(
            row["Field"] for row in rows
            if row["Category"] == "Tax credit cap audit" and row["Field"].startswith("Poz. 48 is limited")
        )
        self.assertIn("source-country", credit_rule)
        self.assertIn("tax-country mapping", values[("Unresolved assumption / review", "Note 1")])

    def test_transaction_ledger_csv_keeps_rate_formula_and_audit_context(self):
        rows = list(csv.DictReader(io.StringIO(generate_steps_csv(audit_result()).decode("utf-8"))))
        tax_row = next(row for row in rows if row["Record type"] == "Tax transaction")
        recon_row = next(row for row in rows if row["Record type"] == "Unit reconciliation")
        transfer_row = next(row for row in rows if row["Record type"] == "Unit event")
        credit_row = next(row for row in rows if row["Record type"] == "Tax-credit cap audit")
        note_row = next(row for row in rows if row["Record type"] == "Unresolved assumption / review")

        self.assertEqual(tax_row["Data Kursu NBP / NBP Date"], "2026-01-02")
        self.assertEqual(tax_row["Kurs NBP / NBP Rate"], "4.200000")
        self.assertIn("FIFO", tax_row["Wzór & FIFO / Audit Formula"])
        self.assertEqual(recon_row["Reconciliation / Status"], "MISMATCH")
        self.assertEqual(transfer_row["Unit event / Quantity"], "1.0")
        self.assertEqual(credit_row["Poz. 48 allowed credit (PLN)"], "10.0")
        self.assertIn("tax-country mapping", note_row["Review note"])

    def test_polish_audit_package_contains_source_manifest_ledger_and_optional_statement(self):
        source = b"TradeDate,Symbol\n2026-01-03,EGLN\n"
        metadata = {
            "source_file": "activity.csv",
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "generated_at": "2026-10-29T12:00:00+01:00",
            "tax_year": 2026,
            "result_status": "needs_review",
            "processed_records": 1,
        }
        package = generate_audit_package(audit_result(), metadata, source, include_original=True)
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            names = archive.namelist()
            self.assertIn("00_CZYTAJ_MNIE_PL.txt", names)
            self.assertIn("01_Raport_i_pelny_rejestr_PIT38_IBKR_2026.pdf", names)
            self.assertIn("02_Rejestr_obliczen_IBKR_2026.csv", names)
            self.assertIn("03_Podsumowanie_PIT38_PITZG_2026.csv", names)
            self.assertIn("04_Rejestr_kursow_NBP_2026.csv", names)
            self.assertIn("05_Zrodlo_IBKR/activity.csv", names)
            printable_pdf = archive.read("01_Raport_i_pelny_rejestr_PIT38_IBKR_2026.pdf")
            self.assertTrue(printable_pdf.startswith(b"%PDF"))
            self.assertIn(b"595.2756 841.8898", printable_pdf)
            self.assertIn(b"841.8898 595.2756", printable_pdf)
            readme = archive.read("00_CZYTAJ_MNIE_PL.txt").decode("utf-8")
            self.assertIn(metadata["source_sha256"], readme)
            ledger = archive.read("02_Rejestr_obliczen_IBKR_2026.csv").decode("utf-8-sig")
            self.assertIn("Wiersz zrodlowego CSV IBKR", ledger)
            self.assertIn("001/A/NBP/2026", ledger)

    def test_polish_audit_package_rejects_a_source_file_with_a_different_hash(self):
        metadata = {"source_sha256": hashlib.sha256(b"expected").hexdigest()}
        with self.assertRaisesRegex(ValueError, "does not match"):
            generate_audit_package(audit_result(), metadata, b"different", include_original=True)


if __name__ == "__main__":
    unittest.main()
