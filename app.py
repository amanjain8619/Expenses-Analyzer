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
# Extract summary from PDF (safe, works for HDFC + BoB)
# ------------------------------
def extract_summary_from_pdf(pdf_file):
    summary = {}
    text_all = ""

    try:
        with pdfplumber.open(pdf_file) as pdf:
            for i in range(min(3, len(pdf.pages))):
                page_text = pdf.pages[i].extract_text()
                if page_text:
                    text_all += "\n" + page_text

                tables = pdf.pages[i].extract_tables()
                if not tables:
                    continue

                for table in tables:
                    if not table or len(table) < 2:
                        continue

                    headers = [str(h).strip() for h in table[0] if h]
                    row = [str(v).strip() for v in table[1] if v]

                    # Case 1: HDFC style (header-value tables)
                    if any("Due Date" in h or "Total Dues" in h or "Credit Limit" in h for h in headers):
                        for h, v in zip(headers, row):
                            if not v or v.lower() in ["nan", ""]:
                                continue
                            if "Payment Due Date" in h:
                                summary["Payment Due Date"] = v.replace(",", "")
                            elif "Total Dues" in h or "Total Due" in h:
                                summary["Total Due"] = v.replace(",", "")
                            elif "Minimum" in h:
                                summary["Minimum Due"] = v.replace(",", "")
                            elif "Credit Limit" in h and "Available" not in h:
                                summary["Credit Limit"] = v.replace(",", "")
                            elif "Available Credit" in h:
                                summary["Available Credit"] = v.replace(",", "")
                            elif "Available Cash" in h:
                                summary["Available Cash"] = v.replace(",", "")
                            elif "Opening Balance" in h:
                                summary["Previous Balance"] = v.replace(",", "")
                            elif "Payment" in h:
                                summary["Total Payments"] = v.replace(",", "")
                            elif "Purchase" in h:
                                summary["Total Purchases"] = v.replace(",", "")
                            elif "Finance" in h:
                                summary["Finance Charges"] = v.replace(",", "")

                    # Case 2: BoB style (row of 4 numbers)
                    numbers = re.findall(r"[\d,]+\.\d{2}", " ".join(row))
                    if len(numbers) == 4:
                        if not summary.get("Credit Limit"):
                            summary["Credit Limit"] = numbers[0].replace(",", "")
                            summary["Available Credit"] = numbers[1].replace(",", "")
                            summary["Total Due"] = numbers[2].replace(",", "")
                            summary["Minimum Due"] = numbers[3].replace(",", "")
                        else:
                            summary["Total Payments"] = numbers[0].replace(",", "")
                            summary["Other Charges"] = numbers[1].replace(",", "")
                            summary["Total Purchases"] = numbers[2].replace(",", "")
                            summary["Previous Balance"] = numbers[3].replace(",", "")

        # Regex fallback for Statement Date
        patterns = [
            r"Statement Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
            r"(\d{2}\s+[A-Za-z]{3},\s*\d{4})\s+To"
        ]
        if "Statement Date" not in summary:
            for pattern in patterns:
                m = re.search(pattern, text_all, re.IGNORECASE)
                if m:
                    summary["Statement Date"] = m.group(1).replace(",", "")
                    break

    except Exception as e:
        st.error(f"⚠️ Error while extracting summary: {e}")

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
                st.subheader(f"📋 Statement Summary for {account_name}")
                st.json(summary)  # show raw summary for now
            else:
                df = pd.DataFrame()

            all_data = pd.concat([all_data, df], ignore_index=True)

    if not all_data.empty:
        all_data = categorize_expenses(all_data)
        st.subheader("📑 Extracted Transactions")
        st.dataframe(all_data)
