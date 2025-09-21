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

def parse_date(date_str):
    """Handle dd/mm/yyyy, Month DD, DD Month formats."""
    date_str = date_str.replace(",", "").strip()
    try:
        return datetime.strptime(date_str, "%d/%m/%Y").strftime("%d/%m/%Y")
    except:
        for fmt in:
            try:
                return datetime.strptime(date_str, fmt).strftime("%d/%m/%Y")
            except:
                pass
        return date_str

# ------------------------------
# Template-Driven PDF Parsing
# ------------------------------

class BaseParser:
    def __init__(self, pdf_file):
        self.pdf_file = pdf_file
        self.text_all = self.get_full_text()
        self.tables = self.get_all_tables()
    
    def get_full_text(self):
        text = ""
        with pdfplumber.open(self.pdf_file) as pdf:
            for page in pdf.pages:
                text += page.extract_text() + "\n"
        return text

    def get_all_tables(self):
        tables =
        with pdfplumber.open(self.pdf_file) as pdf:
            for page in pdf.pages:
                tables.extend(page.extract_tables() or)
        return tables

    def parse_summary(self):
        # Fallback summary parsing using regex
        summary = {}
        patterns = {
            "Credit Limit": r"Credit Limit\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Sanctioned Credit Limit\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)",
            "Available Credit": r"Available Credit Limit\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Available Credit\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)",
            "Total Due": r"Total Dues\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Total Due\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Closing Balance\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Total Amount Due\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)(?:\s*DR)?",
            "Minimum Due": r"Minimum Amount Due\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Minimum Due\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Minimum Payment\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)",
            "Previous Balance": r"Previous Balance\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Opening Balance\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)",
            "Total Payments": r"Total Payments\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|New Credits Rs - ([\d,]+\.?\d*) \+|Payment/ Credits\s*([\d,]+\.?\d*)|Payments/ Credits\s*([\d,]+\.?\d*)|Payment/Credits\s*([\d,]+\.?\d*)",
            "Total Purchases": r"Total Purchases\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|New Debits Rs ([\d,]+\.?\d*)|Purchase/ Debits\s*([\d,]+\.?\d*)|Purchases/Debits\s*([\d,]+\.?\d*)|New Purchases/Debits\s*([\d,]+\.?\d*)",
        }
        
        for key, pat in patterns.items():
            m = re.search(pat, self.text_all, re.IGNORECASE)
            if m:
                val_str = next((g for g in m.groups() if g is not None), None)
                if val_str:
                    summary[key] = fmt_num(parse_number(val_str))
        
        stmt_patterns =?\s*(\d{2}/\d{2}/\d{4})",
            r"(\d{2}\s+[A-Za-z]{3}\s+\d{4})\s+To",
            r"Statement Period\s*From\s*\w+\s*\d+\s*to\s*(\w+\s*\d+ \d{4})",
            r"Statement Period\s*:\s*\d{2}\s+[A-Za-z]{3},\s*\d{4}\s*To\s*(\d{2}\s+[A-Za-z]{3},\s*\d{4})",
        ]
        
        for pat in stmt_patterns:
            m = re.search(pat, self.text_all, re.IGNORECASE)
            if m:
                if len(m.groups()) > 1 and m.group(2):
                    summary = parse_date(m.group(2))
                else:
                    summary = parse_date(m.group(1))
                break
        
        due_patterns =?\s*(\d{2}/\d{2}/\d{4})",
            r"Due by\s*([A-Za-z]+\s*\d+,\s*\d{4})",
            r"Minimum Payment Due\s*([A-Za-z]+\s*\d+,\s*\d{4})",
        ]
        
        for pat in due_patterns:
            m = re.search(pat, self.text_all, re.IGNORECASE)
            if m:
                summary = parse_date(m.group(1))
                break
        
        derived_summary = {}
        derived_summary = summary.get("Credit Limit", "N/A")
        
        cl = parse_number(derived_summary)
        av = parse_number(summary.get("Available Credit", "N/A"))
        td = parse_number(summary.get("Total Due", "N/A"))
        if td is not None:
            derived_summary["Used Limit"] = fmt_num(td)
        elif cl is not None and av is not None:
            derived_summary["Used Limit"] = fmt_num(cl - av)
        else:
            derived_summary["Used Limit"] = "N/A"
        
        derived_summary = summary.get("Statement Date", "N/A")
        derived_summary["Expenses during the month"] = summary.get("Total Purchases", "N/A")
        derived_summary["Available Credit Limit"] = summary.get("Available Credit", "N/A")
        derived_summary = summary.get("Payment Due Date", "N/A")
        
        return derived_summary

    def parse_transactions(self, account_name):
        transactions =
        # General transaction parsing logic
        for line in self.text_all.split("\n"):
            line = line.strip()
            match = re.match(r"(\d{2}/\d{2}/\d{4})(?:\s+\d{2}:\d{2}:\d{2})?\s+(.+?)\s+([\d,]+\.\d{2})\s*(CR|Dr|DR|Cr)?", line)
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
        return pd.DataFrame(transactions, columns=)

