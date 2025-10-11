import streamlit as st
import pandas as pd
import pdfplumber
import re
from rapidfuzz import process
from io import BytesIO
import os
import itertools
from datetime import datetime
import math

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
        return f"{float(val):,.2f}"
    except:
        return val

def parse_number(s):
    try:
        return float(str(s).replace(",", "").strip())
    except:
        return None

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
# Date Parser
# ------------------------------
def parse_date(date_str):
    """Handle dd/mm/yyyy, Month DD, DD Month formats."""
    date_str = date_str.replace(",", "").strip()
    try:
        return datetime.strptime(date_str, "%d/%m/%Y").strftime("%d/%m/%Y")
    except:
        try:
            return datetime.strptime(date_str, "%d %b %Y").strftime("%d/%m/%Y")
        except:
            try:
                return datetime.strptime(date_str, "%B %d %Y").strftime("%d/%m/%Y")
            except:
                try:
                    return datetime.strptime(date_str, "%d %B %Y").strftime("%d/%m/%Y")
                except:
                    try:
                        return datetime.strptime(date_str, "%b %d %Y").strftime("%d/%m/%Y")
                    except:
                        return date_str

# ------------------------------
# Extract transactions from PDF
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
                # Non-AMEX parsing
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
                # AMEX parsing per page
                i = 0
                while i < len(lines):
                    line = lines[i]
                    m = re.match(r"([A-Za-z]{3,9}\s+\d{1,2})\s+(.+?)\s+(?:([\d,]+\.\d{2})\s+)?([\d,]+\.\d{2})\s*(CR|Cr)?$", line)
                    if m:
                        date_str, merchant, foreign, amount, cr_suffix = m.groups()
                        amt_str = foreign if foreign else amount
                        try:
                            amt = float(amt_str.replace(",", ""))
                        except:
                            i += 1
                            continue
                        drcr = "DR"
                        if cr_suffix:
                            amt = -amt
                            drcr = "CR"
                        else:
                            if "PAYMENT RECEIVED" in merchant.upper():
                                if i + 1 < len(lines) and "CR" in lines[i + 1].upper():
                                    amt = -amt
                                    drcr = "CR"
                                    i += 1  # Skip the next line
                        transactions.append([parse_date(date_str), merchant.strip(), round(amt, 2), drcr, account_name])
                    i += 1

    return pd.DataFrame(transactions, columns=["Date", "Merchant", "Amount", "Type", "Account"])

# ------------------------------
# Extract summary from PDF (robust HDFC + BoB mapping)
# ------------------------------
def extract_summary_from_pdf(pdf_file):
    summary = {}
    text_all = ""

    patterns = {
        "Statement Date": r"Statement Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
        "Payment Due Date": r"Payment Due Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
        "Total Dues": r"Total Dues\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Total Due\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Closing Balance\s*(?:Rs )?[:\- ]?\s*([\d,]+\.\d*)|Total Amount Due\s*(?:Rs )?[:\- ]?\s*([\d,]+\.\d*)(?:\s*DR)?|(\d{1,3}(?:,\d{3})*\.\d{2})\s*DR|Closing Balance Rs\s* =?\s*([\d,]+\.\d{2})|New Balance\s*\$?([\d,]+\.\d{2})",
        "Minimum Payable": r"Minimum Amount Due\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Minimum Due\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Minimum Payment\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|(\d{1,3}(?:,\d{3})*\.\d{2})\n\s*\d{1,3}(?:,\d{3})*\.\d{2} DR|Minimum Payment Rs\s*([\d,]+\.\d{2})|Minimum Payment Due\s*\$?([\d,]+\.\d{2})",
    }

    stmt_patterns = [
        r"Statement Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
        r"(\d{2}\s+[A-Za-z]{3}\s+\d{4})\s+To",
        r"Statement Period\s*From\s*\w+\s*\d+\s*to\s*(\w+\s*\d+ \d{4})",
        r"Statement Period\s*:\s*\d{2}\s+[A-Za-z]{3},\s*\d{4}\s*To\s*(\d{2}\s+[A-Za-z]{3},\s*\d{4})",
        r"From\s*(\w+\s*\d+)\s*to\s*(\w+\s*\d+,\s*\d{4})",
        r"Date\s*(\d{2}/\d{2}/\d{4})",
        r"(\d{2}/\d{2}/\d{4})\n\s*\d{2} [A-Za-z]{3}, \d{4} To \d{2} [A-Za-z]{3}, \d{4}",
        r"Date\s*(\d{2}/\d{2}/\d{4})"
    ]

    due_patterns = [
        r"Payment Due Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
        r"Due by\s*([A-Za-z]+\s*\d+,\s*\d{4})",
        r"Minimum Payment Due\s*([A-Za-z]+\s*\d+,\s*\d{4})",
        r"Payment Due Date\s*(\d{2}/\d{2}/\d{4})",
        r"received by [A-Za-z]+ \d{1,2}, \d{4}\s*([A-Za-z]+ \d{1,2}, \d{4})",
        r"(\d{2}/\d{2}/\d{4})\n\s*\d{1,3}(?:,\d{3})*\.\d{2}\n\s*\d{1,3}(?:,\d{3})*\.\d{2} DR"
    ]

    try:
        with pdfplumber.open(pdf_file) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    text_all += text + "\n"

        text_all = re.sub(r"\s+", " ", text_all).strip()

        # Extract using patterns
        for key, pat in patterns.items():
            m = re.search(pat, text_all, re.IGNORECASE)
            if m:
                val_str = next((g for g in m.groups() if g is not None), None)
                if val_str:
                    val = parse_number(val_str)
                    if val is not None:
                        summary[key] = fmt_num(val)

        # Regex fallback for Statement Date if missing
        for pat in stmt_patterns:
            m = re.search(pat, text_all, re.IGNORECASE)
            if m:
                if len(m.groups()) > 1 and m.group(2):
                    summary["Statement Date"] = parse_date(m.group(2))
                else:
                    summary["Statement Date"] = parse_date(m.group(1))
                break

        # Regex fallback for Payment Due Date if missing
        for pat in due_patterns:
            m = re.search(pat, text_all, re.IGNORECASE)
            if m:
                summary["Payment Due Date"] = parse_date(m.group(1))
                break

        # Derived fields for the desired summary
        derived_summary = {}
        derived_summary["Statement date"] = summary.get("Statement Date", "N/A")
        derived_summary["Payment due date"] = summary.get("Payment Due Date", "N/A")
        derived_summary["Total Dues"] = summary.get("Total Due", "N/A")
        derived_summary["Minimum payable"] = summary.get("Minimum Due", "N/A")

        return derived_summary

    except Exception as e:
        st.error(f"⚠️ Error while extracting summary: {e}")

    return {"Info": "No summary details detected in PDF."}

