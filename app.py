# app.py
import streamlit as st
import pandas as pd
import pdfplumber
import re
from rapidfuzz import process
from io import BytesIO
import os
from datetime import datetime
import itertools
import math

# ==============================
# Vendor Mapping (existing behavior)
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
def fmt_currency(val):
    try:
        return "₹{:,.2f}".format(float(val))
    except:
        return val

def parse_number(s):
    if s is None:
        return None
    s = str(s).strip()
    # strip currency symbols & text
    s = re.sub(r"[^\d\.\-\,]", "", s)
    s = s.replace(",", "")
    if s == "":
        return None
    try:
        return float(s)
    except:
        return None

def parse_date_generic(date_str):
    if not date_str:
        return None
    date_str = str(date_str).strip().replace(",", "")
    # try multiple formats
    fmts = ["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%d %m %Y"]
    for f in fmts:
        try:
            return datetime.strptime(date_str, f).strftime("%d/%m/%Y")
        except:
            pass
    # try to extract day month year in text like '14 Aug 2025' or 'Aug 14 2025'
    m = re.search(r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", date_str)
    if m:
        try:
            return datetime.strptime(m.group(1), "%d %b %Y").strftime("%d/%m/%Y")
        except:
            try:
                return datetime.strptime(m.group(1), "%d %B %Y").strftime("%d/%m/%Y")
            except:
                return date_str
    # fallback: return raw
    return date_str

# ------------------------------
# Category matching (unchanged)
# ------------------------------
def get_category(merchant):
    m = str(merchant).lower()
    try:
        matches = process.extractOne(m, vendor_map["merchant"].str.lower().tolist(), score_cutoff=80)
    except Exception:
        return "Others"
    if matches:
        matched_merchant = matches[0]
        category = vendor_map.loc[vendor_map["merchant"].str.lower() == matched_merchant, "category"].iloc[0]
        return category
    return "Others"

# ------------------------------
# Transaction extraction (PDF)
# handles generic banks + AMEX special pattern
# ------------------------------
def extract_transactions_from_pdf(pdf_file, account_name):
    transactions = []
    text_all = ""
    is_amex = False
    try:
        with pdfplumber.open(pdf_file) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                text = page.extract_text()
                if text:
                    text_all += text + "\n"
                    if "American Express" in text or "AmericanExpress" in text:
                        is_amex = True

                    # split into lines
                    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

                    if is_amex:
                        # amex: many statements use 'Month Day' style dates in transactions
                        i = 0
                        while i < len(lines):
                            ln = lines[i]
                            # Example AMEX: "July 01 BILLDESK*AMAZONAWSESC MUM 2.00 CR"
                            m = re.match(r"([A-Za-z]{3,9}\s+\d{1,2})\s+(.+?)\s+([\d,]+\.\d{2})\s*(CR|Cr|cr)?$", ln)
                            if m:
                                date_part, merchant, amount_str, cr_flag = m.groups()
                                # try to guess year from statement elsewhere - keep original month/day
                                # we will keep date as parsed as 'DD/MM/YYYY' only when full year present in statement summary
                                try:
                                    amt = float(amount_str.replace(",", ""))
                                except:
                                    i += 1
                                    continue
                                tr_type = "DR"
                                if cr_flag:
                                    amt = -amt
                                    tr_type = "CR"
                                transactions.append([date_part, merchant.strip(), round(amt,2), tr_type, account_name])
                            else:
                                # fallback generic dd/mm/yyyy
                                m2 = re.match(r"(\d{2}/\d{2}/\d{4})\s+(.+?)\s+([\d,]+\.\d{2})\s*(CR|Dr|DR|Cr)?", ln)
                                if m2:
                                    date, merchant, amount_str, drcr = m2.groups()
                                    try:
                                        amt = float(amount_str.replace(",", ""))
                                    except:
                                        i += 1
                                        continue
                                    if drcr and drcr.lower().startswith("cr"):
                                        amt = -amt
                                        tr_type = "CR"
                                    else:
                                        tr_type = "DR"
                                    transactions.append([parse_date_generic(date), merchant.strip(), round(amt,2), tr_type, account_name])
                            i += 1
                    else:
                        # generic banks: often dd/mm/yyyy ... merchant ... amount [CR/DR]
                        for ln in lines:
                            m = re.match(r"(\d{2}/\d{2}/\d{4})(?:\s+\d{2}:\d{2}:\d{2})?\s+(.+?)\s+([\d,]+\.\d{2})\s*(CR|Dr|DR|Cr)?", ln)
                            if m:
                                date, merchant, amount_str, drcr = m.groups()
                                try:
                                    amt = float(amount_str.replace(",", ""))
                                except:
                                    continue
                                tr_type = "DR"
                                if drcr and drcr.lower().startswith("cr"):
                                    amt = -amt
                                    tr_type = "CR"
                                transactions.append([parse_date_generic(date), merchant.strip(), round(amt,2), tr_type, account_name])
                st.info(f"📄 Page {page_num}: extracted {len(transactions)} rows so far")
    except Exception as e:
        st.error(f"Error reading PDF for transactions: {e}")
    df = pd.DataFrame(transactions, columns=["Date","Merchant","Amount","Type","Account"])
    return df

# ------------------------------
# Summary extraction
# robust approach:
# 1) gather first few pages text and tables
# 2) extract candidate numeric rows (rows containing many currency numbers)
# 3) try bank-specific regex rules (HDFC, AMEX, BOBCARD, ICICI)
# 4) if still ambiguous, permute mapping for numeric rows and score candidate mappings
# ------------------------------
def extract_summary_from_pdf(pdf_file):
    summary = {}
    debug = {"candidates": [], "matches": []}

    def find_numbers(s):
        return re.findall(r"[\d,]+(?:\.\d{1,2})?", s)

    try:
        text_all = ""
        tables_candidates = []  # list of (page_idx, table_idx, rows list)
        with pdfplumber.open(pdf_file) as pdf:
            pages_to_read = min(4, len(pdf.pages))
            for i in range(pages_to_read):
                p = pdf.pages[i]
                t = p.extract_text() or ""
                text_all += t + "\n"
                tbls = p.extract_tables() or []
                for tidx, tbl in enumerate(tbls):
                    # keep rows trimmed
                    rows = [[(str(c).strip() if c is not None else "") for c in r] for r in tbl]
                    tables_candidates.append((i, tidx, rows))

        text_all_norm = re.sub(r"\s+", " ", text_all).strip()
        lower_text = text_all_norm.lower()

        # detect bank
        is_amex = ("american express" in lower_text) or ("americanexpress" in lower_text)
        is_hdfc = ("hdfc bank" in lower_text) or ("hdfc" in lower_text and "credit card" in lower_text)
        is_bob = ("bobcard" in lower_text) or ("bank of baroda" in lower_text) or ("bobcard limited" in lower_text) or ("bob card" in lower_text)
        is_icici = ("icici" in lower_text) and ("credit card" in lower_text)

        debug["matches"].append(f"Bank flags - AMEX:{is_amex} HDFC:{is_hdfc} BOB:{is_bob} ICICI:{is_icici}")

        # 1) Bank-specific regex attempts (fast)
        # AMEX: "Credit Limit Rs" "Available Credit Limit Rs" "Minimum Payment" "Statement Period From ... to ..."
        if is_amex:
            # credit / available
            m = re.search(r"credit summary.*?credit limit.*?([\d,]+(?:\.\d{1,2})?).*?available credit limit.*?([\d,]+(?:\.\d{1,2})?)", text_all_norm, re.IGNORECASE|re.DOTALL)
            if not m:
                # try looser
                m1 = re.search(r"credit limit\s*rs\.?\s*([\d,]+(?:\.\d{1,2})?)", text_all_norm, re.IGNORECASE)
                m2 = re.search(r"available credit limit\s*rs\.?\s*([\d,]+(?:\.\d{1,2})?)", text_all_norm, re.IGNORECASE)
                if m1:
                    summary["Total Limit"] = parse_number(m1.group(1))
                    debug["matches"].append(f"AMEX credit limit (loose): {m1.group(0)}")
                if m2:
                    summary["Available Credit Limit"] = parse_number(m2.group(1))
                    debug["matches"].append(f"AMEX available credit (loose): {m2.group(0)}")
            else:
                summary["Total Limit"] = parse_number(m.group(1))
                summary["Available Credit Limit"] = parse_number(m.group(2))
                debug["matches"].append(f"AMEX credit/avail matched: {m.group(0)[:200]}")

            # used/closing / minimum / statement period
            m_closing = re.search(r"(closing balance|new balance|closing balance at)\s*[:\-]?\s*Rs?\.?\s*([\d,]+(?:\.\d{1,2})?)", text_all_norm, re.IGNORECASE)
            if m_closing:
                summary["Used Limit"] = parse_number(m_closing.group(2))
                debug["matches"].append(f"AMEX closing matched: {m_closing.group(0)}")

            # statement period/from ... to
            m_stmt = re.search(r"statement period\s*from\s*([A-Za-z0-9 ,]+?)\s*to\s*([A-Za-z0-9 ,]+?\d{4})", text_all_norm, re.IGNORECASE)
            if m_stmt:
                # use end date as statement date
                summary["Statement Date"] = parse_date_generic(m_stmt.group(2))
                debug["matches"].append(f"AMEX statement period matched: {m_stmt.group(0)}")

            # payment due / minimum
            m_min = re.search(r"minimum (payment|amount)[:\- ]*Rs?\.?\s*([\d,]+(?:\.\d{1,2})?)", text_all_norm, re.IGNORECASE)
            if m_min:
                summary["Minimum Due"] = parse_number(m_min.group(2))
                debug["matches"].append(f"AMEX minimum matched: {m_min.group(0)}")

        # HDFC: common pattern: a header containing "Payment Due Date Total Dues Minimum Amount Due" followed by a row with (date, total dues, minimum)
        if is_hdfc:
            # locate 'Payment Due Date' and capture following date and numbers
            idx = lower_text.find("payment due date")
            if idx != -1:
                snippet = text_all_norm[idx: idx+300]
                # date first
                mdate = re.search(r"(\d{2}/\d{2}/\d{4})", snippet)
                if mdate:
                    summary["Payment Due Date"] = parse_date_generic(mdate.group(1))
                nums = re.findall(r"[\d,]+(?:\.\d{1,2})?", snippet)
                # collect 2nd and 3rd numbers if present
                if len(nums) >= 2:
                    # often: date total_dues minimum_amount
                    # nums list may include date as digits, so filter by length of digit groups > 4
                    num_vals = [parse_number(n) for n in nums if parse_number(n) is not None and len(re.sub(r"[^\d]", "", n))>1]
                    if len(num_vals) >= 2:
                        summary["Total Due"] = num_vals[0]
                        if len(num_vals) >= 2:
                            summary["Minimum Due"] = num_vals[1]
                        debug["matches"].append(f"HDFC snippet after Payment Due Date: {snippet[:200]}")
            # Credit Limit block
            idx2 = lower_text.find("credit limit available credit limit")
            if idx2 != -1:
                snippet2 = text_all_norm[idx2: idx2+200]
                nums = re.findall(r"[\d,]+(?:\.\d{1,2})?", snippet2)
                if len(nums) >= 2:
                    summary["Total Limit"] = parse_number(nums[0])
                    summary["Available Credit Limit"] = parse_number(nums[1])
                    debug["matches"].append(f"HDFC credit/avail snippet: {snippet2[:200]}")
            # fallback search for "Statement Date"
            mstmt = re.search(r"statement date[:\s\-]*([0-9]{1,2}[\/\-\s][0-9]{1,2}[\/\-\s][0-9]{2,4})", text_all_norm, re.IGNORECASE)
            if mstmt:
                summary["Statement Date"] = parse_date_generic(mstmt.group(1))

        # BOBCARD (Bank of Baroda / bobcard) heuristics
        if is_bob:
            # many BOB statements show table rows with 4 numeric values (often previous/credits/purchases/closing or credit/avail/total/minimum)
            # find tables that include rows with exactly 4 numeric tokens
            candidate_rows = []
            for (pidx,tidx,rows) in tables_candidates:
                for r_idx, row in enumerate(rows):
                    row_text = " ".join(row)
                    nums = find_numbers(row_text)
                    if len(nums) >= 3:
                        # store row and surrounding header if present
                        header_above = rows[r_idx-1] if r_idx-1 >= 0 else []
                        candidate_rows.append((row_text, nums, header_above, pidx, tidx, r_idx))
            debug["candidates"].extend(candidate_rows[:10])
            # attempt to find the best mapping using permutations
            # we will try common label sets:
            # - set A: [Credit Limit, Available Credit, Total Due, Minimum Due]
            # - set B: [Previous Balance, Total Payments, Total Purchases, Total Due]
            fieldsA = ["Credit Limit","Available Credit","Total Due","Minimum Due"]
            fieldsB = ["Previous Balance","Total Payments","Total Purchases","Total Due"]
            # build numeric rows as floats
            numeric_rows = []
            for (row_text, nums, header_above, pidx, tidx, ridx) in candidate_rows:
                # keep only first 4 numbers per row
                nums4 = nums[:4]
                nums_f = []
                ok=True
                for n in nums4:
                    v = parse_number(n)
                    if v is None:
                        ok=False
                        break
                    nums_f.append(v)
                if ok and len(nums_f)>=2:
                    numeric_rows.append((nums_f, row_text, header_above, pidx, tidx, ridx))
            # scoring function: prefer mappings where Available ~ Credit - Used (or credit >= used), and TotalDue <= Credit
            def score_map_map(candidate_map):
                # candidate_map: dict mapping field->value
                score = 0.0
                cl = candidate_map.get("Credit Limit")
                av = candidate_map.get("Available Credit")
                td = candidate_map.get("Total Due") or candidate_map.get("Total Due")
                md = candidate_map.get("Minimum Due") or candidate_map.get("Minimum Due")
                # non-neg checks
                for k,v in candidate_map.items():
                    if v is None or v < -1e6:
                        return -1e9
                # if credit and available exist, prefer av <= cl
                if cl is not None and av is not None:
                    if av <= cl + 1e-6:
                        score += 3.0
                    # prefer av approx cl - td if td known
                    if td is not None:
                        if abs((cl - td) - av) / (cl+1e-6) < 0.15:
                            score += 3.0
                # prefer td <= cl
                if cl is not None and td is not None:
                    if td <= cl + 1e-6:
                        score += 1.5
                    else:
                        score -= 1.0
                # prefer md <= td
                if md is not None and td is not None:
                    if md <= td + 1e-6:
                        score += 1.0
                # prefer reasonable credit limit > 100
                if cl is not None and cl > 100:
                    score += math.log(cl+1)/10
                return score
            # enumerate permutations across numeric_rows
            best = {"score": -1e9, "mapping": None, "source_row": None}
            for nums_f, row_text, header_above, pidx, tidx, ridx in numeric_rows:
                # if len <4 then skip some permutations, but try what we can
                length = len(nums_f)
                if length < 2:
                    continue
                idxs = list(range(length))
                # try assign first 4 potential fields (if length <4, we still map available)
                candidate_field_sets = [fieldsA, fieldsB]
                for fields in candidate_field_sets:
                    # pick order permutations of available values - but limit to permutations of length == len(fields) if possible
                    # we will permute indices for top min(len(fields), length)
                    for perm in itertools.permutations(idxs, min(len(fields), length)):
                        cand_map = {}
                        for f_i, f_name in enumerate(fields):
                            if f_i < len(perm):
                                cand_map[f_name] = nums_f[perm[f_i]]
                            else:
                                cand_map[f_name] = None
                        sc = score_map_map(cand_map)
                        if sc > best["score"]:
                            best["score"] = sc
                            best["mapping"] = cand_map.copy()
                            best["source_row"] = (row_text, header_above, pidx, tidx, ridx)
            if best["mapping"]:
                # write mapping values into summary where present
                for k,v in best["mapping"].items():
                    if v is not None:
                        summary[k] = v
                debug["matches"].append(f"BOB best mapping (score {best['score']}): {best['mapping']}")
            # also try searches for 'Closing Balance' and 'Statement Date'
            m_stmt = re.search(r"(\d{1,2}\s+[A-Za-z]{3,9}\s*,?\s*\d{4})\s+To\s+(\d{1,2}\s+[A-Za-z]{3,9}\s*,?\s*\d{4})", text_all_norm, re.IGNORECASE)
            if m_stmt:
                # use second as statement date
                summary["Statement Date"] = parse_date_generic(m_stmt.group(2))
            else:
                m_alt = re.search(r"Statement Date[:\s\-]*([0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4}|[0-9]{1,2}[\/\-][0-9]{1,2}[\/\-][0-9]{2,4})", text_all_norm, re.IGNORECASE)
                if m_alt:
                    summary["Statement Date"] = parse_date_generic(m_alt.group(1))

        # ICICI heuristics - look for "Total Amount Due" "Available Credit"
        if is_icici:
            m_tot = re.search(r"Total Amount Due\s*[:\-]?\s*Rs?\.?\s*([\d,]+(?:\.\d{1,2})?)", text_all_norm, re.IGNORECASE)
            if m_tot:
                summary["Used Limit"] = parse_number(m_tot.group(1))
                debug["matches"].append(f"ICICI total amount due matched: {m_tot.group(0)}")
            m_av = re.search(r"Available Credit\s*[:\-]?\s*Rs?\.?\s*([\d,]+(?:\.\d{1,2})?)", text_all_norm, re.IGNORECASE)
            if m_av:
                summary["Available Credit Limit"] = parse_number(m_av.group(1))
            m_stmt = re.search(r"Statement Date\s*[:\-]?\s*([0-9]{1,2}[\/\-][0-9]{1,2}[\/\-][0-9]{2,4})", text_all_norm, re.IGNORECASE)
            if m_stmt:
                summary["Statement Date"] = parse_date_generic(m_stmt.group(1))

        # Generic fallback: regex scan for common labels anywhere in first pages
        generic_patterns = {
            "Total Limit": r"(Credit Limit|Sanctioned Credit Limit)\s*[:\-]?\s*Rs?\.?\s*([\d,]+(?:\.\d{1,2})?)",
            "Available Credit Limit": r"(Available Credit Limit|Available Credit)\s*[:\-]?\s*Rs?\.?\s*([\d,]+(?:\.\d{1,2})?)",
            "Used Limit": r"(Total Dues|Total Due|Closing Balance|Closing Balance Rs|Outstanding Balance|Total Amount Due)\s*[:\-]?\s*Rs?\.?\s*([\d,]+(?:\.\d{1,2})?)",
            "Minimum Due": r"(Minimum Amount Due|Minimum Payment|Minimum Due)\s*[:\-]?\s*Rs?\.?\s*([\d,]+(?:\.\d{1,2})?)",
            "Total Purchases": r"(Total Purchases|Purchases/ Debits|New Purchases)\s*[:\-]?\s*Rs?\.?\s*([\d,]+(?:\.\d{1,2})?)",
            "Total Payments": r"(Payment/ Credits|Payments/ Credits|Total Payments|Payment Credits)\s*[:\-]?\s*Rs?\.?\s*([\d,]+(?:\.\d{1,2})?)",
            "Statement Date": r"(Statement Date|Statement Period.*?to)\s*[:\-]?\s*([0-9]{1,2}[\/\-\s][A-Za-z0-9 ,\/\-]+[0-9]{2,4})",
            "Payment Due Date": r"(Payment Due Date|Due by|Due Date)\s*[:\-]?\s*([0-9]{1,2}[\/\-\s][A-Za-z0-9 ,\/\-]+[0-9]{2,4})"
        }
        for k, pat in generic_patterns.items():
            m = re.search(pat, text_all_norm, re.IGNORECASE|re.DOTALL)
            if m:
                # last group tends to be numeric/date
                val = None
                if k in ["Statement Date", "Payment Due Date"]:
                    val = parse_date_generic(m.group(len(m.groups())))
                else:
                    val = parse_number(m.group(len(m.groups())))
                if val is not None:
                    summary[k] = val
                    debug["matches"].append(f"Generic match for {k}: {m.group(0)[:200]} -> {val}")

        # Derived fields / sanity fixes:
        # If we have Total Limit and Available but not Used, compute Used = TotalLimit - Available
        if "Total Limit" in summary and "Available Credit Limit" in summary and "Used Limit" not in summary:
            try:
                summary["Used Limit"] = round(float(summary["Total Limit"]) - float(summary["Available Credit Limit"]), 2)
                debug["matches"].append("Derived Used Limit = TotalLimit - Available")
            except:
                pass

        # If Available missing but we have total limit and used limit, compute available
        if "Available Credit Limit" not in summary and "Total Limit" in summary and "Used Limit" in summary:
            try:
                summary["Available Credit Limit"] = round(float(summary["Total Limit"]) - float(summary["Used Limit"]), 2)
                debug["matches"].append("Derived Available Credit Limit = TotalLimit - UsedLimit")
            except:
                pass

        # if no Statement Date found try to find pattern 'To DD Mon, YYYY' or 'Statement Period From ... To ...'
        if "Statement Date" not in summary:
            m = re.search(r"to\s+([0-9]{1,2}\s+[A-Za-z]{3,9}\s*,?\s*\d{4})", text_all_norm, re.IGNORECASE)
            if m:
                summary["Statement Date"] = parse_date_generic(m.group(1))
                debug["matches"].append(f"Fallback statement date found: {m.group(0)}")

        # produce a cleaned derived summary to display
        derived = {}
        derived["Statement Date"] = summary.get("Statement Date", "N/A")
        if isinstance(derived["Statement Date"], (int, float)):
            derived["Statement Date"] = str(derived["Statement Date"])
        derived["Payment Due Date"] = summary.get("Payment Due Date", "N/A")
        derived["Total Limit"] = fmt_currency(summary["Total Limit"]) if "Total Limit" in summary else "N/A"
        derived["Used Limit"] = fmt_currency(summary["Used Limit"]) if "Used Limit" in summary else "N/A"
        derived["Available Credit Limit"] = fmt_currency(summary["Available Credit Limit"]) if "Available Credit Limit" in summary else "N/A"
        derived["Minimum Due"] = fmt_currency(summary["Minimum Due"]) if "Minimum Due" in summary else "N/A"
        derived["Expenses during the month"] = fmt_currency(summary.get("Total Purchases")) if "Total Purchases" in summary else "N/A"
        derived["Total Payments"] = fmt_currency(summary.get("Total Payments")) if "Total Payments" in summary else "N/A"

        # debug expander
        with st.expander("🔎 Summary extraction debug (candidates & matches)"):
            st.write("Bank detection flags:", {"AMEX": is_amex, "HDFC": is_hdfc, "BOB": is_bob, "ICICI": is_icici})
            st.write("Raw first-pages text preview (trimmed):", text_all_norm[:2000])
            st.write("Debug matches / rules applied:")
            for d in debug["matches"]:
                st.write("-", d)
            st.write("Numeric row candidates (sample):")
            for c in debug["candidates"][:10]:
                st.write(c)

        return derived

    except Exception as e:
        st.error(f"Error extracting summary: {e}")
        return {"Info": "No summary details detected in PDF."}

# ------------------------------
# CSV / Excel extraction helpers (unchanged)
# ------------------------------
def extract_transactions_from_excel(file, account_name):
    df = pd.read_excel(file)
    return normalize_dataframe(df, account_name)

def extract_transactions_from_csv(file, account_name):
    df = pd.read_csv(file)
    return normalize_dataframe(df, account_name)

def normalize_dataframe(df, account_name):
    col_map = {
        "date": "Date", "transaction date": "Date", "txn date": "Date",
        "description": "Merchant", "narration": "Merchant", "merchant": "Merchant",
        "amount": "Amount", "debit": "Debit", "credit": "Credit", "type": "Type"
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
        st.error("❌ Could not detect required columns (Date, Merchant, Amount).")
        return pd.DataFrame(columns=["Date", "Merchant", "Amount", "Type", "Account"])

    df["Amount"] = df["Amount"].astype(float).round(2)
    df["Account"] = account_name
    return df[["Date","Merchant","Amount","Type","Account"]]

# ------------------------------
# Categorize + vendor updating
# ------------------------------
def categorize_expenses(df):
    df["Category"] = df["Merchant"].apply(get_category)
    return df

def add_new_vendor(merchant, category):
    global vendor_map
    new_row = pd.DataFrame([[merchant.lower(), category]], columns=["merchant","category"])
    vendor_map = pd.concat([vendor_map, new_row], ignore_index=True)
    vendor_map.drop_duplicates(subset=["merchant"], keep="last", inplace=True)
    vendor_map.to_csv(VENDOR_FILE, index=False)

# ------------------------------
# Exports
# ------------------------------
def convert_df_to_csv(df):
    df["Amount"] = df["Amount"].round(2)
    return df.to_csv(index=False).encode("utf-8")

def convert_df_to_excel(df):
    df["Amount"] = df["Amount"].round(2)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Expenses", float_format="%.2f")
    return output.getvalue()

# ------------------------------
# UI
# ------------------------------
st.title("💳 Multi-Account Expense Analyzer (improved summary extraction)")
st.write("Upload credit card / bank statements (PDF/CSV/XLSX). Summary extraction uses bank-specific rules + heuristics.")

uploaded_files = st.file_uploader("Upload Statements", type=["pdf","csv","xlsx"], accept_multiple_files=True)

if uploaded_files:
    all_data = pd.DataFrame(columns=["Date","Merchant","Amount","Type","Account"])
    for uploaded_file in uploaded_files:
        account_name = st.text_input(f"Enter account name for {uploaded_file.name}", value=uploaded_file.name)
        if not account_name:
            account_name = uploaded_file.name

        # process file
        if uploaded_file.name.lower().endswith(".pdf"):
            st.info(f"Processing PDF: {uploaded_file.name} ...")
            df = extract_transactions_from_pdf(uploaded_file, account_name)
            summary = extract_summary_from_pdf(uploaded_file)
            # display summary cards
            st.subheader(f"📋 Statement Summary — {account_name}")
            cols = st.columns(3)
            cols[0].markdown(f"**Statement date**\n\n{summary.get('Statement Date','N/A')}")
            cols[1].markdown(f"**Payment Due Date**\n\n{summary.get('Payment Due Date','N/A')}")
            cols[2].markdown(f"**Total Limit**\n\n{summary.get('Total Limit','N/A')}")
            cols2 = st.columns(3)
            cols2[0].markdown(f"**Used / Closing**\n\n{summary.get('Used Limit','N/A')}")
            cols2[1].markdown(f"**Available Credit**\n\n{summary.get('Available Credit Limit','N/A')}")
            cols2[2].markdown(f"**Min Due**\n\n{summary.get('Minimum Due','N/A')}")
        elif uploaded_file.name.lower().endswith(".csv"):
            df = extract_transactions_from_csv(uploaded_file, account_name)
        elif uploaded_file.name.lower().endswith(".xlsx") or uploaded_file.name.lower().endswith(".xls"):
            df = extract_transactions_from_excel(uploaded_file, account_name)
        else:
            df = pd.DataFrame()

        all_data = pd.concat([all_data, df], ignore_index=True)

    if not all_data.empty:
        all_data = categorize_expenses(all_data)
        all_data["Amount"] = all_data["Amount"].round(2)
        st.subheader("📑 Extracted Transactions")
        st.dataframe(all_data.style.format({"Amount":"{:,.2f}"}))

        # unknown merchants -> manual category assign
        unknown = all_data[all_data["Category"] == "Others"]
        if not unknown.empty:
            st.subheader("⚡ Assign Categories for Unknown Merchants")
            for m in unknown["Merchant"].unique():
                cat = st.selectbox(f"Category for {m}", ["Food","Groceries","Shopping","Travel","Utilities","Entertainment","Fuel","Jewellery","Electronics","Banking","Insurance","Education","Healthcare","Others"], key=m)
                if cat != "Others":
                    add_new_vendor(m, cat)
                    all_data.loc[all_data["Merchant"] == m, "Category"] = cat
                    st.success(f"Assigned {m} -> {cat}")

        st.subheader("📊 Expense Analysis")
        expenses = all_data[all_data["Amount"] > 0]
        st.write("💰 **Total Spent:**", fmt_currency(expenses["Amount"].sum()))
        st.write("📈 Expense by Category")
        st.bar_chart(expenses.groupby("Category")["Amount"].sum())
        st.write("🏦 Top 5 Merchants")
        top5 = expenses.groupby("Merchant")["Amount"].sum().sort_values(ascending=False).head()
        st.dataframe(top5.apply(lambda x: fmt_currency(x)))
        st.write("🏦 Expense by Account")
        st.bar_chart(expenses.groupby("Account")["Amount"].sum())

        csv_data = convert_df_to_csv(all_data)
        excel_data = convert_df_to_excel(all_data)
        st.download_button("⬇️ Download CSV", csv_data, file_name="expenses_all.csv", mime="text/csv")
        st.download_button("⬇️ Download XLSX", excel_data, file_name="expenses_all.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
