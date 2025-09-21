import streamlit as st
import pandas as pd
import pdfplumber
import re
from rapidfuzz import process
from io import BytesIO
import os
from datetime import datetime

# ==============================
# Vendor Mapping
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
def fmt_currency(val):
    try:
        return "₹{:,.2f}".format(float(val))
    except:
        return val

def parse_number(s):
    try:
        return float(str(s).replace(",", "").strip())
    except:
        return None

def parse_date(date_str):
    date_str = date_str.replace(",", "").strip()
    for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y"]:
        try:
            return datetime.strptime(date_str, fmt).strftime("%d/%m/%Y")
        except:
            pass
    return date_str

# ------------------------------
# Fuzzy Category Matching
# ------------------------------
def get_category(merchant):
    m = str(merchant).lower()
    try:
        matches = process.extractOne(m, vendor_map["merchant"].str.lower().tolist(), score_cutoff=80)
    except Exception:
        return "Others"
    if matches:
        matched_merchant = matches[0]
        category = vendor_map.loc[vendor_map["merchant"].str.lower() == matched_merchant, "category"].iloc[0]
        return category
    return "Others"

# ------------------------------
# PDF Transaction Extractor
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
                # Generic bank parsing
                for line in lines:
                    match = re.match(
                        r"(\d{2}/\d{2}/\d{4})(?:\s+\d{2}:\d{2}:\d{2})?\s+(.+?)\s+([\d,]+\.\d{2})\s*(CR|Dr|DR|Cr)?",
                        line
                    )
                    if match:
                        date, merchant, amount, drcr = match.groups()
                        amt = float(amount.replace(",", ""))
                        if drcr and drcr.strip().lower().startswith("cr"):
                            amt = -amt
                            tr_type = "CR"
                        else:
                            tr_type = "DR"
                        transactions.append([parse_date(date), merchant.strip(), round(amt, 2), tr_type, account_name])
            else:
                # AMEX parsing
                i = 0
                while i < len(lines):
                    line = lines[i]
                    m = re.match(r"([A-Za-z]{3,9}\s+\d{1,2})\s+(.+?)\s+(?:([\d,]+\.\d{2})\s+)?([\d,]+\.\d{2})\s*(CR|Cr)?$", line)
                    if m:
                        date_str, merchant, foreign, amount, cr_suffix = m.groups()
                        amt_str = foreign if foreign else amount
                        amt = float(amt_str.replace(",", ""))
                        drcr = "DR"
                        if cr_suffix:
                            amt = -amt
                            drcr = "CR"
                        elif "PAYMENT RECEIVED" in merchant.upper():
                            if i + 1 < len(lines) and "CR" in lines[i + 1].upper():
                                amt = -amt
                                drcr = "CR"
                                i += 1
                        transactions.append([parse_date(date_str), merchant.strip(), round(amt, 2), drcr, account_name])
                    i += 1

            st.info(f"📄 Page {page_num}: extracted {len(transactions)} rows so far")

    return pd.DataFrame(transactions, columns=["Date", "Merchant", "Amount", "Type", "Account"])

# ------------------------------
# Summary Extractor (robust)
# ------------------------------
def extract_summary_from_pdf(pdf_file):
    summary = {}
    text_all = ""
    debug_matches = []

    try:
        with pdfplumber.open(pdf_file) as pdf:
            for i in range(min(3, len(pdf.pages))):
                page_text = pdf.pages[i].extract_text()
                if page_text:
                    text_all += page_text + "\n"

        text_all = re.sub(r"\s+", " ", text_all)

        patterns = {
            "Total Limit": r"(Credit Limit|Sanctioned Credit Limit)\s*[:\-]?\s*Rs?\.?\s*([\d,]+\.?\d*)",
            "Available Credit Limit": r"(Available Credit Limit)\s*[:\-]?\s*Rs?\.?\s*([\d,]+\.?\d*)",
            "Used Limit": r"(Closing Balance|Total Due|Total Dues|Outstanding Balance)\s*[:\-]?\s*Rs?\.?\s*([\d,]+\.?\d*)",
            "Statement date": r"(Statement Date|Statement Period.*?to)\s*[:\-]?\s*([0-9]{1,2}[ /-][A-Za-z0-9]+[ /-][0-9]{2,4})",
            "Payment Due Date": r"(Payment Due Date|Due by)\s*[:\-]?\s*([0-9]{1,2}[ /-][A-Za-z0-9]+[ /-][0-9]{2,4})",
        }

        for field, pat in patterns.items():
            m = re.search(pat, text_all, re.IGNORECASE)
            if m:
                raw_line = m.group(0)
                val = m.group(len(m.groups()))
                if field in ["Statement date", "Payment Due Date"]:
                    summary[field] = parse_date(val)
                else:
                    num_val = parse_number(val)
                    if num_val is not None:
                        summary[field] = round(num_val, 2)
                debug_matches.append(f"{field}: '{raw_line}' ➝ {summary.get(field)}")

        m = re.search(r"(Total Purchases|Purchases/ Debits|New Purchases)\s*[:\-]?\s*([\d,]+\.?\d*)", text_all, re.IGNORECASE)
        if m:
            num_val = parse_number(m.group(2))
            if num_val is not None:
                summary["Expenses during the month"] = round(num_val, 2)
                debug_matches.append(f"Expenses: '{m.group(0)}' ➝ {summary['Expenses during the month']}")

        if not summary:
            return {"Info": "No summary details detected in PDF."}

        with st.expander("🔎 Debug Matches"):
            for line in debug_matches:
                st.write(line)

        return summary

    except Exception as e:
        return {"Error": str(e)}

