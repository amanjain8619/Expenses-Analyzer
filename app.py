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

    # Try default ordering first
    default_map = dict(zip(fields_secondary if summary_exists else fields_primary, nums))
    # Constraint check function
    def valid_map(m):
        # require credit limit >= available and minimum <= total_due
        cl = m.get("Credit Limit")
        av = m.get("Available Credit")
        td = m.get("Total Due")
        md = m.get("Minimum Due")
        # If credit limit present check
        if cl is not None and av is not None:
            if cl + 1e-6 < av:
                return False
        if td is not None and cl is not None:
            # total due usually <= credit limit (but can exceed if overlimit) -- be lenient
            if td > cl * 3 and cl > 0:
                return False
        if md is not None and td is not None:
            if md - td > 1e-6:  # minimum should be <= total
                return False
        return True

    if not summary_exists:
        # check default mapping
        m0 = dict(zip(fields_primary, nums))
        if valid_map(m0):
            return {k: f"{v:,.2f}" for k, v in m0.items()}
        # try permutations of mapping positions to fields
        best = None
        candidates = []
        for perm in itertools.permutations(range(4)):
            m = {fields_primary[i]: nums[perm[i]] for i in range(4)}
            if valid_map(m):
                candidates.append(m)
        if candidates:
            # choose the candidate with largest credit limit (most likely correct)
            best = max(candidates, key=lambda x: x.get("Credit Limit", 0) or 0)
            return {k: f"{v:,.2f}" for k, v in best.items()}
        # fallback: pick max as credit limit, min as minimum, rest by descending
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
        # Basic sanity checks: Total Payments & Total Purchases should be non-negative
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

    try:
        with pdfplumber.open(pdf_file) as pdf:
            for i in range(min(3, len(pdf.pages))):
                page = pdf.pages[i]
                page_text = page.extract_text()
                if page_text:
                    text_all += "\n" + page_text

                tables = page.extract_tables() or []
                for table in tables:
                    if not table or len(table) == 0:
                        continue

                    # Normalize rows (strip)
                    rows = [[(str(cell).strip() if cell is not None else "") for cell in r] for r in table]

                    # 1) If first or second row contains header keywords, map header->value using next row(s)
                    header_keywords = ["due", "total", "payment", "credit limit", "available", "purchase", "opening", "minimum", "payments", "purchases"]
                    header_row_idx = None
                    for ridx in range(min(2, len(rows))):
                        row_text = " ".join(rows[ridx]).lower()
                        if any(k in row_text for k in header_keywords):
                            header_row_idx = ridx
                            break
                    if header_row_idx is not None and header_row_idx + 1 < len(rows):
                        headers = rows[header_row_idx]
                        values = rows[header_row_idx + 1]
                        # zip headers->values
                        for h, v in zip(headers, values):
                            if not v or v.lower() in ["nan", ""]:
                                continue
                            h_low = h.lower()
                            v_clean = v.replace(",", "")
                            # map header text heuristically
                            if "payment due" in h_low or "due date" in h_low or "payment due date" in h_low:
                                summary["Payment Due Date"] = v_clean
                            elif "statement date" in h_low or "statement" in h_low and "date" in h_low:
                                summary["Statement Date"] = v_clean
                            elif "total dues" in h_low or "total due" in h_low or "total dues" in h_low:
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
                        # continue to next table after header mapping
                        continue

                    # 2) Otherwise look for ANY rows that contain exactly 4 numeric values (BoB-like)
                    numeric_rows = []
                    for r in rows:
                        # flatten row into string and find all numbers
                        numbers = re.findall(r"[\d,]+\.\d{2}", " ".join(r))
                        if len(numbers) == 4:
                            numeric_rows.append([n.replace(",", "") for n in numbers])

                    # Process numeric rows in order found: first maps to credit-info if not present
                    for nr_idx, nr in enumerate(numeric_rows):
                        nums = [float(x) for x in nr]
                        mapped = map_four_numbers(nums, summary_exists=bool(summary.get("Credit Limit")))
                        # merge mapped fields into summary (do not overwrite existing keys)
                        for k, v in mapped.items():
                            if k not in summary:
                                summary[k] = v

        # Regex fallback for Statement Date and minimal other fields if missing
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
        if isinstance(summary[k], (int, float)):
            summary[k] = fmt_num(summary[k])

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
                # normalization left simple here
                df = df.rename(columns={c: c for c in df.columns})
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
