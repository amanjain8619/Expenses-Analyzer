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
import json
import openai
from typing import Dict, Any

# ==============================
# CONFIG
# ==============================
st.set_page_config(layout="wide")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
USE_AI_FALLBACK_DEFAULT = True  # default toggle

VENDOR_FILE = "vendors.csv"
if os.path.exists(VENDOR_FILE):
    vendor_map = pd.read_csv(VENDOR_FILE)
else:
    vendor_map = pd.DataFrame(columns=["merchant", "category"])
    vendor_map.to_csv(VENDOR_FILE, index=False)

if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

# ==============================
# Helpers
# ==============================
def fmt_rupee(val):
    try:
        n = float(val)
        return f"₹{n:,.2f}"
    except:
        return val

def parse_number(s):
    try:
        return float(str(s).replace(",", "").replace("₹", "").strip())
    except:
        return None

def parse_date(date_str):
    if not date_str:
        return "N/A"
    s = str(date_str).strip().replace(",", "")
    # dd/mm/YYYY
    m = re.search(r"(\d{2}/\d{2}/\d{4})", s)
    if m:
        return m.group(1)
    # forms like "15 Aug 2025" or "Aug 15 2025"
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%d/%m/%Y")
        except:
            pass
    # fallback: return trimmed
    return s

# ------------------------------
# vendor fuzzy match
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

# ==============================
# Transaction extraction
# (unchanged logic but robust)
# ==============================
def extract_transactions_from_pdf(pdf_bytes, account_name):
    transactions = []
    pdf_file = BytesIO(pdf_bytes)
    with pdfplumber.open(pdf_file) as pdf:
        # detect AMEX
        sample_text = ""
        for p in pdf.pages[:3]:
            sample_text += (p.extract_text() or "") + "\n"
        is_amex = "american express" in sample_text.lower() or "americanexpress" in sample_text.lower()

        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text()
            if not text:
                continue
            lines = [l.strip() for l in text.split("\n") if l.strip()]

            if is_amex:
                # AMEX heuristics
                for line in lines:
                    m = re.match(r"([A-Za-z]{3,9}\s+\d{1,2})(?:\s+\d{4})?\s+(.+?)\s+([\d,]+\.\d{2})\s*(CR|Cr|cr)?\s*$", line)
                    if m:
                        date_part, merchant, amount, cr = m.groups()
                        date = parse_date(date_part + " 2025")  # year guess if missing
                        amt = parse_number(amount)
                        if amt is None:
                            continue
                        if cr and cr.lower().startswith("cr"):
                            amt = -round(amt, 2)
                            tr_type = "CR"
                        else:
                            tr_type = "DR"
                        transactions.append([date, merchant.strip(), round(amt, 2), tr_type, account_name])
                    else:
                        # fallback: lines with INR/ Rs and amount
                        m2 = re.search(r"(.+?)\s+(INR|Rs\.?|₹)\s*([\d,]+\.\d{2})\s*(CR|Cr|cr|DR|Dr|dr)?$", line)
                        if m2:
                            merchant = m2.group(1)
                            amount = m2.group(3)
                            cr = m2.group(4)
                            amt = parse_number(amount)
                            if amt is None:
                                continue
                            tr_type = "CR" if cr and cr.lower().startswith("cr") else "DR"
                            if tr_type == "CR":
                                amt = -round(amt, 2)
                            transactions.append(["N/A", merchant.strip(), round(amt, 2), tr_type, account_name])
            else:
                # generic parsing for dd/mm/yyyy lines
                for line in lines:
                    match = re.match(r"(\d{2}/\d{2}/\d{4})(?:\s+\d{2}:\d{2}:\d{2})?\s+(.+?)\s+([\d,]+\.\d{2})\s*(CR|Dr|DR|Cr)?\s*$", line)
                    if match:
                        date, merchant, amount, drcr = match.groups()
                        amt = parse_number(amount)
                        if amt is None:
                            continue
                        if drcr and drcr.strip().lower().startswith("cr"):
                            amt = -round(amt, 2)
                            tr_type = "CR"
                        else:
                            tr_type = "DR"
                        transactions.append([parse_date(date), merchant.strip(), round(amt,2), tr_type, account_name])
                    else:
                        m2 = re.search(r"(.+?)\s+(INR|Rs\.?|₹)\s*([\d,]+\.\d{2})\s*(CR|Cr|cr|DR|Dr|dr)?$", line)
                        if m2:
                            merchant = m2.group(1)
                            amount = m2.group(3)
                            cr = m2.group(4)
                            amt = parse_number(amount)
                            if amt is None:
                                continue
                            tr_type = "CR" if cr and cr.lower().startswith("cr") else "DR"
                            if tr_type == "CR":
                                amt = -round(amt, 2)
                            # try date in same line
                            dmatch = re.search(r"(\d{2}/\d{2}/\d{4})", line)
                            date = parse_date(dmatch.group(1)) if dmatch else "N/A"
                            transactions.append([date, merchant.strip(), round(amt,2), tr_type, account_name])

            st.info(f"📄 Page {page_num}: extracted {len(transactions)} rows so far")

    df = pd.DataFrame(transactions, columns=["Date", "Merchant", "Amount", "Type", "Account"])
    if not df.empty:
        df["Amount"] = df["Amount"].astype(float).round(2)
    return df

