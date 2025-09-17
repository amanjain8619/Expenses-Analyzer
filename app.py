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
# Extract transactions from PDF (supports HDFC + BoB)
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
# Extract summary from PDF (multi-bank support)
# ------------------------------
def extract_summary_from_pdf(pdf_file):
    summary = {}
    text_all = ""

    # Read first 3 pages (HDFC = page 1, BoB often = page 2)
    with pdfplumber.open(pdf_file) as pdf:
        for i in range(min(3, len(pdf.pages))):
            page_text = pdf.pages[i].extract_text()
            if page_text:
                text_all += "\n" + page_text

    if not text_all:
        return summary

    # Regex patterns expanded for HDFC + BoB
    patterns = {
        "Statement Date": [
            r"Statement Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
            r"Stmt Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
            r"(\d{2}\s+[A-Za-z]{3},\s*\d{4})\s+To"
        ],
        "Payment Due Date": [
            r"Payment Due Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
            r"Due Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})"
        ],
        "Previous Balance": [
            r"Previous Balance\s*[:\-]?\s*([\d,]+\.\d{2})",
            r"Opening Balance\s*[:\-]?\s*([\d,]+\.\d{2})"
        ],
        "Total Due": [
            r"(?:Total Dues|Total Amount Due|Total Due)\s*[:\-]?\s*([\d,]+\.\d{2})",
            r"Amount Due\s*[:\-]?\s*([\d,]+\.\d{2})"
        ],
        "Minimum Due": [
            r"(?:Minimum Amount Due|Minimum Due)\s*[:\-]?\s*([\d,]+\.\d{2})"
        ],
        "Total Purchases": [
            r"(?:Total Purchases|Purchases/ Debits|Purchases and Other Debits)\s*[:\-]?\s*([\d,]+\.\d{2})"
        ],
        "Total Payments": [
            r"(?:Total Payments|Payments/ Credits|Payments and Other Credits)\s*[:\-]?\s*([\d,]+\.\d{2})"
        ],
        "Credit Limit": [
            r"(?:Credit Limit|Total Credit Limit)\s*[:\-]?\s*([\d,]+\.\d{2})"
        ],
        "Available Credit": [
            r"(?:Available Credit Limit|Available Credit|Avail\. Credit)\s*[:\-]?\s*([\d,]+\.\d{2})"
        ],
    }

    for key, regex_list in patterns.items():
        found = False
        for pattern in regex_list:
            m = re.search(pattern, text_all, re.IGNORECASE)
            if m:
                summary[key] = m.group(1).replace(",", "")
                found = True
                break
        if not found:
            summary[key] = "-"

    return summary

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
    st.write("💰 **Total Spent:**", f"{df['Amount'].sum():,.2f}")

    st.write("📊 **Expense by Category**")
    cat_data = df.groupby("Category")["Amount"].sum().round(2)
    st.bar_chart(cat_data)

    st.write("🏦 **Top 5 Merchants**")
    top_merchants = df.groupby("Merchant")["Amount"].sum().round(2).sort_values(ascending=False).head()
    st.dataframe(top_merchants.apply(lambda x: f"{x:,.2f}"))

    st.write("🏦 **Expense by Account**")
    acc_data = df.groupby("Account")["Amount"].sum().round(2)
    st.bar_chart(acc_data)

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

