# app.py
import streamlit as st
import os
import tempfile
import json
import io
import pandas as pd
import hashlib
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv
from ai_assistance import (
    OpenRouterExplanationError,
    build_calculation_explanation_context,
    build_import_explanation_context,
    get_openrouter_config,
    request_explanation,
)
from auth import render_auth_gate, render_account_sidebar
from database import database_url
from audit_exports import generate_csv as _generate_csv, generate_steps_csv as _generate_steps_csv
from tax_dossier import generate_audit_package, generate_dossier_pdf, generate_ledger_pdf
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from xml.sax.saxutils import escape

# Load environment variables from local .env file if present
load_dotenv()

def _read_streamlit_secret(name):
    try:
        return st.secrets.get(name, "")
    except Exception:
        return ""


try:
    from ibkr_parser import inspect_ibkr_csv, process_and_calculate_file
    from account_reconciliation import parse_symbol_quantities
except ImportError:
    inspect_ibkr_csv = None
    process_and_calculate_file = None
    parse_symbol_quantities = None

# Page Configuration - Sidebar expanded by default
st.set_page_config(
    page_title="Foreign Capital Gains PL Tax Support",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded"
)

_OPENROUTER_CONFIG = get_openrouter_config(_read_streamlit_secret)

# Match Investment Copilot's light slate design system and component styling.
SHARED_THEME_CSS = Path(__file__).with_name("theme.css").read_text(encoding="utf-8")
st.markdown(f"<style>{SHARED_THEME_CSS}</style>", unsafe_allow_html=True)

TAX_APP_CSS = '''
.custom-success-box {
  background:#fff; border:1px solid #d8e0e8; border-left:4px solid #64748b;
  border-radius:12px; padding:14px 18px; color:#334155; font-size:.92rem;
  line-height:1.55; margin:0 0 20px; box-shadow:0 2px 8px rgba(30,41,59,.04);
}
.custom-warning-box {
  background:#fff1f2; border:1px solid #fecdd3; border-left:4px solid #d9545d;
  border-radius:12px; padding:14px 18px; color:#9f1239; font-size:.92rem;
  line-height:1.55; margin:0 0 16px; box-shadow:0 2px 8px rgba(30,41,59,.04);
}
.sidebar-section-label {
  color:#687586; font-size:.75rem; font-weight:700; text-transform:uppercase;
  letter-spacing:.08em; margin:0 0 4px;
}
.tax-badge-row { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
.tax-status-badge {
  display:inline-flex; align-items:center; gap:7px; padding:6px 10px;
  color:#475569; background:#edf1f5; border:1px solid #d8e0e8;
  border-radius:999px; font-size:.72rem; font-weight:700; letter-spacing:.02em;
}
.tax-status-dot { width:6px; height:6px; border-radius:50%; background:#64748b; }
[data-testid="stFileUploader"] section { border-color:#d8e0e8; }
@media (max-width: 850px) {
  .tax-badge-row { gap:6px; }
  .tax-status-badge { font-size:.68rem; padding:5px 8px; }
}
'''
st.markdown(f"<style>{TAX_APP_CSS}</style>", unsafe_allow_html=True)

# Hosted deployments use Investment Copilot's administrator-provisioned login.
# Local single-user mode must be explicitly enabled and is not for public deployments.
_current_user = None
_database_url = database_url()
if _database_url:
    try:
        _current_user = render_auth_gate(st)
    except Exception:
        logging.getLogger("foreign_capital_gains_tax.auth").exception("Authentication gate failed.")
        st.error("The account database could not be reached or initialized. Check DATABASE_URL and database permissions.")
        st.stop()
    if _current_user is None:
        st.stop()
elif any(
    os.getenv(flag, "").strip().lower() in {"1", "true", "yes"}
    for flag in ("ETF_APP_LOCAL_DEV_MODE", "FOREIGN_TAX_LOCAL_DEV_MODE")
):
    st.markdown(
        '<div class="dev-mode-notice"><strong>Local development mode</strong>'
        '<span>Authentication is bypassed for this single-user local run.</span></div>',
        unsafe_allow_html=True,
    )
else:
    st.error(
        "Deployment is not configured. Add DATABASE_URL and bootstrap administrator secrets. "
        "For local single-user development only, set ETF_APP_LOCAL_DEV_MODE=1."
    )
    st.stop()

# Keep uploaded transactions and calculations scoped to one authenticated browser session.
_auth_token = str(st.session_state.get("auth_session_token", "") or "")
_tax_scope_key = hashlib.sha256(_auth_token.encode("utf-8")).hexdigest() if _auth_token else "local"
if st.session_state.get("_foreign_tax_scope_key") != _tax_scope_key:
    for _key in (
        "calculated_data", "tax_year_used", "loss_cf_used", "calculated_source_signature",
        "audit_metadata", "audit_source_bytes",
        "lang_code", "tax_year_input", "loss_cf_input", "loss_cf_verified_input",
        "cash_currency_input", "cash_country_input", "source_country_input",
        "opening_units_input", "closing_units_input", "assume_zero_opening_input",
        "language_choice", "ibkr_csv_upload",
        "created_user_credentials", "new_user_username", "new_user_display",
        "new_user_email", "auth_login_username", "auth_login_password",
        "login_stay_signed_in",
    ):
        st.session_state.pop(_key, None)
    st.session_state["_foreign_tax_scope_key"] = _tax_scope_key

# Initialize session state after authentication so one account cannot inherit another's results.
if "calculated_data" not in st.session_state:
    st.session_state.calculated_data = None
if "tax_year_used" not in st.session_state:
    st.session_state.tax_year_used = 2026
if "loss_cf_used" not in st.session_state:
    st.session_state.loss_cf_used = 0.0

IBKR_UPLOAD_TEMPLATE_CSV = (
    "Date,Description,Transaction Type,Symbol,Quantity,Price,Price Currency,"
    "Commission,Gross Amount,Net Amount,Flag\n"
)


