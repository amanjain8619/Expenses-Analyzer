# app.py
import streamlit as st
import pandas as pd
import pdfplumber
import re
from rapidfuzz import process
from io import BytesIO
import os
from datetime import datetime
from pathlib import Path

# ============== Configuration ==============
VENDOR_FILE = "vendors.csv"
if os.path.exists(VENDOR_FILE):
    vendor_map = pd.read_csv(VENDOR_FILE)
else:
    vendor_map = pd.DataFrame(columns=["merchant", "category"])
    vendor_map.to_csv(VENDOR_FILE, index=False)

# ============== Helpers ==============
def parse_number(s):
    if s is None:
        return None
    s = str(s)
    s = re.sub(r"[^\d\.,\-]", "", s)
    s = s.replace(",", "")
    s = s.strip()
    if s == "" or s in ("-", "--"):
        return None
    try:
        return float(s)
    except:
        return None

def fmt_cur(v):
    if v is None:
        return "N/A"
    try:
        return "₹{:,.2f}".format(float(v))
    except:
        return str(v)

def parse_date_generic(s):
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    # common formats
    fmts = ("%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y")
    for f in fmts:
        try:
            return datetime.strptime(s, f).strftime("%d/%m/%Y")
        except:
            pass
    # try find dd/mm/yyyy inside
    m = re.search(r"(\d{2}\/\d{2}\/\d{4})", s)
    if m:
        return m.group(1)
    # try '13 Sep 2025' style
    m2 = re.search(r"(\d{1,2}\s+[A-Za-z]{3,9}\s*,?\s*\d{4})", s)
    if m2:
        return m2.group(1)
    return s

def numbers_in_text(s):
    return [parse_number(x) for x in re.findall(r"[\d,]+(?:\.\d{1,2})?", s)]

# ============== Category fuzzy match ==============
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

