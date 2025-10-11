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

# Scoring for candidate primary mapping (Credit Limit, Available Credit, Total Due, Minimum Due)
def score_primary_candidate(mapping, nums, raw=''):
    # mapping: dict with keys 'Credit Limit','Available Credit','Total Due','Minimum Due' -> floats
    # nums: original list of floats for this row
    # Returns numeric score (higher is better)
    cl = mapping.get("Credit Limit")
    av = mapping.get("Available Credit")
    td = mapping.get("Total Due")
    md = mapping.get("Minimum Due")
    if any(x is None for x in [cl, av, td, md]):
        return -1e6
    score = 0.0
    # basic sanity
    if cl < 0 or av < 0 or td < 0 or md < 0:
        return -1e6
    # credit >= available
    if cl + 1e-6 >= av:
        score += 3.0
    else:
        score -= 5.0
    # minimum <= total
    if md <= td + 1e-6:
        score += 3.0
    else:
        score -= 5.0
    # prefer cl being the maximum of row
    if abs(cl - max(nums)) < 1e-6:
        score += 1.5
    # prefer available less than cl
    if av <= cl:
        score += 0.5
    # prefer total_due positive
    if td > 0:
        score += 0.5
    # prefer cl reasonably large ( > 1000 )
    if cl >= 1000:
        score += 0.5
    # penalize wildly inconsistent totals (total >> cl * 3)
    if cl > 0 and td > cl * 3:
        score -= 2.0
    # prefer td <= cl
    if td <= cl + 1e-6:
        score += 1.0
    else:
        score -= 2.0
    # penalize if md == td
    if abs(md - td) < 1e-6:
        score -= 3.0
    # penalize if av == md or av == td
    if abs(av - md) < 1e-6 or abs(av - td) < 1e-6:
        score -= 3.0
    # prefer md / td ~ 0.05
    if td > 0 and md / td < 0.1:
        score += 2.0
    else:
        score -= 2.0
    # penalize if 'cash' in raw
    if 'cash' in raw.lower():
        score -= 5.0
    # add log cl for larger cl
    if cl > 0:
        score += math.log(cl + 1) / 10
    return score

# Scoring for secondary candidate mapping (Total Payments, Other Charges, Total Purchases, Previous Balance)
def score_secondary_candidate(mapping):
    tp = mapping.get("Total Payments")
    oc = mapping.get("Other Charges")
    purch = mapping.get("Total Purchases")
    prev = mapping.get("Previous Balance")
    if any(x is None for x in [tp, oc, purch, prev]):
        return -1e6
    # basic non-negative check
    if any(x < -0.01 for x in [tp, oc, purch, prev]):
        return -1e6
    score = 0.0
    # prefer purchases >= payments (often true)
    if purch + 1e-6 >= tp:
        score += 1.5
    # prefer prev to be reasonably close to purchases (not always true, small weight)
    if purch > 0 and abs(prev - purch) / (purch + 1e-6) < 0.4:
        score += 0.8
    # prefer non-zero purchases or payments
    if purch > 0:
        score += 0.5
    if tp >= 0:
        score += 0.2
    return score

# Try to find best primary mapping across numeric rows
def choose_best_primary_mapping(numeric_rows):
    """
    numeric_rows: list of tuples (nums_list, page_idx, table_idx, row_idx, raw_row_text)
    returns: (best_mapping_dict, primary_row_index_in_numeric_rows, used_perm)
    """
    fields_primary = ["Credit Limit", "Available Credit", "Total Due", "Minimum Due"]
    best_score = -1e9
    best_map = None
    best_idx = None
    best_perm = None

    for idx, (nums, pidx, tidx, ridx, raw) in enumerate(numeric_rows):
        # perms of mapping numbers to fields
        for perm in itertools.permutations(range(4)):
            candidate = {fields_primary[i]: nums[perm[i]] for i in range(4)}
            s = score_primary_candidate(candidate, nums, raw)
            if s > best_score:
                best_score = s
                best_map = candidate
                best_idx = idx
                best_perm = perm

    if best_map is None:
        return None, None, None

    # format mapping
    formatted = {k: fmt_num(v) for k, v in best_map.items()}
    return formatted, best_idx, best_perm