TRANSLATIONS = {
    "EN": {
        "locale_header": "Choose Language",
        "title": "Foreign Capital Gains PL Tax Support",
        "subtitle": "Multi-year FIFO transaction ledger with documented NBP exchange rates.",
        "badge1": "NBP Rate Audit Trail",
        "badge2": "Live NBP Rates API",
        "badge3": "Multi-Year FIFO Inventory",
        "config_header": "Configuration Parameters",
        "target_year": "Target Filing Tax Year",
        "loss_cf": "Eligible prior PIT-38 loss deduction [PLN]",
        "loss_cf_verified": "I verified this loss is eligible for this tax year",
        "cash_currency": "Currency fallback for IBKR cash rows with no currency",
        "cash_currency_help": "Used only when a cash interest, dividend, or withholding row has no currency in the file or description. A currency shown on an individual security trade takes precedence.",
        "cash_country": "Optional source country for IBKR cash interest and related withholding",
        "cash_country_help": "Optional, and separate from the ticker map and cash currency. Applies to Credit Interest/Cash Interest and withholding rows described as interest. The app uses it to group amounts by country for PIT/ZG and its current foreign-tax-credit allocation. If blank, gross interest and Polish tax are still calculated, but country attribution needs review and related withholding is not credited. Verify the country from source records; do not assume it from IBKR's Irish office or the currency.",
        "source_countries": "Optional security source-country mapping for PIT-ZG (TICKER=ISO, e.g. EGLN=IE)",
        "source_countries_help": "This does not identify tickers or affect FIFO matching or aggregate PIT-38 Section C sale totals. It groups target-year sale amounts for PIT-ZG and assigns countries to security dividends/withholding for foreign-tax attribution. Map only securities with target-year sales, dividends, or withholding. If blank, aggregate calculations still run but country attribution is flagged for review; unmapped security withholding is not credited. Enter one TICKER=ISO2 pair per line. Verify the country independently; the ticker and mapping alone do not prove it.",
        "reconciliation_header": "Account Unit Reconciliation",
        "opening_units": "Opening units before earliest imported activity",
        "closing_units": "IBKR closing units at end of uploaded activity",
        "reconciliation_units_help": "Enter one SYMBOL=quantity per line, e.g. EGLN=91. Use opening and closing holdings shown by IBKR.",
        "assume_zero_opening": "Assume opening units are zero for symbols not listed",
        "tab_reconciliation": "Account Reconciliation",
        "reconciliation_formula": "Opening + buys − sells + transfers in − transfers out + corporate-action unit change = calculated closing units.",
        "reconciliation_none": "No security unit activity was found in this file.",
        "reconciliation_events": "Unit-changing IBKR activity",
        "file_header": "Data Import Source",
        "upload_label": "Upload IBKR Export CSV",
        "upload_help": "Upload the original IBKR transaction-history CSV exactly as downloaded; no column changes are needed. FX conversion rows are excluded from PIT-38 and retained in the audit trail for review.",
        "download_upload_template": "Download blank CSV template",
        "upload_template_help": "Optional manual-entry format: one transaction per row. Keep the column headers. Original IBKR CSV exports can still be uploaded directly.",
        "import_check_title": "Import Check",
        "import_check_ready": "Required columns are mapped. Review the detected types and sample rows before calculating.",
        "import_check_blocked": "Calculation is disabled until the required column or data issues below are resolved.",
        "import_previous_result_cleared": "The uploaded file or import settings changed. Previous results were cleared; recalculate after reviewing this Import Check.",
        "import_ack_mapping": "I reviewed and confirm the selected column mapping and sample rows.",
        "ai_import_title": "AI help with import and settings",
        "ai_results_title": "AI explanation of tax results",
        "ai_results_open": "Open the AI question panel",
        "ai_import_privacy": "Your question and up to 8 recent chat messages are sent with the import summary, the app guide, and selected safe settings (tax year, cash currency/country, whether loss/reconciliation inputs are set, and the count of security-country mappings). The app does not attach the CSV, preview rows, account number, ticker names or mapping text, or entered unit quantities; anything you type is sent. Chat stays in this app session. OpenRouter may route requests to providers with their own data policies.",
        "ai_results_privacy": "Your question and up to 8 recent chat messages are sent with selected PLN summary figures, the app guide, and safe settings (tax year, cash currency/country, whether loss/reconciliation inputs are set, and the count of security-country mappings). The app does not attach the CSV, transaction ledger, account number, ticker names or mapping text, or entered unit quantities; anything you type is sent. Chat stays in this app session. OpenRouter may route requests to providers with their own data policies.",
        "ai_import_consent": "I agree to send my question, recent messages, the app guide, safe settings and this import summary to OpenRouter.",
        "ai_results_consent": "I agree to send my question, recent messages, the app guide, safe settings and these summary figures to OpenRouter.",
        "ai_import_placeholder": "Ask about an import issue or app setting…",
        "ai_import_examples": "Try: Explain every configuration parameter and what happens if I leave it blank.",
        "ai_results_placeholder": "Ask about a PIT-38 position, warning or reconciliation status…",
        "ai_results_examples": "Try: What does position 23 mean? Why is this marked for review?",
        "ai_not_configured": "AI explanations are off until OPENROUTER_API_KEY is configured in this app's environment or Streamlit Secrets. OPENROUTER_MODEL defaults to openrouter/free.",
        "ai_clear_chat": "Clear chat",
        "ai_loading": "Getting an answer from OpenRouter...",
        "ai_disclaimer": "AI-generated answers may be inaccurate. They do not change the app's calculations and are not tax advice.",
        "import_format": "Detected format: {format}",
        "import_rows": "Transaction rows: {count}",
        "import_mapping_title": "Review or change column mapping",
        "import_not_present": "Not present",
        "import_select_column": "Select a column",
        "import_selected_mapping": "Selected column mapping",
        "import_types_title": "Detected transaction types",
        "import_excluded_title": "Rows excluded from PIT-38 (kept in the audit trail)",
        "import_preview_title": "Sample rows (including source row)",
        "import_issues_title": "Import issues",
        "import_ack_unknown": "I reviewed the {count} unrecognized security row(s) and understand they will not be included in FIFO tax calculations.",
        "import_type_column": "Transaction type",
        "import_type_rows": "Rows",
        "import_issue_select_required": "Choose which CSV column contains {field}.",
        "import_issue_ambiguous": "More than one CSV column may contain {field}; select the correct one.",
        "import_issue_trade_column": "Trade rows are present. Map the {field} column.",
        "import_issue_income_amount": "Interest or dividend rows are present. Map the gross income amount column.",
        "import_issue_withholding_amount": "Withholding rows need a gross amount or net amount column.",
        "import_issue_withholding_description": "A description column is needed to identify what foreign withholding relates to.",
        "import_issue_bad_dates": "{count} row(s) have a missing or unreadable date: {rows}.",
        "import_issue_row_symbol": "Row {row}: {action} has no ticker or symbol.",
        "import_issue_row_value": "Row {row}: {action} is missing {field}.",
        "import_issue_row_value_invalid": "Row {row}: {field} is not a valid number.",
        "import_issue_row_amount_missing": "Row {row}: the income or withholding amount is blank.",
        "import_issue_row_amount_invalid": "Row {row}: the income or withholding amount is not a valid number.",
        "import_issue_row_currency": "Row {row}: currency is missing, invalid, or ambiguous. Check its column or select the account currency.",
        "import_issue_gross_mismatch": "Row {row}: gross amount does not agree with quantity × price.",
        "import_issue_net_mismatch": "Row {row}: net amount does not agree with gross amount minus commission.",
        "import_issue_unknown_types": "{count} security row(s) use an unrecognized transaction type.",
        "import_issue_mapping_missing": "The selected {field} column is not present in this file.",
        "btn_calculate": "Execute Tax Calculation",
        "msg_upload_first": "Upload your official IBKR CSV export file in the sidebar to initialize full tax analysis.",
        "awaiting_title": "Awaiting Data Import",
        "success_msg": "Calculation complete: {count} supported records processed. Verify holdings, instrument treatment, and other PIT-38 fields before filing.",
        "tab_sec_c": "PIT-38 Section C",
        "tab_sec_g": "Section G (Foreign Tax)",
        "tab_pit_zg": "PIT/ZG positions by country",
        "tab_ledger": "Detailed FIFO Audit Ledger",
        "tab_warnings": "Execution Logs & Validation",
        "excluded_activity_title": "Raw IBKR activity excluded from PIT-38",
        "excluded_activity_note": "{count} raw activity rows were excluded from the PIT-38 calculations and are listed here. FX conversion rows are outside this PIT-38 calculation; this does not decide whether they need separate PIT reporting.",
        "sec_c_title": "IBKR-only Section C figures ({year})",
        "ibkr_section_c_note": "Use IBKR proceeds and costs in positions 22 and 23. Add Polish broker PIT-8C amounts in positions 20/21, then recalculate the combined totals and tax in positions 26–35. The other Section C figures shown here are IBKR-only references.",
        "rev_poz22": "Gross Revenue (Poz. 22)",
        "cost_poz23": "Deductible Costs (Poz. 23)",
        "inc_poz24": "Income (Poz. 28)",
        "loss_poz25": "Current-year loss (Poz. 29)",
        "loss_poz26": "Prior-year loss deducted (Poz. 30)",
        "final_inc": "Taxable base (Poz. 31)",
        "capital_tax": "Art. 30b tax (Poz. 33)",
        "capital_tax_due": "Capital tax due (Poz. 35)",
        "passive_tax": "Foreign Art. 30a tax (Poz. 47)",
        "passive_credit": "Allowed foreign credit (Poz. 48)",
        "passive_due": "Art. 30a tax due (Poz. 49)",
        "calculated_tax_due": "IBKR-only tax estimate (Poz. 35 + 49)",
        "ibkr_section_g_note": "These are IBKR-only amounts. Combine foreign interest/dividends and foreign tax from all sources before finalizing positions 47–49; the credit limit applies to the combined figures.",
        "credit_cap_explanation": "Polish credit (Poz. 48) is limited to the Polish tax on this foreign income (Poz. 47). Any excess foreign tax is not credited here and does not add to Polish tax due.",
        "sec_g_title": "Section G: Passive Income & Foreign Tax Credit",
        "foreign_tax": "Foreign tax allowed as credit (Poz. 48)",
        "pl_tax": "Art. 30a tax due (Poz. 49)",
        "pit_zg_title": "PIT/ZG figures by country",
        "pit_zg_scope_note": "Use the form-entry table below: prepare a separate PIT/ZG(8) for each country. These are only the IBKR amounts; confirm the source country and add amounts from your other brokers for that same country.",
        "pit_zg_form_guide": "**PIT/ZG(8) fields:** Poz. 1 NIP/PESEL; Poz. 2 tax year; Poz. 3–5 taxpayer surname, first name and birth date; Poz. 6 full country name; Poz. 7 two-letter country code. In Section C.3, copy the table's amount to Poz. 29 (income under Art. 30b(5a)/(5b)) and Poz. 30 (foreign tax paid on Poz. 29 income, before the credit limit). Poz. 31–32 are for virtual-currency income and tax; this app does not calculate them.",
        "pit_zg_country": "Country summary",
        "pit_zg_country_name_column": "Poz. 6 — full country name (fill in)",
        "pit_zg_country_name_prompt": "Enter the full country name for {country}",
        "pit_zg_country_code_column": "Poz. 7 — country code",
        "pit_zg_fields_title": "PIT/ZG(8), Section C.3 — copy these IBKR amounts into the form",
        "pit_zg_pos29": "Poz. 29: Art. 30b(5a)/(5b) income (PLN)",
        "pit_zg_pos30": "Poz. 30: foreign tax on Poz. 29 income (PLN)",
        "pit_zg_positions_note": "The country name in Poz. 6 must match the two-letter code in Poz. 7. Poz. 30 is foreign tax paid before the credit limit. Verify the country and treatment from source documents.",
        "pit_zg_supplement_title": "Supporting country detail — not separate PIT/ZG boxes",
        "pit_zg_supplement_note": "Art. 30b proceeds and costs below support the income calculation for PIT/ZG Poz. 29; they are not entered separately on PIT/ZG. Art. 30a interest and dividends are not PIT/ZG fields; use the app's separate PIT-38 Section G totals for Poz. 47–49.",
        "pit_zg_supp_proceeds": "Poz. 29 support: Art. 30b proceeds",
        "pit_zg_supp_costs": "Poz. 29 support: Art. 30b costs",
        "pit_zg_supp_interest": "Art. 30a interest (not PIT/ZG)",
        "pit_zg_supp_dividends": "Art. 30a dividends (not PIT/ZG)",
        "pit_zg_form_link": "Official Ministry of Finance PIT/ZG (8) form",
        "pit_zg_no_positions": "No mapped foreign securities income is available for positions 29–30. Check the ticker-country mapping and unresolved-country notes.",
        "no_zg": "No verified country attribution is available for this year; configure and verify PIT/ZG separately.",
        "ledger_title": "Detailed Transaction & NBP Rate Ledger",
        "ledger_sub": "Explicit currency separation, preceding business day NBP rate dates, and full mathematical exposure:",
        "export_title": "Download calculation reports (PDF, CSV & ZIP)",
        "export_intro": "Start with the summary. Use the detailed ledger to trace individual IBKR records and calculations. The complete report combines both for printing.",
        "summary_export_help": "A short overview of the IBKR-only PIT-38 figures and foreign tax credit. Download the complete report for transaction and review details.",
        "ledger_export_help": "Calculated transactions include the source CSV row, currency conversion, NBP reference, commission, and FIFO cost. Excluded IBKR activity is listed separately.",
        "complete_export_help": "One print-ready Polish report with the summary, reconciliation, review notes, transaction calculations, excluded activity, and the NBP rate register.",
        "btn_summary_pdf": "Download Summary Report (PDF)",
        "btn_summary_csv": "Download Summary Data (CSV)",
        "btn_ledger_pdf": "Download Detailed Audit Ledger (PDF)",
        "btn_ledger_csv": "Download Detailed Audit Ledger (CSV)",
        "btn_audit_package": "Download Polish Tax Office Support Package (ZIP)",
        "btn_print_pdf": "Download Complete Print-Ready Report (PDF)",
        "include_source_csv": "Include the original IBKR CSV in the package",
        "pdf_title": "PIT-38 Calculation Summary — {year}",
        "pdf_sub": "Interactive Brokers export | Prior loss deducted: {loss:.2f} PLN",
        "summary_pdf_disclaimer": "WORKING CALCULATION ONLY — check the source data and tax treatment before filing.",
        "summary_pdf_scope": "This IBKR-only summary shows PIT/ZG positions 6–7 (country/name) and 29–30 (amounts). Confirm the source country and add other broker amounts for the same country.",
        "pdf_zg_heading": "PIT/ZG (8), section C.3 — figures to copy by country",
        "pdf_zg_country": "Position 6 — full country name (enter)",
        "pdf_zg_country_code": "Position 7 — code",
        "pdf_zg_pos29": "Position 29 — Art. 30b income (PLN)",
        "pdf_zg_pos30": "Position 30 — tax on position 29 (PLN)",
        "pdf_zg_note": "Prepare one PIT/ZG per country. Fill positions 1–5 with taxpayer details/year, position 6 with the full country name, and position 7 with its two-letter code. Copy these figures into positions 29 and 30. Position 30 is before the credit limit. The app is IBKR-only and does not calculate virtual-currency positions 31–32.",
        "pdf_sec_c_header": "PIT-38 Section C",
        "pdf_sec_g_header": "PIT-38 Section G (Foreign Tax Credit & Withholding)",
        "pdf_amount_header": "Amount (PLN)",
        "pdf_ledger_header": "Detailed FIFO Ledger and NBP Rate Audit Trail",
    },
    "PL": {
        "locale_header": "Wybierz Język",
        "title": "Foreign Capital Gains PL Tax Support",
        "subtitle": "Wieloletni rejestr transakcji FIFO ze źródłowymi kursami NBP.",
        "badge1": "Ścieżka audytowa kursów NBP",
        "badge2": "Pobieranie Kursów NBP API",
        "badge3": "Wieloletni Magazyn FIFO",
        "config_header": "Parametry Konfiguracyjne",
        "target_year": "Docelowy Rok Podatkowy",
        "loss_cf": "Kwalifikowana strata PIT-38 do odliczenia [PLN]",
        "loss_cf_verified": "Potwierdzam, że strata kwalifikuje się do odliczenia w tym roku",
        "cash_currency": "Waluta zastępcza dla operacji gotówkowych bez waluty w CSV",
        "cash_currency_help": "Używana tylko wtedy, gdy wiersz odsetek, dywidendy lub podatku u źródła nie podaje waluty w pliku ani opisie. Waluta podana przy konkretnej transakcji papieru ma pierwszeństwo.",
        "cash_country": "Opcjonalny kraj źródła odsetek gotówkowych IBKR i powiązanego podatku",
        "cash_country_help": "To ustawienie jest opcjonalne i niezależne od mapowania tickerów oraz waluty gotówkowej. Dotyczy wierszy Credit Interest/Cash Interest i podatku u źródła opisanego jako podatek od odsetek. Aplikacja grupuje te kwoty według kraju na potrzeby PIT/ZG i stosowanego w niej przypisania odliczenia podatku. Bez kraju nadal oblicza przychód brutto z odsetek i polski podatek, ale oznacza przypisanie do kraju do sprawdzenia i nie zalicza powiązanego podatku zagranicznego. Kraj potwierdź w dokumentach źródłowych; nie zakładaj go na podstawie irlandzkiej siedziby IBKR ani waluty.",
        "source_countries": "Opcjonalne przypisanie kraju źródła papieru do PIT-ZG (TICKER=kod ISO, np. EGLN=IE)",
        "source_countries_help": "To ustawienie nie rozpoznaje tickerów ani nie zmienia FIFO lub łącznych kwot sprzedaży w PIT-38 Sekcja C. Grupuje kwoty sprzedaży z bieżącego roku podatkowego do PIT-ZG i przypisuje kraje dywidendom oraz podatkowi pobranemu od papierów przy ustalaniu podatku zagranicznego. Wprowadź tylko papiery z istotną sprzedażą, dywidendą lub podatkiem w tym roku. Bez mapowania kwoty zbiorcze nadal zostaną obliczone, ale przypisanie kraju będzie wymagało sprawdzenia; nieprzypisany podatek od papieru nie jest odliczany. Wpisz po jednej parze TICKER=kod ISO2 w wierszu. Kraj zweryfikuj niezależnie — ticker i mapowanie same w sobie go nie potwierdzają.",
        "reconciliation_header": "Uzgodnienie liczby jednostek",
        "opening_units": "Liczba jednostek przed najwcześniejszą operacją w pliku",
        "closing_units": "Końcowa liczba jednostek według IBKR",
        "reconciliation_units_help": "Wpisz po jednym SYMBOL=liczba w wierszu, np. EGLN=91. Użyj stanów początkowych i końcowych z IBKR.",
        "assume_zero_opening": "Przyjmij zero jednostek początkowych dla niewymienionych symboli",
        "tab_reconciliation": "Uzgodnienie rachunku",
        "reconciliation_formula": "Stan początkowy + kupno − sprzedaż + transfery przychodzące − wychodzące + zmiana z działań korporacyjnych = obliczony stan końcowy.",
        "reconciliation_none": "W pliku nie znaleziono zmian liczby jednostek papierów.",
        "reconciliation_events": "Operacje IBKR zmieniające liczbę jednostek",
        "file_header": "Import Raportu IBKR",
        "upload_label": "Wgraj plik CSV z IBKR",
        "upload_help": "Wgraj oryginalny plik CSV z historią transakcji IBKR bez zmian kolumn. Wiersze przewalutowań zostaną wykluczone z PIT-38 i zachowane w rejestrze audytowym do sprawdzenia.",
        "download_upload_template": "Pobierz pusty szablon CSV",
        "upload_template_help": "Opcjonalny format do ręcznego wprowadzania: jedna operacja w wierszu. Zachowaj nagłówki kolumn. Oryginalne pliki CSV z IBKR nadal można wgrywać bezpośrednio.",
        "import_check_title": "Sprawdzenie importu",
        "import_check_ready": "Wymagane kolumny są przypisane. Przed obliczeniem sprawdź typy operacji i przykładowe wiersze.",
        "import_check_blocked": "Obliczenia są zablokowane do czasu uzupełnienia wymaganych kolumn lub poprawienia danych poniżej.",
        "import_previous_result_cleared": "Zmieniono plik lub ustawienia importu. Poprzedni wynik usunięto; po sprawdzeniu importu wykonaj obliczenia ponownie.",
        "import_ack_mapping": "Sprawdziłem(-am) i potwierdzam przypisanie kolumn oraz przykładowe wiersze.",
        "ai_import_title": "Pomoc AI przy imporcie i ustawieniach",
        "ai_results_title": "Wyjaśnienie wyników podatkowych przez AI",
        "ai_results_open": "Otwórz panel pytań do AI",
        "ai_import_privacy": "Do OpenRouter zostanie wysłane Twoje pytanie i do 8 ostatnich wiadomości czatu wraz z podsumowaniem importu, przewodnikiem po aplikacji oraz wybranymi bezpiecznymi ustawieniami (rok podatkowy, waluta/kraj gotówkowy, informacja o ustawieniu straty i uzgodnienia oraz liczba mapowań krajów papierów). Aplikacja nie dołącza pliku CSV, podglądu, numeru rachunku, nazw tickerów ani treści mapowań oraz wpisanych ilości jednostek; wszystko, co wpiszesz, zostanie wysłane. Czat pozostaje w bieżącej sesji. OpenRouter może przekazywać zapytania dostawcom stosującym własne zasady przetwarzania danych.",
        "ai_results_privacy": "Do OpenRouter zostanie wysłane Twoje pytanie i do 8 ostatnich wiadomości czatu wraz ze zbiorczymi kwotami w PLN, przewodnikiem po aplikacji i bezpiecznymi ustawieniami (rok podatkowy, waluta/kraj gotówkowy, informacja o ustawieniu straty i uzgodnienia oraz liczba mapowań krajów papierów). Aplikacja nie dołącza pliku CSV, rejestru transakcji, numeru rachunku, nazw tickerów ani treści mapowań oraz wpisanych ilości jednostek; wszystko, co wpiszesz, zostanie wysłane. Czat pozostaje w bieżącej sesji. OpenRouter może przekazywać zapytania dostawcom stosującym własne zasady przetwarzania danych.",
        "ai_import_consent": "Zgadzam się wysłać moje pytanie, ostatnie wiadomości, przewodnik aplikacji, bezpieczne ustawienia i podsumowanie importu do OpenRouter.",
        "ai_results_consent": "Zgadzam się wysłać moje pytanie, ostatnie wiadomości, przewodnik aplikacji, bezpieczne ustawienia i te kwoty do OpenRouter.",
        "ai_import_placeholder": "Zapytaj o import lub ustawienie aplikacji…",
        "ai_import_examples": "Przykład: Wyjaśnij wszystkie parametry konfiguracji i co się stanie, gdy je pominę.",
        "ai_results_placeholder": "Zapytaj o pozycję PIT-38, ostrzeżenie lub uzgodnienie…",
        "ai_results_examples": "Przykład: Co oznacza pozycja 23? Dlaczego wynik wymaga sprawdzenia?",
        "ai_not_configured": "Wyjaśnienia AI są wyłączone do czasu skonfigurowania OPENROUTER_API_KEY w środowisku tej aplikacji lub w Streamlit Secrets. Domyślny OPENROUTER_MODEL to openrouter/free.",
        "ai_clear_chat": "Wyczyść czat",
        "ai_loading": "Pobieranie odpowiedzi z OpenRouter...",
        "ai_disclaimer": "Odpowiedzi wygenerowane przez AI mogą być niedokładne. Nie zmieniają obliczeń aplikacji i nie stanowią porady podatkowej.",
        "import_format": "Rozpoznany format: {format}",
        "import_rows": "Wiersze transakcji: {count}",
        "import_mapping_title": "Sprawdź lub zmień przypisanie kolumn",
        "import_not_present": "Brak",
        "import_select_column": "Wybierz kolumnę",
        "import_selected_mapping": "Wybrane przypisanie kolumn",
        "import_types_title": "Rozpoznane typy operacji",
        "import_excluded_title": "Wiersze wyłączone z PIT-38 (zachowane w rejestrze audytowym)",
        "import_preview_title": "Przykładowe wiersze (z numerem źródłowym)",
        "import_issues_title": "Problemy z importem",
        "import_ack_unknown": "Sprawdziłem(-am) {count} nierozpoznanych wierszy papierów i rozumiem, że nie zostaną uwzględnione w obliczeniu FIFO.",
        "import_type_column": "Typ transakcji",
        "import_type_rows": "Wiersze",
        "import_issue_select_required": "Wybierz kolumnę CSV zawierającą: {field}.",
        "import_issue_ambiguous": "Więcej niż jedna kolumna może zawierać {field}; wybierz właściwą.",
        "import_issue_trade_column": "W pliku są transakcje. Przypisz kolumnę: {field}.",
        "import_issue_income_amount": "Są wiersze odsetek lub dywidend. Przypisz kolumnę kwoty przychodu brutto.",
        "import_issue_withholding_amount": "Wiersze podatku u źródła wymagają kolumny kwoty brutto albo netto.",
        "import_issue_withholding_description": "Potrzebna jest kolumna opisu, aby ustalić, czego dotyczy podatek zagraniczny.",
        "import_issue_bad_dates": "{count} wierszy ma brakującą lub nieczytelną datę: {rows}.",
        "import_issue_row_symbol": "Wiersz {row}: operacja {action} nie ma tickera ani symbolu.",
        "import_issue_row_value": "Wiersz {row}: operacja {action} nie ma wartości w polu: {field}.",
        "import_issue_row_value_invalid": "Wiersz {row}: pole {field} nie zawiera prawidłowej liczby.",
        "import_issue_row_amount_missing": "Wiersz {row}: brakuje kwoty przychodu lub podatku u źródła.",
        "import_issue_row_amount_invalid": "Wiersz {row}: kwota przychodu lub podatku u źródła nie jest prawidłową liczbą.",
        "import_issue_row_currency": "Wiersz {row}: brakuje waluty albo jest nieprawidłowa lub niejednoznaczna. Sprawdź kolumnę lub wybierz walutę rachunku.",
        "import_issue_gross_mismatch": "Wiersz {row}: kwota brutto nie zgadza się z ilością × ceną.",
        "import_issue_net_mismatch": "Wiersz {row}: kwota netto nie zgadza się z kwotą brutto minus prowizja.",
        "import_issue_unknown_types": "{count} wierszy papierów ma nierozpoznany typ operacji.",
        "import_issue_mapping_missing": "Wybranej kolumny dla pola {field} nie ma w tym pliku.",
        "btn_calculate": "Uruchom Obliczenia Podatkowe",
        "msg_upload_first": "Proszę wgrać plik CSV z IBKR w panelu bocznym, aby rozpocząć przetwarzanie danych.",
        "awaiting_title": "Oczekiwanie na Import Danych",
        "success_msg": "Obliczenia zakończone: przetworzono {count} obsługiwanych rekordów. Przed złożeniem sprawdź historię, klasyfikację instrumentów i pozostałe pozycje PIT-38.",
        "tab_sec_c": "PIT-38 Sekcja C",
        "tab_sec_g": "Sekcja G (Podatek Zagraniczny)",
        "tab_pit_zg": "PIT/ZG — pozycje według kraju",
        "tab_ledger": "Szczegółowy rejestr FIFO",
        "tab_warnings": "Dziennik Zdarzeń i Walidacja",
        "excluded_activity_title": "Surowe operacje IBKR wyłączone z PIT-38",
        "excluded_activity_note": "Z obliczeń PIT-38 wyłączono {count} surowych wierszy aktywności. Są wymienione poniżej. Wyłączenie przewalutowań z PIT-38 nie rozstrzyga, czy wymagają osobnego rozliczenia PIT.",
        "sec_c_title": "Wartości tylko z IBKR — Sekcja C ({year})",
        "ibkr_section_c_note": "Kwoty z IBKR wpisz jako wkład do poz. 22 (przychody) i 23 (koszty). Po dodaniu PIT-8C polskich brokerów w poz. 20/21 oblicz ponownie łączne wartości i podatek w poz. 26–35. Pozostałe wartości Sekcji C pokazane tutaj dotyczą wyłącznie IBKR.",
        "rev_poz22": "Przychód Brutto (Poz. 22)",
        "cost_poz23": "Koszty Uzyskania (Poz. 23)",
        "inc_poz24": "Dochód (Poz. 28)",
        "loss_poz25": "Strata z bieżącego roku (Poz. 29)",
        "loss_poz26": "Odliczona strata z lat ubiegłych (Poz. 30)",
        "final_inc": "Podstawa opodatkowania (Poz. 31)",
        "capital_tax": "Podatek Art. 30b (Poz. 33)",
        "capital_tax_due": "Podatek kapitałowy do zapłaty (Poz. 35)",
        "passive_tax": "Podatek zagraniczny Art. 30a (Poz. 47)",
        "passive_credit": "Uznany podatek zagraniczny (Poz. 48)",
        "passive_due": "Podatek Art. 30a do zapłaty (Poz. 49)",
        "calculated_tax_due": "Szacunkowy podatek tylko z IBKR (Poz. 35 + 49)",
        "ibkr_section_g_note": "To są kwoty tylko z IBKR. Przed ustaleniem poz. 47–49 zsumuj odsetki/dywidendy zagraniczne i podatek zagraniczny ze wszystkich źródeł; limit odliczenia dotyczy łącznych kwot.",
        "credit_cap_explanation": "Polskie odliczenie (poz. 48) nie może przekroczyć polskiego podatku od tego zagranicznego dochodu (poz. 47). Nadwyżka podatku zagranicznego nie jest tu odliczana i nie zwiększa podatku należnego w Polsce.",
        "sec_g_title": "Sekcja G: Dochody Pasywne i Zapłacony Podatek za Granicą",
        "foreign_tax": "Uznany podatek zagraniczny (Poz. 48)",
        "pl_tax": "Podatek Art. 30a do zapłaty (Poz. 49)",
        "pit_zg_title": "Kwoty PIT/ZG według kraju",
        "pit_zg_scope_note": "Użyj poniższej tabeli do przepisania kwot: dla każdego kraju przygotuj osobny PIT/ZG(8). To są kwoty tylko z IBKR; potwierdź kraj źródła i dodaj kwoty z innych brokerów dla tego samego kraju.",
        "pit_zg_form_guide": "**Pola PIT/ZG(8):** poz. 1 NIP/PESEL; poz. 2 rok podatkowy; poz. 3–5 nazwisko, imię i data urodzenia podatnika; poz. 6 pełna nazwa państwa; poz. 7 dwuliterowy kod państwa. W sekcji C.3 przepisz kwotę z tabeli do poz. 29 (dochód z art. 30b ust. 5a/5b) i poz. 30 (podatek zapłacony za granicą od dochodu z poz. 29, przed limitem odliczenia). Poz. 31–32 dotyczą dochodu i podatku od walut wirtualnych; aplikacja ich nie oblicza.",
        "pit_zg_country": "Zestawienie dla kraju",
        "pit_zg_country_name_column": "Poz. 6 — pełna nazwa państwa (uzupełnij)",
        "pit_zg_country_name_prompt": "Wpisz pełną nazwę państwa dla kodu {country}",
        "pit_zg_country_code_column": "Poz. 7 — kod państwa",
        "pit_zg_fields_title": "PIT/ZG(8), sekcja C.3 — te kwoty przepisz do formularza",
        "pit_zg_pos29": "Poz. 29: dochód z art. 30b ust. 5a/5b (PLN)",
        "pit_zg_pos30": "Poz. 30: podatek od dochodu z poz. 29 (PLN)",
        "pit_zg_positions_note": "Nazwa państwa w poz. 6 musi odpowiadać kodowi dwuliterowemu w poz. 7. Poz. 30 obejmuje podatek zagraniczny przed limitem odliczenia. Potwierdź kraj i kwalifikację w dokumentach źródłowych.",
        "pit_zg_supplement_title": "Szczegóły pomocnicze — nie są odrębnymi polami PIT/ZG",
        "pit_zg_supplement_note": "Przychody i koszty Art. 30b poniżej składają się na obliczenie dochodu do PIT/ZG poz. 29; nie wpisuje się ich osobno do PIT/ZG. Odsetki i dywidendy Art. 30a nie są polami PIT/ZG — użyj oddzielnych łącznych kwot PIT-38, sekcja G, poz. 47–49.",
        "pit_zg_supp_proceeds": "Podstawa poz. 29: przychód Art. 30b",
        "pit_zg_supp_costs": "Podstawa poz. 29: koszt Art. 30b",
        "pit_zg_supp_interest": "Odsetki Art. 30a (nie PIT/ZG)",
        "pit_zg_supp_dividends": "Dywidendy Art. 30a (nie PIT/ZG)",
        "pit_zg_form_link": "Urzędowy formularz PIT/ZG (8) Ministerstwa Finansów",
        "pit_zg_no_positions": "Brak przypisanych do kraju dochodów z zagranicznych papierów dla poz. 29–30. Sprawdź mapowanie tickerów i uwagi o nierozstrzygniętym kraju.",
        "no_zg": "Brak potwierdzonej atrybucji kraju; skonfiguruj i zweryfikuj PIT/ZG osobno.",
        "ledger_title": "Szczegółowy rejestr transakcji i kursów NBP",
        "ledger_sub": "Każdy wiersz pokazuje operację, przeliczenie na PLN, kurs NBP i koszt FIFO.",
        "export_title": "Pobierz raporty z obliczeń (PDF, CSV i ZIP)",
        "export_intro": "Zacznij od podsumowania. Szczegółowy rejestr pozwala prześledzić każdą operację IBKR i jej obliczenie. Kompletny raport łączy oba dokumenty i nadaje się do wydruku.",
        "summary_export_help": "Krótkie podsumowanie kwot tylko z IBKR i odliczenia podatku zagranicznego. Szczegóły transakcji i uwagi znajdziesz w kompletnym raporcie.",
        "ledger_export_help": "Uwzględnione transakcje zawierają numer wiersza CSV, przeliczenie waluty, kurs NBP, prowizję i koszt FIFO. Wykluczone operacje IBKR są pokazane osobno.",
        "complete_export_help": "Jeden polski dokument do wydruku: podsumowanie, uzgodnienie rachunku, uwagi, obliczenia transakcji, wykluczone operacje i rejestr kursów NBP.",
        "btn_summary_pdf": "Pobierz podsumowanie obliczeń (PDF)",
        "btn_summary_csv": "Pobierz dane podsumowania (CSV)",
        "btn_ledger_pdf": "Pobierz szczegółowy rejestr audytowy (PDF)",
        "btn_ledger_csv": "Pobierz szczegółowy rejestr audytowy (CSV)",
        "btn_audit_package": "Pobierz polski pakiet dokumentów pomocniczych (ZIP)",
        "btn_print_pdf": "Pobierz kompletny raport do wydruku (PDF)",
        "include_source_csv": "Dołącz oryginalny plik CSV z IBKR do pakietu",
        "pdf_title": "PIT-38 — podsumowanie obliczeń ({year})",
        "pdf_sub": "Dane z Interactive Brokers | Odliczona strata z lat ubiegłych: {loss:.2f} PLN",
        "summary_pdf_disclaimer": "ZESTAWIENIE POMOCNICZE — przed złożeniem sprawdź dane źródłowe i sposób rozliczenia podatkowego.",
        "summary_pdf_scope": "Podsumowanie obejmuje tylko IBKR. Tabela PIT/ZG wskazuje osobno poz. 6–7 (państwo/kod) oraz kwoty do poz. 29–30. Potwierdź kraj źródła i dodaj kwoty z innych brokerów dla tego samego kraju.",
        "pdf_zg_heading": "PIT/ZG(8), sekcja C.3 — kwoty do przepisania według kraju",
        "pdf_zg_country": "Poz. 6 — pełna nazwa państwa (wpisz)",
        "pdf_zg_country_code": "Poz. 7 — kod",
        "pdf_zg_pos29": "Poz. 29 — dochód Art. 30b 5a/5b (PLN)",
        "pdf_zg_pos30": "Poz. 30 — podatek od poz. 29 (PLN)",
        "pdf_zg_note": "Osobny PIT/ZG dla każdego państwa. Poz. 1–5: dane podatnika i rok; poz. 6: pełna nazwa państwa; poz. 7: kod dwuliterowy. Wpisz kwoty z tabeli w poz. 29 i 30. Poz. 30 jest przed limitem odliczenia. Aplikacja obejmuje tylko IBKR i nie oblicza poz. 31–32 dotyczących walut wirtualnych.",
        "pdf_sec_c_header": "Sekcja C PIT-38",
        "pdf_sec_g_header": "Sekcja G PIT-38 (odliczenie podatku zagranicznego)",
        "pdf_amount_header": "Kwota (PLN)",
        "pdf_ledger_header": "Szczegółowy rejestr FIFO i kursów NBP",
    }
}