# ------------------------------
# Pretty Summary Cards
# ------------------------------
def display_summary(summary, account_name):
    st.subheader(f"📋 Statement Summary for {account_name}")

    def colored_card(label, value, color, icon=""):
        return f"""
        <div style="background:{color};padding:15px;border-radius:12px;margin:5px;text-align:center;color:white;font-weight:bold;">
            <div style="font-size:18px;">{icon} {label}</div>
            <div style="font-size:22px;margin-top:8px;">{value}</div>
        </div>
        """

    def try_float(v):
        try:
            return float(v)
        except:
            return 0.0

    total_due_val = try_float(summary.get("Total Due", "0"))
    min_due_val = try_float(summary.get("Minimum Due", "0"))
    avail_credit_val = try_float(summary.get("Available Credit", "0"))

    total_due_color = "#d9534f" if total_due_val > 0 else "#5cb85c"
    min_due_color = "#f0ad4e" if min_due_val > 0 else "#5cb85c"
    avail_credit_color = "#5bc0de" if avail_credit_val > 0 else "#d9534f"

    col1, col2, col3 = st.columns(3)
    col4, col5, col6 = st.columns(3)
    col7, col8, col9 = st.columns(3)

    with col1:
        if summary.get("Statement Date", "-") != "-":
            st.markdown(colored_card("Statement Date", summary["Statement Date"], "#0275d8", "📅"), unsafe_allow_html=True)
    with col2:
        if summary.get("Payment Due Date", "-") != "-":
            st.markdown(colored_card("Payment Due Date", summary["Payment Due Date"], "#5bc0de", "⏰"), unsafe_allow_html=True)
    with col3:
        if summary.get("Previous Balance", "-") != "-":
            st.markdown(colored_card("Previous Balance", f"{float(summary['Previous Balance']):,.2f}", "#6f42c1", "💳"), unsafe_allow_html=True)

    with col4:
        if summary.get("Total Due", "-") != "-":
            st.markdown(colored_card("Total Due", f"{total_due_val:,.2f}", total_due_color, "💰"), unsafe_allow_html=True)
    with col5:
        if summary.get("Minimum Due", "-") != "-":
            st.markdown(colored_card("Minimum Due", f"{min_due_val:,.2f}", min_due_color, "⚠️"), unsafe_allow_html=True)
    with col6:
        if summary.get("Credit Limit", "-") != "-":
            st.markdown(colored_card("Credit Limit", f"{float(summary['Credit Limit']):,.2f}", "#5cb85c", "🏦"), unsafe_allow_html=True)

    with col7:
        if summary.get("Available Credit", "-") != "-":
            st.markdown(colored_card("Available Credit", f"{avail_credit_val:,.2f}", avail_credit_color, "✅"), unsafe_allow_html=True)
    with col8:
        if summary.get("Total Purchases", "-") != "-":
            st.markdown(colored_card("Total Purchases", f"{float(summary['Total Purchases']):,.2f}", "#0275d8", "🛒"), unsafe_allow_html=True)
    with col9:
        if summary.get("Total Payments", "-") != "-":
            st.markdown(colored_card("Total Payments", f"{float(summary['Total Payments']):,.2f}", "#20c997", "💵"), unsafe_allow_html=True)

# ==============================
# Streamlit UI
# ==============================
st.title("💳 Multi-Account Expense Analyzer")
st.write("Upload your bank/credit card statements (PDF, CSV, or Excel), categorize expenses, and compare across accounts.")

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
                if summary:
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

        st.subheader("🔍 Select Account for Analysis")
        account_options = ["All Accounts"] + sorted(all_data["Account"].unique().tolist())
        selected_account = st.selectbox("Choose account", account_options)

        if selected_account != "All Accounts":
            filtered_data = all_data[all_data["Account"] == selected_account]
        else:
            filtered_data = all_data

        st.subheader("📑 Extracted Transactions")
        st.dataframe(filtered_data.style.format({"Amount": "{:,.2f}"}))

        others_df = filtered_data[filtered_data["Category"] == "Others"]
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
        analyze_expenses(filtered_data)

        st.subheader("📥 Download Results")
        csv_data = convert_df_to_csv(filtered_data)
        excel_data = convert_df_to_excel(filtered_data)

        st.download_button(
            label="⬇️ Download as CSV",
            data=csv_data,
            file_name=f"expenses_{selected_account.replace(' ','_')}.csv",
            mime="text/csv"
        )

        st.download_button(
            label="⬇️ Download as Excel",
            data=excel_data,
            file_name=f"expenses_{selected_account.replace(' ','_')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
