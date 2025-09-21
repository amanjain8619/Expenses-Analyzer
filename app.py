import streamlit as st
import pandas as pd
import pdfplumber
import re
from rapidfuzz import process
from io import BytesIO
import os
from datetime import datetime

# ==============================
# Vendor mapping
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
# Vendor category matcher
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

        # ✅ Patterns for HDFC, BoB, ICICI, AMEX
        patterns = {
            "Credit Limit": r"(Credit Limit|Sanctioned Credit Limit)\s*Rs?\.?\s*([\d,]+\.?\d*)",
            "Available Credit": r"(Available Credit Limit|Available Credit)\s*Rs?\.?\s*([\d,]+\.?\d*)",
            "Total Due": r"(Total Dues|Total Due|Total Amount Due|Closing Balance)\s*Rs?\.?\s*([\d,]+\.?\d*)",
            "Minimum Due": r"(Minimum Amount Due|Minimum Due|Minimum Payment Due)\s*Rs?\.?\s*([\d,]+\.?\d*)",
        }

        # Regex for statement and due date
        stmt_patterns = [
            r"Statement Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
            r"Statement Date\s*[:\-]?\s*([0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4})",
            r"Statement Period.*?to\s*([0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4})"
        ]
        due_patterns = [
            r"Payment Due Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
            r"Payment Due Date\s*[:\-]?\s*([A-Za-z]{3,9}\s*\d{1,2},\s*\d{4})"
        ]

        # ✅ Extract main fields
        for key, pat in patterns.items():
            m = re.search(pat, text_all, re.IGNORECASE)
            if m:
                val = m.group(2)
                if val:
                    summary[key] = fmt_num(val)

        # ✅ Extract statement date
        for pat in stmt_patterns:
            m = re.search(pat, text_all, re.IGNORECASE)
            if m:
                summary["Statement Date"] = parse_date(m.group(1))
                break

        # ✅ Extract due date
        for pat in due_patterns:
            m = re.search(pat, text_all, re.IGNORECASE)
            if m:
                summary["Payment Due Date"] = parse_date(m.group(1))
                break

        # ✅ Normalize keys
        if "Total Due" in summary:
            summary["Used / Closing"] = summary["Total Due"]

        if "Closing Balance" in summary and "Used / Closing" not in summary:
            summary["Used / Closing"] = summary["Closing Balance"]

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
# Display summary cards
# ------------------------------
def display_summary(summary, account_name):
    st.subheader(f"📋 Statement Summary — {account_name}")

    def card(label, value, color):
        return f"""
        <div style="background:{color};padding:12px;border-radius:8px;margin:5px;text-align:center;color:white;">
            <b>{label}</b><br><span style="font-size:16px;">{value}</span>
        </div>
        """

    cols = st.columns(3)
    with cols[0]:
        st.markdown(card("📅 Statement date", summary["Statement date"], "#0275d8"), unsafe_allow_html=True)
    with cols[1]:
        st.markdown(card("⏰ Payment Due Date", summary["Payment Due Date"], "#f0ad4e"), unsafe_allow_html=True)
    with cols[2]:
        st.markdown(card("🏦 Total Limit", summary["Total Limit"], "#5bc0de"), unsafe_allow_html=True)

    cols = st.columns(3)
    with cols[0]:
        st.markdown(card("💰 Used / Closing", summary["Used / Closing"], "#d9534f"), unsafe_allow_html=True)
    with cols[1]:
        st.markdown(card("✅ Available Credit", summary["Available Credit"], "#5cb85c"), unsafe_allow_html=True)
    with cols[2]:
        st.markdown(card("🛒 Min Due", summary["Min Due"], "#6f42c1"), unsafe_allow_html=True)

# ------------------------------
# PDF Transactions
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
                match = re.match(r"(\d{2}/\d{2}/\d{4})\s+(.+?)\s+([\d,]+\.\d{2})\s*(CR|DR)?", line)
                if match:
                    date, merchant, amount, drcr = match.groups()
                    amt = float(amount.replace(",", ""))
                    if drcr == "CR":
                        amt = -amt
                    transactions.append([parse_date(date), merchant, amt, drcr if drcr else "DR", account_name])

    return pd.DataFrame(transactions, columns=["Date", "Merchant", "Amount", "Type", "Account"])

# ------------------------------
# App UI
# ------------------------------
st.title("💳 Multi-Account Expense Analyzer")
st.write("Upload your bank/credit card statements to extract transactions and summaries.")

uploaded_files = st.file_uploader("Upload Statements", type=["pdf"], accept_multiple_files=True)

if uploaded_files:
    for uploaded_file in uploaded_files:
        account_name = uploaded_file.name

        df = extract_transactions_from_pdf(uploaded_file, account_name)
        summary = extract_summary_from_pdf(uploaded_file)

        display_summary(summary, account_name)

        if not df.empty:
            st.subheader(f"📑 Transactions — {account_name}")
            st.dataframe(df)
