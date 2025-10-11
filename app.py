# app.py - Updated robust credit-card statement parser + Streamlit UI
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
        v = float(str(val).replace(",", "").strip())
        return f"₹{v:,.2f}"
    except:
        return val

def fmt_num_plain(val):
    try:
        v = float(str(val).replace(",", "").strip())
        return round(v, 2)
    except:
        return None

def parse_number(s):
    try:
        return float(str(s).replace(",", "").strip())
    except:
        return None

def parse_date(date_str):
    date_str = str(date_str).strip()
    if not date_str:
        return None
    # common formats
    fmts = ["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y",
            "%d %b, %Y", "%d %B, %Y", "%d %m %Y"]
    for f in fmts:
        try:
            return datetime.strptime(date_str.replace(",", ""), f).strftime("%d/%m/%Y")
        except:
            pass
    # "14 Aug, 2025" or "Aug 14, 2025"
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})[\,\s]*(\d{4})", date_str)
    if m:
        try:
            d = int(m.group(1)); mon = m.group(2); y = int(m.group(3))
            return datetime.strptime(f"{d} {mon} {y}", "%d %b %Y").strftime("%d/%m/%Y")
        except:
            try:
                return datetime.strptime(f"{d} {mon} {y}", "%d %B %Y").strftime("%d/%m/%Y")
            except:
                pass
    # fallback return original
    return date_str

# ------------------------------
# Heuristic scoring functions (used for mapping numeric rows)
# ------------------------------
def score_primary_candidate(mapping, nums, raw=''):
    # mapping: dict with keys 'Credit Limit','Available Credit','Total Due','Minimum Due' -> floats
    cl = mapping.get("Credit Limit")
    av = mapping.get("Available Credit")
    td = mapping.get("Total Due")
    md = mapping.get("Minimum Due")
    if any(x is None for x in [cl, av, td, md]):
        return -1e6
    score = 0.0
    # positive values
    if cl < 0 or av < 0 or td < 0 or md < 0:
        return -1e6
    # credit >= available
    if cl + 1e-6 >= av:
        score += 4.0
    else:
        score -= 6.0
    # minimum <= total
    if md <= td + 1e-6:
        score += 3.0
    else:
        score -= 4.0
    # prefer cl being the maximum of row
    if abs(cl - max(nums)) < 1e-6:
        score += 2.0
    # prefer av <= cl
    if av <= cl:
        score += 1.0
    # prefer td > 0
    if td > 0:
        score += 0.5
    # prefer cl reasonably large
    if cl >= 500:
        score += 0.7
    # penalize insane totals
    if cl > 0 and td > cl * 3:
        score -= 2.0
    # td <= cl
    if td <= cl + 1e-6:
        score += 1.0
    else:
        score -= 1.5
    # md/td small
    if td > 0 and md / td < 0.2:
        score += 1.2
    else:
        score -= 1.0
    if 'cash' in raw.lower():
        score -= 2.0
    if cl > 0:
        score += math.log(cl + 1) / 20
    return score

def score_secondary_candidate(mapping):
    tp = mapping.get("Total Payments")
    oc = mapping.get("Other Charges")
    purch = mapping.get("Total Purchases")
    prev = mapping.get("Previous Balance")
    if any(x is None for x in [tp, oc, purch, prev]):
        return -1e6
    if any(x < -0.01 for x in [tp, oc, purch, prev]):
        return -1e6
    score = 0.0
    if purch + 1e-6 >= tp:
        score += 1.2
    if purch > 0:
        score += 0.8
    if prev >= 0:
        score += 0.3
    return score

