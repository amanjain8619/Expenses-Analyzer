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
# Config / Vendor map
# ==============================
VENDOR_FILE = "vendors.csv"
if os.path.exists(VENDOR_FILE):
    vendor_map = pd.read_csv(VENDOR_FILE)
else:
    vendor_map = pd.DataFrame(columns=["merchant", "category"])
    vendor_map.to_csv(VENDOR_FILE, index=False)

# ==============================
# Helpers
# ==============================
def fmt_num(val, add_rupee=True):
    """Format numeric value to 2 decimals with commas. If non-numeric, return as-is."""
    try:
        n = float(val)
        s = f"{n:,.2f}"
        return f"₹{s}" if add_rupee else s
    except Exception:
        return val

def parse_number(s):
    """Parse a string number with commas to float, else None."""
    try:
        return float(str(s).replace(",", "").replace("₹", "").strip())
    except:
        return None

def parse_date(date_str):
    """Parse many date forms and return dd/mm/YYYY if possible, else return original trimmed string."""
    if not date_str:
        return "N/A"
    s = str(date_str).strip().replace(",", "")
    # direct dd/mm/YYYY
    m = re.search(r"(\d{2}/\d{2}/\d{4})", s)
    if m:
        return m.group(1)
    # try many textual formats
    for fmt in ["%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%d %m %Y", "%d %b, %Y", "%B %d,%Y", "%b %d,%Y"]:
        try:
            return datetime.strptime(s, fmt).strftime("%d/%m/%Y")
        except:
            pass
    # fallback: try to extract month-day-year like "August 5 2025"
    m = re.search(r"([A-Za-z]{3,9})\s+(\d{1,2})\s+(\d{4})", s)
    if m:
        mon, day, yr = m.groups()
        try:
            return datetime.strptime(f"{mon} {day} {yr}", "%B %d %Y").strftime("%d/%m/%Y")
        except:
            try:
                return datetime.strptime(f"{mon} {day} {yr}", "%b %d %Y").strftime("%d/%m/%Y")
            except:
                pass
    return date_str