IMPORT_ROLE_LABELS = {
    "EN": {
        "date": "date",
        "transaction_type": "transaction type / action",
        "symbol": "ticker / symbol",
        "description": "description",
        "quantity": "quantity",
        "price": "price",
        "currency": "currency",
        "commission": "commission / fee",
        "gross_amount": "gross or income amount",
        "net_amount": "net amount",
        "flag": "IBKR flag",
    },
    "PL": {
        "date": "datę",
        "transaction_type": "typ operacji",
        "symbol": "ticker / symbol",
        "description": "opis",
        "quantity": "ilość",
        "price": "cenę",
        "currency": "walutę",
        "commission": "prowizję / opłatę",
        "gross_amount": "kwotę brutto lub przychodu",
        "net_amount": "kwotę netto",
        "flag": "oznaczenie IBKR",
    },
}


def import_issue_text(issue, translation, lang_code):
    role = IMPORT_ROLE_LABELS[lang_code].get(issue.get("Role"), "")
    code = issue.get("Code")
    if code == "select_required_column":
        return translation["import_issue_select_required"].format(field=role)
    if code == "ambiguous_column":
        return translation["import_issue_ambiguous"].format(field=role)
    if code == "trade_column_missing":
        return translation["import_issue_trade_column"].format(field=role)
    if code == "income_amount_missing":
        return translation["import_issue_income_amount"]
    if code == "withholding_amount_missing":
        if issue.get("Row") is not None:
            return translation["import_issue_row_amount_missing"].format(row=issue.get("Row"))
        return translation["import_issue_withholding_amount"]
    if code == "withholding_description_missing":
        return translation["import_issue_withholding_description"]
    if code == "bad_dates":
        return translation["import_issue_bad_dates"].format(
            count=issue.get("Count", 0),
            rows=", ".join(map(str, issue.get("Rows", [])[:10])),
        )
    if code == "row_symbol_missing":
        return translation["import_issue_row_symbol"].format(
            row=issue.get("Row"), action=str(issue.get("Action", "transaction")).lower(),
        )
    if code == "row_value_missing":
        return translation["import_issue_row_value"].format(
            row=issue.get("Row"),
            action=str(issue.get("Action", "transaction")).lower(),
            field=role,
        )
    if code == "row_value_invalid":
        return translation["import_issue_row_value_invalid"].format(
            row=issue.get("Row"), field=role,
        )
    if code == "row_amount_missing":
        return translation["import_issue_row_amount_missing"].format(row=issue.get("Row"))
    if code == "row_amount_invalid":
        return translation["import_issue_row_amount_invalid"].format(row=issue.get("Row"))
    if code == "row_currency_invalid":
        return translation["import_issue_row_currency"].format(row=issue.get("Row"))
    if code == "gross_amount_mismatch":
        return translation["import_issue_gross_mismatch"].format(row=issue.get("Row"))
    if code == "net_amount_mismatch":
        return translation["import_issue_net_mismatch"].format(row=issue.get("Row"))
    if code == "unknown_transaction_types":
        return translation["import_issue_unknown_types"].format(count=issue.get("Count", 0))
    if code == "mapped_column_missing":
        return translation["import_issue_mapping_missing"].format(field=role)
    return issue.get("Issue", "")


