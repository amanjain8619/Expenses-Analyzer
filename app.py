import streamlit as st
import pandas as pd
import pdfplumber
import re
from rapidfuzz import process
from io import BytesIO
import os
from datetime import datetime

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
# Helpers
# ------------------------------
def fmt_num(val):
    try:
        return f"₹{float(val):,.2f}"
    except:
        return val

def parse_number(s):
    try:
        return float(str(s).replace(",", "").replace("₹", "").strip())
    except:
        return None

def parse_date(date_str):
    """Handle dd/mm/yyyy, Month DD, DD Month formats."""
    date_str = date_str.replace(",", "").strip()
    for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%d %b, %Y", "%d %B, %Y"]:
        try:
            return datetime.strptime(date_str, fmt).strftime("%d/%m/%Y")
        except:
            pass
    return date_str

# ------------------------------
# Fuzzy matching to find category
# ------------------------------
def get_category(merchant):
    m = str(merchant).lower()
    try:
        matches = process.extractOne(
            m,
            vendor_map["merchant"].str.lower().tolist(),
            score_cutoff=80
        )
    except Exception:
        return "Others"
    if matches:
        matched_merchant = matches[0]
        category = vendor_map.loc[
            vendor_map["merchant"].str.lower() == matched_merchant, "category"
        ].iloc[0]
        return category
    return "Others"

# ------------------------------
# Extract transactions from PDF (generic + AMEX)
# ------------------------------
def extract_transactions_from_pdf(pdf_file, account_name):
    transactions = []
    text_all = ""
    is_amex = False
    with pdfplumber.open(pdf_file) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text()
            if text:
                text_all += text + "\n"
                if "American Express" in text:
                    is_amex = True
            if not text:
                continue
            lines = [l.strip() for l in text.split("\n") if l.strip()]

            if not is_amex:
                # Non-AMEX parsing (HDFC, BOB, ICICI, etc.)
                for line in lines:
                    match = re.match(
                        r"(\d{2}/\d{2}/\d{4})(?:\s+\d{2}:\d{2}:\d{2})?\s+(.+?)\s+([\d,]+\.\d{2})\s*(CR|Dr|DR|Cr)?",
                        line
                    )
                    if match:
                        date, merchant, amount, drcr = match.groups()
                        try:
                            amt = round(float(amount.replace(",", "")), 2)
                        except:
                            continue
                        if drcr and drcr.strip().lower().startswith("cr"):
                            amt = -amt
                            tr_type = "CR"
                        else:
                            tr_type = "DR"
                        transactions.append([parse_date(date), merchant.strip(), amt, tr_type, account_name])
            else:
                # AMEX parsing
                for line in lines:
                    m = re.match(r"([A-Za-z]{3,9}\s+\d{1,2})\s+(.+?)\s+([\d,]+\.\d{2})$", line)
                    if m:
                        date_str, merchant, amount = m.groups()
                        try:
                            amt = float(amount.replace(",", ""))
                        except:
                            continue
                        tr_type = "DR"
                        if "PAYMENT" in merchant.upper():
                            amt = -amt
                            tr_type = "CR"
                        transactions.append([parse_date(date_str), merchant.strip(), amt, tr_type, account_name])

            st.info(f"📄 Page {page_num}: extracted {len(transactions)} rows so far")

    return pd.DataFrame(transactions, columns=["Date", "Merchant", "Amount", "Type", "Account"])