# ------------------------------
# Vendor category fuzzy match
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
# Transactions extraction (PDF)
# Accepts pdf_bytes (bytes) or path-like; returns DataFrame
# ==============================
def extract_transactions_from_pdf(pdf_source, account_name):
    """
    pdf_source: bytes or file-like object or path
    returns DataFrame with columns Date, Merchant, Amount, Type, Account
    """
    transactions = []
    # open pdf consistently from bytes
    if isinstance(pdf_source, (bytes, bytearray)):
        pdf_bytes = BytesIO(pdf_source)
    else:
        # streamlit UploadedFile behaves like a file-like; ensure we seek to start
        try:
            pdf_source.seek(0)
        except Exception:
            pass
        pdf_bytes = pdf_source

    with pdfplumber.open(pdf_bytes) as pdf:
        # detect if american express style (helps parsing)
        lower_text_sample = ""
        for p in pdf.pages[:3]:
            t = p.extract_text() or ""
            lower_text_sample += t.lower() + "\n"

        is_amex = "american express" in lower_text_sample or "americanexpress" in lower_text_sample

        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text()
            if not text:
                continue
            lines = [l.strip() for l in text.split("\n") if l.strip()]

            if is_amex:
                # AMEX: lines often like "July 01 PAYMENT RECEIVED. THANK YOU 8,860.00 CR"
                for i, line in enumerate(lines):
                    # Many AMEX transaction lines begin with MonthName Day
                    m = re.match(r"([A-Za-z]{3,9}\s+\d{1,2})\s+(.+?)\s+([\d,]+\.\d{2})\s*(CR|Cr|cr)?$", line)
                    if m:
                        date_part, merchant, amount, cr = m.groups()
                        date = parse_date(date_part + " 2025")  # year may not be present; keep as-is if wrong
                        try:
                            amt = float(amount.replace(",", ""))
                        except:
                            continue
                        if cr and cr.lower().startswith("cr"):
                            amt = -round(amt, 2)
                            tr_type = "CR"
                        else:
                            tr_type = "DR"
                        transactions.append([date, merchant.strip(), round(amt, 2), tr_type, account_name])
                    else:
                        # fallback pattern: trailing CR/DR token
                        m2 = re.match(r".+?([\d,]+\.\d{2})\s*(CR|Cr|cr|DR|Dr|dr)?$", line)
                        if m2:
                            amount = m2.group(1)
                            cr = m2.group(2)
                            try:
                                amt = float(amount.replace(",", ""))
                            except:
                                continue
                            tr_type = "CR" if cr and cr.lower().startswith("cr") else "DR"
                            # attempt to extract date from beginning of line
                            date_search = re.match(r"([A-Za-z]{3,9}\s+\d{1,2}).+?", line)
                            date = parse_date(date_search.group(1) + " 2025") if date_search else "N/A"
                            # merchant = middle portion
                            merchant = line
                            transactions.append([date, merchant.strip(), round(-amt,2) if tr_type=="CR" else round(amt,2), tr_type, account_name])
            else:
                # Generic: look for common date formats dd/mm/yyyy or dd-mm-yyyy at start
                for line in lines:
                    # pattern: 15/08/2025   AMAZON INDIA   1,234.56 DR
                    match = re.match(r"(\d{2}/\d{2}/\d{4})(?:\s+\d{2}:\d{2}:\d{2})?\s+(.+?)\s+([\d,]+\.\d{2})\s*(CR|Dr|DR|Cr)?\s*$", line)
                    if match:
                        date, merchant, amount, drcr = match.groups()
                        try:
                            amt = float(amount.replace(",", ""))
                        except:
                            continue
                        if drcr and drcr.strip().lower().startswith("cr"):
                            amt = -round(amt, 2)
                            tr_type = "CR"
                        else:
                            tr_type = "DR"
                        transactions.append([parse_date(date), merchant.strip(), round(amt,2), tr_type, account_name])
                    else:
                        # Try extraction from table cells
                        # Look for lines with UPI / CODE and "INR" followed by amount and DR/CR
                        m2 = re.search(r"(.+?)\s+(INR|Rs\.?|Rs)\s*([\d,]+\.\d{2})\s*(CR|Cr|cr|DR|Dr|dr)?$", line)
                        if m2:
                            merchant = m2.group(1)
                            amount = m2.group(3)
                            drcr = m2.group(4)
                            try:
                                amt = float(amount.replace(",", ""))
                            except:
                                continue
                            if drcr and drcr.lower().startswith("cr"):
                                amt = -round(amt,2)
                                tr_type = "CR"
                            else:
                                tr_type = "DR"
                            # try to find a date in the same line or previous token
                            date_search = re.search(r"(\d{2}/\d{2}/\d{4})", line)
                            if date_search:
                                date = parse_date(date_search.group(1))
                            else:
                                date = "N/A"
                            transactions.append([date, merchant.strip(), round(amt,2), tr_type, account_name])

            st.info(f"📄 Page {page_num}: extracted {len(transactions)} rows so far")

    df = pd.DataFrame(transactions, columns=["Date", "Merchant", "Amount", "Type", "Account"])
    # Normalize amounts: amounts where CR are negative already; ensure rounding
    if not df.empty:
        df["Amount"] = df["Amount"].astype(float).round(2)
    return df