def render_ai_chat(kind, context, translation):
    """Render a consent-gated, session-only chat grounded in a whitelisted summary."""
    api_key = _OPENROUTER_CONFIG.get("api_key", "")
    if not api_key:
        st.caption(translation["ai_not_configured"])
        return

    signature = hashlib.sha256(
        json.dumps(context, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]
    prefix = f"ai_{kind}_{signature}"
    history_key = f"{prefix}_messages"
    active_signature_key = f"ai_{kind}_active_signature"
    previous_signature = st.session_state.get(active_signature_key)
    if previous_signature and previous_signature != signature:
        st.session_state.pop(f"ai_{kind}_{previous_signature}_messages", None)
    st.session_state[active_signature_key] = signature

    st.caption(translation[f"ai_{kind}_privacy"])
    consent = st.checkbox(translation[f"ai_{kind}_consent"], key=f"{prefix}_consent")
    st.caption(translation["ai_disclaimer"])
    st.caption(translation[f"ai_{kind}_examples"])

    history = st.session_state.get(history_key, [])
    for message in history:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    if history and st.button(translation["ai_clear_chat"], key=f"{prefix}_clear"):
        st.session_state.pop(history_key, None)
        st.rerun()

    prompt = st.chat_input(
        translation[f"ai_{kind}_placeholder"],
        key=f"{prefix}_input",
        max_chars=1200,
        disabled=not consent,
    )
    if prompt and prompt.strip():
        conversation = history + [{"role": "user", "content": prompt.strip()[:1200]}]
        try:
            with st.spinner(translation["ai_loading"]):
                answer = request_explanation(
                    context,
                    api_key=api_key,
                    model=_OPENROUTER_CONFIG.get("model", "openrouter/free"),
                    conversation=conversation,
                )
        except OpenRouterExplanationError as exc:
            st.error(str(exc))
        else:
            st.session_state[history_key] = (
                conversation + [{"role": "assistant", "content": answer}]
            )[-8:]
            st.rerun()


def clean_pdf_text(text: str) -> str:
    replacements = {
        'ą': 'a', 'Ć': 'C', 'ć': 'c', 'Ę': 'E', 'ę': 'e',
        'Ł': 'L', 'ł': 'l', 'Ń': 'N', 'ń': 'n', 'Ó': 'O',
        'ó': 'o', 'Ś': 'S', 'ś': 's', 'Ź': 'Z', 'ź': 'z',
        'Ż': 'Z', 'ż': 'z'
    }
    for orig, repl in replacements.items():
        text = text.replace(orig, repl)
    return text


def _audit_table(data, widths, font_size=6):
    if not data:
        return None
    header_style = ParagraphStyle(
        f"AuditHeader{font_size}", fontName="Helvetica-Bold", fontSize=font_size,
        leading=font_size + 1, textColor=colors.white,
    )
    cell_style = ParagraphStyle(
        f"AuditCell{font_size}", fontName="Helvetica", fontSize=font_size,
        leading=font_size + 1,
    )
    formatted = [
        [Paragraph(_pdf_text(value), header_style if row_index == 0 else cell_style) for value in row]
        for row_index, row in enumerate(data)
    ]
    table = Table(formatted, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#64748b")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("LEADING", (0, 0), (-1, -1), font_size + 1),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#d8e0e8")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f8")]),
    ]))
    return table


