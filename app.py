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

try:
    from ibkr_parser import process_and_calculate_file
    from account_reconciliation import parse_symbol_quantities
except ImportError:
    process_and_calculate_file = None
    parse_symbol_quantities = None

# Page Configuration - Sidebar expanded by default
st.set_page_config(
    page_title="Foreign Capital Gains PL Tax Support",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded"
)

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
        "calculated_data", "tax_year_used", "loss_cf_used",
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
        "cash_currency": "Currency for IBKR cash rows with no currency",
        "cash_country": "Source country for IBKR cash interest and its withholding",
        "source_countries": "Security source countries (ticker=ISO country, e.g. EGLN=IE)",
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
        "btn_calculate": "Execute Tax Calculation",
        "msg_upload_first": "Upload your official IBKR CSV export file in the sidebar to initialize full tax analysis.",
        "awaiting_title": "Awaiting Data Import",
        "success_msg": "Calculation complete: {count} supported records processed. Verify holdings, instrument treatment, and other PIT-38 fields before filing.",
        "tab_sec_c": "PIT-38 Section C",
        "tab_sec_g": "Section G (Foreign Tax)",
        "tab_pit_zg": "PIT-ZG Schedule",
        "tab_ledger": "Detailed FIFO Audit Ledger",
        "tab_warnings": "Execution Logs & Validation",
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
        "pit_zg_title": "PIT/ZG Foreign Income Schedule",
        "no_zg": "No verified country attribution is available for this year; configure and verify PIT/ZG separately.",
        "ledger_title": "Detailed Transaction & NBP Rate Ledger",
        "ledger_sub": "Explicit currency separation, preceding business day NBP rate dates, and full mathematical exposure:",
        "export_title": "Download calculation reports (PDF, CSV & ZIP)",
        "export_intro": "Start with the summary. Use the detailed ledger to trace individual IBKR records and calculations. The complete report combines both for printing.",
        "summary_export_help": "A short overview of the IBKR-only PIT-38 figures and foreign tax credit. Download the complete report for transaction and review details.",
        "ledger_export_help": "One row per calculated IBKR record, with its source CSV row, currency conversion, NBP reference, commission, and FIFO cost details.",
        "complete_export_help": "One print-ready Polish report with the summary, reconciliation, review notes, every transaction calculation, and the NBP rate register.",
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
        "summary_pdf_scope": "This is an IBKR-only summary. The complete print-ready report contains transaction calculations, source-row references, reconciliation, and review notes.",
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
        "cash_currency": "Waluta operacji gotówkowych bez waluty w CSV",
        "cash_country": "Kraj źródła odsetek gotówkowych IBKR i potrąconego od nich podatku",
        "source_countries": "Kraje źródła papierów (ticker=kod kraju, np. EGLN=IE)",
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
        "btn_calculate": "Uruchom Obliczenia Podatkowe",
        "msg_upload_first": "Proszę wgrać plik CSV z IBKR w panelu bocznym, aby rozpocząć przetwarzanie danych.",
        "awaiting_title": "Oczekiwanie na Import Danych",
        "success_msg": "Obliczenia zakończone: przetworzono {count} obsługiwanych rekordów. Przed złożeniem sprawdź historię, klasyfikację instrumentów i pozostałe pozycje PIT-38.",
        "tab_sec_c": "PIT-38 Sekcja C",
        "tab_sec_g": "Sekcja G (Podatek Zagraniczny)",
        "tab_pit_zg": "Załącznik PIT-ZG",
        "tab_ledger": "Szczegółowy rejestr FIFO",
        "tab_warnings": "Dziennik Zdarzeń i Walidacja",
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
        "pit_zg_title": "Załącznik PIT/ZG (Dochody Zagraniczne)",
        "no_zg": "Brak potwierdzonej atrybucji kraju; skonfiguruj i zweryfikuj PIT/ZG osobno.",
        "ledger_title": "Szczegółowy rejestr transakcji i kursów NBP",
        "ledger_sub": "Każdy wiersz pokazuje operację, przeliczenie na PLN, kurs NBP i koszt FIFO.",
        "export_title": "Pobierz raporty z obliczeń (PDF, CSV i ZIP)",
        "export_intro": "Zacznij od podsumowania. Szczegółowy rejestr pozwala prześledzić każdą operację IBKR i jej obliczenie. Kompletny raport łączy oba dokumenty i nadaje się do wydruku.",
        "summary_export_help": "Krótkie podsumowanie kwot tylko z IBKR i odliczenia podatku zagranicznego. Szczegóły transakcji i uwagi znajdziesz w kompletnym raporcie.",
        "ledger_export_help": "Jeden wiersz dla każdej uwzględnionej operacji IBKR: numer wiersza CSV, przeliczenie waluty, kurs NBP, prowizja i koszt FIFO.",
        "complete_export_help": "Jeden polski dokument do wydruku: podsumowanie, uzgodnienie rachunku, uwagi, obliczenie każdej transakcji i rejestr kursów NBP.",
        "btn_summary_pdf": "Pobierz podsumowanie obliczeń (PDF)",
        "btn_summary_csv": "Pobierz dane podsumowania (CSV)",
        "btn_ledger_pdf": "Pobierz szczegółowy rejestr audytowy (PDF)",
        "btn_ledger_csv": "Pobierz szczegółowy rejestr audytowy (CSV)",
        "btn_audit_package": "Pobierz polski pakiet dokumentów pomocniczych (ZIP)",
        "btn_print_pdf": "Pobierz kompletny raport do wydruku (PDF)",
        "include_source_csv": "Dołącz oryginalny plik CSV z IBKR do pakietu",
        "pdf_title": "PIT-38 — podsumowanie obliczeń ({year})",
        "pdf_sub": "Dane z Interactive Brokers | Odliczona strata z lat ubiegłych: {loss:.2f} PLN",
        "summary_pdf_disclaimer": "ZESTAWIENIE ROBOCZE — przed złożeniem sprawdź dane źródłowe i sposób rozliczenia podatkowego.",
        "summary_pdf_scope": "To jest podsumowanie tylko danych z IBKR. Kompletny raport do wydruku zawiera obliczenia transakcji, numery wierszy źródłowych, uzgodnienie rachunku i uwagi.",
        "pdf_sec_c_header": "Sekcja C PIT-38",
        "pdf_sec_g_header": "Sekcja G PIT-38 (odliczenie podatku zagranicznego)",
        "pdf_amount_header": "Kwota (PLN)",
        "pdf_ledger_header": "Szczegółowy rejestr FIFO i kursów NBP",
    }
}

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
    )
    cash_country_choice = st.selectbox(
        t["cash_country"],
        ["Select country...", "IE", "US", "GB", "DE", "FR", "NL", "LU", "CH", "BE", "ES", "IT", "PL", "SE", "NO", "DK", "FI", "AT", "PT", "CA", "AU", "JP", "SG", "HK"],
        key="cash_country_input",
    )
    source_country_text = st.text_input(
        t["source_countries"],
        value="",
        key="source_country_input",
        help="Map each traded security to its verified source country. Leave blank only if you will verify PIT/ZG separately.",
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

    st.markdown("<div style='height: 4px;'></div>", unsafe_allow_html=True)
    if st.button(t["btn_calculate"], type="primary", use_container_width=True):
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
                
                source_file_bytes = uploaded_file.getvalue()
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
            pit_zg = calc["PIT_ZG_Attachments"]
            if pit_zg:
                for item in pit_zg:
                    st.markdown(f"**Country Schedule:** `{item['Kraj']}`")
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Art. 30b proceeds", f"{item['Przychod_Art30b']:,.2f} PLN")
                    c2.metric("Art. 30b costs", f"{item['Koszty_Art30b']:,.2f} PLN")
                    c3.metric("Art. 30a interest", f"{item['Odsetki_Art30a']:,.2f} PLN")
                    c4.metric("Art. 30a dividends", f"{item['Dywidendy_Art30a']:,.2f} PLN")
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
