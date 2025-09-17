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
# Debug PDF (raw text + tables)
# ------------------------------
def debug_pdf_summary(pdf_file):
    with pdfplumber.open(pdf_file) as pdf:
        for i in range(min(3, len(pdf.pages))):
            st.subheader(f"🔍 Debug Page {i+1}")
            
            # Extract text
            text = pdf.pages[i].extract_text()
            if text:
                st.text_area(f"Raw Text Page {i+1}", text, height=300)
            
            # Extract tables
            tables = pdf.pages[i].extract_tables()
            if tables:
                for t_idx, table in enumerate(tables):
                    st.write(f"Table {t_idx+1} (Page {i+1})")
                    st.table(table)

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
                    amt = round(float(amount.replace(",", "")), 2)
                    if drcr and drcr.strip().lower().startswith("cr"):
                        amt = -amt
                        tr_type = "CR"
                    else:
                        tr_type = "DR"
                    transactions.append([date, merchant.strip(), amt, tr_type, account_name])

            st.info(f"📄 Page {page_num}: extracted {len(transactions)} rows so far")

    return pd.DataFrame(transactions, columns=["Date", "Merchant", "Amount", "Type", "Account"])

# ------------------------------
# Dummy Summary Extractor (placeholder)
# ------------------------------
def extract_summary_from_pdf(pdf_file):
    return {}  # leave empty until we confirm correct regex

# ------------------------------
# Expense analysis
# ------------------------------
def analyze_expenses(df):
    st.write("💰 **Total Spent:**", f"{df['Amount'].sum():,.2f}")
    st.write("📊 **Expense by Category**")
    st.bar_chart(df.groupby("Category")["Amount"].sum().round(2))
    st.write("🏦 **Top 5 Merchants**")
    st.dataframe(df.groupby("Merchant")["Amount"].sum().round(2).sort_values(ascending=False).head())

# ------------------------------
# Streamlit UI
# ------------------------------
st.title("💳 Multi-Account Expense Analyzer (Debug Mode)")
st.write("Upload your statements and view raw extracted text/tables for debugging.")

uploaded_files = st.file_uploader(
    "Upload Statements",
    type=["pdf"],
    accept_multiple_files=True
)

if uploaded_files:
    for uploaded_file in uploaded_files:
        account_name = st.text_input(f"Enter account name for {uploaded_file.name}", value=uploaded_file.name)

        if account_name and uploaded_file.name.endswith(".pdf"):
            df = extract_transactions_from_pdf(uploaded_file, account_name)

            # 🔍 Debug raw PDF extraction
            debug_pdf_summary(uploaded_file)

            if not df.empty:
                st.subheader("📑 Extracted Transactions")
                st.dataframe(df)