# ============== Summary extractor ==============
def extract_summary_from_pdf(pdf_file, max_pages=5):
    """
    Robust, line-oriented summary extraction.
    Returns: dict of derived summary + debug payload
    """
    summary = {}
    debug = {"raw_first_lines": [], "tables": [], "notes": []}

    with pdfplumber.open(pdf_file) as pdf:
        pages_to_scan = min(max_pages, len(pdf.pages))
        text_acc = ""
        for i in range(pages_to_scan):
            page = pdf.pages[i]
            text = page.extract_text() or ""
            # save first 120 lines for debug if requested
            if i == 0:
                debug["raw_first_lines"] = (text[:8000] if len(text) > 8000 else text)
            text_acc += text + "\n"

            # attempt to extract tables on each page for fallback
            try:
                tables = page.extract_tables()
                if tables:
                    # store first non-empty table rows (trim)
                    for t in tables:
                        if not t:
                            continue
                        debug["tables"].append(t[:6])
            except Exception as e:
                debug["notes"].append(f"table-extract-error-page-{i}: {e}")

    # Normalize whitespace
    txt = re.sub(r"\s+", " ", text_acc).strip()
    lines = [l.strip() for l in text_acc.splitlines() if l.strip()]

    # --- HDFC / many Indian bank style: header row followed by a numeric row ---
    # Example: "Payment Due Date Total Dues Minimum Amount Due" then "04/09/2025 53,451.00 2,680.00"
    for idx, line in enumerate(lines):
        low = line.lower()
        if "payment due date" in low and ("total" in low or "minimum" in low):
            candidate = " ".join(lines[idx: idx + 3])
            # find date dd/mm/yyyy
            mdate = re.search(r"(\d{2}\/\d{2}\/\d{4})", candidate)
            if mdate:
                summary["Payment Due Date"] = parse_date_generic(mdate.group(1))
            # find numeric tokens (skip date tokens)
            nums = re.findall(r"[\d,]+(?:\.\d{1,2})?", candidate)
            nums = [n for n in nums if not re.match(r"\d{2}\/\d{2}\/\d{4}", n)]
            parsed = [parse_number(n) for n in nums]
            parsed = [p for p in parsed if p is not None]
            if len(parsed) >= 2:
                # heuristics: first = Total Dues, second = Minimum Amount Due
                summary["Total Due"] = parsed[0]
                summary["Min Due"] = parsed[1]
                debug["notes"].append(("hdfc_payment_line", candidate, parsed[:3]))

    # --- HDFC credit limit header followed by numeric row ---
    for idx, line in enumerate(lines):
        low = line.lower()
        if "credit limit" in low and "available" in low:
            candidate = " ".join(lines[idx: idx + 3])
            nums = re.findall(r"[\d,]+(?:\.\d{1,2})?", candidate)
            parsed = [parse_number(n) for n in nums if parse_number(n) is not None]
            if len(parsed) >= 2:
                # mapping: credit limit, available credit, (maybe available cash)
                summary["Credit Limit"] = parsed[0]
                summary["Available Credit"] = parsed[1]
                if len(parsed) >= 3:
                    summary["Available Cash"] = parsed[2]
                debug["notes"].append(("hdfc_credit_row", candidate, parsed[:3]))
                break

    # --- Masked-card style cluster extraction (BOB-style) ---
    mask_match = re.search(r"(?:\*{4,}\d{2,4}|x{4,}\d{2,4}|xxxx\*\d{2,4})", txt, re.IGNORECASE)
    if mask_match:
        s = max(0, mask_match.start() - 350)
        e = min(len(txt), mask_match.end() + 900)
        window = txt[s:e]
        seq = re.search(r"((?:[\d,]+\.\d{2}\s+){2,3}[\d,]+\.\d{2})", window)
        if seq:
            nums = re.findall(r"[\d,]+\.\d{2}", seq.group(1))
            vals = [parse_number(n) for n in nums]
            # rotate if largest value not first
            if vals:
                max_idx = max(range(len(vals)), key=lambda i: vals[i])
                if max_idx != 0:
                    vals = vals[max_idx:] + vals[:max_idx]
            if len(vals) >= 1 and "Credit Limit" not in summary:
                summary["Credit Limit"] = vals[0]
            if len(vals) >= 2 and "Available Credit" not in summary:
                summary["Available Credit"] = vals[1]
            if len(vals) >= 3 and "Total Due" not in summary:
                summary["Total Due"] = vals[2]
            if len(vals) >= 4 and "Min Due" not in summary:
                summary["Min Due"] = vals[3]
            debug["notes"].append(("masked_cluster", nums, vals))

    # --- Generic pattern lookup (AMEX / generic) ---
    generic_patterns = {
        "Credit Limit": [
            r"Credit Limit\s*[:\-]?\s*(?:Rs\.?|₹)?\s*([\d,]+(?:\.\d{1,2})?)",
            r"Sanctioned Credit Limit\s*[:\-]?\s*(?:Rs\.?|₹)?\s*([\d,]+(?:\.\d{1,2})?)"
        ],
        "Available Credit": [
            r"Available Credit Limit\s*[:\-]?\s*(?:Rs\.?|₹)?\s*([\d,]+(?:\.\d{1,2})?)",
            r"Available Credit\s*[:\-]?\s*(?:Rs\.?|₹)?\s*([\d,]+(?:\.\d{1,2})?)"
        ],
        "Total Due": [
            r"Total Dues\s*[:\-]?\s*(?:Rs\.?|₹)?\s*([\d,]+(?:\.\d{1,2})?)",
            r"Total Amount Due\s*[:\-]?\s*(?:Rs\.?|₹)?\s*([\d,]+(?:\.\d{1,2})?)",
            r"Closing Balance\s*[:\-]?\s*(?:Rs\.?|₹)?\s*([\d,]+(?:\.\d{1,2})?)"
        ],
        "Min Due": [
            r"Minimum Amount Due\s*[:\-]?\s*(?:Rs\.?|₹)?\s*([\d,]+(?:\.\d{1,2})?)",
            r"Minimum Payment Due\s*[:\-]?\s*(?:Rs\.?|₹)?\s*([\d,]+(?:\.\d{1,2})?)",
            r"Minimum Due\s*[:\-]?\s*(?:Rs\.?|₹)?\s*([\d,]+(?:\.\d{1,2})?)"
        ]
    }
    for key, pats in generic_patterns.items():
        if key in summary:
            continue
        for p in pats:
            m = re.search(p, txt, re.IGNORECASE)
            if m:
                val = parse_number(m.group(1))
                if val is not None:
                    summary[key] = val
                    debug["notes"].append((f"generic_{key}", m.group(0)[:120]))
                    break

    # --- Statement Date extraction (common forms) ---
    if "Statement Date" not in summary:
        m = re.search(r"Statement Date\s*[:\-]?\s*([0-9]{2}\/[0-9]{2}\/[0-9]{4})", txt, re.I)
        if m:
            summary["Statement Date"] = parse_date_generic(m.group(1))
        else:
            # try period "14 Aug, 2025 To 13 Sep, 2025" -> take the second date as statement date
            m2 = re.search(r"(\d{1,2}\s+[A-Za-z]{3,9}\s*,?\s*\d{4})\s*To\s*(\d{1,2}\s+[A-Za-z]{3,9}\s*,?\s*\d{4})", txt, re.I)
            if m2:
                # use the second date as statement date
                summary["Statement Date"] = parse_date_generic(m2.group(2))
                debug["notes"].append(("period_to_dates", m2.group(0)))
            else:
                # fallback: any "Statement Period" / "Statement" lines
                m3 = re.search(r"Statement Period.*?to\s*([0-9]{1,2}\s+[A-Za-z]{3,9}\s*,?\s*[0-9]{4})", txt, re.I)
                if m3:
                    summary["Statement Date"] = parse_date_generic(m3.group(1))

    # --- Payment due fallback (if not found earlier) ---
    if "Payment Due Date" not in summary:
        m = re.search(r"Payment Due Date\s*[:\-]?\s*([0-9]{2}\/[0-9]{2}\/[0-9]{4})", txt, re.I)
        if m:
            summary["Payment Due Date"] = parse_date_generic(m.group(1))

    # --- Derived fields ---
    # If we have credit limit and available credit, compute used if closing not available
    if "Credit Limit" in summary and "Available Credit" in summary and "Used / Closing" not in summary:
        try:
            summary["Used / Closing"] = round(summary["Credit Limit"] - summary["Available Credit"], 2)
            debug["notes"].append(("derived_used", summary["Credit Limit"], summary["Available Credit"], summary["Used / Closing"]))
        except Exception:
            pass

    # Format a derived display dict
    derived = {
        "Statement date": summary.get("Statement Date", "N/A"),
        "Payment Due Date": summary.get("Payment Due Date", "N/A"),
        "Total Limit": fmt_cur(summary.get("Credit Limit")),
        "Used / Closing": fmt_cur(summary.get("Used / Closing") or summary.get("Total Due")),
        "Available Credit": fmt_cur(summary.get("Available Credit")),
        "Min Due": fmt_cur(summary.get("Min Due"))
    }

    return derived, summary, debug

