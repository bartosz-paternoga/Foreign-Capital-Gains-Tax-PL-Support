# app.py
import streamlit as st
import os
import tempfile
import json
import io
import pandas as pd
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from ibkr_parser import process_and_calculate_file

# Page Configuration
st.set_page_config(
    page_title="IBKR Ireland PIT-38 Tax Assistant",
    page_icon="📊",
    layout="wide"
)

def clean_pdf_text(text: str) -> str:
    """Replaces Polish diacritics with plain ASCII characters to prevent missing glyphs in ReportLab standard fonts."""
    replacements = {
        'ą': 'a', 'Ć': 'C', 'ć': 'c', 'Ę': 'E', 'ę': 'e',
        'Ł': 'L', 'ł': 'l', 'Ń': 'N', 'ń': 'n', 'Ó': 'O',
        'ó': 'o', 'Ś': 'S', 'ś': 's', 'Ź': 'Z', 'ź': 'z',
        'Ż': 'Z', 'ż': 'z'
    }
    for orig, repl in replacements.items():
        text = text.replace(orig, repl)
    return text

def generate_pdf(calc_data, tax_year, loss_cf):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), rightMargin=20, leftMargin=20, topMargin=20, bottomMargin=20)
    story = []
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle(
        'TitleStyle',
        parent=styles['Heading1'],
        fontSize=15,
        textColor=colors.HexColor('#2b2d42'),
        spaceAfter=4
    )
    story.append(Paragraph(f"PIT-38 Detailed Calculation & Audit Report ({tax_year})", title_style))
    story.append(Paragraph(f"Interactive Brokers Ireland | Loss Carryforward Deducted: {loss_cf:.2f} PLN", styles['Normal']))
    story.append(Spacer(1, 10))
    
    sec_c = calc_data["PIT38_Sekcja_C"]
    c_data = [
        ["PIT-38 Sekcja C Field", "Amount (PLN)"],
        [clean_pdf_text("Inne przychody (Poz. 22)"), f"{sec_c['Inne_przychody_Poz_22']:.2f}"],
        [clean_pdf_text("Koszty uzyskania (Poz. 23)"), f"{sec_c['Koszty_uzyskania_Poz_23']:.2f}"],
        [clean_pdf_text("Dochód przed stratami (Poz. 24)"), f"{sec_c['Dochod_przed_odliczeniem_strat_Poz_24']:.2f}"],
        [clean_pdf_text("Strata za dany rok (Poz. 25)"), f"{sec_c['Strata_za_dany_rok_Poz_25']:.2f}"],
        [clean_pdf_text("Strata z lat ubiegłych odliczona (Poz. 26)"), f"{sec_c['Strata_z_lat_ubieglych_odliczona_Poz_26']:.2f}"],
        [clean_pdf_text("Dochód po odliczeniu strat"), f"{sec_c['Dochod_po_odliczeniu_strat']:.2f}"]
    ]
    t1 = Table(c_data, colWidths=[350, 150])
    t1.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#2b2d42')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0,0), (-1,0), 4),
        ('BACKGROUND', (0,1), (-1,-1), colors.HexColor('#f8f9fa')),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#d1d5db')),
    ]))
    story.append(t1)
    story.append(Spacer(1, 10))
    
    steps = calc_data.get("Calculation_Steps", [])
    if steps:
        story.append(Paragraph("Itemized Step-by-Step Calculation Breakdown", styles['Heading2']))
        headers = ["Date", "Action", "Ticker", "Qty", "NBP Rate", "Revenue (PLN)", "Cost (PLN)", "FIFO / Formula Breakdown"]
        table_data = [headers]
        for s in steps:
            table_data.append([
                s["Date"],
                clean_pdf_text(s["Action"]),
                s["Ticker"],
                str(s["Quantity"]),
                s["NBP Rate"],
                str(s.get("Revenue (PLN)", "-")),
                str(s.get("Cost Basis (PLN)", "-")),
                clean_pdf_text(s["FIFO Details"][:55])
            ])
        t_steps = Table(table_data, colWidths=[65, 85, 45, 35, 55, 75, 75, 325])
        t_steps.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1e293b')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('ALIGN', (0,0), (-1,-1), 'LEFT'),
            ('FONTSIZE', (0,0), (-1,-1), 6.5),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#cbd5e1')),
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

def generate_steps_csv(calc_data):
    steps = calc_data.get("Calculation_Steps", [])
    df_steps = pd.DataFrame(steps)
    return df_steps.to_csv(index=False).encode('utf-8')

# Initialize Session State
if "calculated_data" not in st.session_state:
    st.session_state.calculated_data = None

# Main UI Layout
st.title("📊 IBKR Ireland PIT-38 & PIT-ZG Tax Assistant")
st.markdown("Professional multi-year FIFO tax calculator with persistent session results for the Polish Tax Office.")
st.markdown("---")

with st.sidebar:
    st.header("⚙️ Tax Configuration")
    tax_year = st.number_input("Target Tax Year", min_value=2020, max_value=2030, value=2026, step=1)
    loss_carryforward = st.number_input("Loss Carryforward (Strata z lat ubiegłych) [PLN]", min_value=0.0, value=0.0, step=10.0, format="%.2f")
    
    st.markdown("---")
    st.header("📁 File Upload")
    uploaded_file = st.file_uploader("Upload IBKR CSV Export", type=["csv"])