def _pdf_text(value):
    return escape(clean_pdf_text(str(value if value is not None else "—")))


def _append_audit_context(story, calc_data, styles, lang_code="EN"):
    section_g = calc_data.get("PIT38_Sekcja_G_Art30a", {})
    credit_paid = section_g.get("Podatek_zagraniczny_pobrany_Art30a_PLN", 0.0)
    credit_allowed = section_g.get("Podatek_zagraniczny_uznany_Poz_48", 0.0)
    credit_uncredited = section_g.get("Podatek_zagraniczny_nierozliczony_PLN", 0.0)
    polish_passive_tax = section_g.get("Podatek_Art30a_Poz_47", 0.0)

    story.append(Paragraph("Foreign tax credit cap audit", styles["Heading2"]))
    story.append(Paragraph(
        _pdf_text(
            "Poz. 48 is limited to the Polish tax on the same foreign passive income by source country, "
            "and cannot exceed Poz. 47. Any remaining foreign tax is shown as uncredited, not added to Polish tax due."
        ),
        styles["Normal"],
    ))
    credit_data = [
        ["Poz. 47 Polish tax", "Foreign tax paid", "Poz. 48 allowed credit", "Not credited"],
        [f"{polish_passive_tax:.2f} PLN", f"{credit_paid:.2f} PLN", f"{credit_allowed:.2f} PLN", f"{credit_uncredited:.2f} PLN"],
    ]
    story.append(_audit_table(credit_data, [130, 130, 150, 100], font_size=7))
    story.append(Spacer(1, 7))

    reconciliation = calc_data.get("Account_Reconciliation", {})
    story.append(Paragraph("IBKR account unit reconciliation", styles["Heading2"]))
    story.append(Paragraph(_pdf_text(f"Status: {reconciliation.get('Status', 'Not available')}. {reconciliation.get('Formula', '')}"), styles["Normal"]))
    recon_rows = reconciliation.get("Rows", [])
    if recon_rows:
        headers = [
            "Ticker", "Opening", "Buys", "Sells", "Transfer in", "Transfer out",
            "Corporate action Δ", "Calculated close", "IBKR close", "Difference", "Status",
        ]
        fields = [
            "Ticker", "Opening units", "Buys", "Sells", "Transfers in", "Transfers out",
            "Corporate action units (net)", "Calculated closing units", "IBKR closing units",
            "Difference (calculated - IBKR)", "Status",
        ]
        recon_table = [headers]
        for row in recon_rows:
            recon_table.append([row.get(field) for field in fields])
        story.append(_audit_table(recon_table, [38, 46, 42, 42, 48, 48, 62, 62, 54, 52, 72], font_size=5.5))
    else:
        story.append(Paragraph("No security unit activity was found in this file.", styles["Normal"]))

    unit_events = reconciliation.get("Unit events", [])
    if unit_events:
        event_table = [["Date", "Ticker", "Event", "Units", "Description", "Included"]]
        for event in unit_events:
            event_table.append([
                event.get("Date"),
                event.get("Ticker"),
                event.get("Event"),
                event.get("Quantity"),
                event.get("Description"),
                event.get("Included"),
            ])
        story.append(Spacer(1, 5))
        story.append(_audit_table(event_table, [50, 45, 85, 48, 300, 45], font_size=5.5))
    story.append(Spacer(1, 7))

    notes = list(dict.fromkeys(calc_data.get("Review_Notes", []) + reconciliation.get("Issues", [])))
    story.append(Paragraph("Unresolved assumptions and review notes", styles["Heading2"]))
    if notes:
        for note in notes:
            story.append(Paragraph("• " + _pdf_text(note), styles["Normal"]))
    else:
        story.append(Paragraph("None recorded by the importer.", styles["Normal"]))
    story.append(Spacer(1, 8))