# Choose mappings for remaining numeric rows as secondary
def map_secondary_rows(numeric_rows, exclude_index=None):
    fields_secondary = ["Total Payments", "Other Charges", "Total Purchases", "Previous Balance"]
    mapped = {}
    for idx, (nums, pidx, tidx, ridx, raw) in enumerate(numeric_rows):
        if idx == exclude_index:
            continue
        best_score = -1e9
        best_map = None
        best_perm = None
        for perm in itertools.permutations(range(4)):
            candidate = {fields_secondary[i]: nums[perm[i]] for i in range(4)}
            s = score_secondary_candidate(candidate)
            if s > best_score:
                best_score = s
                best_map = candidate
                best_perm = perm
        if best_map:
            # add if keys not present
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
# Date Parser
# ------------------------------
def parse_date(date_str):
    """Handle dd/mm/yyyy, Month DD, DD Month formats."""
    date_str = date_str.replace(",", "").strip()
    try:
        return datetime.strptime(date_str, "%d/%m/%Y").strftime("%d/%m/%Y")
    except:
        for fmt in ["%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y", "%B %d %Y"]:
            try:
                return datetime.strptime(date_str, fmt).strftime("%d/%m/%Y")
            except:
                pass
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
    numeric_rows_collected = []  # list of (nums_list, page_idx, table_idx, row_idx, raw_row_text)

    patterns = {
        "Credit Limit": r"Credit Limit\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Sanctioned Credit Limit\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)",
        "Available Credit": r"Available Credit Limit\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Available Credit\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)",
        "Available Cash Limit": r"Available Cash Limit\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)",
        "Total Due": r"Total Dues\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Total Due\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Closing Balance\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Total Amount Due\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)(?:\s*DR)?",
        "Minimum Due": r"Minimum Amount Due\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Minimum Due\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Minimum Payment\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)",
        "Previous Balance": r"Previous Balance\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|Opening Balance\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)",
        "Total Payments": r"Total Payments\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|New Credits Rs - ([\d,]+\.?\d*) \+|Payment/ Credits\s*([\d,]+\.?\d*)|Payments/ Credits\s*([\d,]+\.?\d*)|Payment/Credits\s*([\d,]+\.?\d*)",
        "Total Purchases": r"Total Purchases\s*(?:Rs )?[:\- ]?\s*([\d,]+\.?\d*)|New Debits Rs ([\d,]+\.?\d*)|Purchase/ Debits\s*([\d,]+\.?\d*)|Purchases/Debits\s*([\d,]+\.?\d*)|New Purchases/Debits\s*([\d,]+\.?\d*)",
        "Finance Charges": r"Finance Charges\s*[:\- ]?\s*([\d,]+\.?\d*)",
    }

    stmt_patterns = [
        r"Statement Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
        r"(\d{2}\s+[A-Za-z]{3}\s+\d{4})\s+To",
        r"Statement Period\s*From\s*\w+\s*\d+\s*to\s*(\w+\s*\d+ \d{4})",
        r"Statement Period\s*:\s*\d{2}\s+[A-Za-z]{3},\s*\d{4}\s*To\s*(\d{2}\s+[A-Za-z]{3},\s*\d{4})",
        r"From\s*(\w+\s*\d+)\s*to\s*(\w+\s*\d+,\s*\d{4})",
        r"Date\s*(\d{2}/\d{2}/\d{4})"
    ]

    due_patterns = [
        r"Payment Due Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
        r"Due by\s*([A-Za-z]+\s*\d+,\s*\d{4})",
        r"Minimum Payment Due\s*([A-Za-z]+\s*\d+,\s*\d{4})",
        r"Payment Due Date\s*(\d{2}/\d{2}/\d{4})"
    ]

    try:
        with pdfplumber.open(pdf_file) as pdf:
            for i in range(min(3, len(pdf.pages))):
                page = pdf.pages[i]
                page_text = page.extract_text()
                if page_text:
                    text_all += page_text + "\n"

                tables = page.extract_tables() or []
                for t_idx, table in enumerate(tables):
                    if not table or len(table) == 0:
                        continue

                    # Normalize rows (strip)
                    rows = [[(str(cell).strip() if cell is not None else "") for cell in r] for r in table]

                    # 1) Header->value mapping only when the value row contains at least one numeric token
                    header_keywords = ["due", "total", "payment", "credit limit", "available", "purchase", "opening", "minimum", "payments", "purchases"]
                    for ridx in range(len(rows)):
                        if ridx + 1 >= len(rows):
                            continue
                        row_text = " ".join(rows[ridx]).lower()
                        if any(k in row_text for k in header_keywords):
                            values_row = rows[ridx + 1]
                            numbers_in_values = re.findall(r"[\d,]+\.\d{2}", " ".join(values_row))
                            if not numbers_in_values:
                                continue
                            headers = rows[ridx]
                            values = values_row
                            for h, v in zip(headers, values):
                                if not v:
                                    continue
                                v_nums = re.findall(r"[\d,]+\.\d{2}", v)
                                v_clean = v.replace(",", "").replace(" DR", "")
                                h_low = h.lower()
                                if v_nums:
                                    if "payment due" in h_low or "due date" in h_low or "payment due date" in h_low:
                                        summary["Payment Due Date"] = v_clean
                                    elif "statement date" in h_low:
                                        summary["Statement Date"] = v_clean
                                    elif "total dues" in h_low or "total due" in h_low or "closing balance" in h_low or "total amount due" in h_low:
                                        summary["Total Due"] = fmt_num(v_clean)
                                    elif "minimum" in h_low:
                                        summary["Minimum Due"] = fmt_num(v_clean)
                                    elif "credit limit" in h_low and "available" not in h_low:
                                        summary["Credit Limit"] = fmt_num(v_clean)
                                    elif "available credit" in h_low or "available credit limit" in h_low:
                                        summary["Available Credit"] = fmt_num(v_clean)
                                    elif "available cash" in h_low:
                                        summary["Available Cash"] = fmt_num(v_clean)
                                    elif "opening balance" in h_low or "previous balance" in h_low:
                                        summary["Previous Balance"] = fmt_num(v_clean)
                                    elif ("payment" in h_low and "credit" in h_low) or "payments" == h_low.strip() or "payments/ credits" in h_low:
                                        summary["Total Payments"] = fmt_num(v_clean)
                                    elif "purchase" in h_low or "debit" in h_low or "purchases/ debits" in h_low or "new purchases/debits" in h_low:
                                        summary["Total Purchases"] = fmt_num(v_clean)
                                    elif "finance" in h_low:
                                        summary["Finance Charges"] = fmt_num(v_clean)

                    # 2) Collect any rows with exactly 4 numeric values (BoB-like)
                    for ridx, r in enumerate(rows):
                        row_text = " ".join(r)
                        numbers = re.findall(r"[\d,]+\.\d{2}", row_text)
                        if len(numbers) == 4:
                            nums_clean = [n.replace(",", "") for n in numbers]
                            nums_float = []
                            ok = True
                            for n in nums_clean:
                                try:
                                    nums_float.append(float(n))
                                except:
                                    ok = False
                                    break
                            if ok:
                                numeric_rows_collected.append((nums_float, i, t_idx, ridx, row_text))

        text_all = re.sub(r"\s+", " ", text_all).strip()

        # Specific row mapping for limit and summary rows
        limit_row = None
        summary_row = None
        for tup in numeric_rows_collected[:]:  # Copy to modify
            nums, pidx, tidx, ridx, raw = tup
            raw_lower = raw.lower()
            if 'cash limit' in raw_lower or 'available cash limit' in raw_lower:
                limit_row = tup
                summary['Credit Limit'] = fmt_num(nums[0])
                summary['Available Credit'] = fmt_num(nums[1])
                numeric_rows_collected.remove(tup)
            if ('opening balance' in raw_lower or 'previous balance' in raw_lower) and 'payment' in raw_lower and ('purchase' in raw_lower or 'debit' in raw_lower) and ('closing' in raw_lower or 'total' in raw_lower):
                summary_row = tup
                summary['Previous Balance'] = fmt_num(nums[0])
                summary['Total Payments'] = fmt_num(nums[1])
                summary['Total Purchases'] = fmt_num(nums[2])
                summary['Total Due'] = fmt_num(nums[3])
                numeric_rows_collected.remove(tup)

        # Choose best primary mapping among remaining numeric rows using permutation scoring
        if numeric_rows_collected:
            primary_map, primary_idx, perm = choose_best_primary_mapping(numeric_rows_collected)
            if primary_map:
                for k, v in primary_map.items():
                    if k not in summary:
                        summary[k] = v
                # map remaining rows as secondary
                secondary_mapped = map_secondary_rows(numeric_rows_collected, exclude_index=primary_idx)
                for k, v in secondary_mapped.items():
                    if k not in summary:
                        summary[k] = v

        # Additional regex parsing from text_all
        for key, pat in patterns.items():
            m = re.search(pat, text_all, re.IGNORECASE)
            if m:
                val_str = next((g for g in m.groups() if g is not None), None)
                if val_str:
                    val = parse_number(val_str)
                    if val is not None:
                        if key not in summary:
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

        # Unify Opening/Previous Balance
        if "Opening Balance" in summary and "Previous Balance" not in summary:
            summary["Previous Balance"] = summary.pop("Opening Balance")
        elif "Previous Balance" in summary and "Opening Balance" in summary:
            # Prefer Previous if both
            del summary["Opening Balance"]

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