# ============== Transaction extraction (existing logic, improved decimals) ==============
def extract_transactions_from_pdf(pdf_file, account_name):
    transactions = []
    with pdfplumber.open(pdf_file) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text()
            if not text:
                continue
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            for line in lines:
                # try multiple date formats present in lines
                m = re.match(r"(\d{2}/\d{2}/\d{4})(?:\s+\d{2}:\d{2}:\d{2})?\s+(.+?)\s+([\d,]+(?:\.\d{1,2})?)\s*(CR|Dr|DR|Cr)?", line)
                if m:
                    date, merchant, amount, drcr = m.groups()
                    try:
                        amt = round(float(amount.replace(",", "")), 2)
                    except:
                        continue
                    if drcr and drcr.strip().lower().startswith("cr"):
                        amt = -amt
                        tr_type = "CR"
                    else:
                        tr_type = "DR"
                    transactions.append([parse_date_generic(date), merchant.strip(), amt, tr_type, account_name])

            # give progress
            st.info(f"📄 Page {page_num}: extracted {len(transactions)} rows so far")

    df = pd.DataFrame(transactions, columns=["Date", "Merchant", "Amount", "Type", "Account"])
    if not df.empty:
        df["Amount"] = df["Amount"].round(2)
    return df

# ============== CSV / Excel handlers ==============
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

# ============== UI ==============
st.title("💳 Multi-Account Expense Analyzer (Improved Summary + Debug)")

