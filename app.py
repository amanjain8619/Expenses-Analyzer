import streamlit as st
import pandas as pd
import pdfplumber
import re
from rapidfuzz import process
from io import BytesIO
import os

# ==============================
# Vendor Mapping Setup
# ==============================
VENDOR_FILE = "vendors.csv"
if os.path.exists(VENDOR_FILE):
    vendor_map = pd.read_csv(VENDOR_FILE)
else:
    vendor_map = pd.DataFrame(columns=["merchant", "category"])
    vendor_map.to_csv(VENDOR_FILE, index=False)

# ==============================
# Helpers
# ==============================
def fmt_num(val):
    try:
        return f"{float(str(val).replace(',', '').strip()):,.2f}"
    except:
        return val

# ------------------------------
# Extract Statement Summary
# ------------------------------
def extract_summary_from_pdf(pdf_file):
    summary = {}
    text_all = ""

    with pdfplumber.open(pdf_file) as pdf:
        for i in range(min(3, len(pdf.pages))):
            page_text = pdf.pages[i].extract_text()
            if page_text:
                text_all += "\n" + page_text

    lines = [l.strip() for l in text_all.split("\n") if l.strip()]

    # --- Extract Total Due ---
    for line in lines:
        if "total due" in line.lower() or "total dues" in line.lower():
            nums = re.findall(r"[\d,]+\.\d{2}", line)
            if nums:
                summary["Total Due"] = fmt_num(nums[-1])
                break

    # --- Extract Available Credit ---
    for line in lines:
        if "available credit" in line.lower() or "available limit" in line.lower():
            nums = re.findall(r"[\d,]+\.\d{2}", line)
            if nums:
                summary["Available Credit"] = fmt_num(nums[-1])
                break

    # --- Extract Statement Date ---
    m = re.search(r"Statement Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})", text_all, re.IGNORECASE)
    if m:
        summary["Statement Date"] = m.group(1)
    else:
        m2 = re.search(r"(\d{2}\s+[A-Za-z]{3},\s*\d{4})", text_all)
        if m2:
            summary["Statement Date"] = m2.group(1)

    return summary

# ------------------------------
# Fuzzy matching for categories
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
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            for line in lines:
                match = re.match(
                    r"(\d{2}/\d{2}/\d{4})(?:\s+\d{2}:\d{2}:\d{2})?\s+(.+?)\s+([\d,]+\.\d{2})(\s?(Cr|CR|DR|Dr))?$",
                    line
                )
                if match:
                    date, merchant, amount, drcr, _ = match.groups()
                    amt = float(amount.replace(",", ""))
                    if drcr and drcr.strip().lower().startswith("cr"):
                        amt = -amt
                        tr_type = "CR"
                    else:
                        tr_type = "DR"
                    transactions.append([date, merchant.strip(), round(amt, 2), tr_type, account_name])

    return pd.DataFrame(transactions, columns=["Date", "Merchant", "Amount", "Type", "Account"])

# ------------------------------
# Categorize expenses
# ------------------------------
def categorize_expenses(df):
    df["Category"] = df["Merchant"].apply(get_category)
    return df

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

uploaded_files = st.file_uploader(
    "Upload Credit Card Statements (PDF only for summary, CSV/XLSX also for transactions)",
    type=["pdf", "csv", "xlsx"],
    accept_multiple_files=True
)

if uploaded_files:
    all_data = pd.DataFrame(columns=["Date", "Merchant", "Amount", "Type", "Account"])

    for uploaded_file in uploaded_files:
        account_name = st.text_input(f"Enter account name for {uploaded_file.name}", value=uploaded_file.name)

        if account_name:
            if uploaded_file.name.endswith(".pdf"):
                # Extract summary
                st.subheader(f"📑 Statement Summary - {account_name}")
                summary = extract_summary_from_pdf(uploaded_file)
                if summary:
                    cols = st.columns(3)
                    cols[0].metric("📅 Statement Date", summary.get("Statement Date", "N/A"))
                    cols[1].metric("💰 Total Due", summary.get("Total Due", "N/A"))
                    cols[2].metric("🏦 Available Credit", summary.get("Available Credit", "N/A"))

                # Extract transactions
                df = extract_transactions_from_pdf(uploaded_file, account_name)

            elif uploaded_file.name.endswith(".csv"):
                df = pd.read_csv(uploaded_file)
            elif uploaded_file.name.endswith(".xlsx"):
                df = pd.read_excel(uploaded_file)
            else:
                df = pd.DataFrame()

            all_data = pd.concat([all_data, df], ignore_index=True)

    if not all_data.empty:
        all_data = categorize_expenses(all_data)

        st.subheader("📊 Extracted Transactions")
        st.dataframe(all_data.style.format({"Amount": "{:,.2f}"}))

        st.subheader("📥 Download Results")
        csv_data = convert_df_to_csv(all_data)
        excel_data = convert_df_to_excel(all_data)

        st.download_button("⬇️ Download CSV", data=csv_data, file_name="expenses.csv", mime="text/csv")
        st.download_button("⬇️ Download Excel", data=excel_data,
                           file_name="expenses.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
