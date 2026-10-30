"""Polish-language supporting dossier and evidence bundle for IBKR calculations."""
from __future__ import annotations

import hashlib
import io
import os
import re
import zipfile

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    LongTable,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


MF_PIT38_URL = "https://www.podatki.gov.pl/podatki-osobiste/pit/formularze-pit/"
MF_PITZG_FORM_URL = "https://www.gov.pl/web/finanse/pitzg"
NBP_API_URL = "https://api.nbp.pl/"
RECONCILIATION_STATUS_PL = {
    "MATCH": "ZGODNE",
    "MISMATCH": "ROZNICA",
    "INCOMPLETE ACTIVITY": "NIEPELNE DANE",
    "OPENING UNITS NEEDED": "BRAK STANU POCZATKOWEGO",
    "IBKR CLOSING UNITS NEEDED": "BRAK STANU KONCOWEGO IBKR",
    "NO SECURITY ACTIVITY": "BRAK ZDARZEN PAPIEROW",
}
PDF_FONT = "Helvetica"
PDF_BOLD = "Helvetica-Bold"

for font_dir in ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu"):
    regular_font = os.path.join(font_dir, "DejaVuSans.ttf")
    bold_font = os.path.join(font_dir, "DejaVuSans-Bold.ttf")
    if os.path.isfile(regular_font) and os.path.isfile(bold_font):
        pdfmetrics.registerFont(TTFont("DossierDejaVu", regular_font))
        pdfmetrics.registerFont(TTFont("DossierDejaVu-Bold", bold_font))
        PDF_FONT = "DossierDejaVu"
        PDF_BOLD = "DossierDejaVu-Bold"
        break


def _pdf_safe(value):
    """Escape report text, transliterating only if no Unicode font is available."""
    replacements = {
        "ą": "a", "Ą": "A", "ć": "c", "Ć": "C", "ę": "e", "Ę": "E",
        "ł": "l", "Ł": "L", "ń": "n", "Ń": "N", "ó": "o", "Ó": "O",
        "ś": "s", "Ś": "S", "ź": "z", "Ź": "Z", "ż": "z", "Ż": "Z",
    }
    text = str(value if value is not None else "—")
    if PDF_FONT == "Helvetica":
        for source, replacement in replacements.items():
            text = text.replace(source, replacement)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _money(value):
    try:
        return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError):
        return "—"