# ------------------------------
# Extract summary from PDF (supports HDFC, BoB, AMEX, ICICI, SBI)
# ------------------------------
def extract_summary_from_pdf(pdf_file):
    summary = {}
    text_all = ""

    try:
        with pdfplumber.open(pdf_file) as pdf:
            for i in range(min(3, len(pdf.pages))):
                page_text = pdf.pages[i].extract_text()
                if page_text:
                    text_all += page_text + "\n"

        text_all = re.sub(r"\s+", " ", text_all)

        # --- Synonyms for each field ---
        field_patterns = {
            "Credit Limit": [
                r"Credit Limit\s*Rs?\.?\s*([\d,]+\.\d{2})",
                r"Sanctioned Credit Limit\s*Rs?\.?\s*([\d,]+\.\d{2})"
            ],
            "Available Credit": [
                r"Available Credit Limit\s*Rs?\.?\s*([\d,]+\.\d{2})",
                r"Available Credit\s*Rs?\.?\s*([\d,]+\.\d{2})"
            ],
            "Total Due": [
                r"Total Dues\s*Rs?\.?\s*([\d,]+\.\d{2})",
                r"Total Due\s*Rs?\.?\s*([\d,]+\.\d{2})",
                r"Closing Balance\s*Rs?\.?\s*([\d,]+\.\d{2})",
                r"Total Amount Due\s*Rs?\.?\s*([\d,]+\.\d{2})"
            ],
            "Minimum Due": [
                r"Minimum Amount Due\s*Rs?\.?\s*([\d,]+\.\d{2})",
                r"Minimum Payment Due\s*Rs?\.?\s*([\d,]+\.\d{2})",
                r"Minimum Due\s*Rs?\.?\s*([\d,]+\.\d{2})"
            ]
        }

        # --- Extract fields ---
        for key, patterns in field_patterns.items():
            for pat in patterns:
                m = re.search(pat, text_all, re.IGNORECASE)
                if m:
                    summary[key] = fmt_num(m.group(1))
                    break

        # --- Dates ---
        stmt_patterns = [
            r"Statement Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
            r"Statement Date\s*[:\-]?\s*([0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4})",
            r"Statement Period.*?to\s*([0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4})"
        ]
        due_patterns = [
            r"Payment Due Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
            r"Payment Due Date\s*[:\-]?\s*([A-Za-z]{3,9}\s*\d{1,2},\s*\d{4})"
        ]

        for pat in stmt_patterns:
            m = re.search(pat, text_all, re.IGNORECASE)
            if m:
                summary["Statement Date"] = parse_date(m.group(1))
                break

        for pat in due_patterns:
            m = re.search(pat, text_all, re.IGNORECASE)
            if m:
                summary["Payment Due Date"] = parse_date(m.group(1))
                break

        # --- Normalize ---
        if "Total Due" in summary:
            summary["Used / Closing"] = summary["Total Due"]

        return {
            "Statement date": summary.get("Statement Date", "N/A"),
            "Payment Due Date": summary.get("Payment Due Date", "N/A"),
            "Total Limit": summary.get("Credit Limit", "N/A"),
            "Used / Closing": summary.get("Used / Closing", "N/A"),
            "Available Credit": summary.get("Available Credit", "N/A"),
            "Min Due": summary.get("Minimum Due", "N/A"),
        }

    except Exception as e:
        st.error(f"⚠️ Error while extracting summary: {e}")
        return {"Info": "No summary details detected in PDF."}

# ------------------------------
# Display summary
# ------------------------------
def display_summary(summary, account_name):
    st.subheader(f"📋 Statement Summary — {account_name}")
    for k, v in summary.items():
        st.write(f"**{k}**: {v}")

# ------------------------------
# CSV/XLSX Handling
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
# Add new vendor
# ------------------------------
def add_new_vendor(merchant, category):
    global vendor_map
    new_row = pd.DataFrame([[merchant.lower(), category]], columns=["merchant", "category"])
    vendor_map = pd.concat([vendor_map, new_row], ignore_index=True)
    vendor_map.drop_duplicates(subset=["merchant"], keep="last", inplace=True)
    vendor_map.to_csv(VENDOR_FILE, index=False)

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
    processed_data = output.getvalue()
    return processed_data

# ==============================
# Streamlit UI
# ==============================
st.title("💳 Multi-Account Expense Analyzer")
st.write("✅ App loaded successfully, waiting for uploads...")

uploaded_files = st.file_uploader(
    "Upload Statements",
    type=["pdf", "csv", "xlsx"],
    accept_multiple_files=True
)

if uploaded_files:
    all_data = pd.DataFrame(columns=["Date", "Merchant", "Amount", "Type", "Account"])

    for uploaded_file in uploaded_files:
        account_name = st.text_input(f"Enter account name for {uploaded_file.name}", value=uploaded_file.name)

        if account_name:
            if uploaded_file.name.endswith(".pdf"):
                df = extract_transactions_from_pdf(uploaded_file, account_name)
                summary = extract_summary_from_pdf(uploaded_file)
                display_summary(summary, account_name)

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
        st.dataframe(all_data.style.format({"Amount": "{:,.2f}"}))

        # Export
        csv_data = convert_df_to_csv(all_data)
        excel_data = convert_df_to_excel(all_data)

        st.download_button("⬇️ Download as CSV", csv_data, file_name="expenses_all.csv", mime="text/csv")
        st.download_button("⬇️ Download as Excel", excel_data, file_name="expenses_all.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
