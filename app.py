import streamlit as st
import pandas as pd
import pdfplumber
import re
from rapidfuzz import process
from io import BytesIO
import os
import itertools

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
        return float(s.replace(",", ""))
    except:
        return None

# Choose best assignment for 4 numbers using constraints + heuristics
def map_four_numbers(nums, summary_exists=False):
    """
    nums: list of 4 floats (in order as found in table row)
    summary_exists: if True, means we've already mapped a 4-number row earlier,
                   so this is likely Payments/OtherCharges/Purchases/PreviousBalance row.
    Returns dict mapping fields to formatted strings.
    """
    fields_primary = ["Credit Limit", "Available Credit", "Total Due", "Minimum Due"]
    fields_secondary = ["Total Payments", "Other Charges", "Total Purchases", "Previous Balance"]

    # Constraint check function
    def valid_primary_map(m):
        cl = m.get("Credit Limit")
        av = m.get("Available Credit")
        td = m.get("Total Due")
        md = m.get("Minimum Due")
        if cl is not None and av is not None:
            if cl + 1e-6 < av:
                return False
        if td is not None and md is not None:
            if md - td > 1e-6:  # minimum should be <= total
                return False
        return True

    if not summary_exists:
        # try default ordering first
        m0 = dict(zip(fields_primary, nums))
        if valid_primary_map(m0):
            return {k: f"{v:,.2f}" for k, v in m0.items()}

        # try permutations of mapping positions to fields
        candidates = []
        for perm in itertools.permutations(range(4)):
            m = {fields_primary[i]: nums[perm[i]] for i in range(4)}
            if valid_primary_map(m):
                candidates.append(m)
        if candidates:
            # choose the candidate with largest Credit Limit
            best = max(candidates, key=lambda x: x.get("Credit Limit", 0) or 0)
            return {k: f"{v:,.2f}" for k, v in best.items()}

        # fallback: assume descending order: credit, available, total, minimum
        sorted_nums = sorted(nums, reverse=True)
        fallback = {
            "Credit Limit": sorted_nums[0],
            "Available Credit": sorted_nums[1],
            "Total Due": sorted_nums[2],
            "Minimum Due": sorted_nums[3],
        }
        return {k: f"{v:,.2f}" for k, v in fallback.items()}
    else:
        # secondary row mapping (payments etc.)
        m0 = dict(zip(fields_secondary, nums))
        # Basic sanity checks: values should be non-negative
        if all(v >= 0 for v in nums):
            return {k: f"{v:,.2f}" for k, v in m0.items()}
        # try permutations
        for perm in itertools.permutations(range(4)):
            m = {fields_secondary[i]: nums[perm[i]] for i in range(4)}
            if all(v >= 0 for v in m.values()):
                return {k: f"{v:,.2f}" for k, v in m.items()}
        # fallback
        return {k: f"{v:,.2f}" for k, v in m0.items()}

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
                    try:
                        amt = round(float(amount.replace(",", "")), 2)
                    except:
                        continue
                    if drcr and drcr.strip().lower().startswith("cr"):
                        amt = -amt
                        tr_type = "CR"
                    else:
                        tr_type = "DR"
                    transactions.append([date, merchant.strip(), amt, tr_type, account_name])

            st.info(f"📄 Page {page_num}: extracted {len(transactions)} rows so far")

    return pd.DataFrame(transactions, columns=["Date", "Merchant", "Amount", "Type", "Account"])

# ------------------------------
# Extract summary from PDF (robust HDFC + BoB mapping)
# ------------------------------
def extract_summary_from_pdf(pdf_file):
    summary = {}
    text_all = ""
    numeric_rows_collected = []  # list of (nums_list, page_idx, row_idx, raw_row_text)

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
                    # Look for header rows in first two rows
                    for ridx in range(min(2, len(rows))):
                        row_text = " ".join(rows[ridx]).lower()
                        if any(k in row_text for k in header_keywords) and (ridx + 1) < len(rows):
                            values_row = rows[ridx + 1]
                            # ensure values_row has at least one numeric token
                            numbers_in_values = re.findall(r"[\d,]+\.\d{2}", " ".join(values_row))
                            if not numbers_in_values:
                                continue  # skip header mapping if next row is non-numeric
                            headers = rows[ridx]
                            values = values_row
                            # map headers->values but only where value is numeric
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
                            # after header mapping continue to next table
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

        # If numeric rows found, pick primary row as one with largest max value (credit info)
        if numeric_rows_collected:
            # sort by max value descending
            numeric_rows_collected_sorted = sorted(numeric_rows_collected, key=lambda x: max(x[0]) if x[0] else 0, reverse=True)
            # primary
            primary = numeric_rows_collected_sorted[0][0]
            mapped_primary = map_four_numbers(primary, summary_exists=bool(summary.get("Credit Limit")))
            # merge primary
            for k, v in mapped_primary.items():
                if k not in summary:
                    summary[k] = v
            # remaining rows -> map as secondary if any
            for other in numeric_rows_collected_sorted[1:]:
                nums = other[0]
                mapped_secondary = map_four_numbers(nums, summary_exists=True)
                for k, v in mapped_secondary.items():
                    if k not in summary:
                        summary[k] = v

        # Regex fallback for Statement Date if still missing
        if "Statement Date" not in summary:
            patterns = [
                r"Statement Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
                r"(\d{2}\s+[A-Za-z]{3},\s*\d{4})\s+To"
            ]
            for pattern in patterns:
                m = re.search(pattern, text_all, re.IGNORECASE)
                if m:
                    summary["Statement Date"] = m.group(1).replace(",", "")
                    break

    except Exception as e:
        st.error(f"⚠️ Error while extracting summary: {e}")

    if not summary:
        summary = {"Info": "No summary details detected in PDF."}

    # Ensure numeric fields formatted
    for k in list(summary.keys()):
        # if the value looks like a number without comma formatting, attempt to format
        if isinstance(summary[k], (int, float)):
            summary[k] = fmt_num(summary[k])
        else:
            # if it's numeric string, standardize it
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
# Streamlit UI
# ------------------------------
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
                st.subheader(f"📋 Statement Summary for {account_name}")
                st.json(summary)
                display_summary(summary, account_name)

            elif uploaded_file.name.endswith(".csv"):
                df = pd.read_csv(uploaded_file)
            elif uploaded_file.name.endswith(".xlsx"):
                df = pd.read_excel(uploaded_file)
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
        st.write("💰 **Total Spent:**", f"{all_data['Amount'].sum():,.2f}")
        st.bar_chart(all_data.groupby("Category")["Amount"].sum())
        st.write("🏦 **Top 5 Merchants**")
        st.dataframe(all_data.groupby("Merchant")["Amount"].sum().sort_values(ascending=False).head().apply(lambda x: f"{x:,.2f}"))
        st.write("🏦 **Expense by Account**")
        st.bar_chart(all_data.groupby("Account")["Amount"].sum())

        # Export
        csv_data = convert_df_to_csv(all_data)
        excel_data = convert_df_to_excel(all_data)

        st.download_button("⬇️ Download as CSV", csv_data, file_name="expenses_all.csv", mime="text/csv")
        st.download_button("⬇️ Download as Excel", excel_data, file_name="expenses_all.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