def generate_pdf(calc_data, tax_year, loss_cf, lang_code="EN"):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), rightMargin=28, leftMargin=28, topMargin=30, bottomMargin=30)
    story = []
    styles = getSampleStyleSheet()
    t = TRANSLATIONS[lang_code]
    
    title_style = ParagraphStyle(
        'TitleStyle',
        parent=styles['Heading1'],
        fontSize=16,
        textColor=colors.HexColor('#17212f'),
        spaceAfter=4
    )
    story.append(Paragraph(clean_pdf_text(t["pdf_title"].format(year=tax_year)), title_style))
    story.append(Paragraph(clean_pdf_text(t["pdf_sub"].format(loss=loss_cf)), styles['Normal']))
    story.append(Paragraph(clean_pdf_text(t["summary_pdf_disclaimer"]), styles["Italic"]))
    story.append(Paragraph(clean_pdf_text(t["ibkr_section_c_note"]), styles["Normal"]))
    story.append(Spacer(1, 8))
    
    if lang_code == "PL":
        summary_labels = {
            "proceeds": "Przychody z odpłatnego zbycia (poz. 22)",
            "costs": "Koszty uzyskania przychodów (poz. 23)",
            "total_proceeds": "Przychody razem (poz. 26)",
            "total_costs": "Koszty razem (poz. 27)",
            "income": "Dochód (poz. 28)",
            "current_loss": "Strata z bieżącego roku (poz. 29)",
            "prior_loss": "Odliczona strata z lat ubiegłych (poz. 30)",
            "tax_base": "Podstawa opodatkowania (poz. 31)",
            "capital_tax": "Podatek Art. 30b (poz. 33)",
            "foreign_tax": "Podatek zagraniczny (poz. 34)",
            "tax_due": "Podatek należny (poz. 35)",
            "interest": "Odsetki brutto",
            "dividends": "Dywidendy brutto",
            "polish_flat_tax": "Podatek polski (poz. 47)",
            "passive_foreign_tax": "Podatek zagraniczny od dochodów pasywnych",
            "allowed_credit": "Uznane odliczenie podatku zagranicznego (poz. 48)",
            "passive_due": "Podatek do zapłaty (poz. 49)",
            "uncredited_tax": "Nieodliczony podatek zagraniczny",
            "estimate": "Szacunkowa kwota tylko z IBKR (poz. 35 + 49)",
        }
    else:
        summary_labels = {
            "proceeds": "Other proceeds (Poz. 22)",
            "costs": "Deductible costs (Poz. 23)",
            "total_proceeds": "Total proceeds (Poz. 26)",
            "total_costs": "Total costs (Poz. 27)",
            "income": "Income (Poz. 28)",
            "current_loss": "Current-year loss (Poz. 29)",
            "prior_loss": "Prior-year loss deducted (Poz. 30)",
            "tax_base": "Tax base (Poz. 31)",
            "capital_tax": "Art. 30b tax (Poz. 33)",
            "foreign_tax": "Foreign tax paid (Poz. 34)",
            "tax_due": "Tax due (Poz. 35)",
            "interest": "Interest gross (PLN)",
            "dividends": "Dividends gross (PLN)",
            "polish_flat_tax": "Polish flat tax (Poz. 47)",
            "passive_foreign_tax": "Foreign passive tax paid (Art. 30a)",
            "allowed_credit": "Allowed foreign tax credit (Poz. 48)",
            "passive_due": "Difference due (Poz. 49)",
            "uncredited_tax": "Unallocated foreign tax not credited",
            "estimate": "IBKR-only illustrative tax estimate (Poz. 35 + 49)",
        }

    sec_c = calc_data["PIT38_Sekcja_C"]
    c_data = [
        [clean_pdf_text(t["pdf_sec_c_header"]), clean_pdf_text(t["pdf_amount_header"])],
        [clean_pdf_text(summary_labels["proceeds"]), f"{sec_c['Inne_przychody_Poz_22']:.2f}"],
        [clean_pdf_text(summary_labels["costs"]), f"{sec_c['Koszty_uzyskania_Poz_23']:.2f}"],
        [clean_pdf_text(summary_labels["total_proceeds"]), f"{sec_c['Przychody_Razem_Poz_26']:.2f}"],
        [clean_pdf_text(summary_labels["total_costs"]), f"{sec_c['Koszty_Razem_Poz_27']:.2f}"],
        [clean_pdf_text(summary_labels["income"]), f"{sec_c['Dochod_Poz_28']:.2f}"],
        [clean_pdf_text(summary_labels["current_loss"]), f"{sec_c['Strata_Poz_29']:.2f}"],
        [clean_pdf_text(summary_labels["prior_loss"]), f"{sec_c['Strata_z_lat_ubieglych_Poz_30']:.2f}"],
        [clean_pdf_text(summary_labels["tax_base"]), f"{sec_c['Podstawa_Poz_31']:.2f}"],
        [clean_pdf_text(summary_labels["capital_tax"]), f"{sec_c['Podatek_Poz_33']:.2f}"],
        [clean_pdf_text(summary_labels["foreign_tax"]), f"{sec_c['Podatek_zagraniczny_Poz_34']:.2f}"],
        [clean_pdf_text(summary_labels["tax_due"]), f"{sec_c['Podatek_nalezny_Poz_35']:.2f}"],
    ]
    t1 = Table(c_data, colWidths=[590, 150])
    t1.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#64748b')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.HexColor('#ffffff')),
        ('ALIGN', (0,0), (0,-1), 'LEFT'),
        ('ALIGN', (1,1), (1,-1), 'RIGHT'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTNAME', (1,1), (1,-1), 'Helvetica-Bold'),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f4f6f8')]),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#e1e6ec')),
    ]))
    story.append(t1)
    story.append(Spacer(1, 6))

    sec_g = calc_data["PIT38_Sekcja_G_Art30a"]
    g_data = [
        [clean_pdf_text(t["pdf_sec_g_header"]), clean_pdf_text(t["pdf_amount_header"])],
        [clean_pdf_text(summary_labels["interest"]), f"{sec_g['Odsetki_brutto_PLN']:.2f}"],
        [clean_pdf_text(summary_labels["dividends"]), f"{sec_g['Dywidendy_brutto_PLN']:.2f}"],
        [clean_pdf_text(summary_labels["polish_flat_tax"]), f"{sec_g['Podatek_Art30a_Poz_47']:.2f}"],
        [clean_pdf_text(summary_labels["passive_foreign_tax"]), f"{sec_g['Podatek_zagraniczny_pobrany_Art30a_PLN']:.2f}"],
        [clean_pdf_text(summary_labels["allowed_credit"]), f"{sec_g['Podatek_zagraniczny_uznany_Poz_48']:.2f}"],
        [clean_pdf_text(summary_labels["passive_due"]), f"{sec_g['Roznica_Poz_49']:.2f}"],
        [clean_pdf_text(summary_labels["uncredited_tax"]), f"{sec_g['Podatek_zagraniczny_nierozliczony_PLN']:.2f}"],
        [clean_pdf_text(summary_labels["estimate"]), f"{calc_data['IBKR_Only_Illustrative_Tax_Estimate_Poz_35_and_49']:.2f}"],
    ]
    t2 = Table(g_data, colWidths=[590, 150])
    t2.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#64748b')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.HexColor('#ffffff')),
        ('ALIGN', (0,0), (0,-1), 'LEFT'),
        ('ALIGN', (1,1), (1,-1), 'RIGHT'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTNAME', (1,1), (1,-1), 'Helvetica-Bold'),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f4f6f8')]),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#e1e6ec')),
    ]))
    story.append(t2)
    story.append(Spacer(1, 4))
    story.append(Paragraph(clean_pdf_text(t["ibkr_section_g_note"]), styles["Normal"]))
    story.append(Paragraph(clean_pdf_text(t["summary_pdf_scope"]), styles["Italic"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(clean_pdf_text(t["pdf_zg_heading"]), styles["Heading2"]))
    story.append(Paragraph(clean_pdf_text(t["pdf_zg_note"]), styles["Normal"]))
    pit_zg_rows = [[
        clean_pdf_text(t["pdf_zg_country"]),
        clean_pdf_text(t["pdf_zg_country_code"]),
        clean_pdf_text(t["pdf_zg_pos29"]),
        clean_pdf_text(t["pdf_zg_pos30"]),
    ]]
    for item in calc_data.get("PIT_ZG_Position_Summary", []):
        country_code = str(item.get("Kraj", "")).upper()
        pit_zg_rows.append([
            clean_pdf_text(t["pit_zg_country_name_prompt"].format(country=country_code)),
            clean_pdf_text(country_code),
            f"{float(item.get('Poz_29_Dochod_Art30b_5a_5b_PLN', 0.0)):.2f}",
            f"{float(item.get('Poz_30_Podatek_zagraniczny_od_Poz_29_PLN', 0.0)):.2f}",
        ])
    if len(pit_zg_rows) == 1:
        pit_zg_rows.append([clean_pdf_text(t["pit_zg_no_positions"]), "—", "—", "—"])
    t3 = Table(pit_zg_rows, colWidths=[175, 75, 265, 265])
    t3.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#64748b')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f4f6f8')]),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e1e6ec')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    story.append(t3)
    story.append(Spacer(1, 8))
    
    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()

def generate_csv(calc_data):
    return _generate_csv(calc_data)


def generate_steps_csv(calc_data, lang_code="EN"):
    return _generate_steps_csv(calc_data, lang_code=lang_code)

with st.sidebar:
    st.markdown(
        '<div class="brand-lockup"><div class="brand-mark">FC</div>'
        '<div class="brand-name">FOREIGN CAPITAL GAINS ·<br>PL TAX SUPPORT</div></div>',
        unsafe_allow_html=True,
    )
    temp_lang_choice = st.radio("Choose Language / Wybierz Język", ["English", "Polski"], index=0, label_visibility="collapsed", key="language_choice")
    lang_code = "EN" if temp_lang_choice == "English" else "PL"
    st.session_state.lang_code = lang_code
    t = TRANSLATIONS[lang_code]

    st.markdown(f'<p class="sidebar-section-label">{t["locale_header"]}</p>', unsafe_allow_html=True)

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown(f'<p class="sidebar-section-label">{t["config_header"]}</p>', unsafe_allow_html=True)
    
    tax_year = st.number_input(t["target_year"], min_value=2020, max_value=2030, value=2026, step=1, key="tax_year_input")
    loss_carryforward = st.number_input(t["loss_cf"], min_value=0.0, value=0.0, step=100.0, format="%.2f", key="loss_cf_input")
    loss_carryforward_verified = st.checkbox(t["loss_cf_verified"], key="loss_cf_verified_input")
    cash_currency_choice = st.selectbox(
        t["cash_currency"],
        ["Select currency...", "EUR", "USD", "GBP", "CHF", "PLN", "AUD", "CAD", "JPY", "SEK", "NOK", "DKK", "CZK", "HUF", "CNH", "ILS", "KRW", "MYR", "MXN", "NZD", "SAR", "SGD", "ZAR", "TRY", "AED"],
        key="cash_currency_input",
        help=t["cash_currency_help"],
    )
    cash_country_choice = st.selectbox(
        t["cash_country"],
        ["Select country...", "IE", "US", "GB", "DE", "FR", "NL", "LU", "CH", "BE", "ES", "IT", "PL", "SE", "NO", "DK", "FI", "AT", "PT", "CA", "AU", "JP", "SG", "HK"],
        key="cash_country_input",
        help=t["cash_country_help"],
    )
    source_country_text = st.text_input(
        t["source_countries"],
        value="",
        key="source_country_input",
        help=t["source_countries_help"],
    )

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown(f'<p class="sidebar-section-label">{t["reconciliation_header"]}</p>', unsafe_allow_html=True)
    opening_units_text = st.text_area(
        t["opening_units"],
        key="opening_units_input",
        height=70,
        placeholder="EGLN=94\nIWDA=12.5",
        help=t["reconciliation_units_help"],
    )
    closing_units_text = st.text_area(
        t["closing_units"],
        key="closing_units_input",
        height=70,
        placeholder="EGLN=91\nIWDA=12.5",
        help=t["reconciliation_units_help"],
    )
    assume_zero_opening = st.checkbox(t["assume_zero_opening"], key="assume_zero_opening_input")

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown(f'<p class="sidebar-section-label">{t["file_header"]}</p>', unsafe_allow_html=True)
    uploaded_file = st.file_uploader(t["upload_label"], type=["csv"], label_visibility="collapsed", key="ibkr_csv_upload")
    st.caption(t["upload_help"])
    st.download_button(
        t["download_upload_template"],
        data=IBKR_UPLOAD_TEMPLATE_CSV,
        file_name="IBKR_transaction_upload_template.csv",
        mime="text/csv",
        help=t["upload_template_help"],
        use_container_width=True,
        key="download_ibkr_upload_template",
    )

    source_file_bytes = uploaded_file.getvalue() if uploaded_file is not None else None
    selected_cash_currency = None if cash_currency_choice.startswith("Select ") else cash_currency_choice
    selected_cash_country = None if cash_country_choice.startswith("Select ") else cash_country_choice
    source_country_mapping_count = sum(
        1 for item in re.split(r"[,;\n]+", source_country_text) if "=" in item and item.split("=", 1)[0].strip()
    )
    ai_selected_settings = {
        "target_tax_year": int(tax_year),
        "cash_currency_fallback": selected_cash_currency,
        "cash_interest_source_country": selected_cash_country,
        "prior_year_loss_entered": bool(loss_carryforward),
        "prior_year_loss_verified": bool(loss_carryforward_verified),
        "security_country_mapping_count": source_country_mapping_count,
        "opening_units_provided": bool(opening_units_text.strip()),
        "closing_units_provided": bool(closing_units_text.strip()),
        "assume_zero_opening_units": bool(assume_zero_opening),
    }
    selected_column_mapping = None
    import_check = None
    unrecognized_activity_acknowledged = True
    import_mapping_confirmed = True
    if uploaded_file is not None:
        if inspect_ibkr_csv is None:
            st.error("Import inspection is unavailable because ibkr_parser.py could not be imported.")
            import_check = {"status": "error", "error": "Parser unavailable."}
        else:
            initial_check = inspect_ibkr_csv(source_file_bytes, cash_currency=selected_cash_currency)
            if initial_check.get("status") == "error":
                import_check = initial_check
                st.error(initial_check.get("error", "The file could not be inspected."))
            else:
                file_key = hashlib.sha256(source_file_bytes).hexdigest()[:16]
                role_labels = IMPORT_ROLE_LABELS[lang_code]
                with st.expander(t["import_check_title"], expanded=True):
                    st.caption(t["import_format"].format(format=initial_check["source_format"]))
                    st.caption(t["import_rows"].format(count=initial_check["row_count"]))
                    show_column_mapping = st.checkbox(
                        t["import_mapping_title"],
                        value=bool(initial_check.get("blocking_issues")),
                        key=f"ibkr_show_mapping_{file_key}",
                    )
                    selected_column_mapping = {}
                    if show_column_mapping:
                        for role, suggestion in initial_check["suggestions"].items():
                            options = [None] + initial_check["columns"]
                            suggested = suggestion.get("suggested")
                            index = options.index(suggested) if suggested in options else 0
                            selected_column_mapping[role] = st.selectbox(
                                role_labels[role],
                                options,
                                index=index,
                                format_func=lambda value, required=suggestion["required"]: (
                                    t["import_select_column"] if value is None and required
                                    else t["import_not_present"] if value is None
                                    else value
                                ),
                                key=f"ibkr_column_{file_key}_{role}",
                            )
                    else:
                        selected_column_mapping = {
                            role: suggestion.get("suggested")
                            for role, suggestion in initial_check["suggestions"].items()
                        }

                    import_check = inspect_ibkr_csv(
                        source_file_bytes,
                        selected_column_mapping,
                        cash_currency=selected_cash_currency,
                    )
                    mapped_rows = [{
                        "Field": role_labels[role],
                        "CSV column": column if column is not None else t["import_not_present"],
                    } for role, column in import_check.get("column_mapping", {}).items()]
                    st.dataframe(pd.DataFrame(mapped_rows), use_container_width=True, hide_index=True)

                    if import_check.get("action_counts"):
                        type_counts = pd.DataFrame(import_check["action_counts"]).rename(columns={
                            "Transaction type": t["import_type_column"],
                            "Rows": t["import_type_rows"],
                        })
                        st.markdown(f"**{t['import_types_title']}**")
                        st.dataframe(type_counts, use_container_width=True, hide_index=True)

                    if import_check.get("excluded_activity_summary"):
                        excluded_counts = pd.DataFrame([
                            {"Category": category, "Rows": count}
                            for category, count in import_check["excluded_activity_summary"].items()
                        ]).rename(columns={
                            "Category": t["import_type_column"],
                            "Rows": t["import_type_rows"],
                        })
                        st.markdown(f"**{t['import_excluded_title']}**")
                        st.dataframe(excluded_counts, use_container_width=True, hide_index=True)

                    if import_check.get("issues"):
                        st.markdown(f"**{t['import_issues_title']}**")
                        for issue in import_check["issues"]:
                            message = import_issue_text(issue, t, lang_code)
                            if issue.get("Severity") in {"Error", "Required", "Required for trades", "Required for income", "Required for withholding"}:
                                st.error(message)
                            else:
                                st.warning(message)
                    elif not import_check.get("blocking_issues"):
                        st.success(t["import_check_ready"])

                    if import_check.get("unknown_rows"):
                        st.dataframe(pd.DataFrame(import_check["unknown_rows"]), use_container_width=True, hide_index=True)
                        unrecognized_activity_acknowledged = st.checkbox(
                            t["import_ack_unknown"].format(count=len(import_check["unknown_rows"])),
                            key=f"ibkr_unknown_ack_{file_key}",
                        )

                    if import_check.get("preview") is not None and not import_check["preview"].empty:
                        st.markdown(f"**{t['import_preview_title']}**")
                        st.dataframe(import_check["preview"], use_container_width=True, hide_index=True, height=180)

                    mapping_signature = hashlib.sha256(
                        json.dumps(selected_column_mapping, sort_keys=True, ensure_ascii=False).encode("utf-8")
                    ).hexdigest()[:10]
                    import_mapping_confirmed = st.checkbox(
                        t["import_ack_mapping"],
                        key=f"ibkr_mapping_confirm_{file_key}_{mapping_signature}",
                    )
                    if import_check.get("blocking_issues"):
                        st.warning(t["import_check_blocked"])

                    with st.container(key="ai_import_heading"):
                        st.markdown(
                            f'<div class="ai-section-heading"><span class="ai-section-icon" aria-hidden="true">'
                            '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">'
                            '<path d="M12 3.5v4M12 16.5v4M3.5 12h4M16.5 12h4M5.99 5.99l2.83 2.83m6.36 6.36 2.83 2.83m0-12.02-2.83 2.83m-6.36 6.36-2.83 2.83" '
                            'stroke="#475569" stroke-width="1.8" stroke-linecap="round"/>'
                            '</svg></span><strong>' + escape(t["ai_import_title"]) + '</strong></div>',
                            unsafe_allow_html=True,
                        )
                    render_ai_chat(
                        "import",
                        build_import_explanation_context(import_check, lang_code, settings=ai_selected_settings),
                        t,
                    )

    import_input_signature = None
    if source_file_bytes is not None:
        import_input_signature = hashlib.sha256(json.dumps({
            "file_sha256": hashlib.sha256(source_file_bytes).hexdigest(),
            "column_mapping": selected_column_mapping,
            "tax_year": int(tax_year),
            "loss_carryforward": float(loss_carryforward),
            "loss_carryforward_verified": bool(loss_carryforward_verified),
            "cash_currency": selected_cash_currency,
            "cash_country": None if cash_country_choice.startswith("Select ") else cash_country_choice,
            "source_country_mapping": source_country_text.strip(),
            "opening_units": opening_units_text.strip(),
            "closing_units": closing_units_text.strip(),
            "assume_zero_opening_units": bool(assume_zero_opening),
        }, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        stored_signature = st.session_state.get("calculated_source_signature")
        if st.session_state.get("calculated_data") is not None and stored_signature != import_input_signature:
            st.session_state.calculated_data = None
            st.session_state.audit_metadata = None
            st.session_state.audit_source_bytes = None
            st.session_state.calculated_source_signature = None
            st.info(t["import_previous_result_cleared"])

    st.markdown("<div style='height: 4px;'></div>", unsafe_allow_html=True)
    import_not_ready = bool(
        uploaded_file is not None and (
            import_check is None
            or import_check.get("status") == "error"
            or import_check.get("blocking_issues")
            or not import_mapping_confirmed
            or (import_check.get("unknown_rows") and not unrecognized_activity_acknowledged)
        )
    )
    if st.button(
        t["btn_calculate"],
        type="primary",
        use_container_width=True,
        disabled=import_not_ready,
    ):
        if uploaded_file is not None:
            if process_and_calculate_file is None:
                st.markdown(f'<div class="custom-warning-box">Error: ibkr_parser.py could not be imported.</div>', unsafe_allow_html=True)
            else:
                st.session_state.tax_year_used = tax_year
                st.session_state.loss_cf_used = loss_carryforward
                source_country_map = {}
                invalid_country_pairs = []
                for pair in re.split(r"[,;\n]+", source_country_text):
                    pair = pair.strip()
                    if not pair:
                        continue
                    if "=" not in pair:
                        invalid_country_pairs.append(pair)
                        continue
                    symbol, country = (part.strip().upper() for part in pair.split("=", 1))
                    if not symbol or not country:
                        invalid_country_pairs.append(pair)
                    else:
                        source_country_map[symbol] = country
                if invalid_country_pairs:
                    st.error("Invalid source-country mapping. Use TICKER=ISO country pairs, such as EGLN=IE.")
                    st.stop()
                try:
                    opening_units_map = parse_symbol_quantities(opening_units_text, "Opening units")
                    closing_units_map = parse_symbol_quantities(closing_units_text, "IBKR closing units")
                except ValueError as exc:
                    st.error(str(exc))
                    st.stop()
                
                with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp_file:
                    tmp_file.write(source_file_bytes)
                    tmp_path = tmp_file.name

                with st.spinner("Processing transaction ledger & querying NBP exchange rates..."):
                    try:
                        result_json_str = process_and_calculate_file(
                            file_path=tmp_path,
                            tax_year=int(tax_year),
                            loss_carryforward=float(loss_carryforward),
                            loss_carryforward_verified=bool(loss_carryforward_verified),
                            cash_currency=None if cash_currency_choice.startswith("Select ") else cash_currency_choice,
                            cash_country=None if cash_country_choice.startswith("Select ") else cash_country_choice,
                            source_country_by_symbol=source_country_map,
                            opening_units_by_symbol=opening_units_map,
                            ibkr_closing_units_by_symbol=closing_units_map,
                            assume_zero_opening_units=assume_zero_opening,
                            column_mapping=selected_column_mapping,
                        )
                        result_data = json.loads(result_json_str)
                        audit_metadata = {
                            "source_file": uploaded_file.name,
                            "source_sha256": hashlib.sha256(source_file_bytes).hexdigest(),
                            "source_file_bytes": len(source_file_bytes),
                            "generated_at": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat(timespec="seconds"),
                            "tax_year": int(tax_year),
                            "result_status": result_data.get("status"),
                            "processed_records": result_data.get("processed_records", 0),
                            "source_format": result_data.get("source_format", "unknown"),
                            "excluded_activity_summary": result_data.get("excluded_activity_summary", {}),
                            "column_mapping": ", ".join(
                                f"{role}={column if column is not None else 'not present'}"
                                for role, column in (selected_column_mapping or {}).items()
                            ),
                            "currency_country_settings": (
                                f"waluta gotowkowa: {cash_currency_choice if not cash_currency_choice.startswith('Select ') else 'nieustawiona'}; "
                                f"kraj odsetek: {cash_country_choice if not cash_country_choice.startswith('Select ') else 'nieustawiony'}"
                            ),
                            "source_country_mapping": ", ".join(
                                f"{symbol}={country}" for symbol, country in sorted(source_country_map.items())
                            ) or "brak mapowania",
                            "unit_snapshot_settings": (
                                f"otwarcie: {opening_units_text.strip() or 'brak'}; "
                                f"zamkniecie IBKR: {closing_units_text.strip() or 'brak'}; "
                                f"zero dla niewymienionych: {'tak' if assume_zero_opening else 'nie'}"
                            ),
                            "loss_carryforward_settings": (
                                f"kwota: {float(loss_carryforward):.2f} PLN; "
                                f"potwierdzona przez uzytkownika: {'tak' if loss_carryforward_verified else 'nie'}"
                            ),
                        }
                        st.session_state.audit_metadata = audit_metadata
                        st.session_state.audit_source_bytes = source_file_bytes
                        st.session_state.calculated_source_signature = import_input_signature
                        if "pit38_calculation" in result_data:
                            result_data["pit38_calculation"]["Audit_Metadata"] = audit_metadata
                        st.session_state.calculated_data = result_data
                    except Exception as e:
                        st.markdown(f'<div class="custom-warning-box">Execution Error: {str(e)}</div>', unsafe_allow_html=True)
                    finally:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
        else:
            st.markdown(f'<div class="custom-warning-box">{t["msg_upload_first"]}</div>', unsafe_allow_html=True)

# Fallback translation context if sidebar is skipped
if 'temp_lang_choice' not in locals():
    lang_code = "EN"
    t = TRANSLATIONS[lang_code]

# Product hero follows the same typography and badge treatment as Investment Copilot.
status_badges = "".join(
    f"<span class='tax-status-badge'><span class='tax-status-dot'></span>{label}</span>"
    for label in (t["badge1"], t["badge2"], t["badge3"])
)
hero_html = (
    f'<div class="portfolio-hero"><div><div class="terminal-tag">POLISH TAX WORKSPACE</div>'
    f'<h1>{t["title"]}</h1><div class="hero-subtitle">{t["subtitle"]}</div></div>'
    '<div class="hero-badge">PIT-38 · PIT-ZG</div></div>'
    f'<div class="tax-badge-row">{status_badges}</div>'
)
st.markdown(hero_html, unsafe_allow_html=True)

if st.session_state.calculated_data is not None:
    data = st.session_state.calculated_data
    
    if "error" in data:
        st.markdown(f'<div class="custom-warning-box">{data["error"]}</div>', unsafe_allow_html=True)
        for warning in data.get("warnings", []):
            st.warning(warning)
        error_reconciliation = data.get("account_reconciliation")
        if error_reconciliation and error_reconciliation.get("Rows"):
            st.markdown("#### Account unit reconciliation")
            st.caption(error_reconciliation.get("Formula", ""))
            st.warning(f"Reconciliation status: {error_reconciliation.get('Status', 'Unknown')}")
            st.dataframe(pd.DataFrame(error_reconciliation["Rows"]), use_container_width=True)
    else:
        if data.get("status") == "needs_review":
            st.warning("Calculation completed with unresolved assumptions. These figures are working results, not filing-ready.")
        else:
            st.markdown(f'<div class="custom-success-box">{t["success_msg"].format(count=data["processed_records"])}</div>', unsafe_allow_html=True)

        calc = data["pit38_calculation"]
        active_year = st.session_state.tax_year_used
        active_loss_cf = st.session_state.loss_cf_used

        st.markdown(
            f'<div class="ai-section-heading ai-results-heading"><span class="ai-section-icon" aria-hidden="true">'
            '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">'
            '<path d="M4 19.5h16M6.5 16V11m5 5V5m5 11v-7" stroke="#475569" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'
            '</svg></span><strong>' + escape(t["ai_results_title"]) + '</strong></div>',
            unsafe_allow_html=True,
        )
        with st.expander(
            t["ai_results_open"],
            key="ai_results_expander",
            expanded=False,
        ):
            render_ai_chat(
                "results",
                build_calculation_explanation_context(data, lang_code, settings=ai_selected_settings),
                t,
            )
        
        tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
            t["tab_sec_c"], 
            t["tab_sec_g"], 
            t["tab_pit_zg"], 
            t["tab_ledger"], 
            t["tab_warnings"],
            t["tab_reconciliation"],
        ])
        
        with tab1:
            st.markdown(f"#### {t['sec_c_title'].format(year=active_year)}")
            st.info(t["ibkr_section_c_note"])
            sec_c = calc["PIT38_Sekcja_C"]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric(t["rev_poz22"], f"{sec_c['Inne_przychody_Poz_22']:,.2f} PLN")
            c1.metric(t["cost_poz23"], f"{sec_c['Koszty_uzyskania_Poz_23']:,.2f} PLN")
            c2.metric(t["inc_poz24"], f"{sec_c['Dochod_Poz_28']:,.2f} PLN")
            c2.metric(t["loss_poz25"], f"{sec_c['Strata_Poz_29']:,.2f} PLN")
            c3.metric(t["loss_poz26"], f"{sec_c['Strata_z_lat_ubieglych_Poz_30']:,.2f} PLN")
            c3.metric(t["final_inc"], f"{sec_c['Podstawa_Poz_31']:,.2f} PLN")
            c4.metric(t["capital_tax"], f"{sec_c['Podatek_Poz_33']:,.2f} PLN")
            c4.metric(t["capital_tax_due"], f"{sec_c['Podatek_nalezny_Poz_35']:,.2f} PLN")

        with tab2:
            st.markdown(f"#### {t['sec_g_title']}")
            st.info(t["ibkr_section_g_note"])
            sec_g = calc["PIT38_Sekcja_G_Art30a"]
            c1, c2, c3 = st.columns(3)
            c1.metric(t["passive_tax"], f"{sec_g['Podatek_Art30a_Poz_47']:,.2f} PLN")
            c2.metric(t["foreign_tax"], f"{sec_g['Podatek_zagraniczny_uznany_Poz_48']:,.2f} PLN")
            c3.metric(t["passive_due"], f"{sec_g['Roznica_Poz_49']:,.2f} PLN")
            st.metric(t["calculated_tax_due"], f"{calc['IBKR_Only_Illustrative_Tax_Estimate_Poz_35_and_49']:,.2f} PLN")
            if sec_g["Podatek_zagraniczny_nierozliczony_PLN"] > 0:
                st.info(t["credit_cap_explanation"])
            st.caption(
                f"Gross interest: {sec_g['Odsetki_brutto_PLN']:,.2f} PLN · "
                f"gross dividends: {sec_g['Dywidendy_brutto_PLN']:,.2f} PLN · "
                f"foreign tax paid: {sec_g['Podatek_zagraniczny_pobrany_Art30a_PLN']:,.2f} PLN · "
                f"foreign tax not credited: {sec_g['Podatek_zagraniczny_nierozliczony_PLN']:,.2f} PLN"
            )

        with tab3:
            st.markdown(f"#### {t['pit_zg_title']}")
            st.info(t["pit_zg_scope_note"])
            st.markdown(f"##### {t['pit_zg_fields_title']}")
            st.markdown(t["pit_zg_form_guide"])
            pit_zg_positions = calc.get("PIT_ZG_Position_Summary", [])
            if pit_zg_positions:
                position_rows = [{
                    t["pit_zg_country_name_column"]: t["pit_zg_country_name_prompt"].format(country=item["Kraj"]),
                    t["pit_zg_country_code_column"]: item["Kraj"],
                    t["pit_zg_pos29"]: item["Poz_29_Dochod_Art30b_5a_5b_PLN"],
                    t["pit_zg_pos30"]: item["Poz_30_Podatek_zagraniczny_od_Poz_29_PLN"],
                } for item in pit_zg_positions]
                st.dataframe(pd.DataFrame(position_rows), use_container_width=True, hide_index=True)
            else:
                st.info(t["pit_zg_no_positions"])
            st.caption(t["pit_zg_positions_note"])
            st.markdown(f"[ {t['pit_zg_form_link']} ](https://www.gov.pl/web/finanse/pitzg)")
            st.markdown(f"##### {t['pit_zg_supplement_title']}")
            st.caption(t["pit_zg_supplement_note"])
            pit_zg = calc["PIT_ZG_Attachments"]
            if pit_zg:
                for item in pit_zg:
                    st.markdown(f"**{t['pit_zg_country']}:** `{item['Kraj']}`")
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric(t["pit_zg_supp_proceeds"], f"{item['Przychod_Art30b']:,.2f} PLN")
                    c2.metric(t["pit_zg_supp_costs"], f"{item['Koszty_Art30b']:,.2f} PLN")
                    c3.metric(t["pit_zg_supp_interest"], f"{item['Odsetki_Art30a']:,.2f} PLN")
                    c4.metric(t["pit_zg_supp_dividends"], f"{item['Dywidendy_Art30a']:,.2f} PLN")
            else:
                st.info(t["no_zg"])
            for unresolved in calc.get("PIT_ZG_Unresolved", []):
                st.warning(unresolved)

        with tab4:
            st.markdown(f"#### {t['ledger_title']}")
            st.markdown(f"<p style='color: #687586; font-size: 0.85rem; margin-bottom: 12px;'>{t['ledger_sub']}</p>", unsafe_allow_html=True)
            
            st.markdown("<div style='height: 4px;'></div>", unsafe_allow_html=True)

            steps = calc.get("Calculation_Steps", [])
            if steps:
                formatted_steps = []
                for s in steps:
                    if lang_code == "PL":
                        formatted_steps.append({
                            "Rok": s["Year"],
                            "Data": s["Date"],
                            "Data NBP": s["NBP Date"],
                            "Operacja": s["Action_PL"],
                            "Ticker": s["Ticker"],
                            "Waluta": s["Curr"],
                            "Ilość": s["Qty"],
                            "Cena (FX)": s["Price (FX)"],
                            "Kurs NBP": s["NBP Rate"],
                            "Prowizja (PLN)": s["Comm (PLN)"],
                            "Przychód (PLN)": s["Revenue (PLN)"],
                            "Koszty (PLN)": s["Cost Basis (PLN)"],
                            "Dochód/Strata (PLN)": s["Net (PLN)"],
                            "Wzór & FIFO Szczegóły": s["Details_PL"]
                        })
                    else:
                        formatted_steps.append({
                            "Year": s["Year"],
                            "Date": s["Date"],
                            "NBP Date": s["NBP Date"],
                            "Action": s["Action_EN"],
                            "Ticker": s["Ticker"],
                            "Curr": s["Curr"],
                            "Qty": s["Qty"],
                            "Price (FX)": s["Price (FX)"],
                            "NBP Rate": s["NBP Rate"],
                            "Comm (PLN)": s["Comm (PLN)"],
                            "Revenue (PLN)": s["Revenue (PLN)"],
                            "Cost Basis (PLN)": s["Cost Basis (PLN)"],
                            "Net (PLN)": s["Net (PLN)"],
                            "Audit Formula & FIFO Details": s["Details_EN"]
                        })
                df_steps = pd.DataFrame(formatted_steps)
                st.dataframe(df_steps, use_container_width=True)
            else:
                st.info("No transaction calculation steps recorded.")

        with tab5:
            st.markdown("#### System Execution Logs & Validation Diagnostics")
            warnings = data.get("warnings", [])
            if warnings:
                for w in warnings:
                    st.markdown(f'<div class="custom-warning-box">{w}</div>', unsafe_allow_html=True)
            elif data.get("status") == "success":
                st.markdown(f'<div class="custom-success-box">All imported rows and filing assumptions were resolved.</div>', unsafe_allow_html=True)
            else:
                st.warning("Review unresolved source-country or loss-carryforward assumptions before using these figures.")
            excluded_rows = calc.get("Excluded_Activity", [])
            excluded_count = calc.get("Excluded_Activity_Summary", {}).get("total", len(excluded_rows))
            if excluded_rows:
                st.markdown(f"#### {t['excluded_activity_title']}")
                st.info(t["excluded_activity_note"].format(count=excluded_count))
                st.dataframe(pd.DataFrame(excluded_rows), use_container_width=True)

        with tab6:
            reconciliation = calc.get("Account_Reconciliation", {})
            st.markdown(f"#### {t['tab_reconciliation']}")
            st.caption(t["reconciliation_formula"])
            recon_status = reconciliation.get("Status", "No reconciliation data")
            if recon_status == "MATCH":
                st.success("All entered IBKR closing quantities match the calculated units.")
            elif recon_status == "MISMATCH":
                st.error("The calculated units do not match IBKR's closing quantities. Check the statement rows and opening balances.")
            else:
                st.warning(f"Reconciliation status: {recon_status}. Enter the missing IBKR opening or closing quantities, or review incomplete activity.")
            if reconciliation.get("Rows"):
                st.dataframe(pd.DataFrame(reconciliation["Rows"]), use_container_width=True)
            else:
                st.info(t["reconciliation_none"])
            for issue in reconciliation.get("Issues", []):
                st.warning(issue)
            if reconciliation.get("Unit events"):
                st.markdown(f"**{t['reconciliation_events']}**")
                st.dataframe(pd.DataFrame(reconciliation["Unit events"]), use_container_width=True)

        st.markdown("<hr>", unsafe_allow_html=True)
        st.markdown(f"#### {t['export_title']}")
        st.caption(t["export_intro"])
        
        deducted_loss = calc["PIT38_Sekcja_C"]["Strata_z_lat_ubieglych_Poz_30"]
        summary_pdf_bytes = generate_pdf(calc, active_year, deducted_loss, lang_code=lang_code)
        summary_csv_bytes = generate_csv(calc)
        ledger_pdf_bytes = generate_ledger_pdf(calc, active_year, lang_code=lang_code)
        ledger_csv_bytes = generate_steps_csv(calc, lang_code=lang_code)
        audit_metadata = st.session_state.get("audit_metadata", calc.get("Audit_Metadata", {}))
        audit_source_bytes = st.session_state.get("audit_source_bytes")
        include_source_csv = st.checkbox(t["include_source_csv"], value=False, key="include_source_csv_in_audit_package")
        print_ready_pdf = generate_dossier_pdf(calc, audit_metadata)
        audit_package_bytes = generate_audit_package(
            calc,
            metadata=audit_metadata,
            original_csv=audit_source_bytes,
            include_original=include_source_csv,
            dossier_pdf=print_ready_pdf,
        )
        
        d_col1, d_col2 = st.columns(2)
        
        with d_col1:
            st.markdown(f"**{t['sec_c_title'].format(year=active_year)} & PIT-ZG**")
            st.caption(t["summary_export_help"])
            st.download_button(
                label=t["btn_summary_pdf"],
                data=summary_pdf_bytes,
                file_name=f"pit38_summary_report_{active_year}_{lang_code}.pdf",
                mime="application/pdf",
                use_container_width=True,
                key="bottom_summary_pdf"
            )
            st.download_button(
                label=t["btn_summary_csv"],
                data=summary_csv_bytes,
                file_name=f"pit38_summary_data_{active_year}.csv",
                mime="text/csv",
                use_container_width=True,
                key="bottom_summary_csv"
            )
            
        with d_col2:
            st.markdown(f"**{t['ledger_title']}**")
            st.caption(t["ledger_export_help"])
            st.download_button(
                label=t["btn_ledger_pdf"],
                data=ledger_pdf_bytes,
                file_name=f"pit38_detailed_ledger_{active_year}_{lang_code}.pdf",
                mime="application/pdf",
                use_container_width=True,
                key="bottom_ledger_pdf"
            )
            st.download_button(
                label=t["btn_ledger_csv"],
                data=ledger_csv_bytes,
                file_name=f"pit38_detailed_ledger_{active_year}_{lang_code}.csv",
                mime="text/csv",
                use_container_width=True,
                key="bottom_ledger_csv"
            )

        st.download_button(
            label=t["btn_print_pdf"],
            data=print_ready_pdf,
            file_name=f"pit38_ibkr_kompletny_raport_{active_year}.pdf",
            mime="application/pdf",
            use_container_width=True,
            key="download_complete_print_ready_pdf",
            help=t["complete_export_help"],
        )

        st.download_button(
            label=t["btn_audit_package"],
            data=audit_package_bytes,
            file_name=f"pit38_ibkr_pakiet_pomocniczy_{active_year}.zip",
            mime="application/zip",
            use_container_width=True,
            key="download_tax_office_audit_package",
            help="Zawiera polski raport, rejestr obliczen, podsumowanie, rejestr kursow NBP i opcjonalnie oryginalny CSV.",
        )
else:
    st.markdown(f"""
    <div class="empty-panel">
        <div class="empty-icon">↗</div>
        <strong>{t["awaiting_title"]}</strong>
        <p>{t["msg_upload_first"]}</p>
    </div>
    """, unsafe_allow_html=True)

if _current_user is not None:
    render_account_sidebar(st, _current_user)