def _table(rows, widths, font_size=7, long_table=False):
    cooked = []
    for row_index, row in enumerate(rows):
        cooked.append([
            Paragraph(_pdf_safe(cell), ParagraphStyle(
                f"cell-{row_index}-{col_index}",
                fontName=PDF_BOLD if row_index == 0 else PDF_FONT,
                fontSize=font_size,
                leading=font_size + 2,
                textColor=colors.white if row_index == 0 else colors.HexColor("#17212f"),
            ))
            for col_index, cell in enumerate(row)
        ])
    table_type = LongTable if long_table else Table
    table = table_type(cooked, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#52647c")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f5f8")]),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#d5dde7")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table

LEDGER_COLUMN_WIDTHS_MM = [23, 30, 16, 27, 34, 27, 22, 22, 19, 52]

def transaction_ledger_table(calc_data, lang_code="PL"):
    """Build a print-readable transaction ledger with source, FX and FIFO references."""
    if lang_code == "PL":
        labels = [
            "Data / wiersz<br/>CSV IBKR", "Operacja / ticker", "Liczba",
            "Cena / kwota<br/>(waluta)", "Kurs NBP / data /<br/>numer tabeli",
            "Prowizja<br/>(waluta / PLN)", "Przychód<br/>PLN", "Koszt<br/>PLN",
            "Wynik<br/>PLN", "Wzór FIFO / szczegóły kosztu",
        ]
        row_label = "Wiersz CSV"
        action_key, details_key = "Action_PL", "Details_PL"
    else:
        labels = [
            "Date / IBKR<br/>CSV row", "Activity / ticker", "Quantity",
            "Unit price / amount<br/>(currency)", "NBP rate / date /<br/>table number",
            "Commission<br/>(currency / PLN)", "Revenue<br/>PLN", "Cost basis<br/>PLN",
            "Net<br/>PLN", "FIFO formula / cost details",
        ]
        row_label = "CSV row"
        action_key, details_key = "Action_EN", "Details_EN"

    header_style = ParagraphStyle(
        f"LedgerHeader{lang_code}", fontName=PDF_BOLD, fontSize=7, leading=8,
        textColor=colors.white,
    )
    cell_style = ParagraphStyle(
        f"LedgerCell{lang_code}", fontName=PDF_FONT, fontSize=6.8, leading=8,
        textColor=colors.HexColor("#17212f"),
    )
    rows = [[Paragraph(label, header_style) for label in labels]]
    for step in calc_data.get("Calculation_Steps", []):
        source_row = step.get("Source row") or "—"
        date_cell = Paragraph(
            f"{_pdf_safe(step.get('Date', '—'))}<br/><font size=6>{row_label}: {_pdf_safe(source_row)}</font>",
            cell_style,
        )
        activity_cell = Paragraph(
            f"{_pdf_safe(step.get(action_key, '—'))}<br/><b>{_pdf_safe(step.get('Ticker', '—'))}</b>",
            cell_style,
        )
        nbp_meta = _pdf_safe(step.get("NBP Date") or "PLN; no FX conversion")
        if step.get("NBP Table"):
            nbp_meta += f"<br/>{_pdf_safe(step.get('NBP Table'))}"
        commission = (
            f"{step.get('Comm (FX)', '0.00')} {step.get('Curr', '')}"
            f"<br/>{_money(step.get('Comm (PLN)'))} PLN"
        )
        rows.append([
            date_cell,
            activity_cell,
            Paragraph(_pdf_safe(step.get("Qty", "—")), cell_style),
            Paragraph(
                f"{_pdf_safe(step.get('Price (FX)', '—'))} {_pdf_safe(step.get('Curr', ''))}",
                cell_style,
            ),
            Paragraph(
                f"{_pdf_safe(step.get('NBP Rate', '—'))}<br/>{nbp_meta}",
                cell_style,
            ),
            Paragraph(commission, cell_style),
            Paragraph(f"{_money(step.get('Revenue (PLN)'))} PLN", cell_style),
            Paragraph(f"{_money(step.get('Cost Basis (PLN)'))} PLN", cell_style),
            Paragraph(f"{_money(step.get('Net (PLN)'))} PLN", cell_style),
            Paragraph(_pdf_safe(step.get(details_key, "—")), cell_style),
        ])

    if len(rows) == 1:
        rows.append([Paragraph("Brak pozycji do pokazania.", cell_style)] + [Paragraph("—", cell_style) for _ in labels[1:]])

    table = LongTable(
        rows, colWidths=[width * mm for width in LEDGER_COLUMN_WIDTHS_MM],
        repeatRows=1, hAlign="LEFT",
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#52647c")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f5f8")]),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#d5dde7")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (2, 1), (3, -1), "RIGHT"),
        ("ALIGN", (5, 1), (8, -1), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _polish_excluded_reason(item):
    return {
        "FX conversion component": "Wiersz skladowy przewalutowania IBKR; wykluczono z tej kalkulacji PIT-38. Raport nie rozstrzyga odrebnego opodatkowania wymiany walut.",
        "FX translation adjustment": "Korekta przeliczeniowa IBKR; nie ujeta jako transakcja papierem wartosciowym ani przychod z odsetek lub dywidend w PIT-38.",
        "Cash deposit": "Wplata na rachunek; nie jest przychodem ze sprzedazy papierow wartosciowych ani odsetkami lub dywidenda.",
        "Cash withdrawal": "Wyplata z rachunku; nie jest przychodem ze sprzedazy papierow wartosciowych ani odsetkami lub dywidenda.",
    }.get(item.get("Category"), item.get("Reason excluded from PIT-38", ""))


def excluded_activity_table(calc_data, lang_code="PL"):
    """Show imported IBKR activity omitted from the PIT-38 calculation."""
    if lang_code == "PL":
        labels = ["Data", "Wiersz CSV", "Typ IBKR", "Kategoria", "Symbol", "Opis z IBKR", "Powod wykluczenia"]
    else:
        labels = ["Date", "CSV row", "IBKR type", "Category", "Symbol", "IBKR description", "Reason excluded"]

    header_style = ParagraphStyle(
        f"ExcludedHeader{lang_code}", fontName=PDF_BOLD, fontSize=6.7, leading=8,
        textColor=colors.white,
    )
    cell_style = ParagraphStyle(
        f"ExcludedCell{lang_code}", fontName=PDF_FONT, fontSize=6.5, leading=7.8,
        textColor=colors.HexColor("#17212f"),
    )
    rows = [[Paragraph(label, header_style) for label in labels]]
    for item in calc_data.get("Excluded_Activity", []):
        reason = _polish_excluded_reason(item) if lang_code == "PL" else item.get("Reason excluded from PIT-38", "")
        values = [
            item.get("Date", "—"), item.get("Source row", "—"),
            item.get("IBKR transaction type", "—"), item.get("Category", "—"),
            item.get("Symbol") or "—", item.get("Description") or "—", reason,
        ]
        rows.append([Paragraph(_pdf_safe(value), cell_style) for value in values])

    if len(rows) == 1:
        rows.append([Paragraph("Brak aktywnosci wykluczonej z PIT-38.", cell_style)] + [Paragraph("—", cell_style) for _ in labels[1:]])

    table = LongTable(rows, colWidths=[17 * mm, 17 * mm, 30 * mm, 34 * mm, 18 * mm, 70 * mm, 91 * mm], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#52647c")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f5f8")]),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#d5dde7")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def generate_ledger_pdf(calc_data, tax_year, lang_code="EN"):
    """Export the transaction ledger as a separate, print-friendly A4 landscape PDF."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4), rightMargin=10 * mm, leftMargin=10 * mm,
        topMargin=15 * mm, bottomMargin=16 * mm,
        title=f"IBKR transaction audit ledger {tax_year}",
        author="Foreign Capital Gains PL Tax Support",
    )
    styles = getSampleStyleSheet()
    if lang_code == "PL":
        title = f"Szczegółowy rejestr transakcji IBKR — {tax_year}"
        intro = (
            "Każdy wiersz odpowiada operacji użytej w obliczeniach. Numer wiersza CSV pozwala znaleźć ją w raporcie IBKR. "
            "Kurs NBP, prowizja i szczegóły FIFO pokazują, jak wyliczono kwoty w PLN."
        )
        footer_label = f"IBKR | Rejestr obliczeń | Rok {tax_year}"
    else:
        title = f"Detailed IBKR Transaction Audit Ledger — {tax_year}"
        intro = (
            "Each row is an IBKR record used in the calculation. The CSV row locates it in the source statement. "
            "NBP, commission and FIFO fields show how PLN figures were calculated."
        )
        footer_label = f"IBKR | Calculation ledger | Tax year {tax_year}"

    title_style = ParagraphStyle(
        f"LedgerTitle{lang_code}", parent=styles["Title"], fontName=PDF_BOLD,
        fontSize=17, leading=21, textColor=colors.HexColor("#17212f"),
        alignment=TA_CENTER, spaceAfter=5,
    )
    intro_style = ParagraphStyle(
        f"LedgerIntro{lang_code}", parent=styles["BodyText"], fontName=PDF_FONT,
        fontSize=9, leading=12, textColor=colors.HexColor("#475569"), spaceAfter=10,
    )
    story = [
        Paragraph(_pdf_safe(title), title_style),
        Paragraph(_pdf_safe(intro), intro_style),
        transaction_ledger_table(calc_data, lang_code),
    ]
    excluded = calc_data.get("Excluded_Activity", [])
    if excluded:
        if lang_code == "PL":
            excluded_heading = "Aktywnosc z pliku IBKR wykluczona z obliczen PIT-38"
            excluded_intro = "Ponizsze wiersze pozostaja w rejestrze audytowym, ale nie wchodza do obliczen PIT-38. Samo wykluczenie nie rozstrzyga odrebnego opodatkowania wymiany walut."
        else:
            excluded_heading = "IBKR activity excluded from PIT-38 calculations"
            excluded_intro = "These source rows remain in the audit trail but are not included in the PIT-38 calculation. Their separate tax treatment is not determined here."
        story.extend([
            Spacer(1, 10),
            Paragraph(_pdf_safe(excluded_heading), ParagraphStyle(
                f"ExcludedTitle{lang_code}", parent=styles["Heading2"], fontName=PDF_BOLD,
                fontSize=11, leading=13, textColor=colors.HexColor("#17212f"), spaceAfter=4,
            )),
            Paragraph(_pdf_safe(excluded_intro), intro_style),
            excluded_activity_table(calc_data, lang_code),
        ])

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont(PDF_FONT, 7)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(10 * mm, 8 * mm, footer_label)
        canvas.drawRightString(landscape(A4)[0] - 10 * mm, 8 * mm, f"Page {_doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    buffer.seek(0)
    return buffer.getvalue()


def _review_notes(calc_data):
    reconciliation = calc_data.get("Account_Reconciliation", {})
    return list(dict.fromkeys(
        calc_data.get("Review_Notes", []) + reconciliation.get("Issues", [])
    ))


def _polish_review_note(note):
    text = str(note)
    match = re.search(r"Source country for (.+?) is unresolved; verify PIT/ZG attribution\.", text)
    if match:
        return f"Nie potwierdzono kraju zrodla dla {match.group(1)}. Sprawdz kraj dla PIT/ZG."
    match = re.search(r"Source country for (interest|dividend) income on (\d{4}-\d{2}-\d{2}) is unresolved", text)
    if match:
        kind = "odsetek" if match.group(1) == "interest" else "dywidendy"
        return f"Nie potwierdzono kraju zrodla {kind} z dnia {match.group(2)}. Sprawdz atrybucje PIT/ZG."
    if "Foreign withholding has no confirmed source country" in text:
        return "Nie potwierdzono kraju podatku pobranego za granica; ta kwota nie zostala zaliczona do polskiego odliczenia."
    if "Prior-year loss entered manually" in text:
        return "Wpisana strata z poprzednich lat wymaga sprawdzenia: czy dotyczy PIT-38, miesci sie w terminie i limicie odliczenia."
    if "Passive foreign tax paid:" in text:
        amounts = re.search(r"paid: ([0-9.]+) PLN; allowed Polish credit \(Poz\. 48\): ([0-9.]+) PLN; uncredited: ([0-9.]+) PLN", text)
        if amounts:
            return (
                f"Podatek zagraniczny: {amounts.group(1)} PLN; odliczenie w poz. 48: {amounts.group(2)} PLN; "
                f"nieodliczona kwota: {amounts.group(3)} PLN. Nieodliczona kwota nie zwieksza polskiego podatku do zaplaty."
            )
        return "Podatek zagraniczny przekracza limit polskiego odliczenia. Sprawdz, czy nadwyzke mozna odzyskac za granica."
    if "tax basis is not imported" in text:
        return "Transfer zmienia liczbe jednostek, ale nie przenosi automatycznie kosztu nabycia. Zachowaj potwierdzenie transferu i pierwotnego zakupu."
    if "tax-basis treatment is not calculated" in text:
        return "Dzialanie korporacyjne zmienia liczbe jednostek, ale jego skutku dla kosztu podatkowego nie wyliczono. Zachowaj dokument korporacyjny."
    if "cancel/correction record" in text.lower():
        return "Raport zawiera anulowanie lub korekte. Dopasuj ja do pierwotnej transakcji; kalkulacja podatkowa zostala zatrzymana."
    if "accrual activity was not treated as paid income" in text:
        return "Zdarzenie jest naliczeniem, a nie potwierdzona wyplata. Sprawdz date i moment rozpoznania przychodu."
    if "Opening units default to zero for tickers omitted" in text:
        return "Dla niewymienionych tickerow przyjeto zero jednostek na poczatku. Potwierdz, ze stan rachunku rzeczywiscie wynosil zero."
    match = re.search(r"Row (\d+): transfer direction or quantity for (.+?) is unresolved", text)
    if match:
        return f"Wiersz {match.group(1)}: nie mozna ustalic, czy transfer {match.group(2)} byl przychodzacy czy wychodzacy ani ile jednostek obejmowal."
    match = re.search(r"Row (\d+): corporate action for (.+?) reports zero units", text)
    if match:
        return f"Wiersz {match.group(1)}: dzialanie korporacyjne dla {match.group(2)} pokazuje zero jednostek; potwierdz jego wplyw na pozycje."
    if "unsupported or unclear IBKR event type" in text:
        return "Nie rozpoznano rodzaju operacji IBKR. Sprawdz opis w tym wierszu i potwierdz, czy zmienia liczbe jednostek lub koszt podatkowy."
    match = re.search(r"IBKR unit mismatch for (.+?): calculated closing units ([^,]+), IBKR reported ([^ ]+) \(difference ([^)]+)\)", text)
    if match:
        return (
            f"Liczba jednostek {match.group(1)} nie zgadza sie: wyliczono {match.group(2)}, "
            f"IBKR pokazuje {match.group(3)}, roznica {match.group(4)}. Porownaj wiersze aktywnosci i stany z raportu IBKR."
        )
    if "Unit reconciliation needs opening quantities" in text:
        return "Brakuje stanu poczatkowego dla wymienionych tickerow. Uzyj stanu z raportu IBKR sprzed pierwszej operacji w pliku."
    if "Unit reconciliation needs IBKR-reported closing quantities" in text:
        return "Brakuje stanu koncowego IBKR. Porownaj wyliczenie z raportem pozycji na koniec okresu."
    if "Source country" in text or "source country" in text:
        return "Nie potwierdzono kraju zrodla dochodu. Sprawdz instrument i jego dokumenty przed uzyciem PIT/ZG."
    if text.startswith("Row "):
        return f"Wiersz aktywnosci wymaga sprawdzenia: {text}"
    return f"Wymaga sprawdzenia: {text}"


def generate_dossier_pdf(calc_data, metadata=None):
    metadata = metadata or calc_data.get("Audit_Metadata", {})
    sec_c = calc_data.get("PIT38_Sekcja_C", {})
    sec_g = calc_data.get("PIT38_Sekcja_G_Art30a", {})
    reconciliation = calc_data.get("Account_Reconciliation", {})
    credit_audit = calc_data.get("Passive_Tax_Credit_Audit", {})
    notes = _review_notes(calc_data)
    buffer = io.BytesIO()
    doc = BaseDocTemplate(
        buffer, pagesize=A4,
        title=f"Zestawienie pomocnicze PIT-38 {metadata.get('tax_year', calc_data.get('Target_Tax_Year', ''))}",
        author="Foreign Capital Gains PL Tax Support",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "DossierTitle", parent=styles["Title"], fontName=PDF_BOLD,
        fontSize=18, leading=22, textColor=colors.HexColor("#17212f"), alignment=TA_CENTER,
        spaceAfter=6,
    )
    heading = ParagraphStyle(
        "DossierHeading", parent=styles["Heading2"], fontName=PDF_BOLD, fontSize=12,
        textColor=colors.HexColor("#26364a"), spaceBefore=9, spaceAfter=5,
    )
    ledger_heading = ParagraphStyle(
        "DossierLedgerHeading", parent=heading, fontName=PDF_BOLD, fontSize=14,
        leading=17, textColor=colors.HexColor("#17212f"), spaceBefore=10, spaceAfter=7,
    )
    body = ParagraphStyle("DossierBody", parent=styles["BodyText"], fontName=PDF_FONT, fontSize=8.5, leading=12, spaceAfter=5)
    small = ParagraphStyle("DossierSmall", parent=styles["BodyText"], fontName=PDF_FONT, fontSize=7, leading=9)

    story = [
        Paragraph("ZESTAWIENIE POMOCNICZE DO PIT-38 / PIT-ZG", title_style),
        Paragraph(
            _pdf_safe(f"Dane z rachunku Interactive Brokers | Rok podatkowy {metadata.get('tax_year', calc_data.get('Target_Tax_Year', '—'))}"),
            ParagraphStyle("DossierSubtitle", parent=styles["Heading3"], fontName=PDF_BOLD),
        ),
        Spacer(1, 4),
    ]
    story.append(Paragraph(
        "To jest material pomocniczy pokazujacy, skad pochodza liczby i jak zostaly przeliczone. "
        "Nie jest formularzem urzedowym PIT-38/PIT-ZG ani opinia podatkowa. Obejmuje tylko dane z IBKR; "
        "kwoty od polskich brokerow trzeba dolaczyc i rozliczyc osobno.", body
    ))

    metadata_rows = [
        ["Element", "Informacja"],
        ["Plik zrodlowy IBKR", metadata.get("source_file", "Nie zapisano")],
        ["SHA-256 pliku", metadata.get("source_sha256", "Nie zapisano")],
        ["Utworzono", metadata.get("generated_at", "Nie zapisano")],
        ["Rok podatkowy", metadata.get("tax_year", calc_data.get("Target_Tax_Year", "—"))],
        ["Wynik importu", {
            "success": "obliczenia wykonane; brak uwag importera",
            "needs_review": "obliczenia wykonane; wymagaja sprawdzenia",
            "error": "obliczenia przerwane",
        }.get(metadata.get("result_status"), metadata.get("result_status", "Nie zapisano"))],
        ["Wiersze obsluzone przez kalkulator", metadata.get("processed_records", "—")],
        ["Wiersze wykluczone z obliczen PIT-38", calc_data.get("Excluded_Activity_Summary", {}).get("total", 0)],
        ["Format pliku IBKR", calc_data.get("Source_Format", "Nie rozpoznano")],
        ["Mapowanie kolumn CSV", metadata.get("column_mapping", "Nie zapisano")],
        ["Ustawienia walut/krajow", metadata.get("currency_country_settings", "Nie zapisano")],
        ["Mapowanie tickerow na kraje", metadata.get("source_country_mapping", "Nie zapisano")],
        ["Stany otwarcia / zamkniecia", metadata.get("unit_snapshot_settings", "Nie zapisano")],
        ["Rozliczana strata z lat ubieglych", metadata.get("loss_carryforward_settings", "Nie zapisano")],
    ]
    story.extend([_table(metadata_rows, [49 * mm, 125 * mm], 7), Spacer(1, 5)])

    story.append(Paragraph("1. Jak czytac podsumowanie", heading))
    capital_rows = [
        ["Pozycja PIT-38", "Wartosc z IBKR (PLN)", "Co oznacza"],
        ["22", _money(sec_c.get("Inne_przychody_Poz_22")), "Przychody ze sprzedazy wykazane przez IBKR."],
        ["23", _money(sec_c.get("Koszty_uzyskania_Poz_23")), "Koszty sprzedazy i przypisany koszt nabycia FIFO."],
        ["26 / 27", f"{_money(sec_c.get('Przychody_Razem_Poz_26'))} / {_money(sec_c.get('Koszty_Razem_Poz_27'))}", "Suma tej czesci danych IBKR; nie obejmuje innych brokerow."],
        ["28 / 29", f"{_money(sec_c.get('Dochod_Poz_28'))} / {_money(sec_c.get('Strata_Poz_29'))}", "Dochodu lub straty tylko z danych IBKR."],
        ["30 / 31", f"{_money(sec_c.get('Strata_z_lat_ubieglych_Poz_30'))} / {_money(sec_c.get('Podstawa_Poz_31'))}", "Wynik po odliczeniu wpisanej straty; sprawdz jej uprawnienie."],
        ["33 / 34 / 35", f"{_money(sec_c.get('Podatek_Poz_33'))} / {_money(sec_c.get('Podatek_zagraniczny_Poz_34'))} / {_money(sec_c.get('Podatek_nalezny_Poz_35'))}", "Podatek od tej czesci danych. Nie jest to laczny wynik ze wszystkich brokerow."],
    ]
    story.append(_table(capital_rows, [31 * mm, 35 * mm, 108 * mm], 6.8))
    passive_rows = [
        ["Pozycja", "Kwota (PLN)", "Opis"],
        ["Odsetki / dywidendy brutto", f"{_money(sec_g.get('Odsetki_brutto_PLN'))} / {_money(sec_g.get('Dywidendy_brutto_PLN'))}", "Przychod z odsetek i dywidend po przeliczeniu na PLN."],
        ["47", _money(sec_g.get("Podatek_Art30a_Poz_47")), "Polski podatek obliczony od dochodow pasywnych ujetych przez kalkulator."],
        ["Podatek zagraniczny zaplacony", _money(sec_g.get("Podatek_zagraniczny_pobrany_Art30a_PLN")), "Kwota z danych o potraceniu podatku."],
        ["48", _money(sec_g.get("Podatek_zagraniczny_uznany_Poz_48")), "Odliczenie ograniczone limitem podatku polskiego."],
        ["49 / nieodliczone", f"{_money(sec_g.get('Roznica_Poz_49'))} / {_money(sec_g.get('Podatek_zagraniczny_nierozliczony_PLN'))}", "Podatek do zaplaty / czesc zagraniczna, ktorej kalkulator nie odliczyl."],
    ]
    story.extend([Spacer(1, 7), _table(passive_rows, [36 * mm, 37 * mm, 101 * mm], 6.8)])

    story.append(Paragraph("2. PIT/ZG (8) — przewodnik po pozycjach formularza", heading))
    story.append(Paragraph(
        "Sklada sie oddzielny zalacznik PIT/ZG dla kazdego panstwa uzyskania dochodu/przychodu. "
        "W formularzu uzupelnij poz. 1 (NIP/PESEL), poz. 2 (rok), poz. 3–5 (nazwisko, imie, data urodzenia), "
        "poz. 6 (pelna nazwa panstwa) i poz. 7 (dwuliterowy kod kraju). W tabeli ponizej kod IBKR trafia do poz. 7; "
        "w poz. 6 recznie wpisz pelna nazwe panstwa odpowiadajaca temu kodowi. Kwota w kolumnie poz. 29 jest "
        "do przepisania do PIT/ZG C.3 poz. 29 (dochod z art. 30b ust. 5a i 5b). Kwota w kolumnie poz. 30 jest "
        "do przepisania do poz. 30 (podatek zaplacony za granica od dochodu z poz. 29), przed limitem odliczenia. "
        "To wklad z IBKR, nie gotowy formularz: potwierdz kraj i kwalifikacje podatkowa, dodaj inne zrodla dla kraju. "
        "PIT/ZG poz. 31–32 dotycza art. 30b ust. 5e i 5f (waluty wirtualne) i nie sa obliczane przez aplikacje.", small,
    ))
    story.append(Paragraph(
        f'<link href="{MF_PITZG_FORM_URL}" color="#475569">Aktualny formularz PIT/ZG (8) Ministerstwa Finansow</link>',
        small,
    ))
    position_rows = [[
        "PIT/ZG poz. 6 — nazwa panstwa (wpisz)", "Poz. 7 — kod kraju",
        "Poz. 29 — dochod Art. 30b ust. 5a/5b (PLN)",
        "Poz. 30 — podatek od dochodu z poz. 29 (PLN)",
    ]]
    for item in calc_data.get("PIT_ZG_Position_Summary", []):
        country_code = str(item.get("Kraj", "")).upper()
        position_rows.append([
            f"Wpisz pelna nazwe dla kodu {country_code}", country_code,
            _money(item.get("Poz_29_Dochod_Art30b_5a_5b_PLN")),
            _money(item.get("Poz_30_Podatek_zagraniczny_od_Poz_29_PLN")),
        ])
    if len(position_rows) == 1:
        position_rows.append(["Brak przypisanych kwot.", "—", "—", "—"])
    story.append(_table(position_rows, [47 * mm, 20 * mm, 60 * mm, 60 * mm], 5.8))
    story.append(Spacer(1, 7))

    story.append(Paragraph(
        "Szczegoly wspierajace obliczenia — nie wpisuj ich jako odrebnych pol PIT/ZG", heading,
    ))
    story.append(Paragraph(
        "Przychod i koszt Art. 30b sa skladnikami obliczenia kwoty PIT/ZG poz. 29; do formularza przepisz "
        "wynik netto z tabeli PIT/ZG powyzej, a nie te skladniki osobno. Odsetki i dywidendy Art. 30a nie sa "
        "polami PIT/ZG; aplikacja pokazuje je jako szczegoly pomocnicze dla lacznych kwot PIT-38, sekcja G, poz. 47–49. "
        "Podatek zagraniczny ze wszystkich kategorii w tej tabeli nie jest kwota do wpisania w PIT/ZG poz. 30.", small,
    ))
    zg_rows = [[
        "Kod kraju", "Art. 30b przychod (podstawa poz. 29)", "Art. 30b koszt (podstawa poz. 29)",
        "Odsetki Art. 30a — nie PIT/ZG", "Dywidendy Art. 30a — nie PIT/ZG",
        "Podatek zagraniczny — wszystkie kategorie, pomocniczo",
    ]]
    for item in calc_data.get("PIT_ZG_Attachments", []):
        zg_rows.append([
            item.get("Kraj"), _money(item.get("Przychod_Art30b")), _money(item.get("Koszty_Art30b")),
            _money(item.get("Odsetki_Art30a")), _money(item.get("Dywidendy_Art30a")),
            _money(item.get("Podatek_Zagraniczny")),
        ])
    if len(zg_rows) == 1:
        zg_rows.append(["Brak potwierdzonego przypisania kraju.", "—", "—", "—", "—", "—"])
    story.append(_table(zg_rows, [20 * mm, 32 * mm, 32 * mm, 25 * mm, 25 * mm, 40 * mm], 6.2))
    story.append(Paragraph(
        "Mapowanie kraju wymaga sprawdzenia dla kazdego instrumentu i rodzaju dochodu. Kraj rachunku IBKR "
        "nie zawsze jest krajem zrodla dochodu.", small
    ))

    story.append(Paragraph("3. Jak kalkulator dochodzi do kwot", heading))
    methodology = [
        "Transakcje kupna i sprzedazy sa porzadkowane chronologicznie. Przy sprzedazy koszt nabycia jest przypisywany metoda FIFO: najpierw rozliczane sa najstarsze dostepne zakupy tego samego tickeru.",
        "Kwoty walutowe sa przeliczane kursem srednim NBP z ostatniego dnia roboczego przed uzyskaniem przychodu, poniesieniem kosztu, wydatku lub zaplata podatku. Rejestr CSV podaje walute, kurs, date obowiazywania, numer tabeli, link do NBP i wiersz pliku IBKR.",
        "Prowizja od kupna jest doliczana do kosztu nabycia; prowizja od sprzedazy jest doliczana do kosztu sprzedazy. Kazdy wiersz sprzedazy wskazuje wykorzystane zakupy FIFO.",
        "Dywidendy, odsetki i zagraniczne potracenia podatku sa ujete oddzielnie od zbycia papierow. CSV pokazuje przeliczenie kazdej pozycji z waluty pierwotnej na PLN.",
        "Aplikacja ogranicza poz. 48 limitem wyliczonym dla dochodu pasywnego z danego kraju, a nastepnie suma nie moze przekroczyc poz. 47. Szczegoly sa w pliku podsumowania CSV; sprawdz zastosowanie limitu do swojej sytuacji.",
        "Transfery i dzialania korporacyjne sa uwzgledniane w uzgodnieniu liczby jednostek, ale ich koszt podatkowy nie jest ustalany automatycznie. Zdarzenia oznaczone jako wymagajace sprawdzenia nie powinny byc traktowane jako rozstrzygniete.",
    ]
    story.extend(Paragraph(f"• {_pdf_safe(item)}", body) for item in methodology)
    story.append(Paragraph(
        "Podstawa informacyjna: aktualna broszura Ministerstwa Finansow do PIT-38 (sekcja formularzy PIT) "
        f"{MF_PIT38_URL}. Broszura wskazuje sredni kurs NBP z ostatniego dnia roboczego przed uzyskaniem "
        "przychodu, poniesieniem kosztu, wydatku lub zaplata podatku. Sprawdz zastosowanie reguly dla konkretnej operacji i roku.", small
    ))

    story.append(Paragraph("4. Uzgodnienie liczby jednostek z IBKR", heading))
    story.append(Paragraph(
        "Wzor: stan poczatkowy + kupna - sprzedaze + transfery przychodzace - transfery wychodzace "
        "+ zmiana z dzialan korporacyjnych = obliczony stan koncowy. Numer wiersza wskazuje pozycje w CSV.", small
    ))
    rec_rows = [["Ticker", "Otwarcie", "Kupno", "Sprzedaz", "Transf. +", "Transf. -", "Akcje korp.", "Obliczono", "IBKR", "Roznica", "Status"]]
    event_labels = {
        "BUY": "KUPNO", "SELL": "SPRZEDAZ", "TRANSFER_IN": "TRANSFER PRZYCHODZACY",
        "TRANSFER_OUT": "TRANSFER WYCHODZACY", "CORPORATE_ACTION": "DZIALANIE KORPORACYJNE",
    }
    for item in reconciliation.get("Rows", []):
        rec_rows.append([
            item.get("Ticker"), item.get("Opening units"), item.get("Buys"), item.get("Sells"),
            item.get("Transfers in"), item.get("Transfers out"), item.get("Corporate action units (net)"),
            item.get("Calculated closing units"), item.get("IBKR closing units"),
            item.get("Difference (calculated - IBKR)"), RECONCILIATION_STATUS_PL.get(item.get("Status"), item.get("Status")),
        ])
    if len(rec_rows) == 1:
        rec_rows.append(["Brak danych", "—", "—", "—", "—", "—", "—", "—", "—", "—", reconciliation.get("Status", "—")])
    story.append(_table(rec_rows, [18 * mm, 18 * mm, 15 * mm, 15 * mm, 14 * mm, 14 * mm, 19 * mm, 18 * mm, 15 * mm, 16 * mm, 18 * mm], 5.4))
    event_rows = [["Data", "Ticker", "Zdarzenie", "Jednostki", "Wiersz CSV", "Opis"]]
    for event in reconciliation.get("Unit events", []):
        event_rows.append([
            event.get("Date"), event.get("Ticker"), event_labels.get(event.get("Event"), event.get("Event")), event.get("Quantity"),
            event.get("Row"), event.get("Description"),
        ])
    if len(event_rows) > 1:
        story.append(Spacer(1, 5))
        story.append(_table(event_rows, [23 * mm, 20 * mm, 35 * mm, 21 * mm, 22 * mm, 82 * mm], 6.0))

    story.append(Paragraph("5. Limit odliczenia podatku zagranicznego", heading))
    story.append(Paragraph(_pdf_safe(credit_audit.get("Formula_PL", "Szczegolowe wyliczenie limitu nie zostalo zapisane.")), body))
    credit_rows = [["Kraj", "Odsetki", "Dywidendy", "Limit podatku PL", "Podatek zaplacony", "Limit przed limitem lacznym"]]
    for item in credit_audit.get("By country", []):
        credit_rows.append([
            item.get("Country"), _money(item.get("Interest income (PLN)")),
            _money(item.get("Dividend income (PLN)")), _money(item.get("Polish tax cap for country (PLN)")),
            _money(item.get("Foreign tax paid (PLN)")), _money(item.get("Credit before overall cap (PLN)")),
        ])
    if len(credit_rows) == 1:
        credit_rows.append(["—", "—", "—", "—", "—", "—"])
    story.append(_table(credit_rows, [22 * mm, 26 * mm, 26 * mm, 31 * mm, 30 * mm, 40 * mm], 6.0))
    story.append(Paragraph(
        f"Poz. 47: {_money(sec_g.get('Podatek_Art30a_Poz_47'))} PLN; "
        f"podatek zagraniczny: {_money(sec_g.get('Podatek_zagraniczny_pobrany_Art30a_PLN'))} PLN; "
        f"nieodliczone: {_money(sec_g.get('Podatek_zagraniczny_nierozliczony_PLN'))} PLN.", body
    ))

    story.append(Paragraph("6. Nierozstrzygniete kwestie i dokumenty zrodlowe", heading))
    if notes:
        for note in notes:
            story.append(Paragraph(f"• {_pdf_safe(_polish_review_note(note))}", body))
    else:
        story.append(Paragraph("Importer nie wygenerowal nierozstrzygnietych uwag; nadal sprawdz dokumenty zrodlowe.", body))
    source_docs = [
        "Oryginalny raport Activity Statement / CSV z IBKR odpowiadajacy podanemu SHA-256.",
        "Potwierdzenia transakcji i pozycje rachunku na dzien otwarcia oraz zamkniecia okresu.",
        "Potwierdzenia dywidend i zagranicznego podatku u zrodla.",
        "Dokumenty transferow oraz dzialan korporacyjnych wraz z informacja o pierwotnym koszcie nabycia.",
        "Zlozony PIT-38/PIT-ZG, UPO oraz obliczenia z pozostalych rachunkow, jesli sa potrzebne do uzgodnienia sumy.",
    ]
    story.append(Paragraph("Zachowaj i dolacz na zadanie urzedu odpowiednie dokumenty:", body))
    story.extend(Paragraph(f"• {_pdf_safe(item)}", small) for item in source_docs)
    story.append(Paragraph(
        "Dokumenty zrodlowe nie sa zastapione przez ten raport. Sam raport nie przesadza o prawidlowosci "
        "kwalifikacji podatkowej instrumentu ani kraju zrodla.", small
    ))

    story.extend([
        NextPageTemplate("landscape"),
        PageBreak(),
        Paragraph("7. Szczegółowy rejestr: operacje, przeliczenia i koszty", ledger_heading),
        Paragraph(
            "Ponizsza tabela zawiera kazda operacje wykorzystana w kalkulacji. Kolumna 'Wiersz IBKR' "
            "pozwala znalezc rekord w oryginalnym CSV. Przy sprzedazy kolumna FIFO wskazuje date i numer "
            "wiersza zakupu, z ktorego pochodzi koszt.", body
        ),
    ])
    story.append(transaction_ledger_table(calc_data, "PL"))
    if calc_data.get("Excluded_Activity"):
        story.extend([
            PageBreak(),
            Paragraph("8. Operacje z IBKR wykluczone z obliczen PIT-38", ledger_heading),
            Paragraph(
                "Ponizsze wiersze zachowano w rejestrze zrodlowym i wykluczono z obliczen PIT-38. "
                "Nie oznacza to rozstrzygniecia ich odrebnego traktowania podatkowego; w szczegolnosci "
                "raport nie ocenia, czy rzeczywista wymiana walut wymaga osobnego wykazania.",
                body,
            ),
            excluded_activity_table(calc_data, "PL"),
        ])
    rate_section_number = 9 if calc_data.get("Excluded_Activity") else 8
    story.extend([Spacer(1, 8), Paragraph(f"{rate_section_number}. Rejestr kursow NBP uzytych w obliczeniach", heading)])
    story.append(Paragraph(
        "Numer tabeli i link odnosza sie do opublikowanych danych NBP; lista transakcji wskazuje, "
        "ktore rekordy wykorzystaly dany kurs.", body
    ))
    rate_rows = [[
        "Waluta", "Kurs", "Data obowiazywania", "Numer tabeli", "Transakcje: data / wiersz IBKR", "Zrodlo NBP",
    ]]
    for quote in _nbp_rate_rows(calc_data):
        rate_rows.append([
            quote.get("Waluta"), quote.get("Kurs NBP"), quote.get("Data obowiazywania"),
            quote.get("Numer tabeli"), quote.get("Transakcje (data i wiersz IBKR)"), quote.get("Link do danych NBP"),
        ])
    if len(rate_rows) == 1:
        rate_rows.append(["PLN lub brak kursow walut obcych", "—", "—", "—", "—", "—"])
    story.append(_table(rate_rows, [20 * mm, 22 * mm, 29 * mm, 33 * mm, 76 * mm, 68 * mm], font_size=6.0, long_table=True))

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont(PDF_FONT, 7)
        canvas.setFillColor(colors.HexColor("#64748b"))
        page_width, _ = canvas._pagesize
        canvas.drawString(10 * mm, 8 * mm, f"PIT-38 / IBKR | Rok {metadata.get('tax_year', '')} | Material pomocniczy")
        canvas.drawRightString(page_width - 10 * mm, 8 * mm, f"Strona {_doc.page}")
        canvas.restoreState()

    doc.addPageTemplates([
        PageTemplate(
            id="portrait",
            frames=[Frame(15 * mm, 16 * mm, A4[0] - 30 * mm, A4[1] - 31 * mm, id="portrait-frame", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)],
            pagesize=A4,
            onPage=footer,
        ),
        PageTemplate(
            id="landscape",
            frames=[Frame(10 * mm, 14 * mm, landscape(A4)[0] - 20 * mm, landscape(A4)[1] - 24 * mm, id="landscape-frame", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)],
            pagesize=landscape(A4),
            onPage=footer,
        ),
    ])
    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


def _polish_ledger_csv(calc_data):
    rows = []
    for step in calc_data.get("Calculation_Steps", []):
        rows.append({
            "Rodzaj rekordu": "Transakcja podatkowa",
            "Rok": step.get("Year"),
            "Data": step.get("Date"),
            "Wiersz zrodlowego CSV IBKR": step.get("Source row"),
            "Operacja": step.get("Action_PL"),
            "Ticker": step.get("Ticker"),
            "Waluta": step.get("Curr"),
            "Ilosc": step.get("Qty"),
            "Kwota/cena w walucie": step.get("Price (FX)"),
            "Prowizja w walucie": step.get("Comm (FX)"),
            "Kurs NBP": step.get("NBP Rate"),
            "Data obowiazywania kursu NBP": step.get("NBP Date"),
            "Numer tabeli NBP": step.get("NBP Table"),
            "Link do danych NBP": step.get("NBP Source"),
            "Prowizja PLN": step.get("Comm (PLN)"),
            "Przychod PLN": step.get("Revenue (PLN)"),
            "Koszt PLN": step.get("Cost Basis (PLN)"),
            "Wynik PLN": step.get("Net (PLN)"),
            "Wzor i szczegoly FIFO": step.get("Details_PL"),
        })

    reconciliation = calc_data.get("Account_Reconciliation", {})
    for item in reconciliation.get("Rows", []):
        rows.append({
            "Rodzaj rekordu": "Uzgodnienie jednostek",
            "Ticker": item.get("Ticker"),
            "Jednostki poczatkowe": item.get("Opening units"),
            "Zrodlo stanu poczatkowego": {
                "Provided": "podano przez uzytkownika",
                "Assumed zero": "przyjeto zero",
                "Not provided": "nie podano",
            }.get(item.get("Opening units source"), item.get("Opening units source")),
            "Kupno": item.get("Buys"),
            "Liczba wierszy kupna": item.get("Buy rows"),
            "Sprzedaz": item.get("Sells"),
            "Liczba wierszy sprzedazy": item.get("Sell rows"),
            "Transfery przychodzace": item.get("Transfers in"),
            "Transfery wychodzace": item.get("Transfers out"),
            "Zmiana jednostek z dzialan korporacyjnych": item.get("Corporate action units (net)"),
            "Wyliczone jednostki koncowe": item.get("Calculated closing units"),
            "Jednostki koncowe IBKR": item.get("IBKR closing units"),
            "Roznica (wyliczone - IBKR)": item.get("Difference (calculated - IBKR)"),
            "Status": RECONCILIATION_STATUS_PL.get(item.get("Status"), item.get("Status")),
            "Uwagi": " | ".join(
                _polish_review_note(note) for note in str(item.get("Issues") or "").split(" | ") if note
            ),
        })
    event_labels = {
        "BUY": "KUPNO", "SELL": "SPRZEDAZ", "TRANSFER_IN": "TRANSFER PRZYCHODZACY",
        "TRANSFER_OUT": "TRANSFER WYCHODZACY", "CORPORATE_ACTION": "DZIALANIE KORPORACYJNE",
    }
    for event in reconciliation.get("Unit events", []):
        rows.append({
            "Rodzaj rekordu": "Zdarzenie jednostek",
            "Data": event.get("Date"),
            "Ticker": event.get("Ticker"),
            "Zdarzenie": event_labels.get(event.get("Event"), event.get("Event")),
            "Liczba jednostek": event.get("Quantity"),
            "Opis z IBKR": event.get("Description"),
            "Wiersz CSV IBKR": event.get("Row"),
            "Uwzgledniono w uzgodnieniu": event.get("Included"),
        })
    for item in calc_data.get("Excluded_Activity", []):
        rows.append({
            "Rodzaj rekordu": "Aktywnosc wykluczona z PIT-38",
            "Data": item.get("Date"),
            "Wiersz zrodlowego CSV IBKR": str(item.get("Source row", "")),
            "Typ transakcji IBKR": item.get("IBKR transaction type"),
            "Kategoria": item.get("Category"),
            "Ticker / symbol": item.get("Symbol"),
            "Opis z IBKR": item.get("Description"),
            "Powod wykluczenia": _polish_excluded_reason(item),
        })
    for note in _review_notes(calc_data):
        rows.append({
            "Rodzaj rekordu": "Uwaga do sprawdzenia",
            "Wyjasnienie po polsku": _polish_review_note(note),
            "Oryginalna uwaga systemu": note,
        })
    return pd.DataFrame(rows).to_csv(index=False, sep=";", decimal=",", encoding="utf-8-sig").encode("utf-8-sig")


def _polish_summary_csv(calc_data, metadata):
    sec_c = calc_data.get("PIT38_Sekcja_C", {})
    sec_g = calc_data.get("PIT38_Sekcja_G_Art30a", {})
    rows = [
        {"Sekcja": "Metadane", "Pole": "Plik zrodlowy IBKR", "Wartosc": metadata.get("source_file")},
        {"Sekcja": "Metadane", "Pole": "SHA-256 pliku", "Wartosc": metadata.get("source_sha256")},
        {"Sekcja": "Metadane", "Pole": "Data utworzenia", "Wartosc": metadata.get("generated_at")},
        {"Sekcja": "Metadane", "Pole": "Rok podatkowy", "Wartosc": metadata.get("tax_year")},
        {"Sekcja": "Metadane", "Pole": "Wiersze wykluczone z obliczen PIT-38", "Wartosc": calc_data.get("Excluded_Activity_Summary", {}).get("total", 0)},
        {"Sekcja": "Metadane", "Pole": "Format pliku IBKR", "Wartosc": calc_data.get("Source_Format")},
        {"Sekcja": "Metadane", "Pole": "Mapowanie kolumn CSV", "Wartosc": metadata.get("column_mapping")},
    ]
    position_fields = [
        ("22", "Przychod z danych IBKR", "Inne_przychody_Poz_22"),
        ("23", "Koszty z danych IBKR", "Koszty_uzyskania_Poz_23"),
        ("26", "Przychody razem w imporcie IBKR", "Przychody_Razem_Poz_26"),
        ("27", "Koszty razem w imporcie IBKR", "Koszty_Razem_Poz_27"),
        ("28", "Dochod z danych IBKR", "Dochod_Poz_28"),
        ("29", "Strata z danych IBKR", "Strata_Poz_29"),
        ("30", "Odliczona strata z lat ubieglych", "Strata_z_lat_ubieglych_Poz_30"),
        ("31", "Podstawa z danych IBKR", "Podstawa_Poz_31"),
        ("33", "Podatek przed odliczeniem", "Podatek_Poz_33"),
        ("34", "Podatek zagraniczny od zbycia", "Podatek_zagraniczny_Poz_34"),
        ("35", "Podatek nalezny tylko dla zaimportowanych danych", "Podatek_nalezny_Poz_35"),
    ]
    for position, label, key in position_fields:
        rows.append({"Sekcja": "PIT-38 - wklad IBKR", "Pole": f"Poz. {position}: {label}", "Wartosc": sec_c.get(key)})
    for position, label, key in [
        ("47", "Podatek polski od dochodow pasywnych", "Podatek_Art30a_Poz_47"),
        ("48", "Dozwolone odliczenie podatku zagranicznego", "Podatek_zagraniczny_uznany_Poz_48"),
        ("49", "Roznica / kwota do zaplaty", "Roznica_Poz_49"),
    ]:
        rows.append({"Sekcja": "PIT-38 - dochody pasywne IBKR", "Pole": f"Poz. {position}: {label}", "Wartosc": sec_g.get(key)})
    rows.append({
        "Sekcja": "Limit odliczenia", "Pole": calc_data.get("Passive_Tax_Credit_Audit", {}).get("Formula_PL", ""),
        "Wartosc": sec_g.get("Podatek_zagraniczny_uznany_Poz_48"),
    })
    for country_row in calc_data.get("Passive_Tax_Credit_Audit", {}).get("By country", []):
        credit_labels = {
            "Country": "Kraj",
            "Interest income (PLN)": "Odsetki (PLN)",
            "Dividend income (PLN)": "Dywidendy (PLN)",
            "Passive income (PLN)": "Dochody pasywne razem (PLN)",
            "Polish tax cap for country (PLN)": "Limit podatku polskiego dla kraju (PLN)",
            "Foreign tax paid (PLN)": "Podatek zagraniczny zaplacony (PLN)",
            "Credit before overall cap (PLN)": "Odliczenie przed limitem lacznym (PLN)",
        }
        for field, value in country_row.items():
            rows.append({
                "Sekcja": f"Limit odliczenia - {country_row.get('Country')}",
                "Pole": credit_labels.get(field, field),
                "Wartosc": value,
            })
    for item in calc_data.get("PIT_ZG_Position_Summary", []):
        country = str(item.get("Kraj", "")).upper()
        section = f"PIT/ZG (8), formularz dla kraju {country} — wklad IBKR"
        rows.extend([
            {
                "Sekcja": section,
                "Pole": "Poz. 6: pelna nazwa panstwa (wpisz nazwe odpowiadajaca kodowi z poz. 7)",
                "Wartosc": f"UZUPELNIJ NAZWE DLA KODU {country}",
            },
            {
                "Sekcja": section,
                "Pole": "Poz. 7: dwuliterowy kod panstwa (ISO)",
                "Wartosc": country,
            },
            {
                "Sekcja": section,
                "Pole": "Poz. 29: dochod z art. 30b ust. 5a/5b (PLN) — przepisz do PIT/ZG",
                "Wartosc": item.get("Poz_29_Dochod_Art30b_5a_5b_PLN"),
            },
            {
                "Sekcja": section,
                "Pole": "Poz. 30: podatek zaplacony za granica od dochodu z poz. 29 (PLN), przed limitem — przepisz do PIT/ZG",
                "Wartosc": item.get("Poz_30_Podatek_zagraniczny_od_Poz_29_PLN"),
            },
        ])
    for item in calc_data.get("PIT_ZG_Attachments", []):
        zg_labels = {
            "Kraj": "Kod kraju (PIT/ZG poz. 7)",
            "Przychod_Art30b": "Art. 30b: przychod wspierajacy obliczenie PIT/ZG poz. 29 — nie odrebne pole formularza, PLN",
            "Koszty_Art30b": "Art. 30b: koszty FIFO wspierajace obliczenie PIT/ZG poz. 29 — nie odrebne pole formularza, PLN",
            "Dochod_Art30b": "Art. 30b: wynik netto wspierajacy PIT/ZG poz. 29, PLN",
            "Strata_Art30b": "Art. 30b: strata pomocnicza; nie wykazuje sie jej w PIT/ZG poz. 29 jako dochodu, PLN",
            "Odsetki_Art30a": "Odsetki Art. 30a — nie PIT/ZG; PIT-38 sekcja G, pozycje laczne 47–49, PLN",
            "Dywidendy_Art30a": "Dywidendy Art. 30a — nie PIT/ZG; PIT-38 sekcja G, pozycje laczne 47–49, PLN",
            "Podatek_Zagraniczny": "Podatek zagraniczny — wszystkie kategorie, pomocniczo; nie kopiuj jako PIT/ZG poz. 30, PLN",
        }
        for field, value in item.items():
            rows.append({
                "Sekcja": f"Szczegoly pomocnicze wedlug kraju — sprawdz mapowanie ({item.get('Kraj')})",
                "Pole": zg_labels.get(field, field),
                "Wartosc": value,
            })

    reconciliation = calc_data.get("Account_Reconciliation", {})
    rows.append({"Sekcja": "Uzgodnienie jednostek", "Pole": "Status calosciowy", "Wartosc": RECONCILIATION_STATUS_PL.get(reconciliation.get("Status"), reconciliation.get("Status"))})
    for item in reconciliation.get("Rows", []):
        rows.append({
            "Sekcja": f"Uzgodnienie jednostek - {item.get('Ticker')}",
            "Pole": "Roznica (wyliczone - IBKR)",
            "Wartosc": item.get("Difference (calculated - IBKR)"),
        })
    for note in _review_notes(calc_data):
        rows.append({"Sekcja": "Do sprawdzenia", "Pole": _polish_review_note(note), "Wartosc": note})
    return pd.DataFrame(rows).to_csv(index=False, sep=";", decimal=",", encoding="utf-8-sig").encode("utf-8-sig")


def _nbp_rate_rows(calc_data):
    unique = {}
    for step in calc_data.get("Calculation_Steps", []):
        if step.get("Curr") == "PLN":
            continue
        key = (step.get("Curr"), step.get("NBP Date"), step.get("NBP Rate"), step.get("NBP Table"))
        unique.setdefault(key, {
            "Waluta": step.get("Curr"),
            "Kurs NBP": step.get("NBP Rate"),
            "Data obowiazywania": step.get("NBP Date"),
            "Numer tabeli": step.get("NBP Table"),
            "Link do danych NBP": step.get("NBP Source"),
            "Transakcje (data i wiersz IBKR)": [],
        })
        unique[key]["Transakcje (data i wiersz IBKR)"].append(
            f"{step.get('Date')} / wiersz {step.get('Source row') or '?'}"
        )
    rows = []
    for quote in unique.values():
        quote["Transakcje (data i wiersz IBKR)"] = ", ".join(quote["Transakcje (data i wiersz IBKR)"])
        rows.append(quote)
    return rows


def _nbp_rates_csv(calc_data):
    rows = _nbp_rate_rows(calc_data)
    return pd.DataFrame(rows).to_csv(index=False, sep=";", decimal=",", encoding="utf-8-sig").encode("utf-8-sig")


def _polish_excluded_activity_csv(calc_data):
    rows = [{
        "Data": item.get("Date"),
        "Wiersz zrodlowego CSV IBKR": str(item.get("Source row", "")),
        "Typ transakcji IBKR": item.get("IBKR transaction type"),
        "Kategoria": item.get("Category"),
        "Ticker / symbol": item.get("Symbol"),
        "Opis z IBKR": item.get("Description"),
        "Powod wykluczenia z obliczen PIT-38": _polish_excluded_reason(item),
    } for item in calc_data.get("Excluded_Activity", [])]
    if not rows:
        rows = [{
            "Data": "",
            "Wiersz zrodlowego CSV IBKR": "",
            "Typ transakcji IBKR": "",
            "Kategoria": "Brak wykluczonych wierszy",
            "Ticker / symbol": "",
            "Opis z IBKR": "",
            "Powod wykluczenia z obliczen PIT-38": "",
        }]
    return pd.DataFrame(rows).to_csv(index=False, sep=";", encoding="utf-8-sig").encode("utf-8-sig")


def generate_audit_package(
    calc_data, metadata=None, original_csv=None, include_original=False, dossier_pdf=None,
):
    metadata = metadata or calc_data.get("Audit_Metadata", {})
    source_bytes = original_csv if include_original and original_csv is not None else None
    if source_bytes is not None:
        digest = hashlib.sha256(source_bytes).hexdigest()
        if metadata.get("source_sha256") and digest != metadata["source_sha256"]:
            raise ValueError("The source CSV does not match the SHA-256 recorded for this calculation.")

    buffer = io.BytesIO()
    year = metadata.get("tax_year", calc_data.get("Target_Tax_Year", "rok"))
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"01_Raport_i_pelny_rejestr_PIT38_IBKR_{year}.pdf",
            dossier_pdf if dossier_pdf is not None else generate_dossier_pdf(calc_data, metadata),
        )
        archive.writestr(f"02_Rejestr_obliczen_IBKR_{year}.csv", _polish_ledger_csv(calc_data))
        archive.writestr(f"03_Podsumowanie_PIT38_PITZG_{year}.csv", _polish_summary_csv(calc_data, metadata))
        archive.writestr(f"04_Rejestr_kursow_NBP_{year}.csv", _nbp_rates_csv(calc_data))
        archive.writestr(f"05_Wiersze_wylaczone_z_PIT38_{year}.csv", _polish_excluded_activity_csv(calc_data))

        readme = [
            "PAKIET DOKUMENTOW POMOCNICZYCH DO PIT-38 / PIT-ZG",
            f"Rok podatkowy: {year}",
            f"Plik zrodlowy: {metadata.get('source_file', 'nie zapisano')}",
            f"SHA-256 pliku zrodlowego: {metadata.get('source_sha256', 'nie zapisano')}",
            f"Utworzono: {metadata.get('generated_at', 'nie zapisano')}",
            "",
            "ZAWARTOSC PAKIETU",
            "01 - Kompletny polski PDF: podsumowanie, metoda, limity, uzgodnienie, kazda transakcja i kursy NBP.",
            "02 - Szczegolowy rejestr transakcji, wierszy IBKR, kursow NBP i obliczen FIFO.",
            "03 - Podsumowanie wartosci pomocniczych PIT-38/PIT-ZG i nierozstrzygnietych kwestii.",
            "04 - Rejestr unikalnych kursow NBP z data, numerem tabeli i linkiem do zrodla.",
            "05 - Surowe wiersze IBKR wykluczone z obliczen PIT-38, z numerami wierszy i powodami.",
            "",
            "DOKUMENTY, KTORYCH TEN PAKIET NIE ZASTEPUJE",
            "Zachowaj oryginalny raport IBKR, potwierdzenia transakcji, zestawienia pozycji otwarcia/zamkniecia,",
            "potwierdzenia dywidend i podatku u zrodla, dokumenty transferow/dzialan korporacyjnych oraz zlozony PIT-38/PIT-ZG i UPO.",
            "Jesli urzad poprosi o plik zrodlowy, dolacz CSV o podanym SHA-256. Surowy CSV jest opcjonalnie dolaczany w tym ZIP-ie.",
            "",
            "Raport jest materialem pomocniczym, nie urzedowym formularzem ani opinia podatkowa.",
            f"Strona Ministerstwa Finansow dotyczaca formularzy PIT-38: {MF_PIT38_URL}",
            f"Serwis kursow NBP: {NBP_API_URL}",
        ]
        archive.writestr("00_CZYTAJ_MNIE_PL.txt", "\n".join(readme).encode("utf-8"))
        if source_bytes is not None:
            original_name = os.path.basename(metadata.get("source_file") or "IBKR_source.csv")
            safe_name = "".join(char for char in original_name if char.isalnum() or char in "-_.") or "IBKR_source.csv"
            archive.writestr(f"06_Zrodlo_IBKR/{safe_name}", source_bytes)
    buffer.seek(0)
    return buffer.getvalue()
