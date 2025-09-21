import streamlit as st
import pandas as pd
import pdfplumber
import re
from rapidfuzz import process
from io import BytesIO
import os
import itertools
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
        return f"{float(val):,.2f}"
    except:
        return val

def parse_number(s):
    try:
        return float(str(s).replace(",", "").strip())
    except:
        return None

# Scoring for candidate primary mapping (Credit Limit, Available Credit, Total Due, Minimum Due)
def score_primary_candidate(mapping, nums):
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
            s = score_primary_candidate(candidate, nums)
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
        for fmt in ["%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y"]:
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

            st.info(f"📄 Page {page_num}: extracted {len(transactions)} rows so far")

    return pd.DataFrame(transactions, columns=["Date", "Merchant", "Amount", "Type", "Account"])

# ------------------------------
# Extract summary from PDF (robust HDFC + BoB mapping)
# ------------------------------
def extract_summary_from_pdf(pdf_file):
    summary = {}
    text_all = ""
    numeric_rows_collected = []  # list of (nums_list, page_idx, table_idx, row_idx, raw_row_text)

    patterns = {
        "Credit Limit": r"(?:Credit Limit|Sanctioned Credit Limit)\s*(?:Rs)?\s*[:\-]?\s*([\d,]+\.?\d*)",
        "Available Credit Limit": r"Available Credit Limit\s*(?:Rs)?\s*[:\-]?\s*([\d,]+\.?\d*)",
        "Available Cash Limit": r"Available Cash Limit\s*(?:Rs)?\s*([\d,]+\.?\d*)",
        "Total Due": r"(?:Total Dues|Total Due|Closing Balance Rs|Total Amount Due)\s*(?:Rs)?\s*[:\-]?\s*([\d,]+\.?\d*)(?:\s*DR)?",
        "Minimum Due": r"(?:Minimum Amount Due|Minimum Due|Minimum Payment Rs|Minimum Payment)\s*(?:Rs)?\s*[:\-]?\s*([\d,]+\.?\d*)",
        "Opening Balance": r"Opening Balance\s*(?:Rs)?\s*[:\-]?\s*([\d,]+\.?\d*)",
        "Previous Balance": r"Previous Balance\s*(?:Rs)?\s*[:\-]?\s*([\d,]+\.?\d*)",
        "Total Payments": r"(?:Total Payments|Payment/ Credits|New Credits Rs|Payment/Credits)\s*[:\-]?\s*([\d,]+\.?\d*)",
        "Total Purchases": r"(?:Total Purchases|Purchase/ Debits|New Debits Rs|Purchase/Debits)\s*[:\-]?\s*([\d,]+\.?\d*)",
        "Finance Charges": r"Finance Charges\s*[:\-]?\s*([\d,]+\.?\d*)",
    }

    try:
        with pdfplumber.open(pdf_file) as pdf:
            for i in range(min(3, len(pdf.pages))):
                page = pdf.pages[i]
                page_text = page.extract_text()
                if page_text:
                    text_all += "\n" + page_text

                tables = page.extract_tables() or []
                for t_idx, table in enumerate(tables):
                    if not table or len(table) == 0:
                        continue

                    # Normalize rows (strip)
                    rows = [[(str(cell).strip() if cell is not None else "") for cell in r] for r in table]

                    # 1) Header->value mapping only when the value row contains at least one numeric token
                    header_keywords = ["due", "total", "payment", "credit limit", "available", "purchase", "opening", "minimum", "payments", "purchases"]
                    for ridx in range(min(2, len(rows))):
                        row_text = " ".join(rows[ridx]).lower()
                        if any(k in row_text for k in header_keywords) and (ridx + 1) < len(rows):
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
                                v_clean = v.replace(",", "")
                                h_low = h.lower()
                                if v_nums:
                                    if "payment due" in h_low or "due date" in h_low or "payment due date" in h_low:
                                        summary["Payment Due Date"] = v_clean
                                    elif "statement date" in h_low:
                                        summary["Statement Date"] = v_clean
                                    elif "total dues" in h_low or "total due" in h_low:
                                        summary["Total Due"] = fmt_num(v_clean)
                                    elif "minimum" in h_low:
                                        summary["Minimum Due"] = fmt_num(v_clean)
                                    elif "credit limit" in h_low and "available" not in h_low:
                                        summary["Credit Limit"] = fmt_num(v_clean)
                                    elif "available credit" in h_low or "available credit limit" in h_low:
                                        summary["Available Credit"] = fmt_num(v_clean)
                                    elif "available cash" in h_low:
                                        summary["Available Cash"] = fmt_num(v_clean)
                                    elif "opening balance" in h_low:
                                        summary["Previous Balance"] = fmt_num(v_clean)
                                    elif ("payment" in h_low and "credit" in h_low) or "payments" == h_low.strip():
                                        summary["Total Payments"] = fmt_num(v_clean)
                                    elif "purchase" in h_low or "debit" in h_low:
                                        summary["Total Purchases"] = fmt_num(v_clean)
                                    elif "finance" in h_low:
                                        summary["Finance Charges"] = fmt_num(v_clean)
                            continue

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

        # Choose best primary mapping among numeric rows using permutation scoring
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
                val_str = m.group(1).strip()
                val = parse_number(val_str)
                if val is not None:
                    if key not in summary:
                        summary[key] = fmt_num(val)

        # Unify Opening/Previous Balance
        if "Opening Balance" in summary and "Previous Balance" not in summary:
            summary["Previous Balance"] = summary.pop("Opening Balance")
        elif "Previous Balance" in summary and "Opening Balance" in summary:
            # Prefer Previous if both
            del summary["Opening Balance"]

        # Regex fallback for Statement Date if missing
        if "Statement Date" not in summary:
            stmt_patterns = [
                r"Statement Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
                r"(\d{2}\s+[A-Za-z]{3},\s*\d{4})\s+To",
                r"Date\s*(\d{2}/\d{2}/\d{4})",
                r"At ([A-Za-z]+ \d{1,2}, \d{4})",
                r"Statement Period\s*:\s*[\d A-Za-z,]+ To (\d{2} [A-Za-z]+, \d{4})"
            ]
            for pattern in stmt_patterns:
                m = re.search(pattern, text_all, re.IGNORECASE)
                if m:
                    date_str = m.group(1).replace(",", "")
                    summary["Statement Date"] = parse_date(date_str)
                    break

        # Regex fallback for Payment Due Date if missing
        if "Payment Due Date" not in summary:
            due_patterns = [
                r"Payment Due Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
                r"Due by ([A-Za-z0-9 /,]+)",
                r"Minimum Payment Due\s*([A-Za-z0-9 ,/]+)(?:\n|$)",
            ]
            for pattern in due_patterns:
                m = re.search(pattern, text_all, re.IGNORECASE)
                if m:
                    date_str = m.group(1).replace(",", "")
                    summary["Payment Due Date"] = parse_date(date_str)
                    break

    except Exception as e:
        st.error(f"⚠️ Error while extracting summary: {e}")

    if not summary:
        summary = {"Info": "No summary details detected in PDF."}

    # Final numeric normalization
    for k in list(summary.keys()):
        # if numeric-like string, format
        if isinstance(summary[k], (int, float)):
            summary[k] = fmt_num(summary[k])
        else:
            mnum = re.match(r"^\s*[\d,]+(?:\.\d+)?\s*$", str(summary[k]))
            if mnum:
                try:
                    summary[k] = fmt_num(str(summary[k]).replace(",", ""))
                except:
                    pass

    return summary

