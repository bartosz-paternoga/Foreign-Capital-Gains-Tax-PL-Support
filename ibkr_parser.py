# ibkr_parser.py
import os
import json
import pandas as pd
from tax_calculator import PIT38Calculator

def find_matching_column(columns, candidates):
    for col in columns:
        clean_col = str(col).strip().lower()
        for cand in candidates:
            if cand in clean_col:
                return col
    return None

def process_and_calculate_file(file_path: str, tax_year: int = 2026, loss_carryforward: float = 0.0) -> str:
    calculator = PIT38Calculator(target_tax_year=tax_year, loss_carryforward=loss_carryforward)

    current_dir = os.getcwd()
    target_path = file_path
    
    if not os.path.exists(target_path):
        target_path = os.path.join(current_dir, file_path)
        
    if not os.path.exists(target_path):
        csv_files = [f for f in os.listdir(current_dir) if f.endswith('.csv') and not f.startswith('.')]
        if csv_files:
            target_path = os.path.join(current_dir, csv_files[0])
        else:
            return json.dumps({
                "error": f"File '{file_path}' not found in '{current_dir}'."
            }, ensure_ascii=False)

    file_path = target_path

    try:
        df = pd.read_csv(file_path, low_memory=False)
        df.columns = [str(c).strip() for c in df.columns]
        
        cols_lower = [str(c).lower() for c in df.columns]
        if not any('symbol' in c or 'ticker' in c for c in cols_lower):
            for idx, row in df.iterrows():
                row_str = " ".join(str(val) for val in row.values).lower()
                if 'symbol' in row_str or 'ticker' in row_str or 'date' in row_str:
                    df.columns = [str(c).strip() for c in row.values]
                    df = df.iloc[idx+1:]
                    break
    except Exception as e:
        return json.dumps({"error": f"Failed to read file: {str(e)}"}, ensure_ascii=False)

    df.columns = [str(c).strip() for c in df.columns]
    cols = df.columns

    date_col = find_matching_column(cols, ['date', 'date/time', 'time', 'day', 'trade_date', 'transaction_date', 'datetime'])
    ticker_col = find_matching_column(cols, ['symbol', 'ticker', 'asset', 'code', 'instrument'])
    action_col = find_matching_column(cols, ['transaction type', 'buy/sell', 'action', 'type', 'side', 'operation', 'description'])
    qty_col = find_matching_column(cols, ['quantity', 'shares', 'qty', 'units'])
    price_col = find_matching_column(cols, ['t. price', 't_price', 'price', 'rate', 'trade_price'])
    curr_col = find_matching_column(cols, ['price currency', 'currency', 'curr', 'ccy', 'listing currency'])
    comm_col = find_matching_column(cols, ['commission', 'comm/fee', 'fee', 'comm'])

    if not date_col:
        return json.dumps({"error": "Critical Error: Could not find Date column."}, ensure_ascii=False)

    df['__parsed_date'] = pd.to_datetime(df[date_col], errors='coerce')
    df = df.dropna(subset=['__parsed_date'])
    df = df.sort_values(by='__parsed_date', ascending=True).reset_index(drop=True)

    processed_count = 0
    errors = []

    for idx, row in df.iterrows():
        row_num = idx + 2
        parsed_date = row['__parsed_date']
        date = parsed_date.strftime('%Y-%m-%d')

        action_val = str(row[action_col]).strip().lower() if action_col and pd.notna(row[action_col]) else ""
        desc_val = str(row.get('Description', '')).strip().lower()
        full_text = f"{action_val} {desc_val}"

        # Default to EUR for European IBKR accounts, check currency column or description
        currency = 'EUR'
        if curr_col and pd.notna(row[curr_col]) and str(row[curr_col]) != '-':
            val = str(row[curr_col]).strip().upper()
            if len(val) == 3:
                currency = val
        
        for c_code in ['EUR', 'USD', 'GBP', 'CHF', 'PLN', 'AUD', 'CAD', 'JPY']:
            if c_code in full_text.upper():
                currency = c_code
                break

        # 1. Tax Withholding (reads exact amount e.g. 0.19)
        if 'withholding' in full_text or 'tax' in full_text:
            try:
                tax_amt = 0.0
                for c_cand in ['amount', 'net amount', 'gross amount', 'proceeds', 'total']:
                    matched_c = find_matching_column(cols, [c_cand])
                    if matched_c and pd.notna(row[matched_c]) and str(row[matched_c]) != '-':
                        val_str = str(row[matched_c]).replace(',', '').strip()
                        if val_str:
                            tax_amt = float(val_str)
                            break
                tax_amt = abs(tax_amt)
                
                calculator.add_withholding_tax(date=date, tax_paid_fx=tax_amt, currency=currency)
                processed_count += 1
            except Exception as e:
                errors.append(f"Row {row_num} (Withholding): {str(e)}")
            continue

        # 2. Credit Interest
        if 'credit interest' in full_text:
            try:
                gross = 0.0
                for c_cand in ['amount', 'gross amount', 'net amount', 'proceeds', 'total']:
                    matched_c = find_matching_column(cols, [c_cand])
                    if matched_c and pd.notna(row[matched_c]) and str(row[matched_c]) != '-':
                        val_str = str(row[matched_c]).replace(',', '').strip()
                        if val_str:
                            gross = float(val_str)
                            break
                calculator.add_interest(date=date, gross_fx=abs(gross), currency=currency, country="Irlandia (IE)")
                processed_count += 1
            except Exception as e:
                errors.append(f"Row {row_num} (Interest): {str(e)}")
            continue

        # 3. Trades (Buy / Sell)
        ticker = str(row[ticker_col]).strip().upper() if ticker_col and pd.notna(row[ticker_col]) else ''
        if not ticker or ticker in ['NAN', '-', 'TOTAL'] or len(ticker) > 15:
            continue

        raw_qty = 0.0
        try:
            if qty_col and pd.notna(row[qty_col]) and str(row[qty_col]) != '-':
                raw_qty = float(str(row[qty_col]).replace(',', ''))
        except ValueError:
            continue

        action = None
        if raw_qty < 0 or any(k in full_text for k in ['sell', 'sld', 'sold', 'zbycie', 'sprzedaż']):
            action = 'SELL'
        elif raw_qty > 0 or any(k in full_text for k in ['buy', 'bot', 'bought', 'nabycie', 'kupno']):
            action = 'BUY'

        if action in ['BUY', 'SELL']:
            quantity = abs(raw_qty)
            try:
                price = float(str(row[price_col]).replace(',', '')) if price_col and pd.notna(row[price_col]) and str(row[price_col]) != '-' else 0.0
                comm_val = row[comm_col] if comm_col and pd.notna(row[comm_col]) and str(row[comm_col]) != '-' else 0.0
                commission = abs(float(str(comm_val).replace(',', ''))) if comm_val != '' else 0.0
            except ValueError:
                continue

            if quantity <= 0 or price <= 0:
                continue

            try:
                calculator.add_transaction(
                    date=date, ticker=ticker, action=action,
                    quantity=quantity, price_fx=price,
                    currency=currency, commission_fx=commission,
                    country="Irlandia (IE)"
                )
                processed_count += 1
            except Exception as e:
                errors.append(f"Row {row_num} ({ticker}): {str(e)}")

    results = calculator.calculate_pit38()
    response_data = {
        "status": "success",
        "file_processed": file_path,
        "processed_records": processed_count,
        "warnings": errors,
        "pit38_calculation": results
    }
    return json.dumps(response_data, indent=2, ensure_ascii=False)