# ==============================
# RULE-BASED SUMMARY extraction
# returns dict with 4 keys; if missing any, returns "N/A" for them
# ==============================
def extract_summary_rules(pdf_bytes) -> Dict[str, Any]:
    summary = {
        "Statement date": "N/A",
        "Payment due date": "N/A",
        "Minimum payable": "N/A",
        "Total Dues": "N/A"
    }
    pdf_file = BytesIO(pdf_bytes)
    try:
        with pdfplumber.open(pdf_file) as pdf:
            first_pages_text = ""
            for p in pdf.pages[:5]:
                first_pages_text += (p.extract_text() or "") + "\n"

            txt = first_pages_text
            # Generic regex patterns
            # Statement date
            m = re.search(r"statement date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})", txt, re.IGNORECASE)
            if m:
                summary["Statement date"] = parse_date(m.group(1))
            else:
                # e.g. "14 Aug, 2025 To 13 Sep, 2025" -> take end
                m2 = re.search(r"(\d{1,2}\s+[A-Za-z]{3,9}\,?\s*\d{4})\s*(to|-)\s*(\d{1,2}\s+[A-Za-z]{3,9}\,?\s*\d{4})", txt, re.IGNORECASE)
                if m2:
                    summary["Statement date"] = parse_date(m2.group(3))

            # Payment due date
            m = re.search(r"payment due date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})", txt, re.IGNORECASE)
            if m:
                summary["Payment due date"] = parse_date(m.group(1))
            else:
                m2 = re.search(r"due by\s*([A-Za-z]{3,9}\s*\d{1,2}\,?\s*\d{4})", txt, re.IGNORECASE)
                if m2:
                    summary["Payment due date"] = parse_date(m2.group(1))

            # Minimum payable
            m = re.search(r"minimum (?:amount )?due\s*[:\-]?\s*(?:rs\.?|rs|₹)?\s*([\d,]+\.\d{2})", txt, re.IGNORECASE)
            if m:
                summary["Minimum payable"] = fmt_rupee(m.group(1))
            else:
                m = re.search(r"minimum payment\s*[:\-]?\s*(?:rs\.?|rs|₹)?\s*([\d,]+\.\d{2})", txt, re.IGNORECASE)
                if m:
                    summary["Minimum payable"] = fmt_rupee(m.group(1))

            # Total dues
            m = re.search(r"(?:total dues|total due|total amount due|closing balance)\s*[:\-]?\s*(?:rs\.?|rs|₹)?\s*([\d,]+\.\d{2})", txt, re.IGNORECASE)
            if m:
                summary["Total Dues"] = fmt_rupee(m.group(1))
            else:
                # BOB style: date ... 1,409.42 28,188.33 DR => map smaller to min and larger to total
                m2 = re.search(r"(\d{2}/\d{2}/\d{4}).{0,40}?([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+(?:DR|Dr|dr)", txt)
                if m2:
                    d = parse_date(m2.group(1))
                    v1 = parse_number(m2.group(2)); v2 = parse_number(m2.group(3))
                    if v1 is not None and v2 is not None:
                        # assign by magnitude
                        mn = min(v1, v2); mx = max(v1, v2)
                        summary["Payment due date"] = summary["Payment due date"] if summary["Payment due date"]!="N/A" else d
                        summary["Minimum payable"] = fmt_rupee(mn)
                        summary["Total Dues"] = fmt_rupee(mx)

            # HDFC table row heuristics: find line with "Payment Due Date" header and subsequent numbers
            # We'll scan first 20 lines looking for a header line containing both 'Payment' and 'Total' or 'Minimum'
            lines = [l.strip() for l in txt.split("\n") if l.strip()]
            for idx, ln in enumerate(lines[:20]):
                low = ln.lower()
                if "payment due date" in low and ("total" in low or "minimum" in low):
                    # next 1-2 lines often contain the values
                    for j in range(idx+1, min(idx+4, len(lines))):
                        row = lines[j]
                        # extract date + two numbers
                        m3 = re.search(r"(\d{2}/\d{2}/\d{4})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})", row)
                        if m3:
                            d = parse_date(m3.group(1))
                            n1 = parse_number(m3.group(2)); n2 = parse_number(m3.group(3))
                            # decide mapping by magnitude
                            if n1 is not None and n2 is not None:
                                if n1 <= n2:
                                    summary["Payment due date"] = d
                                    summary["Minimum payable"] = fmt_rupee(n1)
                                    summary["Total Dues"] = fmt_rupee(n2)
                                else:
                                    summary["Payment due date"] = d
                                    summary["Minimum payable"] = fmt_rupee(n2)
                                    summary["Total Dues"] = fmt_rupee(n1)
                            break
                    break

    except Exception as e:
        st.warning(f"Rule extraction error: {e}")

    return summary