def choose_best_primary_mapping(numeric_rows):
    fields_primary = ["Credit Limit", "Available Credit", "Total Due", "Minimum Due"]
    best_score = -1e9
    best_map = None
    best_idx = None
    best_perm = None
    for idx, (nums, pidx, tidx, ridx, raw) in enumerate(numeric_rows):
        # try permutations mapping 4 numbers -> these fields
        for perm in itertools.permutations(range(len(nums)), 4):
            # take first 4 positions of perm mapping if nums has >=4; if >4 we attempted combinations clipped to 4
            try:
                candidate = {}
                for i, field in enumerate(fields_primary):
                    candidate[field] = float(nums[perm[i]])
            except:
                continue
            s = score_primary_candidate(candidate, [float(n) for n in nums], raw)
            if s > best_score:
                best_score = s
                best_map = candidate
                best_idx = idx
                best_perm = perm
    if best_map is None:
        return None, None, None
    formatted = {k: fmt_num(v) for k, v in best_map.items()}
    return formatted, best_idx, best_perm

def map_secondary_rows(numeric_rows, exclude_index=None):
    fields_secondary = ["Total Payments", "Other Charges", "Total Purchases", "Previous Balance"]
    mapped = {}
    for idx, (nums, pidx, tidx, ridx, raw) in enumerate(numeric_rows):
        if idx == exclude_index:
            continue
        best_score = -1e9
        best_map = None
        for perm in itertools.permutations(range(len(nums)), 4):
            try:
                candidate = {fields_secondary[i]: float(nums[perm[i]]) for i in range(4)}
            except:
                continue
            s = score_secondary_candidate(candidate)
            if s > best_score:
                best_score = s
                best_map = candidate
        if best_map:
            for k, v in best_map.items():
                if k not in mapped:
                    mapped[k] = fmt_num(v)
    return mapped

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
    text_all = ""
    is_amex = False
    with pdfplumber.open(pdf_file) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text()
            if text:
                text_all += text + "\n"
                if "American Express" in text or "AmericanExpress" in text or "AEBC" in text:
                    is_amex = True
            else:
                # try OCR? (not implemented here)
                continue

            lines = [l.strip() for l in text.split("\n") if l.strip()]

            if not is_amex:
                # Generic parsing: look for lines starting with DD/MM/YYYY
                for line in lines:
                    match = re.match(
                        r"^(\d{2}/\d{2}/\d{4})\s+(.+?)\s+([\d,]+\.\d{2})\s*(CR|Dr|DR|Cr)?\s*$",
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
                        continue

                    # BOBCARD / HDFC style lines: date + code + merchant + amount + amount type
                    match2 = re.match(r"^(\d{2}/\d{2}/\d{4})\s+(.+?)\s+INR\s*([\d,]+\.\d{2})\s*([\d,]+\.\d{2})\s*(CR|DR|CR|Dr|Dr)?", line)
                    if match2:
                        date, merchant, amt1, amt2, drcr = match2.groups()
                        # sometimes two amounts and CR/DR labelled
                        try:
                            amt = round(float(amt2.replace(",", "")), 2)
                        except:
                            amt = None
                        if amt is not None:
                            if drcr and drcr.strip().lower().startswith("cr"):
                                amt = -amt
                                tr_type = "CR"
                            else:
                                tr_type = "DR"
                            transactions.append([parse_date(date), merchant.strip(), amt, tr_type, account_name])
            else:
                # AMEX parsing — many AMEX statements show "Month DD  merchant  amt  CR?"
                i = 0
                while i < len(lines):
                    line = lines[i]
                    # Example: "July 01 PAYMENT RECEIVED. THANK YOU 8,860.00"
                    m = re.search(r"([A-Za-z]{3,9}\s+\d{1,2}).*?([\d,]+\.\d{2})(\s*CR|\s*Cr|\s*DR|\s*Dr)?$", line)
                    if m:
                        date_str = m.group(1)
                        amount_s = m.group(2)
                        cr_suf = m.group(3)
                        try:
                            amt = round(float(amount_s.replace(",", "")), 2)
                        except:
                            i += 1
                            continue
                        drcr = "DR"
                        if cr_suf and cr_suf.strip().lower().startswith("cr"):
                            amt = -amt
                            drcr = "CR"
                        # Merchant might be earlier in the same line — take middle text
                        pieces = line.split()
                        merchant = " ".join(pieces[2:-1]) if len(pieces) > 4 else line
                        # normalize date: AMEX often has "July 01" -> add year by finding statement year later; fallback to original
                        transactions.append([date_str, merchant.strip(), amt, drcr, account_name])
                    i += 1

    # final normalization of dates (AMEX lines may be "July 01" without year; leave as-is)
    df = pd.DataFrame(transactions, columns=["Date", "Merchant", "Amount", "Type", "Account"])
    # attempt to parse dd/mm/yyyy present dates
    def norm_date(d):
        try:
            if isinstance(d, str) and re.match(r"\d{2}/\d{2}/\d{4}", d):
                return parse_date(d)
            return d
        except:
            return d
    if not df.empty:
        df["Date"] = df["Date"].apply(norm_date)
        df["Amount"] = df["Amount"].astype(float).round(2)
    return df

# ------------------------------
# Extract summary from PDF (robust)
# ------------------------------
def extract_summary_from_pdf(pdf_file):
    """
    Robust summary extraction:
    - Collects text of first 3 pages (most statements have summary on first page)
    - Collects table rows with numeric tokens
    - Applies pattern matching and permutation scoring to pick the best mapping
    - Returns derived summary with fields:
      Statement date, Payment due date, Total Dues, Minimum payable, Total Limit, Available Credit, Used / Closing
    """
    summary = {}
    text_all = ""
    numeric_rows_collected = []  # (nums_list (strings), page_idx, table_idx, row_idx, raw_text)

    # patterns to detect common fields
    simple_num_pattern = r"[\d,]+\.\d{2}"
    stmt_date_patterns = [
        r"Statement Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
        r"Statement Date\s*[:\-]?\s*([A-Za-z]{3,9}\s+\d{1,2},?\s*\d{4})",
        r"Statement Period\s*From\s*.*?To\s*([A-Za-z]{3,9}\s+\d{1,2},?\s*\d{4})",
        r"Statement Period\s*From\s*([A-Za-z]{3,9}\s+\d{1,2})\s*to\s*([A-Za-z]{3,9}\s+\d{1,2},?\s*\d{4})",
    ]
    due_date_patterns = [
        r"Payment Due Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
        r"Due by\s*([A-Za-z]{3,9}\s+\d{1,2},?\s*\d{4})",
        r"Due Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
        r"Due Date\s*[:\-]?\s*([A-Za-z]{3,9}\s+\d{1,2},?\s*\d{4})"
    ]

    try:
        with pdfplumber.open(pdf_file) as pdf:
            pages_to_scan = min(4, len(pdf.pages))
            for pidx in range(pages_to_scan):
                page = pdf.pages[pidx]
                page_text = page.extract_text() or ""
                text_all += page_text + "\n"

                # extract any tables and collect numeric rows
                tables = page.extract_tables() or []
                for t_idx, table in enumerate(tables):
                    if not table:
                        continue
                    # normalize rows to strings
                    rows = [[("" if c is None else str(c)).strip() for c in r] for r in table]
                    for ridx, row in enumerate(rows):
                        raw = " ".join(row)
                        # find numeric tokens in the row
                        nums = re.findall(simple_num_pattern, raw)
                        # also capture numbers without decimals (rare) - fallback
                        if not nums:
                            nums = re.findall(r"[\d,]{1,}\b", raw)
                        # clean numbers to consistent format
                        clean_nums = []
                        for n in nums:
                            # filter tokens that are long (page numbers etc)
                            if re.match(r"^\d{4,}$", n):  # skip long integers
                                continue
                            clean_nums.append(n.replace(",", ""))
                        if len(clean_nums) >= 1:
                            numeric_rows_collected.append((clean_nums, pidx, t_idx, ridx, raw))

                # additionally parse plain text lines for helpful rows
                for line in page_text.split("\n"):
                    l = line.strip()
                    if not l:
                        continue
                    # lines with 2-5 numeric tokens (common summary lines)
                    nums_line = re.findall(simple_num_pattern, l)
                    if len(nums_line) >= 1:
                        tokens = [n.replace(",", "") for n in nums_line]
                        numeric_rows_collected.append((tokens, pidx, -1, -1, l))

        # normalize text_all
        text_all_norm = re.sub(r"\s+", " ", text_all).strip()

        # 1) Try direct regexes in text_all for exact fields
        direct_patterns = {
            "Credit Limit": r"(?:Credit Limit|Sanctioned Credit Limit)\s*(?:Rs\.?\s*)?[:\-]?\s*([\d,]+\.?\d*)",
            "Available Credit": r"(?:Available Credit Limit|Available Credit)\s*(?:Rs\.?\s*)?[:\-]?\s*([\d,]+\.?\d*)",
            "Total Due": r"(?:Total Dues|Total Due|Total Amount Due|Closing Balance)\s*(?:Rs\.?\s*)?[:\-]?\s*([\d,]+\.?\d*)",
            "Minimum Due": r"(?:Minimum Amount Due|Minimum Due|Minimum Payment)\s*(?:Rs\.?\s*)?[:\-]?\s*([\d,]+\.?\d*)",
            "Total Purchases": r"(?:Total Purchases|New Debits Rs|Purchase/ Debits|Purchases/Debits)\s*(?:Rs\.?\s*)?[:\-]?\s*([\d,]+\.?\d*)",
            "Total Payments": r"(?:Total Payments|Payment/ Credits|Payments/ Credits|Payment/Credits)\s*(?:Rs\.?\s*)?[:\-]?\s*([\d,]+\.?\d*)",
            "Previous Balance": r"(?:Previous Balance|Opening Balance)\s*(?:Rs\.?\s*)?[:\-]?\s*([\d,]+\.?\d*)",
            "Finance Charges": r"(?:Finance Charges)\s*(?:Rs\.?\s*)?[:\-]?\s*([\d,]+\.?\d*)"
        }
        for key, pat in direct_patterns.items():
            m = re.search(pat, text_all_norm, re.IGNORECASE)
            if m:
                val = m.group(1)
                parsed = parse_number(val)
                if parsed is not None:
                    summary[key] = fmt_num(parsed)

        # 2) Attempt to find "Credit Summary" table style where multiple numbers cluster together
        # pick numeric rows with 3-6 numbers as candidates for mapping
        candidate_rows = []
        for idx, (nums, p, t, r, raw) in enumerate(numeric_rows_collected):
            # only keep numeric tokens that look like currency (with decimal) or reasonable integers
            filtered = [n for n in nums if re.match(r"^\d+(\.\d{2})?$", str(n)) or re.match(r"^\d{1,3}(,\d{3})+(\.\d{2})?$", str(n))]
            if len(filtered) >= 2:
                # normalize numeric strings (remove commas)
                clean = [n.replace(",", "") for n in filtered]
                # keep rows with 2-6 numbers
                if 1 <= len(clean) <= 6:
                    candidate_rows.append((clean, p, t, r, raw))

        # if we have rows of 4 numbers (BOB style), try the permutation-based mapping
        numeric_rows = []
        # convert numeric strings to floats for permutation mapping
        for (nums_str, p, t, r, raw) in candidate_rows:
            nums_f = []
            ok = True
            for s in nums_str:
                try:
                    nums_f.append(float(s))
                except:
                    ok = False
                    break
            if ok and len(nums_f) >= 2:
                numeric_rows.append((nums_f, p, t, r, raw))

        if numeric_rows:
            # try primary mapping using our heuristic mapping function
            primary_map, primary_idx, prim_perm = choose_best_primary_mapping(numeric_rows)
            if primary_map:
                for k, v in primary_map.items():
                    if k not in summary:
                        summary[k] = v
                # map secondary
                secondary = map_secondary_rows(numeric_rows, exclude_index=primary_idx)
                for k, v in secondary.items():
                    if k not in summary:
                        summary[k] = v

        # 3) AMEX style "Opening Balance ... = ClosingBalance" detection
        # Try to find an expression like "Opening Balance Rs New Credits Rs New Debits Rs Closing Balance Rs ... = 34,899.91"
        m_eq = re.search(r"=\s*([\d,]+\.\d{2})", text_all_norm)
        if m_eq and "Total Due" not in summary:
            val = parse_number(m_eq.group(1))
            if val is not None:
                summary["Total Due"] = fmt_num(val)

        # 4) Extract statement date (fallback)
        if "Statement Date" not in summary:
            for pat in stmt_date_patterns:
                m = re.search(pat, text_all, re.IGNORECASE)
                if m:
                    datecand = m.group(1)
                    sd = parse_date(datecand)
                    if sd:
                        summary["Statement Date"] = sd
                        break
        # 5) Payment Due Date fallback
        if "Payment Due Date" not in summary:
            for pat in due_date_patterns:
                m = re.search(pat, text_all, re.IGNORECASE)
                if m:
                    datecand = m.group(1)
                    dd = parse_date(datecand)
                    if dd:
                        summary["Payment Due Date"] = dd
                        break

        # 6) If Credit Limit or Available Credit still missing, look for "Credit Summary" local clusters
        if "Credit Limit" not in summary or "Available Credit" not in summary:
            m = re.search(r"Credit Summary\s*(.*?)\n", text_all, re.IGNORECASE)
            if m:
                block = m.group(1)
                nums = re.findall(r"([\d,]+\.\d{2})", block)
                if nums and "Credit Limit" not in summary:
                    summary["Credit Limit"] = fmt_num_plain(nums[0].replace(",", "")) if nums else summary.get("Credit Limit")

        # 7) final derived summary fields for display
        derived = {}
        derived["Statement date"] = summary.get("Statement Date", "N/A")
        derived["Payment due date"] = summary.get("Payment Due Date", "N/A")
        derived["Total Dues"] = summary.get("Total Due", "N/A")
        derived["Minimum payable"] = summary.get("Minimum Due", "N/A")
        derived["Total Limit"] = summary.get("Credit Limit", "N/A")
        derived["Available Credit Limit"] = summary.get("Available Credit", "N/A")
        derived["Used / Closing"] = summary.get("Total Due", "N/A")  # closing often same as total due

        # If still missing some fields, try secondary small heuristics:
        # - If we found Credit Limit and Available Credit as plain numbers without formatting, format them
        for k in ["Total Limit", "Available Credit Limit", "Total Dues", "Minimum payable", "Used / Closing"]:
            v = derived.get(k)
            if isinstance(v, float) or (isinstance(v, str) and re.match(r"^\d+(\.\d+)?$", v.replace(",", ""))):
                # format
                try:
                    derived[k] = fmt_num(float(str(v).replace(",", "")))
                except:
                    pass

        # If everything looks N/A -> give back a helpful debug entry
        if all(v == "N/A" for v in derived.values()):
            return {"Info": "No summary details detected in PDF."}

        return derived

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
            <div style="font-size:14px;">{icon} {label}</div>
            <div style="font-size:16px;margin-top:6px;">{value}</div>
        </div>
        """

    def try_float_str(s):
        if s in (None, "N/A"):
            return 0.0
        try:
            return float(str(s).replace("₹", "").replace(",", "").strip())
        except:
            return 0.0

    total_due_val = try_float_str(summary.get("Total Dues", "0"))
    min_val = try_float_str(summary.get("Minimum payable", "0"))
    avail_val = try_float_str(summary.get("Available Credit Limit", "0"))

    total_due_color = "#d9534f" if total_due_val > 0 else "#5cb85c"
    min_color = "#f0ad4e" if min_val > 0 else "#5cb85c"
    avail_color = "#5cb85c" if avail_val > 0 else "#d9534f"

    c1, c2, c3 = st.columns(3)
    c4, c5, c6 = st.columns(3)

    with c1:
        st.markdown(colored_card("📅 Statement date", summary.get("Statement date", "N/A"), "#0275d8"), unsafe_allow_html=True)
    with c2:
        st.markdown(colored_card("⏰ Payment due date", summary.get("Payment due date", "N/A"), "#f0ad4e"), unsafe_allow_html=True)
    with c3:
        st.markdown(colored_card("💰 Total Limit", summary.get("Total Limit", "N/A"), "#5bc0de"), unsafe_allow_html=True)
    with c4:
        st.markdown(colored_card("🏦 Used / Closing", summary.get("Used / Closing", "N/A"), total_due_color), unsafe_allow_html=True)
    with c5:
        st.markdown(colored_card("✅ Available Credit", summary.get("Available Credit Limit", "N/A"), avail_color), unsafe_allow_html=True)
    with c6:
        st.markdown(colored_card("⚠️ Minimum payable", summary.get("Minimum payable", "N/A"), min_color), unsafe_allow_html=True)

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
    if df.empty:
        return df
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
st.set_page_config(layout="wide")
st.title("💳 Credit Card Expense Analyzer")
st.write("Upload your unlocked credit-card statements (PDF/CSV/XLSX). This app extracts transactions, categorizes merchants and shows a summary card (Statement date, Payment due, Total limit, Closing/Used, Available credit, Min due).")

uploaded_files = st.file_uploader(
    "Upload Statements (unlocked PDFs recommended)",
    type=["pdf", "csv", "xlsx"],
    accept_multiple_files=True
)

if uploaded_files:
    all_data = pd.DataFrame(columns=["Date", "Merchant", "Amount", "Type", "Account"])

    for uploaded_file in uploaded_files:
        account_name = st.text_input(f"Enter account name for {uploaded_file.name}", value=uploaded_file.name)

        if account_name:
            if uploaded_file.name.lower().endswith(".pdf"):
                with st.spinner(f"Parsing PDF: {uploaded_file.name}"):
                    df = extract_transactions_from_pdf(uploaded_file, account_name)
                    summary = extract_summary_from_pdf(uploaded_file)
                    display_summary(summary, account_name)
            elif uploaded_file.name.lower().endswith(".csv"):
                df = extract_transactions_from_csv(uploaded_file, account_name)
            elif uploaded_file.name.lower().endswith(".xlsx"):
                df = extract_transactions_from_excel(uploaded_file, account_name)
            else:
                df = pd.DataFrame()

            if df is None:
                df = pd.DataFrame(columns=["Date", "Merchant", "Amount", "Type", "Account"])
            all_data = pd.concat([all_data, df], ignore_index=True)

    if not all_data.empty:
        all_data = categorize_expenses(all_data)
        all_data["Amount"] = all_data["Amount"].round(2)

        st.subheader("📑 Extracted Transactions")
        st.dataframe(all_data.style.format({"Amount": "{:,.2f}"}), use_container_width=True)

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
        st.write("💰 **Total Spent:**", f"₹{total_spent:,.2f}")
        st.bar_chart(expenses.groupby("Category")["Amount"].sum())
        st.write("🏦 **Top 10 Merchants**")
        top_merchants = expenses.groupby("Merchant")["Amount"].sum().sort_values(ascending=False).head(10)
        st.dataframe(top_merchants.apply(lambda x: f"₹{x:,.2f}"))

        st.write("🏦 **Expense by Account**")
        st.bar_chart(expenses.groupby("Account")["Amount"].sum())

        # Export
        csv_data = convert_df_to_csv(all_data)
        excel_data = convert_df_to_excel(all_data)

        st.download_button("⬇️ Download as CSV", csv_data, file_name="expenses_all.csv", mime="text/csv")
        st.download_button("⬇️ Download as Excel", excel_data, file_name="expenses_all.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
else:
    st.info("✅ App loaded successfully — upload one or more credit-card statements (PDF, CSV, XLSX).")