# ------------------------------
# Pretty Summary Cards (color-coded)
# ------------------------------
def display_summary(summary, account_name):
    st.subheader(f"📋 Statement Summary for {account_name}")

    def colored_card(label, value, color, icon=""):
        return f"""
        <div style="background:{color};padding:12px;border-radius:10px;margin:3px;text-align:center;color:white;font-weight:600;">
            <div style="font-size:15px;">{icon} {label}</div>
            <div style="font-size:18px;margin-top:6px;">{value}</div>
        </div>
        """

    def try_float_str(s):
        try:
            return float(str(s).replace(",", ""))
        except:
            return 0.0

    total_due_val = try_float_str(summary.get("Total Dues", "0"))
    min_val = try_float_str(summary.get("Minimum payable", "0"))

    total_due_color = "#d9534f" if total_due_val > 0 else "#5cb85c"
    min_color = "#f0ad4e" if min_val > 0 else "#5cb85c"

    col1, col2 = st.columns(2)
    col3, col4 = st.columns(2)

    with col1:
        st.markdown(colored_card("📅 Statement date", summary["Statement date"], "#0275d8"), unsafe_allow_html=True)
    with col2:
        st.markdown(colored_card("⏰ Payment due date", summary["Payment due date"], "#f0ad4e"), unsafe_allow_html=True)
    with col3:
        st.markdown(colored_card("💰 Total Dues", summary["Total Dues"], total_due_color), unsafe_allow_html=True)
    with col4:
        st.markdown(colored_card("⚠️ Minimum payable", summary["Minimum payable"], min_color), unsafe_allow_html=True)

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
# Categorize expenses (simple)
# ------------------------------
def categorize_expenses(df):
    df["Category"] = df["Merchant"].apply(get_category)
    return df

# ------------------------------
# Add new vendor (persist)
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
st.title("💳 Credit card Expenses Analyzer")
st.write("Upload your unlocked CC statement and get insights")

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

                # show summary and cards
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

        # Unknown merchant handling
        others_df = all_data[all_data["Category"] == "Others"]
        if not others_df.empty:
            st.subheader("⚡ Assign Categories for Unknown Merchants")
            for merchant in others_df["Merchant"].unique():
                category = st.selectbox(
                    f"Select category for {merchant}:",
                    ["Food", "Shopping", "Travel", "Utilities", "Entertainment", "Groceries", "Jewellery", "Healthcare", "Fuel", "Electronics", "Banking", "Insurance", "Education", "Others"],
                    key=merchant
                )
                if category != "Others":
                    add_new_vendor(merchant, category)
                    all_data.loc[all_data["Merchant"] == merchant, "Category"] = category
                    st.success(f"✅ {merchant} categorized as {category}")

        st.subheader("📊 Expense Analysis")
        expenses = all_data[all_data["Amount"] > 0]
        total_spent = expenses["Amount"].sum()
        st.write("💰 **Total Spent:**", f"{total_spent:,.2f}")
        st.bar_chart(expenses.groupby("Category")["Amount"].sum())
        st.write("🏦 **Top 5 Merchants**")
        top_merchants = expenses.groupby("Merchant")["Amount"].sum().sort_values(ascending=False).head()
        st.dataframe(top_merchants.apply(lambda x: f"{x:,.2f}"))
        st.write("🏦 **Expense by Account**")
        st.bar_chart(expenses.groupby("Account")["Amount"].sum())

        # Export
        csv_data = convert_df_to_csv(all_data)
        excel_data = convert_df_to_excel(all_data)

        st.download_button("⬇️ Download as CSV", csv_data, file_name="expenses_all.csv", mime="text/csv")
        st.download_button("⬇️ Download as Excel", excel_data, file_name="expenses_all.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