st.write("Upload credit-card or bank statements (PDF/CSV/XLSX). The app tries multiple heuristics to extract the statement summary (limit, available, used/closing, min due). If a field is `N/A`, open the Debug panel to inspect the raw text/table snippet — this helps tune the extractor for new statement layouts.")

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
            if uploaded_file.name.lower().endswith(".pdf"):
                # Save to a temporary file path to re-open with pdfplumber (Streamlit gives UploadedFile-like)
                tmp_path = f"/tmp/{uploaded_file.name}"
                with open(tmp_path, "wb") as f:
                    f.write(uploaded_file.getvalue())

                # extract transactions and summary
                df = extract_transactions_from_pdf(tmp_path, account_name)
                derived, raw_summary, debug = extract_summary_from_pdf(tmp_path)

                # display summary
                st.subheader(f"📋 Statement Summary — {account_name}")
                cols = st.columns(3)
                cols[0].write("**Statement date**")
                cols[0].write(derived["Statement date"])
                cols[1].write("**Payment Due Date**")
                cols[1].write(derived["Payment Due Date"])
                cols[2].write("**Total Limit**")
                cols[2].write(derived["Total Limit"])

                cols2 = st.columns(3)
                cols2[0].write("**Used / Closing**")
                cols2[0].write(derived["Used / Closing"])
                cols2[1].write("**Available Credit**")
                cols2[1].write(derived["Available Credit"])
                cols2[2].write("**Min Due**")
                cols2[2].write(derived["Min Due"])

                # if any important fields are N/A, show debug panel
                if any(v in ("N/A", None) for v in derived.values()):
                    with st.expander("⚠️ Debug: raw text & table preview (open if some fields are N/A)"):
                        st.write("**Raw extracted text (first ~8KB)**")
                        st.code(debug.get("raw_first_lines", "No text extracted")[:8000])
                        st.write("**Extracted tables (first few rows each)**")
                        tables = debug.get("tables", [])
                        if tables:
                            for ti, t in enumerate(tables[:5]):
                                st.write(f"Table {ti+1} (first rows):")
                                try:
                                    df_table = pd.DataFrame(t)
                                    st.dataframe(df_table.head(10))
                                except:
                                    st.write(t)
                        st.write("**Notes (debug trace)**")
                        for n in debug.get("notes", [])[:30]:
                            st.write(n)

                # append transactions
                all_data = pd.concat([all_data, df], ignore_index=True)

                # remove temp file
                try:
                    os.remove(tmp_path)
                except:
                    pass

            elif uploaded_file.name.lower().endswith(".csv"):
                df = extract_transactions_from_csv(uploaded_file, account_name)
                all_data = pd.concat([all_data, df], ignore_index=True)
            elif uploaded_file.name.lower().endswith(".xlsx"):
                df = extract_transactions_from_excel(uploaded_file, account_name)
                all_data = pd.concat([all_data, df], ignore_index=True)
            else:
                st.warning("Unsupported file type")

    if not all_data.empty:
        all_data = all_data.drop_duplicates(subset=["Date", "Merchant", "Amount", "Account"])
        # Categorize
        all_data["Category"] = all_data["Merchant"].apply(get_category)
        all_data["Amount"] = all_data["Amount"].round(2)

        st.subheader("📑 Extracted Transactions (combined)")
        st.dataframe(all_data.style.format({"Amount": "{:,.2f}"}))

        st.subheader("📊 Expense Analysis")
        expenses = all_data[all_data["Amount"] > 0]
        total_spent = expenses["Amount"].sum()
        st.write("💰 **Total Spent:**", f"{total_spent:,.2f}")
        st.bar_chart(expenses.groupby("Category")["Amount"].sum())

        st.write("🏦 **Top 10 Merchants**")
        top_merchants = expenses.groupby("Merchant")["Amount"].sum().sort_values(ascending=False).head(10)
        st.dataframe(top_merchants.apply(lambda x: f"{x:,.2f}"))

        # Export
        csv_data = all_data.to_csv(index=False).encode("utf-8")
        output = BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            all_data.to_excel(writer, index=False, sheet_name="Expenses", float_format="%.2f")
        excel_data = output.getvalue()

        st.download_button("⬇️ Download as CSV", csv_data, file_name="expenses_all.csv", mime="text/csv")
        st.download_button("⬇️ Download as Excel", excel_data, file_name="expenses_all.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