# ------------------------------
# Pretty Summary Cards (color-coded)
# ------------------------------
def display_summary(summary, account_name):
    st.subheader(f"📋 Statement Summary for {account_name}")
    st.json(summary)

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

    total_due_val = try_float_str(summary.get("Total Due", "0"))
    min_due_val = try_float_str(summary.get("Minimum Due", "0"))
    avail_credit_val = try_float_str(summary.get("Available Credit", "0"))

    total_due_color = "#d9534f" if total_due_val > 0 else "#5cb85c"
    min_due_color = "#f0ad4e" if min_due_val > 0 else "#5cb85c"
    avail_credit_color = "#5bc0de" if avail_credit_val > 0 else "#d9534f"

    col1, col2, col3 = st.columns(3)
    col4, col5, col6 = st.columns(3)
    col7, col8, col9 = st.columns(3)

    with col1:
        if summary.get("Statement Date"):
            st.markdown(colored_card("📅 Statement Date", summary["Statement Date"], "#0275d8"), unsafe_allow_html=True)
    with col2:
        if summary.get("Payment Due Date"):
            st.markdown(colored_card("⏰ Payment Due Date", summary["Payment Due Date"], "#5bc0de"), unsafe_allow_html=True)
    with col3:
        if summary.get("Previous Balance"):
            st.markdown(colored_card("💳 Previous Balance", summary["Previous Balance"], "#6f42c1"), unsafe_allow_html=True)

    with col4:
        if summary.get("Total Due"):
            st.markdown(colored_card("💰 Total Due", summary["Total Due"], total_due_color), unsafe_allow_html=True)
    with col5:
        if summary.get("Minimum Due"):
            st.markdown(colored_card("⚠️ Minimum Due", summary["Minimum Due"], min_due_color), unsafe_allow_html=True)
    with col6:
        if summary.get("Credit Limit"):
            st.markdown(colored_card("🏦 Credit Limit", summary["Credit Limit"], "#5cb85c"), unsafe_allow_html=True)

    with col7:
        if summary.get("Available Credit"):
            st.markdown(colored_card("✅ Available Credit", summary["Available Credit"], avail_credit_color), unsafe_allow_html=True)
    with col8:
        if summary.get("Total Purchases"):
            st.markdown(colored_card("🛒 Total Purchases", summary["Total Purchases"], "#0275d8"), unsafe_allow_html=True)
    with col9:
        if summary.get("Total Payments"):
            st.markdown(colored_card("💵 Total Payments", summary["Total Payments"], "#20c997"), unsafe_allow_html=True)

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
st.title("💳 Multi-Account Expense Analyzer")
st.write("✅ App loaded successfully, waiting for uploads...")

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
