"""CSV exports for tax results, unit reconciliation, and audit notes."""
import pandas as pd


def generate_csv(calc_data):
    rows = []
    audit_metadata = calc_data.get("Audit_Metadata", {})
    if audit_metadata.get("column_mapping"):
        rows.append({
            "Category": "Import column mapping",
            "Field": "Selected CSV column mapping",
            "Value": audit_metadata["column_mapping"],
        })
    excluded = calc_data.get("Excluded_Activity", [])
    excluded_summary = calc_data.get("Excluded_Activity_Summary", {})
    rows.append({
        "Category": "PIT-38 import scope",
        "Field": "Raw activity rows excluded from PIT-38 calculations",
        "Value": excluded_summary.get("total", len(excluded)),
    })
    sec_c = calc_data["PIT38_Sekcja_C"]
    for key, value in sec_c.items():
        rows.append({"Category": "IBKR-only PIT-38 Sekcja C contribution", "Field": key, "Value": value})

    for key, value in calc_data["PIT38_Sekcja_D_Art30b"].items():
        rows.append({"Category": "IBKR-only Section D estimate (Art. 30b)", "Field": key, "Value": value})

    sec_g = calc_data["PIT38_Sekcja_G_Art30a"]
    for key, value in sec_g.items():
        rows.append({"Category": "IBKR-only Section G figures (Art. 30a)", "Field": key, "Value": value})

    rows.append({
        "Category": "IBKR-only illustrative tax estimate",
        "Field": "Do not use as combined return total; combine broker amounts first",
        "Value": calc_data["IBKR_Only_Illustrative_Tax_Estimate_Poz_35_and_49"],
    })

    for item in calc_data.get("PIT_ZG_Position_Summary", []):
        country = item["Kraj"]
        rows.append({
            "Category": f"IBKR contribution to PIT/ZG(8) section C.3 — {country}",
            "Field": "Position 29 — Art. 30b(5a)/(5b) foreign income (PLN)",
            "Value": item["Poz_29_Dochod_Art30b_5a_5b_PLN"],
        })
        rows.append({
            "Category": f"IBKR contribution to PIT/ZG(8) section C.3 — {country}",
            "Field": "Position 30 — foreign tax paid on position 29 income, before limit (PLN)",
            "Value": item["Poz_30_Podatek_zagraniczny_od_Poz_29_PLN"],
        })

    for item in calc_data["PIT_ZG_Attachments"]:
        for key, value in item.items():
            if key != "Kraj":
                rows.append({
                    "Category": f"Supplementary country summary — not the official PIT/ZG form ({item['Kraj']})",
                    "Field": key,
                    "Value": value,
                })

    reconciliation = calc_data.get("Account_Reconciliation", {})
    rows.append({"Category": "Account unit reconciliation", "Field": "Status", "Value": reconciliation.get("Status", "Not available")})
    rows.append({"Category": "Account unit reconciliation", "Field": "Formula", "Value": reconciliation.get("Formula", "")})
    for item in reconciliation.get("Rows", []):
        symbol = item.get("Ticker", "")
        for key, value in item.items():
            rows.append({"Category": f"Unit reconciliation ({symbol})", "Field": key, "Value": value})
    for event in reconciliation.get("Unit events", []):
        for key, value in event.items():
            rows.append({"Category": f"Unit event ({event.get('Ticker', '')})", "Field": key, "Value": value})

    for item in excluded:
        for key, value in item.items():
            rows.append({"Category": "Activity excluded from PIT-38", "Field": key, "Value": value})

    rows.append({
        "Category": "Tax credit cap audit",
        "Field": "Poz. 48 is limited by the Polish tax for the same source-country income and total Poz. 47",
        "Value": sec_g["Podatek_zagraniczny_uznany_Poz_48"],
    })
    notes = dict.fromkeys(calc_data.get("Review_Notes", []) + reconciliation.get("Issues", []))
    for index, note in enumerate(notes, start=1):
        rows.append({"Category": "Unresolved assumption / review", "Field": f"Note {index}", "Value": note})

    return pd.DataFrame(rows).to_csv(index=False).encode("utf-8")


def generate_steps_csv(calc_data, lang_code="EN"):
    steps = calc_data.get("Calculation_Steps", [])
    formatted_steps = []
    for step in steps:
        formatted_steps.append({
            "Record type": "Tax transaction",
            "Rok / Year": step["Year"],
            "Data / Date": step["Date"],
            "Data Kursu NBP / NBP Date": step["NBP Date"],
            "Operacja / Action": step["Action_PL"] if lang_code == "PL" else step["Action_EN"],
            "Ticker": step["Ticker"],
            "Waluta / Currency": step["Curr"],
            "Ilość / Quantity": step["Qty"],
            "Cena / Price (FX)": step["Price (FX)"],
            "Kurs NBP / NBP Rate": step["NBP Rate"],
            "Prowizja PLN / Comm (PLN)": step["Comm (PLN)"],
            "Przychód PLN / Revenue (PLN)": step["Revenue (PLN)"],
            "Koszt PLN / Cost Basis (PLN)": step["Cost Basis (PLN)"],
            "Dochód PLN / Net (PLN)": step["Net (PLN)"],
            "Wzór & FIFO / Audit Formula": step["Details_PL"] if lang_code == "PL" else step["Details_EN"],
        })

    reconciliation = calc_data.get("Account_Reconciliation", {})
    for item in reconciliation.get("Rows", []):
        row = {"Record type": "Unit reconciliation", "Ticker": item.get("Ticker", "")}
        row.update({f"Reconciliation / {key}": value for key, value in item.items() if key != "Ticker"})
        formatted_steps.append(row)
    for event in reconciliation.get("Unit events", []):
        row = {"Record type": "Unit event"}
        row.update({f"Unit event / {key}": value for key, value in event.items()})
        formatted_steps.append(row)

    for item in calc_data.get("Excluded_Activity", []):
        row = {"Record type": "Activity excluded from PIT-38"}
        row.update({
            f"Excluded activity / {key}": str(value) if key == "Source row" else value
            for key, value in item.items()
        })
        formatted_steps.append(row)

    section_g = calc_data.get("PIT38_Sekcja_G_Art30a", {})
    formatted_steps.append({
        "Record type": "Tax-credit cap audit",
        "Tax-credit rule": "Poz. 48 limited by source-country Polish tax and total Poz. 47",
        "Poz. 47 (PLN)": section_g.get("Podatek_Art30a_Poz_47"),
        "Foreign tax paid (PLN)": section_g.get("Podatek_zagraniczny_pobrany_Art30a_PLN"),
        "Poz. 48 allowed credit (PLN)": section_g.get("Podatek_zagraniczny_uznany_Poz_48"),
        "Uncredited foreign tax (PLN)": section_g.get("Podatek_zagraniczny_nierozliczony_PLN"),
    })
    notes = dict.fromkeys(calc_data.get("Review_Notes", []) + reconciliation.get("Issues", []))
    for index, note in enumerate(notes, start=1):
        formatted_steps.append({"Record type": "Unresolved assumption / review", "Review note": f"{index}. {note}"})

    return pd.DataFrame(formatted_steps).to_csv(index=False).encode("utf-8")
