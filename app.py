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
        return f"{float(str(val).replace(',', '').strip()):,.2f}"
    except:
        return val

def parse_number(s):
    try:
        return float(str(s).replace(",", "").strip())
    except:
        return None

def parse_date(date_str):
    """Handle dd/mm/yyyy, Month DD, DD Month formats."""
    date_str = date_str.replace(",", "").strip()
    try:
        return datetime.strptime(date_str, "%d/%m/%Y").strftime("%d/%m/%Y")
    except:
        for fmt in ["%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y", "%B %d", "%b %d"]:
            try:
                d = datetime.strptime(date_str, fmt)
                d = d.replace(year=datetime.today().year)
                return d.strftime("%d/%m/%Y")
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
# Extract transactions from PDF
# ------------------------------
def extract_transactions_from_pdf(pdf_file, account_name):
    transactions = []
    is_amex = False
    with pdfplumber.open(pdf_file) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text()
            if not text:
                continue
            if "American Express" in text:
                is_amex = True
            lines = [l.strip() for l in text.split("\n") if l.strip()]

            if not is_amex:
                # HDFC / ICICI / BoB style
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
                # AMEX style
                i = 0
                while i < len(lines):
                    line = lines[i]
                    m = re.match(r"([A-Za-z]{3,9}\s+\d{1,2})\s+(.+?)\s+([\d,]+\.\d{2})$", line)
                    if m:
                        date_str, merchant, amount = m.groups()
                        try:
                            amt = float(amount.replace(",", ""))
                        except:
                            i += 1
                            continue
                        drcr = "DR"
                        if "PAYMENT RECEIVED" in merchant.upper() or "CR" in line.upper() or "CREDIT" in line.upper():
                            amt = -amt
                            drcr = "CR"
                        transactions.append([parse_date(date_str), merchant.strip(), round(amt, 2), drcr, account_name])
                    i += 1

            st.info(f"📄 Page {page_num}: extracted {len(transactions)} rows so far")

    return pd.DataFrame(transactions, columns=["Date", "Merchant", "Amount", "Type", "Account"])