# ==============================
# OpenAI fallback: send first-page text and ask for 4 fields JSON
# Requires OPENAI_API_KEY in env
# ==============================
def openai_extract_summary(pdf_bytes, timeout_sec=20) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        st.error("OpenAI API key not set (OPENAI_API_KEY). AI fallback disabled.")
        return {"Statement date":"N/A","Payment due date":"N/A","Minimum payable":"N/A","Total Dues":"N/A"}

    # assemble text (first 2-3 pages only)
    pdf_file = BytesIO(pdf_bytes)
    text_all = ""
    with pdfplumber.open(pdf_file) as pdf:
        for p in pdf.pages[:3]:
            text_all += (p.extract_text() or "") + "\n"

    # truncate to token-safe length (approx)
    prompt_text = text_all[:3500]

    system_msg = (
        "You are an assistant that extracts exactly four fields from a credit card statement page: "
        "Statement date, Payment due date, Minimum payable, Total Dues. "
        "Return ONLY a JSON object with these keys exactly: "
        '{"Statement date","Payment due date","Minimum payable","Total Dues"} '
        "Dates should be in dd/mm/YYYY format where possible; amounts should include currency (₹) and two decimals. "
        "If a field cannot be determined, set its value to \"N/A\". "
        "Do not include any extra commentary."
    )

    user_msg = f"Extract values from this card statement text. Return JSON only:\n\n{prompt_text}"

    try:
        # Use chat completion
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",  # may change depending on your account availability
            messages=[
                {"role":"system","content":system_msg},
                {"role":"user","content":user_msg}
            ],
            max_tokens=400,
            temperature=0.0,
            timeout=timeout_sec
        )
        content = resp["choices"][0]["message"]["content"].strip()
        # sanitize content: sometimes the model wraps in ``` or text. Extract JSON
        json_text = content
        # remove markdown fences
        json_text = re.sub(r"^```json\s*","", json_text, flags=re.IGNORECASE)
        json_text = re.sub(r"```$","", json_text, flags=re.IGNORECASE).strip()
        # find first { ... }
        jmatch = re.search(r"(\{.*\})", json_text, flags=re.DOTALL)
        if jmatch:
            json_text = jmatch.group(1)
        data = json.loads(json_text)
        # normalize and ensure keys
        out = {}
        for k in ["Statement date","Payment due date","Minimum payable","Total Dues"]:
            v = data.get(k, "N/A") if isinstance(data, dict) else "N/A"
            # format date/amount
            if "date" in k.lower() and v!="N/A":
                v = parse_date(v)
            if "payable" in k.lower() or "dues" in k.lower():
                if v!="N/A":
                    # try to parse numeric and format
                    n = parse_number(v)
                    v = fmt_rupee(n) if n is not None else v
            out[k] = v
        return out
    except Exception as e:
        st.warning(f"OpenAI extraction failed: {e}")
        return {"Statement date":"N/A","Payment due date":"N/A","Minimum payable":"N/A","Total Dues":"N/A"}

# ==============================
# Main Streamlit UI
# ==============================
st.title("💳 Credit Card Statement — Summary + Transactions (AI fallback)")
st.write("Uploads: PDF/CSV/XLSX. Summary fields: Statement date, Payment due date, Minimum payable, Total Dues.")

use_ai = st.checkbox("Use AI fallback when rules fail", value=USE_AI_FALLBACK_DEFAULT)
if use_ai and not OPENAI_API_KEY:
    st.warning("AI fallback enabled but OPENAI_API_KEY not found in env — please set it to use AI fallback.")