class HDFCParser(BaseParser):
    def __init__(self, pdf_file):
        super().__init__(pdf_file)
        # HDFC specific regex patterns
        self.summary_patterns = {
            "Total Due": r"(?:Total Dues|Total Amount Due)\s*[:\s]+([\d,]+\.\d{2})",
            "Minimum Due": r"(?:Minimum Amount Due|Minimum Payment)\s*[:\s]+([\d,]+\.\d{2})",
            "Credit Limit": r"Credit Limit\s*[:\s]+([\d,]+\.\d{2})",
            "Available Credit": r"Available Credit\s*[:\s]+([\d,]+\.\d{2})",
            "Statement Date": r"Statement Date\s*[:\s]+(\d{2}/\d{2}/\d{4})",
            "Payment Due Date": r"Payment Due Date\s*[:\s]+(\d{2}/\d{2}/\d{4})",
            "Total Purchases": r"(?:Purchase|Purchases)\/Debits\s*([\d,]+\.\d{2})",
            "Total Payments": r"(?:Payment|Payments)\/Credits\s*([\d,]+\.\d{2})",
        }
    
    def parse_summary(self):
        summary_data = {}
        for key, pattern in self.summary_patterns.items():
            match = re.search(pattern, self.text_all, re.IGNORECASE)
            if match:
                summary_data[key] = fmt_num(parse_number(match.group(1)))
        
        derived_summary = {}
        derived_summary = summary_data.get("Credit Limit", "N/A")
        
        cl = parse_number(derived_summary)
        av = parse_number(summary_data.get("Available Credit", "N/A"))
        td = parse_number(summary_data.get("Total Due", "N/A"))
        if td is not None:
            derived_summary["Used Limit"] = fmt_num(td)
        elif cl is not None and av is not None:
            derived_summary["Used Limit"] = fmt_num(cl - av)
        else:
            derived_summary["Used Limit"] = "N/A"
        
        derived_summary = summary_data.get("Statement Date", "N/A")
        derived_summary["Expenses during the month"] = summary_data.get("Total Purchases", "N/A")
        derived_summary["Available Credit Limit"] = summary_data.get("Available Credit", "N/A")
        derived_summary = summary_data.get("Payment Due Date", "N/A")
        
        return derived_summary

    def parse_transactions(self, account_name):
        transactions =
        # AMEX-like parsing with date, merchant, amount
        i = 0
        lines = [l.strip() for l in self.text_all.split("\n") if l.strip()]
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
        return pd.DataFrame(transactions, columns=)

class BOBParser(BaseParser):
    def __init__(self, pdf_file):
        super().__init__(pdf_file)
        # Bank of Baroda specific regex patterns
        self.summary_patterns = {
            "Total Due": r"Total Amount Due\s*₹?\s*([\d,]+\.\d{2})",
            "Minimum Due": r"Minimum Amount Due\s*₹?\s*([\d,]+\.\d{2})",
            "Statement Date": r"Statement Date\s*(\d{2}-[A-Za-z]{3}-\d{4})",
            "Payment Due Date": r"Payment Due Date\s*(\d{2}-[A-Za-z]{3}-\d{4})",
        }

    def parse_summary(self):
        summary_data = {}
        for key, pattern in self.summary_patterns.items():
            match = re.search(pattern, self.text_all, re.IGNORECASE)
            if match:
                summary_data[key] = fmt_num(parse_number(match.group(1)))
        
        # Fallback to general parser if specific fields are not found
        if not summary_data:
            return super().parse_summary()
        
        derived_summary = {}
        derived_summary = summary_data.get("Credit Limit", "N/A") # Not always in BOB, so N/A fallback
        derived_summary["Used Limit"] = summary_data.get("Total Due", "N/A")
        derived_summary = summary_data.get("Statement Date", "N/A")
        derived_summary["Expenses during the month"] = summary_data.get("Total Purchases", "N/A") # Not always in BOB
        derived_summary["Available Credit Limit"] = summary_data.get("Available Credit", "N/A") # Not always in BOB
        derived_summary = summary_data.get("Payment Due Date", "N/A")

        return derived_summary

    def parse_transactions(self, account_name):
        transactions =
        lines = [l.strip() for l in self.text_all.split("\n") if l.strip()]
        # Example for a specific transaction format, can be extended
        # Date Ref. No. Particulars Source Source Amt. Amount. Reward. Points
        transaction_pattern = r"(\d{2}\s+[A-Za-z]{3,9}\s+\d{4})\s+([\d,]+)\s+([\d,]+\.\d{2})"
        for line in lines:
            m = re.match(transaction_pattern, line)
            if m:
                # This is a placeholder for a specific format; needs real-world testing.
                # A robust solution would need to handle the wide variety of BoB formats.
                pass
        
        # As a fallback, use the general-purpose transaction parser
        transactions = super().parse_transactions(account_name).to_dict('records')
        return pd.DataFrame(transactions, columns=)

