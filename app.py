import streamlit as st
import pandas as pd
import pdfplumber
import re
from rapidfuzz import process
from io import BytesIO
from datetime import datetime
import os

# ==============================
# Load vendor mapping
# ==============================
VENDOR_FILE = "vendors.csv"
if os.path.exists(VENDOR_FILE):
    vendor_map = pd.read_csv(VENDOR_FILE)
else:
    vendor_map = pd.DataFrame(columns=["merchant", "category"])
    vendor_map.to_csv(VENDOR_FILE, index=False)

# ------------------------------
# Fuzzy matching to find category
# ------------------------------
def get_category(merchant):
    m = str(merchant).lower()
    matches = process.extractOne(
        m,
        vendor_map["merchant"].str.lower().tolist(),
        score_cutoff=80
    )
    if matches:
        matched_merchant = matches[0]
        category = vendor_map.loc[
            vendor_map["merchant"].str.lower() == matched_merchant, "category"
        ].iloc[0]
        return category
    return "Others"

# ------------------------------
# Date Parser
# ------------------------------
def parse_date(date_str):
    """Handle dd/mm/yyyy, Month DD, DD Month formats."""
    try:
        return datetime.strptime(date_str, "%d/%m/%Y").strftime("%d/%m/%Y")
    except:
        for fmt in ["%b %d", "%B %d", "%d %b", "%d %B"]:
            try:
                return datetime.strptime(date_str + " 2025", f"{fmt} %Y").strftime("%d/%m/%Y")
            except:
                pass
        return date_str

