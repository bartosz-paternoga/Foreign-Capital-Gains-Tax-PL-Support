# app.py
import streamlit as st
import os
import glob
import tempfile
import json
import io
import base64
import requests
import pandas as pd
from dotenv import load_dotenv
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

# Load environment variables from local .env file if present
load_dotenv()

try:
    from ibkr_parser import process_and_calculate_file
except ImportError:
    process_and_calculate_file = None

# Page Configuration - Sidebar expanded by default
st.set_page_config(
    page_title="IBKR Ireland Tax Compliance Engine | PIT-38 & PIT-ZG",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Initialize Session State Variables safely
if "calculated_data" not in st.session_state:
    st.session_state.calculated_data = None
if "tax_year_used" not in st.session_state:
    st.session_state.tax_year_used = 2026
if "loss_cf_used" not in st.session_state:
    st.session_state.loss_cf_used = 0.0
if "open_ai_modal" not in st.session_state:
    st.session_state.open_ai_modal = False
if "ai_modal_messages" not in st.session_state:
    st.session_state.ai_modal_messages = []

# Sleek Midnight Slate & Electric Blue Design System CSS (100% Green-Free)
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

    .stApp {
        background-color: #0d1117;
        color: #e6edf3;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }

    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    
    header[data-testid="stHeader"] {
        background-color: transparent !important;
        z-index: 99999 !important;
    }

    [data-testid="stSidebar"] {
        background-color: #161b22;
        border-right: 1px solid #30363d;
    }
    
    [data-testid="stSidebar"] .block-container {
        padding-top: 0.6rem;
        padding-bottom: 1rem;
    }

    .custom-success-box {
        background-color: #161b22;
        border: 1px solid #3b82f6;
        border-radius: 10px;
        padding: 14px 18px;
        color: #58a6ff;
        font-weight: 500;
        font-size: 0.92rem;
        margin-bottom: 20px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    }

    .custom-warning-box {
        background-color: #161b22;
        border: 1px solid #d29922;
        border-radius: 10px;
        padding: 14px 18px;
        color: #e3b341;
        font-weight: 500;
        font-size: 0.92rem;
        margin-bottom: 16px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    }

    .hero-banner {
        background: linear-gradient(135deg, #161b22 0%, #0d1117 100%);
        border: 1px solid #30363d;
        border-radius: 12px;
        padding: 24px 28px;
        margin-bottom: 24px;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
        display: flex;
        align-items: center;
        gap: 20px;
    }
    .hero-logo-slick {
        width: 65px;
        height: 65px;
        border-radius: 12px;
        background: linear-gradient(135deg, #21262d 0%, #161b22 100%);
        border: 1px solid #58a6ff;
        display: flex;
        align-items: center;
        justify-content: center;
        box-shadow: 0 0 16px rgba(88, 166, 255, 0.3);
        flex-shrink: 0;
    }
    .hero-title {
        font-size: 1.5rem;
        font-weight: 700;
        color: #f0f6fc;
        margin: 0 0 4px 0;
        letter-spacing: -0.02em;
    }
    .hero-subtitle {
        font-size: 0.88rem;
        color: #8b949e;
        margin: 0 0 12px 0;
        line-height: 1.4;
    }
    .badge-bar {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
    }
    .status-badge {
        display: inline-flex;
        align-items: center;
        background-color: #21262d;
        color: #58a6ff;
        font-size: 0.72rem;
        font-weight: 500;
        padding: 3px 10px;
        border-radius: 9999px;
        border: 1px solid #30363d;
    }
    .status-dot {
        width: 5px;
        height: 5px;
        background-color: #58a6ff;
        border-radius: 50%;
        margin-right: 6px;
        box-shadow: 0 0 6px #58a6ff;
    }

    [data-testid="stMetric"] {
        background: #161b22;
        border: 1px solid #30363d;
        padding: 18px 20px;
        border-radius: 10px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    }
    [data-testid="stMetricLabel"] {
        color: #8b949e !important;
        font-size: 0.75rem !important;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.06em;
    }
    [data-testid="stMetricValue"] {
        color: #f0f6fc !important;
        font-family: 'JetBrains Mono', monospace !important;
        font-size: 1.35rem !important;
        font-weight: 600;
        margin-top: 4px;
    }

    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        background-color: #161b22;
        padding: 6px;
        border-radius: 10px;
        border: 1px solid #30363d;
    }
    .stTabs [data-baseweb="tab"] {
        background-color: transparent;
        color: #8b949e;
        border-radius: 6px;
        padding: 8px 18px;
        font-size: 0.85rem;
        font-weight: 500;
    }
    .stTabs [aria-selected="true"] {
        background: linear-gradient(180deg, #21262d 0%, #161b22 100%) !important;
        color: #58a6ff !important;
        border: 1px solid #58a6ff !important;
        font-weight: 600;
    }

    .stButton button {
        background: linear-gradient(180deg, #1f6feb 0%, #1158c7 100%);
        color: #ffffff;
        border-radius: 8px;
        font-weight: 600;
        font-size: 0.9rem;
        padding: 0.65rem 1.2rem;
        border: 1px solid #388bfd;
    }
    .stButton button:hover {
        background: linear-gradient(180deg, #1158c7 100%, #1f6feb 0%);
        border-color: #58a6ff;
    }

    [data-testid="stDataFrame"] {
        border: 1px solid #30363d;
        border-radius: 10px;
        background-color: #161b22;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.82rem;
    }

    hr {
        border-color: #30363d;
        margin: 16px 0;
    }
</style>
""", unsafe_allow_html=True)

TRANSLATIONS = {
    "EN": {
        "locale_header": "Choose Language",
        "title": "IBKR Ireland Tax Compliance Engine",
        "subtitle": "Institutional Multi-Year FIFO Tax Engine with Art. 11a NBP Business Day Rate Auditability.",
        "badge1": "Art. 11a PIT Act Compliant",
        "badge2": "Live NBP Rates API",
        "badge3": "Multi-Year FIFO Inventory",
        "config_header": "Configuration Parameters",
        "target_year": "Target Filing Tax Year",
        "loss_cf": "Loss Carryforward Deduction [PLN]",
        "file_header": "Data Import Source",
        "upload_label": "Upload IBKR Export CSV",
        "btn_calculate": "Execute Tax Calculation",
        "msg_upload_first": "Upload your official IBKR CSV export file in the sidebar to initialize full tax analysis.",
        "awaiting_title": "Awaiting Data Import",
        "success_msg": "Audit Complete: Successfully processed {count} records across historical years.",
        "tab_sec_c": "PIT-38 Section C",
        "tab_sec_g": "Section G (Foreign Tax)",
        "tab_pit_zg": "PIT-ZG Schedule",
        "tab_ledger": "Granular FIFO Audit Ledger",
        "tab_warnings": "Execution Logs & Validation",
        "sec_c_title": "Section C: Capital Gains & Losses ({year})",
        "rev_poz22": "Gross Revenue (Poz. 22)",
        "cost_poz23": "Deductible Costs (Poz. 23)",
        "inc_poz24": "Pre-Loss Income (Poz. 24)",
        "loss_poz25": "Tax Year Loss (Poz. 25)",
        "loss_poz26": "Loss Carryforward (Poz. 26)",
        "final_inc": "Net Taxable Income",
        "sec_g_title": "Section G: Passive Income & Foreign Tax Credit",
        "foreign_tax": "Foreign Tax Credited (Exempt)",
        "pl_tax": "Remaining Tax Due in PL",
        "pit_zg_title": "PIT/ZG Foreign Income Schedule",
        "no_zg": "No foreign capital gains recorded for the selected filing tax year.",
        "ledger_title": "Granular Calculation Ledger & NBP Compliance Audit Trail",
        "ledger_sub": "Explicit currency separation, preceding business day NBP rate dates, and full mathematical exposure:",
        "export_title": "Export Complete Audit Packages & Tax Filing Schedules (PDF & CSV)",
        "btn_summary_pdf": "Download Summary Report (PDF)",
        "btn_summary_csv": "Download Summary Metrics (CSV)",
        "btn_ledger_pdf": "Download Granular Audit Ledger (PDF)",
        "btn_ledger_csv": "Download Granular Audit Ledger (CSV)",
        "pdf_title": "PIT-38 Detailed Audit & Calculation Report ({year})",
        "pdf_sub": "Interactive Brokers Ireland | Loss Carryforward Deducted: {loss:.2f} PLN",
        "pdf_sec_c_header": "PIT-38 Sekcja C Field",
        "pdf_sec_g_header": "PIT-38 Sekcja G (Foreign Tax Credit & Withholding)",
        "pdf_amount_header": "Amount (PLN)",
        "pdf_ledger_header": "Granular Itemized Calculation Ledger (Art. 11a NBP Rate Compliance)",
        "ai_title": "AI Polish Tax & Compliance Advisor",
        "ai_sub": "Ask questions about PIT-38, PIT-ZG schedules, Art. 11a NBP rules, or app navigation. Powered by OpenRouter free models.",
        "ai_privacy_note": "🔒 Privacy Guarantee: Your raw CSV transaction records and financial figures are NEVER transmitted to OpenRouter.",
        "floating_ai_label": "Ask question to AI agent"
    },
    "PL": {
        "locale_header": "Wybierz Język",
        "title": "Silnik Podatkowy IBKR Ireland",
        "subtitle": "Profesjonalny wieloletni kalkulator FIFO zgodny z art. 11a ustawy o PIT z pełną ścieżką audytową NBP.",
        "badge1": "Zgodność z Art. 11a Ustawy o PIT",
        "badge2": "Pobieranie Kursów NBP API",
        "badge3": "Wieloletni Magazyn FIFO",
        "config_header": "Parametry Konfiguracyjne",
        "target_year": "Docelowy Rok Podatkowy",
        "loss_cf": "Strata z lat ubiegłych do odliczenia [PLN]",
        "file_header": "Import Raportu IBKR",
        "upload_label": "Wgraj plik CSV z IBKR",
        "btn_calculate": "Uruchom Obliczenia Podatkowe",
        "msg_upload_first": "Proszę wgrać plik CSV z IBKR w panelu bocznym, aby rozpocząć przetwarzanie danych.",
        "awaiting_title": "Oczekiwanie na Import Danych",
        "success_msg": "Audyt zakończony sukcesem: Pomyślnie przetworzono {count} rekordów ze wszystkich lat.",
        "tab_sec_c": "PIT-38 Sekcja C",
        "tab_sec_g": "Sekcja G (Podatek Zagraniczny)",
        "tab_pit_zg": "Załącznik PIT-ZG",
        "tab_ledger": "Szczegółowy Rejestr FIFO",
        "tab_warnings": "Dziennik Zdarzeń i Walidacja",
        "sec_c_title": "Sekcja C: Dochody / Straty ze zbycia papierów wartościowych ({year})",
        "rev_poz22": "Przychód Brutto (Poz. 22)",
        "cost_poz23": "Koszty Uzyskania (Poz. 23)",
        "inc_poz24": "Dochód przed Stratą (Poz. 24)",
        "loss_poz25": "Strata w Roku (Poz. 25)",
        "loss_poz26": "Odliczona Strata (Poz. 26)",
        "final_inc": "Dochód Do Opodatkowania",
        "sec_g_title": "Sekcja G: Dochody Pasywne i Zapłacony Podatek za Granicą",
        "foreign_tax": "Zwolniony Podatek Zapłacony za Granicą",
        "pl_tax": "Podatek Do Zapłaty w PL",
        "pit_zg_title": "Załącznik PIT/ZG (Dochody Zagraniczne)",
        "no_zg": "Brak zagranicznych dochodów kapitałowych w wybranym roku podatkowym.",
        "ledger_title": "Szczegółowy Rejestr Obliczeń i Ścieżka Audytowa NBP",
        "ledger_sub": "Wyszczególnione waluty, daty kursów NBP z poprzedniego dnia roboczego oraz wzory FIFO:",
        "export_title": "Eksport Kompletnych Pakietów Audytowych i Deklaracji (PDF i CSV)",
        "btn_summary_pdf": "Pobierz Raport Podsumowujący (PDF)",
        "btn_summary_csv": "Pobierz Podsumowanie Finansowe (CSV)",
        "btn_ledger_pdf": "Pobierz Szczegółowy Rejestr (PDF)",
        "btn_ledger_csv": "Pobierz Szczegółowy Rejestr (CSV)",
        "pdf_title": "PIT-38 Raport Audytowy i Szczegółowe Obliczenia ({year})",
        "pdf_sub": "Interactive Brokers Ireland | Strata z lat ubiegłych odliczona: {loss:.2f} PLN",
        "pdf_sec_c_header": "Pole PIT-38 Sekcja C",
        "pdf_sec_g_header": "PIT-38 Sekcja G (Podatek Zapłacony za Granicą)",
        "pdf_amount_header": "Kwota (PLN)",
        "pdf_ledger_header": "Szczegółowy Rejestr Transakcji (Zgodność z Art. 11a Ustawy o PIT)",
        "ai_title": "Inteligentny Doradca Podatkowy PIT (AI)",
        "ai_sub": "Zadawaj pytania dotyczące PIT-38, załącznika PIT-ZG, zasad NBP z art. 11a lub obsługi aplikacji. Napędzany darmowymi modelami OpenRouter.",
        "ai_privacy_note": "🔒 Gwarancja prywatności: Twoje surowe transakcje z pliku CSV oraz poufne kwoty finansowe NIGDY nie są przesyłane do OpenRouter.",
        "floating_ai_label": "Zapytaj agenta AI"
    }
}

def query_openrouter_ai(api_key, messages):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://streamlit.io",
        "X-Title": "IBKR Ireland Tax Compliance Engine",
        "Content-Type": "application/json"
    }
    
    system_prompt = {
        "role": "system",
        "content": (
            "You are an expert Polish tax and compliance advisor specializing in PIT-38, PIT-ZG schedules, "
            "Interactive Brokers Ireland taxation, FIFO cost basis methods, and Art. 11a NBP business day exchange rate rules. "
            "Your role is to help users understand Polish tax laws and how to navigate this tax compliance engine. "
            "CRITICAL PRIVACY RULE: You must NEVER ask for, receive, or process raw user transaction data, exact portfolio values, "
            "bank account numbers, or personal identifying information (PII). Remind users to keep their financial data private. "
            "Keep your answers precise, professional, clear, and grounded in Polish tax regulations."
        )
    }
    
    payload = {
        "model": "openrouter/free",
        "messages": [system_prompt] + messages
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        if response.status_code == 200:
            res_json = response.json()
            return res_json["choices"][0]["message"]["content"]
        else:
            return f"Error from OpenRouter API (Status {response.status_code}): {response.text}"
    except Exception as e:
        return f"Connection Error: {str(e)}"

# Define Original Gorgeous AI Chat Modal Dialog
@st.dialog("🤖 AI Tax Advisor")
def ai_chat_modal():
    t_local = TRANSLATIONS.get(st.session_state.get("lang_code", "EN"), TRANSLATIONS["EN"])
    st.markdown(f"<p style='color: #8b949e; font-size: 0.85rem; margin-bottom: 12px;'>{t_local['ai_sub']}</p>", unsafe_allow_html=True)
    st.markdown(f'<div class="custom-success-box">{t_local["ai_privacy_note"]}</div>', unsafe_allow_html=True)
    
    env_api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not env_api_key:
        try:
            env_api_key = st.secrets.get("OPENROUTER_API_KEY", "")
        except Exception:
            env_api_key = ""

    openrouter_key = env_api_key if env_api_key else st.text_input("OpenRouter API Key", type="password", key="modal_openrouter_key")
        
    for message in st.session_state.ai_modal_messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            
    if prompt := st.chat_input("Ask a Polish tax or app question..."):
        if not openrouter_key:
            st.warning("Please configure your OPENROUTER_API_KEY in your .env file or Streamlit Secrets.")
        else:
            st.session_state.ai_modal_messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)
                
            with st.chat_message("assistant"):
                with st.spinner("Analyzing tax regulations..."):
                    api_messages = [{"role": m["role"], "content": m["content"]} for m in st.session_state.ai_modal_messages]
                    ai_reply = query_openrouter_ai(openrouter_key, api_messages)
                    st.markdown(ai_reply)
                    st.session_state.ai_modal_messages.append({"role": "assistant", "content": ai_reply})

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

def generate_pdf(calc_data, tax_year, loss_cf, lang_code="EN"):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), rightMargin=10, leftMargin=10, topMargin=15, bottomMargin=15)
    story = []
    styles = getSampleStyleSheet()
    t = TRANSLATIONS[lang_code]
    
    title_style = ParagraphStyle(
        'TitleStyle',
        parent=styles['Heading1'],
        fontSize=14,
        textColor=colors.HexColor('#0d1117'),
        spaceAfter=4
    )
    story.append(Paragraph(clean_pdf_text(t["pdf_title"].format(year=tax_year)), title_style))
    story.append(Paragraph(clean_pdf_text(t["pdf_sub"].format(loss=loss_cf)), styles['Normal']))
    story.append(Spacer(1, 8))
    
    sec_c = calc_data["PIT38_Sekcja_C"]
    c_data = [
        [clean_pdf_text(t["pdf_sec_c_header"]), clean_pdf_text(t["pdf_amount_header"])],
        [clean_pdf_text("Inne przychody (Poz. 22)"), f"{sec_c['Inne_przychody_Poz_22']:.2f}"],
        [clean_pdf_text("Koszty uzyskania (Poz. 23)"), f"{sec_c['Koszty_uzyskania_Poz_23']:.2f}"],
        [clean_pdf_text("Dochód przed stratami (Poz. 24)"), f"{sec_c['Dochod_przed_odliczeniem_strat_Poz_24']:.2f}"],
        [clean_pdf_text("Strata za dany rok (Poz. 25)"), f"{sec_c['Strata_za_dany_rok_Poz_25']:.2f}"],
        [clean_pdf_text("Strata z lat ubiegłych odliczona (Poz. 26)"), f"{sec_c['Strata_z_lat_ubieglych_odliczona_Poz_26']:.2f}"],
        [clean_pdf_text("Dochód po odliczeniu strat"), f"{sec_c['Dochod_po_odliczeniu_strat']:.2f}"]
    ]
    t1 = Table(c_data, colWidths=[350, 150])
    t1.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#161b22')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.HexColor('#58a6ff')),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0,0), (-1,0), 3),
        ('BACKGROUND', (0,1), (-1,-1), colors.HexColor('#f0f6fc')),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#30363d')),
    ]))
    story.append(t1)
    story.append(Spacer(1, 6))

    sec_g = calc_data["PIT38_Sekcja_G_Dividends"]
    g_data = [
        [clean_pdf_text(t["pdf_sec_g_header"]), clean_pdf_text(t["pdf_amount_header"])],
        [clean_pdf_text("Zwolniony podatek zapłacony za granicą (Foreign Tax Credit)"), f"{sec_g['Zwolniony_podatek_zaplacony_za_granica']:.2f}"],
        [clean_pdf_text("Podatek do zapłaty w Polsce (Tax Due in Poland)"), f"{sec_g['Podatek_do_zaplaty_w_PL']:.2f}"]
    ]
    t2 = Table(g_data, colWidths=[350, 150])
    t2.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#161b22')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.HexColor('#58a6ff')),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0,0), (-1,0), 3),
        ('BACKGROUND', (0,1), (-1,-1), colors.HexColor('#f0f6fc')),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#30363d')),
    ]))
    story.append(t2)
    story.append(Spacer(1, 8))
    
    steps = calc_data.get("Calculation_Steps", [])
    if steps:
        story.append(Paragraph(clean_pdf_text(t["pdf_ledger_header"]), styles['Heading2']))
        
        cell_style = ParagraphStyle('Cell', parent=styles['Normal'], fontSize=6, leading=7.5)
        header_style = ParagraphStyle('HeaderCell', parent=styles['Normal'], fontSize=5, leading=6, textColor=colors.HexColor('#58a6ff'), fontName='Helvetica-Bold')

        if lang_code == "PL":
            headers = ["Rok", "Data", "Data NBP", "Operacja", "Ticker", "Waluta", "Ilość", "Cena", "NBP", "Prowizja (PLN)", "Przychód (PLN)", "Koszty (PLN)", "Wynik (PLN)", "Wzór & Szczegóły FIFO"]
        else:
            headers = ["Year", "Date", "NBP Date", "Action", "Ticker", "Currency", "Quantity", "Price", "NBP Rate", "Commission (PLN)", "Revenue (PLN)", "Cost Basis (PLN)", "Net (PLN)", "Audit Formula & FIFO Details"]
            
        table_data = [[Paragraph(clean_pdf_text(h), header_style) for h in headers]]
        
        for s in steps:
            action_text = s["Action_PL"] if lang_code == "PL" else s["Action_EN"]
            details_text = s["Details_PL"] if lang_code == "PL" else s["Details_EN"]

            table_data.append([
                Paragraph(str(s["Year"]), cell_style),
                Paragraph(str(s["Date"]), cell_style),
                Paragraph(str(s["NBP Date"]), cell_style),
                Paragraph(clean_pdf_text(str(action_text)), cell_style),
                Paragraph(str(s["Ticker"]), cell_style),
                Paragraph(str(s["Curr"]), cell_style),
                Paragraph(str(s["Qty"]), cell_style),
                Paragraph(str(s["Price (FX)"]), cell_style),
                Paragraph(str(s["NBP Rate"]), cell_style),
                Paragraph(str(s["Comm (PLN)"]), cell_style),
                Paragraph(str(s["Revenue (PLN)"]), cell_style),
                Paragraph(str(s["Cost Basis (PLN)"]), cell_style),
                Paragraph(str(s["Net (PLN)"]), cell_style),
                Paragraph(clean_pdf_text(str(details_text)), cell_style)
            ])
            
        t_steps = Table(table_data, colWidths=[30, 50, 50, 65, 36, 42, 38, 40, 40, 58, 55, 55, 45, 160])
        t_steps.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#161b22')),
            ('ALIGN', (0,0), (-1,-1), 'LEFT'),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#30363d')),
        ]))
        story.append(t_steps)

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()

def generate_ledger_pdf(calc_data, tax_year, lang_code="EN"):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), rightMargin=10, leftMargin=10, topMargin=15, bottomMargin=15)
    story = []
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle(
        'TitleStyle',
        parent=styles['Heading1'],
        fontSize=14,
        textColor=colors.HexColor('#0d1117'),
        spaceAfter=4
    )
    story.append(Paragraph(clean_pdf_text(f"Granular FIFO Audit Ledger ({tax_year})"), title_style))
    story.append(Spacer(1, 8))
    
    steps = calc_data.get("Calculation_Steps", [])
    if steps:
        cell_style = ParagraphStyle('Cell', parent=styles['Normal'], fontSize=6, leading=7.5)
        header_style = ParagraphStyle('HeaderCell', parent=styles['Normal'], fontSize=5, leading=6, textColor=colors.HexColor('#58a6ff'), fontName='Helvetica-Bold')

        if lang_code == "PL":
            headers = ["Rok", "Data", "Data NBP", "Operacja", "Ticker", "Waluta", "Ilość", "Cena", "NBP", "Prowizja (PLN)", "Przychód (PLN)", "Koszty (PLN)", "Wynik (PLN)", "Wzór & Szczegóły FIFO"]
        else:
            headers = ["Year", "Date", "NBP Date", "Action", "Ticker", "Currency", "Quantity", "Price", "NBP Rate", "Commission (PLN)", "Revenue (PLN)", "Cost Basis (PLN)", "Net (PLN)", "Audit Formula & FIFO Details"]
            
        table_data = [[Paragraph(clean_pdf_text(h), header_style) for h in headers]]
        
        for s in steps:
            action_text = s["Action_PL"] if lang_code == "PL" else s["Action_EN"]
            details_text = s["Details_PL"] if lang_code == "PL" else s["Details_EN"]

            table_data.append([
                Paragraph(str(s["Year"]), cell_style),
                Paragraph(str(s["Date"]), cell_style),
                Paragraph(str(s["NBP Date"]), cell_style),
                Paragraph(clean_pdf_text(str(action_text)), cell_style),
                Paragraph(str(s["Ticker"]), cell_style),
                Paragraph(str(s["Curr"]), cell_style),
                Paragraph(str(s["Qty"]), cell_style),
                Paragraph(str(s["Price (FX)"]), cell_style),
                Paragraph(str(s["NBP Rate"]), cell_style),
                Paragraph(str(s["Comm (PLN)"]), cell_style),
                Paragraph(str(s["Revenue (PLN)"]), cell_style),
                Paragraph(str(s["Cost Basis (PLN)"]), cell_style),
                Paragraph(str(s["Net (PLN)"]), cell_style),
                Paragraph(clean_pdf_text(str(details_text)), cell_style)
            ])
            
        t_steps = Table(table_data, colWidths=[30, 50, 50, 65, 36, 42, 38, 40, 40, 58, 55, 55, 45, 160])
        t_steps.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#161b22')),
            ('ALIGN', (0,0), (-1,-1), 'LEFT'),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#30363d')),
        ]))
        story.append(t_steps)

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()

def generate_csv(calc_data):
    rows = []
    sec_c = calc_data["PIT38_Sekcja_C"]
    for k, v in sec_c.items():
        rows.append({"Category": "PIT-38 Sekcja C", "Field": k, "Value (PLN)": v})
        
    sec_g = calc_data["PIT38_Sekcja_G_Dividends"]
    for k, v in sec_g.items():
        rows.append({"Category": "PIT-38 Sekcja G (Dividends)", "Field": k, "Value (PLN)": v})
        
    for item in calc_data["PIT_ZG_Attachments"]:
        rows.append({"Category": f"PIT-ZG ({item['Kraj']})", "Field": "Przychód", "Value (PLN)": item['Przychód']})
        rows.append({"Category": f"PIT-ZG ({item['Kraj']})", "Field": "Koszty", "Value (PLN)": item['Koszty']})
        rows.append({"Category": f"PIT-ZG ({item['Kraj']})", "Field": "Dochód", "Value (PLN)": item['Dochód']})
        rows.append({"Category": f"PIT-ZG ({item['Kraj']})", "Field": "Strata", "Value (PLN)": item['Strata']})
        
    df_export = pd.DataFrame(rows)
    return df_export.to_csv(index=False).encode('utf-8')

def generate_steps_csv(calc_data, lang_code="EN"):
    steps = calc_data.get("Calculation_Steps", [])
    formatted_steps = []
    for s in steps:
        step_dict = {
            "Rok / Year": s["Year"],
            "Data / Date": s["Date"],
            "Data Kursu NBP / NBP Date": s["NBP Date"],
            "Operacja / Action": s["Action_PL"] if lang_code == "PL" else s["Action_EN"],
            "Ticker": s["Ticker"],
            "Waluta / Currency": s["Curr"],
            "Ilość / Quantity": s["Qty"],
            "Cena / Price (FX)": s["Price (FX)"],
            "Kurs NBP / NBP Rate": s["NBP Rate"],
            "Prowizja PLN / Comm (PLN)": s["Comm (PLN)"],
            "Przychód PLN / Revenue (PLN)": s["Revenue (PLN)"],
            "Koszt PLN / Cost Basis (PLN)": s["Cost Basis (PLN)"],
            "Dochód PLN / Net (PLN)": s["Net (PLN)"],
            "Wzór & FIFO / Audit Formula": s["Details_PL"] if lang_code == "PL" else s["Details_EN"]
        }
        formatted_steps.append(step_dict)
    df_steps = pd.DataFrame(formatted_steps)
    return df_steps.to_csv(index=False).encode('utf-8')

with st.sidebar:
    temp_lang_choice = st.radio("Choose Language / Wybierz Język", ["English", "Polski"], index=0, label_visibility="collapsed")
    lang_code = "EN" if temp_lang_choice == "English" else "PL"
    st.session_state.lang_code = lang_code
    t = TRANSLATIONS[lang_code]

    st.markdown(f"<p style='font-size: 0.75rem; font-weight: 700; color: #8b949e; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 4px;'>{t['locale_header']}</p>", unsafe_allow_html=True)

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown(f"<p style='font-size: 0.75rem; font-weight: 700; color: #8b949e; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 4px;'>{t['config_header']}</p>", unsafe_allow_html=True)
    
    tax_year = st.number_input(t["target_year"], min_value=2020, max_value=2030, value=2026, step=1)
    loss_carryforward = st.number_input(t["loss_cf"], min_value=0.0, value=0.0, step=100.0, format="%.2f")
    
    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown(f"<p style='font-size: 0.75rem; font-weight: 700; color: #8b949e; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 4px;'>{t['file_header']}</p>", unsafe_allow_html=True)
    uploaded_file = st.file_uploader(t["upload_label"], type=["csv"], label_visibility="collapsed")

    st.markdown("<div style='height: 4px;'></div>", unsafe_allow_html=True)
    if st.button(t["btn_calculate"], type="primary", use_container_width=True):
        if uploaded_file is not None:
            if process_and_calculate_file is None:
                st.markdown(f'<div class="custom-warning-box">Error: ibkr_parser.py could not be imported.</div>', unsafe_allow_html=True)
            else:
                st.session_state.tax_year_used = tax_year
                st.session_state.loss_cf_used = loss_carryforward
                
                with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp_file:
                    tmp_file.write(uploaded_file.getvalue())
                    tmp_path = tmp_file.name

                with st.spinner("Processing transaction ledger & querying NBP exchange rates..."):
                    try:
                        result_json_str = process_and_calculate_file(
                            file_path=tmp_path,
                            tax_year=int(tax_year),
                            loss_carryforward=float(loss_carryforward)
                        )
                        st.session_state.calculated_data = json.loads(result_json_str)
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

# Hero Banner with Modern Sleek Vector SVG Logo
modern_vector_logo = '''<div class="hero-logo-slick"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#58a6ff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="34" height="34"><path d="m16 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/><path d="m2 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/><path d="M7 21h10"/><path d="M12 3v18"/><path d="M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2"/></svg></div>'''

hero_html = f'<div class="hero-banner">{modern_vector_logo}<div><div class="hero-title">{t["title"]}</div><div class="hero-subtitle">{t["subtitle"]}</div><div class="badge-bar"><span class="status-badge"><span class="status-dot"></span>{t["badge1"]}</span><span class="status-badge"><span class="status-dot"></span>{t["badge2"]}</span><span class="status-badge"><span class="status-dot"></span>{t["badge3"]}</span></div></div></div>'

st.markdown(hero_html, unsafe_allow_html=True)

if st.session_state.calculated_data is not None:
    data = st.session_state.calculated_data
    
    if "error" in data:
        st.markdown(f'<div class="custom-warning-box">{data["error"]}</div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="custom-success-box">{t["success_msg"].format(count=data["processed_records"])}</div>', unsafe_allow_html=True)
        
        calc = data["pit38_calculation"]
        active_year = st.session_state.tax_year_used
        active_loss_cf = st.session_state.loss_cf_used
        
        tab1, tab2, tab3, tab4, tab5 = st.tabs([
            t["tab_sec_c"], 
            t["tab_sec_g"], 
            t["tab_pit_zg"], 
            t["tab_ledger"], 
            t["tab_warnings"]
        ])
        
        with tab1:
            st.markdown(f"#### {t['sec_c_title'].format(year=active_year)}")
            sec_c = calc["PIT38_Sekcja_C"]
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric(t["rev_poz22"], f"{sec_c['Inne_przychody_Poz_22']:,.2f} PLN")
                st.metric(t["loss_poz25"], f"{sec_c['Strata_za_dany_rok_Poz_25']:,.2f} PLN")
            with col2:
                st.metric(t["cost_poz23"], f"{sec_c['Koszty_uzyskania_Poz_23']:,.2f} PLN")
                st.metric(t["loss_poz26"], f"{sec_c['Strata_z_lat_ubieglych_odliczona_Poz_26']:,.2f} PLN")
            with col3:
                st.metric(t["inc_poz24"], f"{sec_c['Dochod_przed_odliczeniem_strat_Poz_24']:,.2f} PLN")
                st.metric(t["final_inc"], f"{sec_c['Dochod_po_odliczeniu_strat']:,.2f} PLN")

        with tab2:
            st.markdown(f"#### {t['sec_g_title']}")
            sec_g = calc["PIT38_Sekcja_G_Dividends"]
            col1, col2 = st.columns(2)
            with col1:
                st.metric(t["foreign_tax"], f"{sec_g['Zwolniony_podatek_zaplacony_za_granica']:,.2f} PLN")
            with col2:
                st.metric(t["pl_tax"], f"{sec_g['Podatek_do_zaplaty_w_PL']:,.2f} PLN")

        with tab3:
            st.markdown(f"#### {t['pit_zg_title']}")
            pit_zg = calc["PIT_ZG_Attachments"]
            if pit_zg:
                for item in pit_zg:
                    st.markdown(f"**Country Schedule:** `{item['Kraj']}`")
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Revenue / Przychód", f"{item['Przychód']:,.2f} PLN")
                    c2.metric("Costs / Koszty", f"{item['Koszty']:,.2f} PLN")
                    c3.metric("Income / Dochód", f"{item['Dochód']:,.2f} PLN")
                    c4.metric("Loss / Strata", f"{item['Strata']:,.2f} PLN")
            else:
                st.info(t["no_zg"])

        with tab4:
            st.markdown(f"#### {t['ledger_title']}")
            st.markdown(f"<p style='color: #8b949e; font-size: 0.85rem; margin-bottom: 12px;'>{t['ledger_sub']}</p>", unsafe_allow_html=True)
            
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
            else:
                st.markdown(f'<div class="custom-success-box">All records parsed cleanly with 0 system errors or warnings.</div>', unsafe_allow_html=True)

        st.markdown("<hr>", unsafe_allow_html=True)
        st.markdown(f"#### {t['export_title']}")
        
        summary_pdf_bytes = generate_pdf(calc, active_year, active_loss_cf, lang_code=lang_code)
        summary_csv_bytes = generate_csv(calc)
        ledger_pdf_bytes = generate_ledger_pdf(calc, active_year, lang_code=lang_code)
        ledger_csv_bytes = generate_steps_csv(calc, lang_code=lang_code)
        
        d_col1, d_col2 = st.columns(2)
        
        with d_col1:
            st.markdown(f"**{t['sec_c_title'].format(year=active_year)} & PIT-ZG**")
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
            st.download_button(
                label=t["btn_ledger_pdf"],
                data=ledger_pdf_bytes,
                file_name=f"pit38_granular_ledger_{active_year}_{lang_code}.pdf",
                mime="application/pdf",
                use_container_width=True,
                key="bottom_ledger_pdf"
            )
            st.download_button(
                label=t["btn_ledger_csv"],
                data=ledger_csv_bytes,
                file_name=f"pit38_granular_ledger_{active_year}_{lang_code}.csv",
                mime="text/csv",
                use_container_width=True,
                key="bottom_ledger_csv"
            )
else:
    st.markdown(f"""
    <div style="text-align: center; padding: 40px 20px; background-color: #161b22; border: 1px dashed #30363d; border-radius: 12px; margin-top: 10px;">
        <div style="font-size: 0.95rem; font-weight: 600; color: #f0f6fc; margin-bottom: 6px;">{t["awaiting_title"]}</div>
        <p style="color: #8b949e; font-size: 0.85rem; max-width: 480px; margin: 0 auto;">{t["msg_upload_first"]}</p>
    </div>
    """, unsafe_allow_html=True)

# Trigger AI Chat Modal smoothly ONLY when explicitly clicked
if st.session_state.open_ai_modal:
    ai_chat_modal()
    # Reset flag immediately so background app reruns (file uploads) won't reopen it!
    st.session_state.open_ai_modal = False

# Explicitly load icon from local path or fallback to SVG robot
ai_icon_paths = [
    "/home/bart/pit38-ibkr-agent/ai_robot_icon.png",
    "ai_robot_icon.png",
    "ai_robot_icon.jpg"
]

ai_icon_b64 = ""
for path in ai_icon_paths:
    if os.path.exists(path):
        with open(path, "rb") as f:
            ai_icon_b64 = base64.b64encode(f.read()).decode("utf-8")
        break

if ai_icon_b64:
    button_bg_css = f'background-image: url("data:image/png;base64,{ai_icon_b64}") !important; background-size: cover !important; background-position: center !important;'
else:
    svg_robot = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#ffffff"><path d="M12,2A2,2 0 0,1 14,4C14,4.74 13.6,5.39 13,5.73V7H15A2,2 0 0,1 17,9V11H18A2,2 0 0,1 20,13V16A2,2 0 0,1 18,18H17V20A2,2 0 0,1 15,22H9A2,2 0 0,1 7,20V18H6A2,2 0 0,1 4,16V13A2,2 0 0,1 6,11H7V9A2,2 0 0,1 9,7H11V5.73C10.4,5.39 10,4.74 10,4A2,2 0 0,1 12,2M7.5,13A1.5,1.5 0 0,0 6,14.5A1.5,1.5 0 0,0 7.5,16A1.5,1.5 0 0,0 9,14.5A1.5,1.5 0 0,0 7.5,13M16.5,13A1.5,1.5 0 0,0 15,14.5A1.5,1.5 0 0,0 16.5,16A1.5,1.5 0 0,0 18,14.5A1.5,1.5 0 0,0 16.5,13M10,18V20H14V18H10Z"/></svg>'''
    svg_b64 = base64.b64encode(svg_robot.encode("utf-8")).decode("utf-8")
    button_bg_css = f'background: linear-gradient(135deg, #1f6feb 0%, #1158c7 100%) url("data:image/svg+xml;base64,{svg_b64}") no-repeat center center !important; background-size: 32px 32px !important;'

floating_label = t.get("floating_ai_label", "Ask question to AI agent")

st.markdown(f"""
<style>
    :root {{
        --fab-label: "{floating_label}";
    }}
    [data-testid="stMain"] div[data-testid="stButton"]:last-of-type {{
        position: fixed !important;
        bottom: 30px !important;
        right: 30px !important;
        z-index: 999999 !important;
        margin: 0 !important;
        width: 65px !important;
        display: flex !important;
        justify-content: center !important;
    }}
    [data-testid="stMain"] div[data-testid="stButton"]:last-of-type button {{
        width: 65px !important;
        height: 65px !important;
        min-height: 65px !important;
        border-radius: 50% !important;
        {button_bg_css}
        border: 2px solid #58a6ff !important;
        box-shadow: 0 8px 24px rgba(88, 166, 255, 0.4) !important;
        color: transparent !important;
        font-size: 0px !important;
        padding: 0 !important;
        cursor: pointer !important;
        transition: transform 0.2s ease, box-shadow 0.2s ease !important;
    }}
    [data-testid="stMain"] div[data-testid="stButton"]:last-of-type button:hover {{
        transform: scale(1.08) !important;
        box-shadow: 0 12px 32px rgba(88, 166, 255, 0.6) !important;
    }}
    [data-testid="stMain"] div[data-testid="stButton"]:last-of-type::before {{
        content: var(--fab-label);
        position: absolute;
        bottom: 78px;
        left: 50%;
        transform: translateX(-50%);
        background-color: #161b22;
        color: #58a6ff;
        border: 1px solid #30363d;
        padding: 6px 12px;
        border-radius: 10px;
        font-size: 0.75rem;
        font-weight: 600;
        box-shadow: 0 4px 16px rgba(0,0,0,0.5);
        white-space: nowrap;
        font-family: 'Inter', sans-serif;
        pointer-events: none;
    }}
</style>
""", unsafe_allow_html=True)

if st.button("🤖", key="ai_robot_fab"):
    st.session_state.open_ai_modal = True
    st.rerun()