# ==============================
# Summary extraction (robust for many card vendors)
# Goal: return dict with keys: Statement date, Payment due date, Minimum payable, Total Dues
# Accepts pdf_source bytes or file-like
# ==============================
def extract_summary_from_pdf(pdf_source):
    """
    Robust heuristic-based extraction:
    - Targeted parsing for American Express, HDFC, BOB (Bank of Baroda), ICICI (and generic).
    - Returns dict:
        {
            "Statement date": "dd/mm/YYYY" or "N/A",
            "Payment due date": "dd/mm/YYYY" or "N/A",
            "Minimum payable": "₹x,xxx.xx" or "N/A",
            "Total Dues": "₹x,xxx.xx" or "N/A"
        }
    """
    # default
    result = {
        "Statement date": "N/A",
        "Payment due date": "N/A",
        "Minimum payable": "N/A",
        "Total Dues": "N/A"
    }

    # prepare text_all
    if isinstance(pdf_source, (bytes, bytearray)):
        pdf_bytes = BytesIO(pdf_source)
    else:
        try:
            pdf_source.seek(0)
        except Exception:
            pass
        pdf_bytes = pdf_source

    try:
        with pdfplumber.open(pdf_bytes) as pdf:
            pages_to_read = min(5, len(pdf.pages))
            pages_text = []
            lines = []
            for i in range(pages_to_read):
                txt = pdf.pages[i].extract_text() or ""
                pages_text.append(txt)
                lines.extend([l.strip() for l in txt.split("\n") if l.strip()])

            text_all = "\n".join(pages_text)
            text_all_norm = re.sub(r"\s+", " ", text_all).strip()
            low = text_all.lower()

            # detect vendor hints
            is_amex = "american express" in low or "americanexpress" in low
            is_hdfc = "hdfc" in low
            is_bob = "bobcard" in low or "bank of baroda" in low or "bob card" in low or "b o bcard" in low
            is_icici = "icici" in low

            # 1) Try easy label-based regex first (generic)
            # Statement date
            m = re.search(r"statement date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})", text_all, re.IGNORECASE)
            if m:
                result["Statement date"] = parse_date(m.group(1))
            else:
                # other patterns: "Statement Period From June 19 to July 18, 2025" or "19 Jun - 18 Jul"
                m2 = re.search(r"statement period\s*from\s*([A-Za-z0-9\s,]+?)\s*to\s*([A-Za-z0-9\s,]+?\d{4})", text_all, re.IGNORECASE)
                if m2:
                    # take the end date (group 2)
                    result["Statement date"] = parse_date(m2.group(2))
                else:
                    # dd Month, YYYY To dd Month, YYYY pattern at top
                    m3 = re.search(r"(\d{1,2}\s+[A-Za-z]{3,9}\s*,?\s*\d{4})\s+to\s+(\d{1,2}\s+[A-Za-z]{3,9}\s*,?\s*\d{4})", text_all, re.IGNORECASE)
                    if m3:
                        # set statement date as end date
                        result["Statement date"] = parse_date(m3.group(2))

            # Payment due date generic
            m = re.search(r"payment due date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})", text_all, re.IGNORECASE)
            if m:
                result["Payment due date"] = parse_date(m.group(1))
            else:
                # look for "Due by August 5 2025" / "Due by August 5, 2025"
                m2 = re.search(r"due by\s*([A-Za-z]{3,9}\s+\d{1,2}\,?\s*\d{4})", text_all, re.IGNORECASE)
                if m2:
                    result["Payment due date"] = parse_date(m2.group(1))

            # Minimum payable generic
            m = re.search(r"minimum (?:amount )?due\s*[:\-]?\s*(?:rs\.?|rs|₹)?\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE)
            if m:
                result["Minimum payable"] = fmt_num(m.group(1))
            else:
                m2 = re.search(r"minimum payment\s*[:\-]?\s*(?:rs\.?|rs|₹)?\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE)
                if m2:
                    result["Minimum payable"] = fmt_num(m2.group(1))

            # Total Dues generic
            m = re.search(r"(?:total dues|total due|total amount due|closing balance)\s*[:\-]?\s*(?:rs\.?|rs|₹)?\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE)
            if m:
                result["Total Dues"] = fmt_num(m.group(1))
            else:
                # sometimes "Closing Balance Rs = 34,899.91" or pattern with '='
                m2 = re.search(r"\=\s*([\d,]+\.\d{2})\s*(?:[\s\D]{0,10})(?:minimum|min|due)?", text_all, re.IGNORECASE)
                if m2:
                    # choose as total dues if not set
                    if result["Total Dues"] == "N/A":
                        result["Total Dues"] = fmt_num(m2.group(1))

            # 2) Bank-specific heuristics (when generic didn't pick correct values)
            # AMEX: line with "Opening Balance Rs New Credits Rs New Debits Rs Closing Balance Rs Minimum Payment Rs"
            if is_amex:
                # try to find the arithmetic line containing '=' near "Opening Balance"
                m = re.search(r"opening balance[^\n]*?([\d,]+\.\d{2})\s*[\-\+]\s*([\d,]+\.\d{2})\s*[\+\-]?\s*([\d,]+\.\d{2})\s*=\s*([\d,]+\.\d{2})\s*([\d,]+\.\d{2})?", text_all, re.IGNORECASE)
                if m:
                    # groups: opening, credit, debit, closing, minimum(optional)
                    try:
                        opening = parse_number(m.group(1))
                        credit = parse_number(m.group(2))
                        debit = parse_number(m.group(3))
                        closing = parse_number(m.group(4))
                        minimum = parse_number(m.group(5)) if m.group(5) else None
                        if closing is not None:
                            result["Total Dues"] = fmt_num(closing)
                        if minimum is not None:
                            result["Minimum payable"] = fmt_num(minimum)
                    except Exception:
                        pass

                # fallback: explicit "Minimum Payment: Rs 1,745.00" and "Due by" near it
                mmin = re.search(r"minimum payment\s*[:\-]?\s*(?:rs\.?|rs|₹)?\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE)
                if mmin:
                    result["Minimum payable"] = fmt_num(mmin.group(1))
                mdue = re.search(r"due by\s*([A-Za-z]{3,9}\s+\d{1,2}\,?\s*\d{4})", text_all, re.IGNORECASE)
                if mdue:
                    result["Payment due date"] = parse_date(mdue.group(1))

                # credit summary block: "Credit Limit Rs Available Credit Limit Rs At July 18, 2025 470,000.00 435,100.09"
                mcred = re.search(r"at [A-Za-z0-9,\s]+?([\d,]+\.\d{2})\s+([\d,]+\.\d{2})", text_all, re.IGNORECASE)
                if mcred and result["Total Dues"] == "N/A":
                    # sometimes AMEX shows closing or available; keep only if applicable (no strong mapping)
                    pass

            # HDFC specific
            if is_hdfc:
                # often HDFC has the header and then a row: "Payment Due Date Total Dues Minimum Amount Due" followed by a row with date and 2 numbers
                for idx, ln in enumerate(lines):
                    lowln = ln.lower()
                    if "payment due date" in lowln and ("total dues" in lowln or "total due" in lowln or "minimum amount" in lowln):
                        # check next 1-2 lines for numbers
                        for j in range(idx, min(idx+3, len(lines))):
                            nums = re.findall(r"(\d{2}/\d{2}/\d{4})|([\d,]+\.\d{2})", lines[j])
                            # flatten
                            flat = [t[0] or t[1] for t in nums]
                            if len(flat) >= 3:
                                # first is date, then total dues and minimum (or date + total + min)
                                # Heuristic: if first token is date
                                if re.match(r"\d{2}/\d{2}/\d{4}", flat[0]):
                                    # Example order: date total min OR date min total -> we will choose based on magnitude (min should be <= total)
                                    cand = flat[1:3]
                                    vals = [parse_number(x) for x in cand]
                                    if vals[0] is not None and vals[1] is not None:
                                        # decide mapping: if second >= first then second is total, else first is total
                                        if vals[1] >= vals[0]:
                                            # first likely min, second likely total
                                            result["Payment due date"] = parse_date(flat[0])
                                            result["Minimum payable"] = fmt_num(vals[0])
                                            result["Total Dues"] = fmt_num(vals[1])
                                        else:
                                            result["Payment due date"] = parse_date(flat[0])
                                            result["Total Dues"] = fmt_num(vals[0])
                                            result["Minimum payable"] = fmt_num(vals[1])
                                        break
                                else:
                                    # if date not found in same line, maybe in previous line
                                    prev_date = None
                                    if idx > 0:
                                        dmatch = re.search(r"(\d{2}/\d{2}/\d{4})", lines[idx-1])
                                        if dmatch:
                                            prev_date = parse_date(dmatch.group(1))
                                    vals = [parse_number(x) for x in flat[:2]]
                                    if vals[0] is not None and vals[1] is not None:
                                        result["Payment due date"] = prev_date or result["Payment due date"]
                                        result["Total Dues"] = fmt_num(vals[0])
                                        result["Minimum payable"] = fmt_num(vals[1])
                                        break

                # fallback: simple search for Minimum Amount Due and Total Dues
                if result["Minimum payable"] == "N/A":
                    m = re.search(r"minimum amount due\s*[:\-]?\s*(?:rs\.?|rs|₹)?\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE)
                    if m:
                        result["Minimum payable"] = fmt_num(m.group(1))
                if result["Total Dues"] == "N/A":
                    m = re.search(r"total dues\s*[:\-]?\s*(?:rs\.?|rs|₹)?\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE)
                    if m:
                        result["Total Dues"] = fmt_num(m.group(1))

            # BOB heuristics (Bank of Baroda)
            if is_bob:
                # Look for a pattern where a date is followed by two numbers and 'DR' token, e.g. "02/10/2025 1,409.42 28,188.33 DR"
                for ln in lines:
                    m = re.search(r"(\d{2}/\d{2}/\d{4})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+(?:DR|Dr|dr)\b", ln)
                    if m:
                        d = parse_date(m.group(1))
                        a1 = parse_number(m.group(2))
                        a2 = parse_number(m.group(3))
                        # choose mapping: smaller value is minimum, larger is total
                        if a1 is not None and a2 is not None:
                            if a1 <= a2:
                                result["Payment due date"] = d
                                result["Minimum payable"] = fmt_num(a1)
                                result["Total Dues"] = fmt_num(a2)
                            else:
                                result["Payment due date"] = d
                                result["Minimum payable"] = fmt_num(a2)
                                result["Total Dues"] = fmt_num(a1)
                            break

                # If not found, look for any "DR" occurrence with two numbers before it
                if result["Total Dues"] == "N/A":
                    m2 = re.search(r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+(?:DR|Dr|dr)\b", text_all)
                    if m2:
                        a1 = parse_number(m2.group(1)); a2 = parse_number(m2.group(2))
                        if a1 is not None and a2 is not None:
                            if a1 <= a2:
                                result["Minimum payable"] = fmt_num(a1)
                                result["Total Dues"] = fmt_num(a2)
                            else:
                                result["Minimum payable"] = fmt_num(a2)
                                result["Total Dues"] = fmt_num(a1)

                # BOB also prints credit limits as a list of four numbers; try to find any line with four numbers and map by heuristics
                m4 = re.search(r"((?:[\d,]+\.\d{2}\s+){3}[\d,]+\.\d{2})", text_all)
                if m4 and result["Total Dues"] == "N/A":
                    nums = re.findall(r"[\d,]+\.\d{2}", m4.group(1))
                    nums_f = [parse_number(x) for x in nums]
                    # try mapping: guess which is closing/total: pick the one that is followed by "DR" in another nearby chunk
                    # fallback: choose the smallest non-zero as minimum and one in middle as total (heuristic)
                    if nums_f and len(nums_f) >= 4:
                        possible_total = max(nums_f)
                        possible_min = min(nums_f)
                        result["Total Dues"] = fmt_num(possible_total)
                        result["Minimum payable"] = fmt_num(possible_min)

            # Generic fallback: sometimes numbers exist in table rows with exactly two numbers (total and min)
            if result["Total Dues"] == "N/A" or result["Minimum payable"] == "N/A" or result["Statement date"] == "N/A":
                # scan for lines that contain both a date and two monetary numbers
                for ln in lines[:10]:
                    # check within first few lines of each file: statement header area often there
                    m = re.search(r"(\d{2}/\d{2}/\d{4}).{0,40}([\d,]+\.\d{2}).{0,40}([\d,]+\.\d{2})", ln)
                    if m:
                        d = parse_date(m.group(1))
                        v1 = parse_number(m.group(2)); v2 = parse_number(m.group(3))
                        if result["Statement date"] == "N/A":
                            result["Statement date"] = d
                        if result["Minimum payable"] == "N/A" or result["Total Dues"] == "N/A":
                            # assign by magnitude
                            if v1 is not None and v2 is not None:
                                if v1 <= v2:
                                    result["Minimum payable"] = fmt_num(v1)
                                    result["Total Dues"] = fmt_num(v2)
                                else:
                                    result["Minimum payable"] = fmt_num(v2)
                                    result["Total Dues"] = fmt_num(v1)
                        break

            # Final safety: ensure Minimum payable and Total Dues are present by searching the whole text for keywords near numbers again
            if result["Minimum payable"] == "N/A":
                m = re.search(r"minimum\s*(?:amount)?\s*(?:due|payable)?\s*[:\-]?\s*(?:rs\.?|rs|₹)?\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE)
                if m:
                    result["Minimum payable"] = fmt_num(m.group(1))

            if result["Total Dues"] == "N/A":
                m = re.search(r"(?:total\s+due|total\s+dues|closing\s+balance|total\s+amount\s+due)\s*[:\-]?\s*(?:rs\.?|rs|₹)?\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE)
                if m:
                    result["Total Dues"] = fmt_num(m.group(1))

            # Some statements show "Minimum Payment: Rs 1,745.00 Due by 05/08/2025" extract both
            m = re.search(r"minimum\s*payment\s*[:\-]?\s*(?:rs\.?|rs|₹)?\s*([\d,]+\.\d{2}).{0,40}?due\s*by\s*([A-Za-z0-9,\s]+?\d{4})", text_all, re.IGNORECASE)
            if m:
                result["Minimum payable"] = fmt_num(m.group(1))
                result["Payment due date"] = parse_date(m.group(2))

            # Final normalize: ensure statement date in dd/mm/YYYY if possible (we used parse_date earlier)
            if result["Statement date"] != "N/A":
                result["Statement date"] = parse_date(result["Statement date"])

            # return only the 4 requested keys
            return {
                "Statement date": result.get("Statement date", "N/A"),
                "Payment due date": result.get("Payment due date", "N/A"),
                "Minimum payable": result.get("Minimum payable", "N/A"),
                "Total Dues": result.get("Total Dues", "N/A")
            }

    except Exception as e:
        st.error(f"⚠️ Error extracting summary: {e}")
        return {
            "Statement date": "N/A",
            "Payment due date": "N/A",
            "Minimum payable": "N/A",
            "Total Dues": "N/A"
        }

    return {
        "Statement date": "N/A",
        "Payment due date": "N/A",
        "Minimum payable": "N/A",
        "Total Dues": "N/A"
    }

# ==============================
# Excel / CSV extractors
# ==============================
def extract_transactions_from_excel(file_bytes, account_name):
    df = pd.read_excel(BytesIO(file_bytes))
    return normalize_dataframe(df, account_name)

def extract_transactions_from_csv(file_bytes, account_name):
    df = pd.read_csv(BytesIO(file_bytes))
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

# ==============================
# Categorize & other helpers
# ==============================
def categorize_expenses(df):
    df["Category"] = df["Merchant"].apply(get_category)
    return df

def add_new_vendor(merchant, category):
    global vendor_map
    new_row = pd.DataFrame([[merchant.lower(), category]], columns=["merchant", "category"])
    vendor_map = pd.concat([vendor_map, new_row], ignore_index=True)
    vendor_map.drop_duplicates(subset=["merchant"], keep="last", inplace=True)
    vendor_map.to_csv(VENDOR_FILE, index=False)

def convert_df_to_csv(df):
    df["Amount"] = df["Amount"].round(2)
    return df.to_csv(index=False).encode("utf-8")

def convert_df_to_excel(df):
    df["Amount"] = df["Amount"].round(2)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Expenses", float_format="%.2f")
    return output.getvalue()

# ==============================
# UI: Display summary neatly
# ==============================
def display_summary(summary, account_name):
    st.subheader(f"📋 Statement Summary — {account_name}")
    def card_html(label, value, color, icon=""):
        return f"""
        <div style="background:{color};padding:10px;border-radius:10px;margin:6px;text-align:center;color:white;font-weight:600;">
            <div style="font-size:14px;">{icon} {label}</div>
            <div style="font-size:20px;margin-top:8px;">{value}</div>
        </div>
        """
    col1, col2, col3, col4 = st.columns([2,2,2,2])
    with col1:
        st.markdown(card_html("📅 Statement date", summary.get("Statement date","N/A"), "#0275d8"), unsafe_allow_html=True)
    with col2:
        st.markdown(card_html("⏰ Payment due date", summary.get("Payment due date","N/A"), "#f0ad4e"), unsafe_allow_html=True)
    with col3:
        st.markdown(card_html("⚠️ Minimum payable", summary.get("Minimum payable","N/A"), "#f39c12"), unsafe_allow_html=True)
    with col4:
        st.markdown(card_html("💰 Total Dues", summary.get("Total Dues","N/A"), "#5cb85c"), unsafe_allow_html=True)

# ==============================
# Streamlit App
# ==============================
st.title("💳 Credit Card Expense Analyzer — Summary + Transactions")
st.write("Upload unlocked Credit Card statements (PDF/CSV/XLSX). The app extracts transactions and a 4-field summary: Statement date, Payment due date, Minimum payable, Total Dues.")

uploaded_files = st.file_uploader("Upload Statements", type=["pdf","csv","xlsx"], accept_multiple_files=True)

if uploaded_files:
    all_data = pd.DataFrame(columns=["Date","Merchant","Amount","Type","Account"])
    for uploaded_file in uploaded_files:
        # Read once (bytes) so we can open multiple times
        try:
            pdf_bytes = uploaded_file.read()
        except Exception:
            uploaded_file.seek(0)
            pdf_bytes = uploaded_file.read()

        account_name = st.text_input(f"Enter account name for {uploaded_file.name}", value=uploaded_file.name)

        if account_name:
            if uploaded_file.name.lower().endswith(".pdf"):
                # extract transactions and summary from bytes
                df = extract_transactions_from_pdf(pdf_bytes, account_name)
                summary = extract_summary_from_pdf(pdf_bytes)
                display_summary(summary, uploaded_file.name)
            elif uploaded_file.name.lower().endswith(".csv"):
                df = extract_transactions_from_csv(pdf_bytes, account_name)
                summary = {
                    "Statement date": "N/A",
                    "Payment due date": "N/A",
                    "Minimum payable": "N/A",
                    "Total Dues": "N/A"
                }
                display_summary(summary, uploaded_file.name)
            elif uploaded_file.name.lower().endswith(".xlsx"):
                df = extract_transactions_from_excel(pdf_bytes, account_name)
                summary = {
                    "Statement date": "N/A",
                    "Payment due date": "N/A",
                    "Minimum payable": "N/A",
                    "Total Dues": "N/A"
                }
                display_summary(summary, uploaded_file.name)
            else:
                df = pd.DataFrame()

            all_data = pd.concat([all_data, df], ignore_index=True)

    if not all_data.empty:
        all_data = categorize_expenses(all_data)
        all_data["Amount"] = all_data["Amount"].round(2)
        st.subheader("📑 Extracted Transactions")
        st.dataframe(all_data.style.format({"Amount":"{:,.2f}"}))

        # Unknown merchants handling
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
        st.write("🏦 **Top 10 Merchants**")
        top_merchants = expenses.groupby("Merchant")["Amount"].sum().sort_values(ascending=False).head(10)
        st.dataframe(top_merchants.apply(lambda x: f"{x:,.2f}"))

        # Export
        csv_data = convert_df_to_csv(all_data)
        excel_data = convert_df_to_excel(all_data)
        st.download_button("⬇️ Download as CSV", csv_data, file_name="expenses_all.csv", mime="text/csv")
        st.download_button("⬇️ Download as Excel", excel_data, file_name="expenses_all.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

else:
    st.info("✅ App loaded successfully — upload your statements to get started.")