# ------------------------------
# Extract summary from PDF
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

        # --- AMEX style ---
        amex_patterns = {
            "Total Limit": r"Credit Limit\s*Rs\.?\s*([\d,]+\.\d{2})",
            "Used Limit": r"Closing Balance\s*Rs\.?\s*([\d,]+\.\d{2})",
            "Available Credit Limit": r"Available Credit Limit\s*Rs\.?\s*([\d,]+\.\d{2})",
            "Statement date": r"Statement Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
            "Payment Due Date": r"Payment Due Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
        }

        # --- HDFC / ICICI style ---
        hdfc_patterns = {
            "Total Limit": r"Credit Limit\s*[:\- ]?\s*([\d,]+)",
            "Available Credit Limit": r"Available Credit Limit\s*[:\- ]?\s*([\d,]+)",
            "Used Limit": r"Total Dues\s*[:\- ]?\s*([\d,]+)",
            "Statement date": r"Statement Date\s*[:\- ]?\s*(\d{2}/\d{2}/\d{4})",
            "Payment Due Date": r"Payment Due Date\s*[:\- ]?\s*(\d{2}/\d{2}/\d{4})",
        }

        # --- BoB style ---
        bob_patterns = {
            "Total Limit": r"Sanctioned Credit Limit\s*[:\- ]?\s*([\d,]+\.\d{2})",
            "Available Credit Limit": r"Available Credit Limit\s*[:\- ]?\s*([\d,]+\.\d{2})",
            "Used Limit": r"(Closing Balance|Total Due)\s*[:\- ]?\s*([\d,]+\.\d{2})",
            "Statement date": r"Statement Period\s*From\s*.*?\s*to\s*(\d{2}\s+\w+\s+\d{4})",
            "Payment Due Date": r"Payment Due Date\s*[:\- ]?\s*(\d{2}/\d{2}/\d{4})",
        }

        if "American Express" in text_all:
            chosen = amex_patterns
        elif "HDFC Bank" in text_all or "ICICI Bank" in text_all:
            chosen = hdfc_patterns
        elif "BOB" in text_all or "Bank of Baroda" in text_all:
            chosen = bob_patterns
        else:
            chosen = {**amex_patterns, **hdfc_patterns, **bob_patterns}

        for field, pat in chosen.items():
            m = re.search(pat, text_all, re.IGNORECASE)
            if m:
                val = next((g for g in m.groups() if g), None)
                if val:
                    summary[field] = fmt_num(val)

        # Expenses
        m = re.search(r"Total Purchases\s*[:\- ]?\s*([\d,]+\.\d{2})|Purchases/ Debits\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE)
        if m:
            val = next((g for g in m.groups() if g), None)
            if val:
                summary["Expenses during the month"] = fmt_num(val)

        return summary if summary else {"Info": "No summary details detected"}

    except Exception as e:
        st.error(f"⚠️ Error extracting summary: {e}")
        return {"Info": "No summary details detected"}

# ------------------------------
# Display Summary Cards
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

    col1, col2, col3 = st.columns(3)
    col4, col5, col6 = st.columns(3)

    with col1:
        st.markdown(colored_card("📅 Statement date", summary.get("Statement date", "N/A"), "#0275d8"), unsafe_allow_html=True)
    with col2:
        st.markdown(colored_card("⏰ Payment Due Date", summary.get("Payment Due Date", "N/A"), "#f0ad4e"), unsafe_allow_html=True)
    with col3:
        st.markdown(colored_card("🏦 Total Limit", summary.get("Total Limit", "N/A"), "#5bc0de"), unsafe_allow_html=True)
    with col4:
        st.markdown(colored_card("💰 Used Limit", summary.get("Used Limit", "N/A"), "#d9534f"), unsafe_allow_html=True)
    with col5:
        st.markdown(colored_card("✅ Available Credit Limit", summary.get("Available Credit Limit", "N/A"), "#5cb85c"), unsafe_allow_html=True)
    with col6:
        st.markdown(colored_card("🛒 Expenses during the month", summary.get("Expenses during the month", "N/A"), "#0275d8"), unsafe_allow_html=True)

# ------------------------------
# CSV/XLSX Extractors
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
    df = df.rename(columns={c: col_map[c.lower().strip()] for c in df.columns if c.lower().strip() in col_map})

    if "Debit" in df and "Credit" in df:
        df["Amount"] = df["Debit"].fillna(0) - df["Credit"].fillna(0)
        df["Type"] = df.apply(lambda x: "DR" if x["Debit"] > 0 else "CR", axis=1)
    elif "Amount" in df and "Type" in df:
        df["Amount"] = df.apply(lambda x: -abs(x["Amount"]) if str(x["Type"]).upper().startswith("CR") else abs(x["Amount"]), axis=1)
    elif "Amount" in df and "Type" not in df:
        df["Type"] = "DR"

    if "Date" not in df or "Merchant" not in df or "Amount" not in df:
        st.error("❌ Could not detect required columns (Date, Merchant, Amount).")
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
    return output.getvalue()

# ==============================
# Streamlit UI
# ==============================
st.title("💳 Multi-Account Expense Analyzer")
st.write("✅ App loaded successfully, waiting for uploads...")

uploaded_files = st.file_uploader("Upload Statements", type=["pdf", "csv", "xlsx"], accept_multiple_files=True)

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

        others_df = all_data[all_data["Category"] == "Others"]
        if not others_df.empty:
            st.subheader("⚡ Assign Categories for Unknown Merchants")
            for merchant in others_df["Merchant"].unique():
                category = st.selectbox(
                    f"Select category for {merchant}:",
                    ["Food","Shopping","Travel","Utilities","Entertainment","Groceries","Jewellery","Healthcare","Fuel","Electronics","Banking","Insurance","Education","Others"],
                    key=merchant
                )
                if category != "Others":
                    add_new_vendor(merchant, category)
                    all_data.loc[all_data["Merchant"] == merchant, "Category"] = category
                    st.success(f"✅ {merchant} categorized as {category}")

        st.subheader("📊 Expense Analysis")
        expenses = all_data[all_data["Amount"] > 0]
        st.write("💰 **Total Spent:**", f"{expenses['Amount'].sum():,.2f}")
        st.bar_chart(expenses.groupby("Category")["Amount"].sum())
        st.write("🏦 **Top 5 Merchants**")
        st.dataframe(expenses.groupby("Merchant")["Amount"].sum().sort_values(ascending=False).head().apply(lambda x: f"{x:,.2f}"))
        st.write("🏦 **Expense by Account**")
        st.bar_chart(expenses.groupby("Account")["Amount"].sum())

        st.subheader("📥 Download Results")
        st.download_button("⬇️ Download as CSV", convert_df_to_csv(all_data), "expenses_all.csv", "text/csv")
        st.download_button("⬇️ Download as Excel", convert_df_to_excel(all_data), "expenses_all.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