def detect_bank(text_content):
    if "HDFC Bank" in text_content or "HDFC Bank Ltd." in text_content:
        return "HDFC"
    if "Bank of Baroda" in text_content or "BOBCARD" in text_content:
        return "BOB"
    if "American Express" in text_content:
        return "AMEX" # Acknowledging AMEX, but using HDFC's parser for now
    return "Generic"

def parse_pdf_statement(pdf_file, account_name):
    try:
        text = BaseParser(pdf_file).get_full_text()
        bank = detect_bank(text)
        st.info(f"✅ Identified bank as {bank}. Using specific parser.")

        if bank == "HDFC":
            parser = HDFCParser(pdf_file)
        elif bank == "BOB":
            parser = BOBParser(pdf_file)
        else: # Generic or AMEX fallback
            parser = BaseParser(pdf_file)
        
        summary = parser.parse_summary()
        transactions = parser.parse_transactions(account_name)
        
        return summary, transactions

    except Exception as e:
        st.error(f"⚠️ Error while parsing PDF statement: {e}")
        return {"Info": "Parsing failed."}, pd.DataFrame()


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
        matched_merchant = matches
        category = vendor_map.loc[
            vendor_map["merchant"].str.lower() == matched_merchant, "category"
        ].iloc
        return category
    return "Others"

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
        df["Amount"] = df.fillna(0) - df["Credit"].fillna(0)
        df = df.apply(lambda x: "DR" if x > 0 else "CR", axis=1)
    elif "Amount" in df and "Type" in df:
        df["Amount"] = df.apply(lambda x: -abs(x["Amount"]) if str(x).upper().startswith("CR") else abs(x["Amount"]), axis=1)
    elif "Amount" in df and "Type" not in df:
        df = "DR"

    if "Date" not in df or "Merchant" not in df or "Amount" not in df:
        st.error("❌ Could not detect required columns (Date, Merchant, Amount). Please check your file.")
        return pd.DataFrame(columns=)

    df["Amount"] = df["Amount"].astype(float).round(2)
    df["Account"] = account_name
    return df]

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

    used_val = try_float_str(summary.get("Used Limit", "0"))
    avail_val = try_float_str(summary.get("Available Credit Limit", "0"))

    used_color = "#d9534f" if used_val > 0 else "#5cb85c"
    avail_color = "#5cb85c" if avail_val > 0 else "#d9534f"

    col1, col2, col3 = st.columns(3)
    col4, col5, col6 = st.columns(3)

    with col1:
        st.markdown(colored_card("📅 Statement date", summary, "#0275d8"), unsafe_allow_html=True)
    with col2:
        st.markdown(colored_card("⏰ Payment Due Date", summary, "#f0ad4e"), unsafe_allow_html=True)
    with col3:
        st.markdown(colored_card("🏦 Total Limit", summary, "#5bc0de"), unsafe_allow_html=True)
    with col4:
        st.markdown(colored_card("💰 Used Limit", summary["Used Limit"], used_color), unsafe_allow_html=True)
    with col5:
        st.markdown(colored_card("✅ Available Credit Limit", summary["Available Credit Limit"], avail_color), unsafe_allow_html=True)
    with col6:
        st.markdown(colored_card("🛒 Expenses during the month", summary["Expenses during the month"], "#0275d8"), unsafe_allow_html=True)

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
    all_data = pd.DataFrame(columns=)

    for uploaded_file in uploaded_files:
        account_name = st.text_input(f"Enter account name for {uploaded_file.name}", value=uploaded_file.name)

        if account_name:
            if uploaded_file.name.endswith(".pdf"):
                summary, df = parse_pdf_statement(uploaded_file, account_name)
                if not df.empty:
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
                   ,
                    key=merchant
                )
                if category!= "Others":
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