uploaded_files = st.file_uploader("Upload statements (multiple)", type=["pdf","csv","xlsx"], accept_multiple_files=True)

if uploaded_files:
    all_data = pd.DataFrame(columns=["Date","Merchant","Amount","Type","Account"])
    for up in uploaded_files:
        st.markdown(f"### 📄 {up.name}")
        # read bytes once
        try:
            raw = up.read()
        except Exception:
            up.seek(0)
            raw = up.read()
        account_name = st.text_input(f"Account name for {up.name}", value=up.name)

        df = pd.DataFrame(columns=["Date","Merchant","Amount","Type","Account"])
        summary = {"Statement date":"N/A","Payment due date":"N/A","Minimum payable":"N/A","Total Dues":"N/A"}

        if up.name.lower().endswith(".pdf"):
            # transactions
            try:
                df = extract_transactions_from_pdf(raw, account_name)
            except Exception as e:
                st.error(f"Transaction extraction error: {e}")
                df = pd.DataFrame(columns=["Date","Merchant","Amount","Type","Account"])
            # rule extraction
            try:
                summary = extract_summary_rules(raw)
            except Exception as e:
                st.warning(f"Rule summary extraction error: {e}")

            # if any field missing and AI allowed -> call OpenAI
            missing = [k for k,v in summary.items() if v=="N/A"]
            if missing and use_ai:
                st.info("Calling AI fallback to extract missing fields...")
                ai_out = openai_extract_summary(raw)
                # merge preferring rule-based (only replace N/A)
                for k,v in ai_out.items():
                    if summary.get(k,"N/A") == "N/A" and v:
                        summary[k] = v

            display_cols = st.columns(4)
            with display_cols[0]:
                st.caption("📅 Statement date")
                st.header(summary.get("Statement date","N/A"))
            with display_cols[1]:
                st.caption("⏰ Payment due date")
                st.header(summary.get("Payment due date","N/A"))
            with display_cols[2]:
                st.caption("⚠️ Minimum payable")
                st.header(summary.get("Minimum payable","N/A"))
            with display_cols[3]:
                st.caption("💰 Total Dues")
                st.header(summary.get("Total Dues","N/A"))

        elif up.name.lower().endswith(".csv"):
            try:
                df = pd.read_csv(BytesIO(raw))
                df = normalize_dataframe(df, account_name)
            except Exception as e:
                st.error(f"CSV read error: {e}")
        elif up.name.lower().endswith(".xlsx"):
            try:
                df = pd.read_excel(BytesIO(raw))
                df = normalize_dataframe(df, account_name)
            except Exception as e:
                st.error(f"XLSX read error: {e}")

        all_data = pd.concat([all_data, df], ignore_index=True)

    if not all_data.empty:
        all_data = categorize_expenses(all_data)
        all_data["Amount"] = all_data["Amount"].round(2)
        st.subheader("📑 Extracted Transactions")
        st.dataframe(all_data.style.format({"Amount":"{:,.2f}"}))

        # unknown merchant handling
        others_df = all_data[all_data["Category"]=="Others"]
        if not others_df.empty:
            st.subheader("⚡ Assign categories for unknown merchants")
            for merchant in others_df["Merchant"].unique():
                category = st.selectbox(
                    f"Select category for {merchant}:",
                    ["Food","Shopping","Travel","Utilities","Entertainment","Groceries","Jewellery","Healthcare","Fuel","Electronics","Banking","Insurance","Education","Others"],
                    key=merchant
                )
                if category!="Others":
                    add_new_vendor(merchant, category)
                    all_data.loc[all_data["Merchant"]==merchant,"Category"] = category
                    st.success(f"✅ {merchant} categorized as {category}")

        st.subheader("📊 Expense Analysis")
        expenses = all_data[all_data["Amount"]>0]
        st.write("💰 **Total Spent:**", f"{expenses['Amount'].sum():,.2f}")
        st.bar_chart(expenses.groupby("Category")["Amount"].sum())
        st.write("🏦 Top merchants")
        top_merchants = expenses.groupby("Merchant")["Amount"].sum().sort_values(ascending=False).head(10)
        st.dataframe(top_merchants.apply(lambda x: f"{x:,.2f}"))

        csv_data = convert_df_to_csv(all_data)
        excel_data = convert_df_to_excel(all_data)
        st.download_button("⬇️ Download CSV", csv_data, file_name="expenses_all.csv", mime="text/csv")
        st.download_button("⬇️ Download Excel", excel_data, file_name="expenses_all.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
else:
    st.info("Upload one or more credit card statements (PDF/CSV/XLSX).")