# ------------------------------
# Extract transactions from PDF (supports HDFC/ICICI/BoB + AMEX)
# ------------------------------
def extract_transactions_from_pdf(pdf_file, account_name, debug=False):
    transactions = []
    with pdfplumber.open(pdf_file) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            tables = page.extract_tables()
            extracted_from_table = False
            if tables:
                for table in tables:
                    if len(table) < 2:
                        continue
                    header = [h.lower() if h else '' for h in table[0]]
                    date_idx = next((i for i, h in enumerate(header) if 'date' in h or 'detail' in h), None)  # Flexible for 'Details'
                    merch_idx = next((i for i, h in enumerate(header) if 'desc' in h or 'narr' in h or 'merchant' in h), None)
                    amt_idx = next((i for i, h in enumerate(header) if 'amount' in h), None)
                    type_idx = next((i for i, h in enumerate(header) if 'cr' in h or 'dr' in h or 'type' in h), None)
                    
                    if date_idx is None or merch_idx is None or amt_idx is None:
                        continue  # Not a transaction table
                    
                    for row in table[1:]:  # Skip header
                        if len(row) < max(date_idx, merch_idx, amt_idx) + 1:
                            continue
                        date = row[date_idx].strip() if row[date_idx] else ''
                        merchant = row[merch_idx].strip() if row[merch_idx] else ''
                        amount_str = row[amt_idx].strip().replace(',', '') if row[amt_idx] else ''
                        drcr = row[type_idx].strip() if type_idx is not None and row[type_idx] else 'DR'
                        
                        if not date or not merchant or not amount_str:
                            continue
                        
                        try:
                            amt_match = re.search(r'[\d.]+', amount_str)
                            amt = float(amt_match.group())
                        except:
                            continue
                        
                        # Detect credits more robustly
                        if drcr.upper() == 'CR' or 'CR' in amount_str.upper() or 'CREDIT' in merchant.upper() or 'PAYMENT RECEIVED' in merchant.upper():
                            amt = -amt
                            drcr = 'CR'
                        
                        transactions.append([parse_date(date), merchant, round(amt, 2), drcr, account_name])
                    extracted_from_table = True
            
            if not extracted_from_table:
                # Fallback to text-based parsing
                text = page.extract_text()
                if debug and text:
                    st.write(f"🔎 Debug Text Page {page_num}", text.split("\n")[:20])

                if not text:
                    continue

                lines = [l.strip() for l in text.split("\n") if l.strip()]

                if "American Express" in text:
                    # Multi-line parsing for AMEX
                    try:
                        i = lines.index("Amount Rs") + 1
                    except ValueError:
                        i = 0

                    while i < len(lines):
                        if re.match(r"^[A-Za-z]{3,9} \d{1,2}$", lines[i]):
                            date_str = lines[i]
                            i += 1
                            merchant = ""
                            while i < len(lines) and not re.match(r"^[\d,]+\.\d{2}$", lines[i]) and lines[i] != "CR" and not re.match(r"^[A-Za-z]{3,9} \d{1,2}$", lines[i]):
                                if "Card Number" not in lines[i]:
                                    merchant += lines[i] + " "
                                i += 1
                            if i >= len(lines) or not re.match(r"^[\d,]+\.\d{2}$", lines[i]):
                                continue
                            amount_str = lines[i]
                            amt = float(amount_str.replace(",", ""))
                            drcr = "DR"
                            i += 1
                            while i < len(lines) and not re.match(r"^[A-Za-z]{3,9} \d{1,2}$", lines[i]) and not re.match(r"^[\d,]+\.\d{2}$", lines[i]):
                                if lines[i] == "CR":
                                    drcr = "CR"
                                    amt = -amt
                                # else skip
                                i += 1
                            transactions.append([parse_date(date_str), merchant.strip(), round(amt, 2), drcr, account_name])
                        else:
                            i += 1
                else:
                    # Single-line parsing for other banks
                    for line in lines:
                        m1 = re.match(r"(\d{2}/\d{2}/\d{4})\s+(.+?)\s+([\d,]+\.\d{2})\s?(CR|DR)?", line)
                        if m1:
                            date, merchant, amount, drcr = m1.groups()
                            amt = float(amount.replace(",", ""))
                            if drcr and drcr.upper() == "CR":
                                amt = -amt
                            transactions.append([parse_date(date), merchant.strip(), round(amt, 2), drcr if drcr else "DR", account_name])
                            continue

                        m2 = re.match(r"(\d{1,2}\s+[A-Za-z]{3,9})(?:\s+\d{1,2}\s+[A-Za-z]{3,9})?\s+(.+?)\s+([\d,]+\.\d{2})\s*(CR|Cr)?$", line)
                        if m2:
                            date_str, merchant, amount, cr_suffix = m2.groups()
                            amt = float(amount.replace(",", ""))
                            drcr = "DR"
                            if cr_suffix or "CR" in line.upper() or "CREDIT" in line.upper() or "PAYMENT RECEIVED" in line.upper():
                                amt = -amt
                                drcr = "CR"
                            transactions.append([parse_date(date_str), merchant.strip(), round(amt, 2), drcr, account_name])
                            continue

            if debug:
                st.write(f"🔎 Debug Tables Page {page_num}", tables[:2] if tables else "No tables detected")  # Print sample tables
            
            st.info(f"📄 Page {page_num}: extracted {len(transactions)} rows so far")

    return pd.DataFrame(transactions, columns=["Date", "Merchant", "Amount", "Type", "Account"])

# ------------------------------
# Extract transactions from CSV/XLSX
# ------------------------------
def extract_transactions_from_excel(file, account_name):
    df = pd.read_excel(file)
    return normalize_dataframe(df, account_name)

def extract_transactions_from_csv(file, account_name):
    df = pd.read_csv(file)
    return normalize_dataframe(df, account_name)

def normalize_dataframe(df, account_name):
    col_map = {
        "date": "Date",
        "transaction date": "Date",
        "txn date": "Date",
        "description": "Merchant",
        "narration": "Merchant",
        "merchant": "Merchant",
        "amount": "Amount",
        "debit": "Debit",
        "credit": "Credit",
        "type": "Type"
    }
    df_renamed = {}
    for col in df.columns:
        key = col.lower().strip()
        if key in col_map:
            df_renamed[col] = col_map[key]

    df = df.rename(columns=df_renamed)

    if "Debit" in df and "Credit" in df:
        df["Amount"] = df["Debit"].fillna(0) - df["Credit"].fillna(0)
        df["Type"] = df.apply(lambda x: "DR" if x["Debit"] > 0 else "CR", axis=1)
    elif "Amount" in df and "Type" in df:
        df["Amount"] = df.apply(lambda x: -abs(x["Amount"]) if str(x["Type"]).upper().startswith("CR") else abs(x["Amount"]), axis=1)
    elif "Amount" in df and "Type" not in df:
        df["Type"] = "DR"

    if "Date" not in df or "Merchant" not in df or "Amount" not in df:
        st.error("❌ Could not detect required columns (Date, Merchant, Amount). Please check your file.")
        return pd.DataFrame(columns=["Date", "Merchant", "Amount", "Type", "Account"])

    df["Amount"] = df["Amount"].astype(float).round(2)
    df["Account"] = account_name
    return df[["Date", "Merchant", "Amount", "Type", "Account"]]

