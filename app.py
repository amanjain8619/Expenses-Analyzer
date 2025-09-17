import streamlit as st
import pandas as pd
import pdfplumber
import re
from rapidfuzz import process
from io import BytesIO
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
# Helpers
# ------------------------------
def fmt_num(val):
    try:
        return f"{float(str(val).replace(',', '').strip()):,.2f}"
    except:
        return val

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
# Extract transactions from PDF
# ------------------------------
def extract_transactions_from_pdf(pdf_file, account_name):
    transactions = []
    with pdfplumber.open(pdf_file) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text()
            if not text:
                continue

            lines = [l.strip() for l in text.split("\n") if l.strip()]
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
                    transactions.append([date, merchant.strip(), amt, tr_type, account_name])

    return pd.DataFrame(transactions, columns=["Date", "Merchant", "Amount", "Type", "Account"])

# ------------------------------
# Extract summary from PDF (keyword-driven for HDFC + BoB)
# ------------------------------
def extract_summary_from_pdf(pdf_file):
    summary = {}
    text_all = ""

    try:
        with pdfplumber.open(pdf_file) as pdf:
            for i in range(min(3, len(pdf.pages))):  # first 3 pages usually enough
                page_text = pdf.pages[i].extract_text()
                if page_text:
                    text_all += "\n" + page_text

        # Clean lines
        lines = [l.strip() for l in text_all.split("\n") if l.strip()]

        # Keyword → field mapping
        keyword_map = {
            "statement date": "Statement Date",
            "payment due date": "Payment Due Date",
            "total due": "Total Due",
            "total dues": "Total Due",
            "minimum amount due": "Minimum Due",
            "minimum due": "Minimum Due",
            "credit limit": "Credit Limit",
            "available credit": "Available Credit",
            "available cash": "Available Cash",
            "previous balance": "Previous Balance",
            "opening balance": "Previous Balance",
            "payments / credits": "Payments / Credits",
            "payment/credits": "Payments / Credits",
            "purchases / debits": "Purchases / Debits",
            "purchases/debits": "Purchases / Debits",
            "other charges": "Other Charges",
            "finance charges": "Finance Charges",
        }

        # Scan line by line for known keywords + numbers
        for line in lines:
            for key, field in keyword_map.items():
                if key in line.lower():
                    nums = re.findall(r"[\d,]+\.\d{2}", line)
                    if nums:
                        summary[field] = fmt_num(nums[-1])  # last number on line is usually the value

        # Regex fallback for missing Statement Date
        if "Statement Date" not in summary:
            m = re.search(r"Statement Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})", text_all, re.IGNORECASE)
            if m:
                summary["Statement Date"] = m.group(1)

    except Exception as e:
        st.error(f"⚠️ Error extracting summary: {e}")

    if not summary:
        summary = {"Info": "No summary details detected in PDF."}

    return summary

# ------------------------------
# Categorize expenses
# ------------------------------
def categorize_expenses(df):
    df["Category"] = df["Merchant"].apply(get_category)
    return df

# ------------------------------
# Streamlit UI
# ------------------------------
st.title("💳 Multi-Account Expense Analyzer")
st.write("Upload statements (PDF, CSV, or Excel) to view transactions + summary.")

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

                st.subheader(f"📋 Statement Summary for {account_name}")
                st.json(summary)

            elif uploaded_file.name.endswith(".csv"):
                df = pd.read_csv(uploaded_file)
            elif uploaded_file.name.endswith(".xlsx"):
                df = pd.read_excel(uploaded_file)
            else:
                df = pd.DataFrame()

            all_data = pd.concat([all_data, df], ignore_index=True)

    if not all_data.empty:
        all_data = categorize_expenses(all_data)
        all_data["Amount"] = all_data["Amount"].round(2)

        st.subheader("📑 Extracted Transactions")
        st.dataframe(all_data.style.format({"Amount": "{:,.2f}"}))