# ------------------------------
# Display Summary Cards
# ------------------------------
def display_summary(summary, account_name):
    st.subheader(f"📋 Statement Summary for {account_name}")

    def colored_card(label, value, color, icon=""):
        if isinstance(value, (int, float)):
            value = fmt_currency(value)
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
# CSV / Excel Extraction
# ------------------------------
def extract_transactions_from_excel(file, account_name):
    df = pd.read_excel(file)
    return normalize_dataframe(df, account_name)

def extract_transactions_from_csv(file, account_name):
    df = pd.read_csv(file)
    return normalize_dataframe(df, account_name)

def normalize_dataframe(df, account_name):
    col_map = {
        "date": "Date", "transaction date": "Date", "txn date": "Date",
        "description": "Merchant", "narration": "Merchant", "merchant": "Merchant",
        "amount": "Amount", "debit": "Debit", "credit": "Credit", "type": "Type"
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
        st.error("❌ Could not detect required columns (Date, Merchant, Amount).")
        return pd.DataFrame(columns=["Date", "Merchant", "Amount", "Type", "Account"])

    df["Amount"] = df["Amount"].astype(float).round(2)
    df["Account"] = account_name
    return df[["Date", "Merchant", "Amount", "Type", "Account"]]

# ------------------------------
# Categorize Expenses
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

        # Unknown merchant categorization
        others_df = all_data[all_data["Category"] == "Others"]
        if not others_df.empty:
            st.subheader("⚡ Assign Categories for Unknown Merchants")
            for merchant in others_df["Merchant"].unique():
                category = st.selectbox(f"Select category for {merchant}:", 
                                        ["Food", "Shopping", "Travel", "Utilities", "Entertainment", "Groceries", "Jewellery",
                                         "Healthcare", "Fuel", "Electronics", "Banking", "Insurance", "Education", "Others"], 
                                        key=merchant)
                if category != "Others":
                    add_new_vendor(merchant, category)
                    all_data.loc[all_data["Merchant"] == merchant, "Category"] = category
                    st.success(f"✅ {merchant} categorized as {category}")

        # Expense analysis
        st.subheader("📊 Expense Analysis")
        expenses = all_data[all_data["Amount"] > 0]
        total_spent = expenses["Amount"].sum()
        st.write("💰 **Total Spent:**", fmt_currency(total_spent))
        st.bar_chart(expenses.groupby("Category")["Amount"].sum())
        st.write("🏦 **Top 5 Merchants**")
        top_merchants = expenses.groupby("Merchant")["Amount"].sum().sort_values(ascending=False).head()
        st.dataframe(top_merchants.apply(fmt_currency))
        st.write("🏦 **Expense by Account**")
        st.bar_chart(expenses.groupby("Account")["Amount"].sum())

        # Export
        csv_data = convert_df_to_csv(all_data)
        excel_data = convert_df_to_excel(all_data)
        st.download_button("⬇️ Download as CSV", csv_data, file_name="expenses_all.csv", mime="text/csv")
        st.download_button("⬇️ Download as Excel", excel_data, file_name="expenses_all.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