# ------------------------------
# Categorize expenses
# ------------------------------
def categorize_expenses(df):
    df["Category"] = df["Merchant"].apply(get_category)
    return df

# ------------------------------
# Add new vendor if categorized by user
# ------------------------------
def add_new_vendor(merchant, category):
    global vendor_map
    new_row = pd.DataFrame([[merchant.lower(), category]], columns=["merchant", "category"])
    vendor_map = pd.concat([vendor_map, new_row], ignore_index=True)
    vendor_map.drop_duplicates(subset=["merchant"], keep="last", inplace=True)
    vendor_map.to_csv(VENDOR_FILE, index=False)

# ------------------------------
# Expense analysis
# ------------------------------
def analyze_expenses(df):
    # Total spent should be sum of debits (positive amounts)
    total_spent = df[df['Amount'] > 0]['Amount'].sum()
    st.write("💰 **Total Spent:**", f"{total_spent:,.2f}")
    st.write("📊 **Expense by Category**")
    st.bar_chart(df[df['Amount'] > 0].groupby("Category")["Amount"].sum().round(2))
    st.write("🏦 **Top 5 Merchants**")
    st.dataframe(df[df['Amount'] > 0].groupby("Merchant")["Amount"].sum().round(2).sort_values(ascending=False).head())
    st.write("🏦 **Expense by Account**")
    st.bar_chart(df[df['Amount'] > 0].groupby("Account")["Amount"].sum().round(2))

# ------------------------------
# Export Helpers
# ------------------------------
def convert_df_to_csv(df):
    df["Amount"] = df["Amount"].round(2)
    return df.to_csv(index=False).encode("utf-8")

def convert_df_to_excel(df):
    df["Amount"] = df["Amount"].round(2)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Expenses", float_format="%.2f")
    return output.getvalue()

# ==============================
# Streamlit UI
# ==============================
st.title("💳 Multi-Account Expense Analyzer")
st.write("Upload your bank/credit card statements (PDF, CSV, or Excel).")

debug_mode = st.checkbox("Enable Debug Mode 🔎", value=False)

uploaded_files = st.file_uploader("Upload Statements", type=["pdf", "csv", "xlsx"], accept_multiple_files=True)

if uploaded_files:
    all_data = pd.DataFrame(columns=["Date", "Merchant", "Amount", "Type", "Account"])

    for uploaded_file in uploaded_files:
        account_name = st.text_input(f"Enter account name for {uploaded_file.name}", value=uploaded_file.name)
        if account_name:
            if uploaded_file.name.endswith(".pdf"):
                df = extract_transactions_from_pdf(uploaded_file, account_name, debug=debug_mode)
            elif uploaded_file.name.endswith(".csv"):
                df = extract_transactions_from_csv(uploaded_file, account_name)
            elif uploaded_file.name.endswith(".xlsx"):
                df = extract_transactions_from_excel(uploaded_file, account_name)
            else:
                df = pd.DataFrame()
            all_data = pd.concat([all_data, df], ignore_index=True)

    if not all_data.empty:
        all_data = categorize_expenses(all_data)
        all_data["Amount"] = all_data["Amount"].round(2)

        st.subheader("📑 Extracted Transactions")
        st.dataframe(all_data)

        st.subheader("📊 Expense Analysis")
        analyze_expenses(all_data)

        st.subheader("📥 Download Results")
        st.download_button("⬇️ CSV", convert_df_to_csv(all_data), "expenses.csv", "text/csv")
        st.download_button("⬇️ Excel", convert_df_to_excel(all_data),
                           "expenses.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