if uploaded_file is not None:
    if st.button("🚀 Calculate Tax Summary", type="primary"):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp_file:
            tmp_file.write(uploaded_file.getvalue())
            tmp_path = tmp_file.name

        with st.spinner("Processing historical multi-year records and NBP rates..."):
            try:
                result_json_str = process_and_calculate_file(
                    file_path=tmp_path,
                    tax_year=int(tax_year),
                    loss_carryforward=float(loss_carryforward)
                )
                st.session_state.calculated_data = json.loads(result_json_str)
            except Exception as e:
                st.error(f"An error occurred during calculation: {str(e)}")
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

# Render results from session state if available
if st.session_state.calculated_data:
    data = st.session_state.calculated_data
    
    if "error" in data:
        st.error(data["error"])
    else:
        st.success(f"Successfully processed {data['processed_records']} records across all years!")
        
        calc = data["pit38_calculation"]
        
        tab1, tab2, tab3, tab4, tab5 = st.tabs([
            "📋 PIT-38 Section C", 
            "💰 Dividends (Section G)", 
            "🌍 PIT-ZG Attachment", 
            "🧮 Detailed Calculation Breakdown", 
            "⚠️ Warnings"
        ])
        
        with tab1:
            st.subheader("Sekcja C: Capital Gains & Losses")
            sec_c = calc["PIT38_Sekcja_C"]
            col1, col2 = st.columns(2)
            with col1:
                st.metric("Przychody (Poz. 22)", f"{sec_c['Inne_przychody_Poz_22']:.2f} PLN")
                st.metric("Koszty uzyskania (Poz. 23)", f"{sec_c['Koszty_uzyskania_Poz_23']:.2f} PLN")
                st.metric("Dochód przed stratami (Poz. 24)", f"{sec_c['Dochod_przed_odliczeniem_strat_Poz_24']:.2f} PLN")
            with col2:
                st.metric("Strata za dany rok (Poz. 25)", f"{sec_c['Strata_za_dany_rok_Poz_25']:.2f} PLN")
                st.metric("Strata z lat ubiegłych odliczona (Poz. 26)", f"{sec_c['Strata_z_lat_ubieglych_odliczona_Poz_26']:.2f} PLN")
                st.metric("Dochód po odliczeniu strat", f"{sec_c['Dochod_po_odliczeniu_strat']:.2f} PLN")

        with tab2:
            st.subheader("Sekcja G: Dividends & Withholding Tax")
            sec_g = calc["PIT38_Sekcja_G_Dividends"]
            col1, col2 = st.columns(2)
            with col1:
                st.metric("Zwolniony podatek zapłacony za granicą", f"{sec_g['Zwolniony_podatek_zaplacony_za_granica']:.2f} PLN")
            with col2:
                st.metric("Podatek do zapłaty w Polsce", f"{sec_g['Podatek_do_zaplaty_w_PL']:.2f} PLN")

        with tab3:
            st.subheader("Załącznik PIT/ZG")
            pit_zg = calc["PIT_ZG_Attachments"]
            if pit_zg:
                for item in pit_zg:
                    st.markdown(f"### Kraj: **{item['Kraj']}**")
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Przychód", f"{item['Przychód']:.2f} PLN")
                    c2.metric("Koszty", f"{item['Koszty']:.2f} PLN")
                    c3.metric("Dochód", f"{item['Dochód']:.2f} PLN")
                    c4.metric("Strata", f"{item['Strata']:.2f} PLN")
            else:
                st.info("Brak zagranicznych dochodów kapitałowych w wybranym roku.")

        with tab4:
            st.subheader("🧮 Itemized Step-by-Step Calculation Breakdown")
            st.markdown("Every transaction mapped with its NBP exchange rate, currency conversion, and FIFO layer matching:")
            steps = calc.get("Calculation_Steps", [])
            if steps:
                df_steps = pd.DataFrame(steps)
                st.dataframe(df_steps, use_container_width=True)
            else:
                st.info("No calculation steps recorded.")

        with tab5:
            st.subheader("Parsing Logs & Warnings")
            warnings = data.get("warnings", [])
            if warnings:
                for w in warnings:
                    st.warning(w)
            else:
                st.info("No warnings or errors reported.")

        st.markdown("---")
        st.subheader("📥 Export Official Tax & Audit Reports")
        d_col1, d_col2, d_col3 = st.columns(3)
        
        pdf_bytes = generate_pdf(calc, tax_year, loss_carryforward)
        csv_bytes = generate_csv(calc)
        steps_csv_bytes = generate_steps_csv(calc)
        
        with d_col1:
            st.download_button(
                label="📄 Download Detailed Audit PDF",
                data=pdf_bytes,
                file_name=f"pit38_detailed_audit_{tax_year}.pdf",
                mime="application/pdf",
                use_container_width=True
            )
        with d_col2:
            st.download_button(
                label="📊 Download Summary CSV",
                data=csv_bytes,
                file_name=f"pit38_summary_{tax_year}.csv",
                mime="text/csv",
                use_container_width=True
            )
        with d_col3:
            st.download_button(
                label="🧮 Download Full Calculation Ledger (CSV)",
                data=steps_csv_bytes,
                file_name=f"pit38_calculation_ledger_{tax_year}.csv",
                mime="text/csv",
                use_container_width=True
            )
elif uploaded_file is None:
    st.info("👈 Please use the sidebar to upload your multi-year IBKR CSV export file and start calculation.")