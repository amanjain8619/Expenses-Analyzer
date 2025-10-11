# app.py
import streamlit as st
import pdfplumber
import re
from datetime import datetime
import math

st.set_page_config(layout="wide")
st.title("📋 Credit Card Statement — Summary Extractor")
st.write("Uploads: unlocked PDF statements. Extracts 4 summary fields: Statement date, Payment due date, Minimum payable, Total Dues.")

# ------------------------------
# Utilities
# ------------------------------
def clean_num_str(s):
    if s is None: 
        return None
    s = str(s).strip()
    s = s.replace("₹", "").replace("Rs.", "").replace("Rs", "").replace(",", "").replace("INR", "")
    s = s.replace(" ", "")
    # remove trailing non-numeric
    m = re.search(r"(-?\d+\.?\d*)", s)
    return m.group(1) if m else None

def to_float(s):
    try:
        return float(clean_num_str(s))
    except:
        return None

def fmt_amount(v):
    try:
        return f"₹{float(v):,.2f}"
    except:
        return "N/A"

def parse_date_flexible(s):
    if not s:
        return None
    s = s.strip()
    # try dd/mm/yyyy or dd-mm-yyyy
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%b-%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(s.replace(",", ""), fmt).strftime("%d/%m/%Y")
        except:
            pass
    # try patterns like "14 Aug, 2025" or "Aug 14, 2025"
    m = re.search(r"(\d{1,2})\s*([A-Za-z]{3,9})[, ]*\s*(\d{4})", s)
    if m:
        try:
            d = int(m.group(1)); mon = m.group(2); y = int(m.group(3))
            return datetime.strptime(f"{d} {mon} {y}", "%d %b %Y").strftime("%d/%m/%Y")
        except:
            try:
                return datetime.strptime(f"{d} {mon} {y}", "%d %B %Y").strftime("%d/%m/%Y")
            except:
                pass
    # try "14 Aug 2025 To 13 Sep 2025" -> take first or last date
    m2 = re.search(r"(\d{1,2}\s+[A-Za-z]{3,9}\s*,?\s*\d{4})", s)
    if m2:
        try:
            return parse_date_flexible(m2.group(1))
        except:
            pass
    # numeric fallback like 15/08/2025 contained inside
    m3 = re.search(r"(\d{2}/\d{2}/\d{4})", s)
    if m3:
        return m3.group(1)
    return None

# ------------------------------
# Patterns for labels (many variants)
# ------------------------------
LABEL_PATTERNS = {
    "statement_date": [
        r"Statement Date\s*[:\-]?\s*([^\n]+)",
        r"Statement Period\s*[:\-]?\s*([^\n]+To[^\n]+)",
        r"Statement Period\s*[^\n]*To\s*([A-Za-z0-9 ,]+ \d{4})",
        r"Statement Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})"
    ],
    "payment_due": [
        r"Payment Due Date\s*[:\-]?\s*([^\n]+)",
        r"Due Date\s*[:\-]?\s*([^\n]+)",
        r"Payment Due\s*[:\-]?\s*([^\n]+)",
        r"Due by\s*([A-Za-z]{3,9}\s*\d{1,2},?\s*\d{4})",
    ],
    "minimum": [
        r"Minimum Amount Due\s*[:\-]?\s*([^\n]+)",
        r"Minimum Due\s*[:\-]?\s*([^\n]+)",
        r"Minimum Payment\s*[:\-]?\s*([^\n]+)",
        r"Min Amount Due\s*[:\-]?\s*([^\n]+)",
        r"Minimum\s*[:\-]?\s*([^\n]+)"
    ],
    "total_due": [
        r"Total Dues\s*[:\-]?\s*([^\n]+)",
        r"Total Due\s*[:\-]?\s*([^\n]+)",
        r"Total Amount Due\s*[:\-]?\s*([^\n]+)",
        r"Closing Balance\s*[:\-]?\s*([^\n]+)",
        r"Amount Due\s*[:\-]?\s*([^\n]+)",
        r"Balance Due\s*[:\-]?\s*([^\n]+)"
    ]
}

# ------------------------------
# Summary extraction function
# ------------------------------
def extract_summary_from_pdf(pdf_path, debug=False):
    """
    Returns a dict with keys:
    - Statement date (dd/mm/YYYY or 'N/A')
    - Payment due date
    - Minimum payable
    - Total Dues
    """
    text_all = ""
    candidate_numeric_clusters = []  # list of tuples (page_index, raw_line, [numbers_as_floats])
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages = pdf.pages
            # scan first 4 pages (most statements keep summary here)
            for pidx in range(min(4, len(pages))):
                page = pages[pidx]
                txt = page.extract_text() or ""
                text_all += txt + "\n"
                # collect lines with currency looking numbers
                for line in txt.split("\n"):
                    # skip empty
                    if not line.strip():
                        continue
                    nums = re.findall(r"₹?\s?[\d,]+\.\d{2}", line)  # captures ₹ and commas
                    if not nums:
                        # also capture plain numbers like 15000.00 or 15000
                        nums = re.findall(r"[\d,]+\.\d{2}", line)
                    if nums:
                        clean_nums = []
                        for n in nums:
                            s = clean_num_str(n)
                            f = to_float(s)
                            if f is not None:
                                clean_nums.append(f)
                        if clean_nums:
                            candidate_numeric_clusters.append((pidx, line.strip(), clean_nums))
    except Exception as e:
        if debug:
            st.error(f"Error reading PDF: {e}")
        return {"Statement date":"N/A","Payment due date":"N/A","Minimum payable":"N/A","Total Dues":"N/A"}

    # Normalize full text (single string)
    text_norm = re.sub(r"\s+", " ", text_all).strip()

    # 1) Direct label match attempts for each field
    extracted = {}
    for key, patterns in LABEL_PATTERNS.items():
        extracted[key] = None
        for pat in patterns:
            m = re.search(pat, text_all, re.IGNORECASE)
            if m:
                # take the first capturing group if present
                cand = m.group(1).strip()
                if key == "statement_date":
                    sd = parse_date_flexible(cand)
                    if sd:
                        extracted[key] = sd
                        break
                    else:
                        # try if cand contains a date like "14 Aug, 2025 To 13 Sep, 2025" -> take end date or start
                        m2 = re.search(r"(\d{1,2}\s+[A-Za-z]{3,9},?\s*\d{4})", cand)
                        if m2:
                            sd = parse_date_flexible(m2.group(1))
                            if sd:
                                extracted[key] = sd
                                break
                elif key == "payment_due":
                    pdv = parse_date_flexible(cand)
                    if pdv:
                        extracted[key] = pdv
                        break
                elif key in ("minimum", "total_due"):
                    # extract numeric from cand
                    n = clean_num_str(cand)
                    f = to_float(n)
                    if f is not None:
                        extracted[key] = fmt_amount(f)
                        break
        # end patterns loop

    # 2) If direct label didn't find, try keyword-near-number heuristics
    # For total_due: search for text near keywords "closing", "balance", "total due", "amount due"
    if not extracted.get("total_due"):
        # find occurrences where one of keywords and a number are in same small window
        keywords_total = ["closing balance", "total due", "amount due", "balance due", "total dues", "closing balance (in rs)"]
        for kw in keywords_total:
            # try to find "keyword ... number" in text_norm
            m = re.search(rf"{kw}.*?([\d,]+\.\d{{2}})", text_norm, re.IGNORECASE)
            if m:
                f = to_float(m.group(1))
                if f is not None:
                    extracted["total_due"] = fmt_amount(f)
                    break
            m2 = re.search(rf"([\d,]+\.\d{{2}}).*?{kw}", text_norm, re.IGNORECASE)
            if m2:
                f = to_float(m2.group(1))
                if f is not None:
                    extracted["total_due"] = fmt_amount(f)
                    break

    # For minimum payable
    if not extracted.get("minimum"):
        keywords_min = ["minimum amount due", "minimum due", "min amount due", "minimum payment"]
        for kw in keywords_min:
            m = re.search(rf"{kw}.*?([\d,]+\.\d{{2}})", text_norm, re.IGNORECASE)
            if m:
                f = to_float(m.group(1))
                if f is not None:
                    extracted["minimum"] = fmt_amount(f)
                    break
            m2 = re.search(rf"([\d,]+\.\d{{2}}).*?{kw}", text_norm, re.IGNORECASE)
            if m2:
                f = to_float(m2.group(1))
                if f is not None:
                    extracted["minimum"] = fmt_amount(f)
                    break

    # For payment due date fallback: look for "Payment Due Date", "Due by", "Pay by", etc.
    if not extracted.get("payment_due"):
        m = re.search(r"(Payment Due Date|Due Date|Due by|Pay by)\s*[:\-]?\s*([A-Za-z0-9 ,/]{6,30})", text_all, re.IGNORECASE)
        if m:
            cand = m.group(2).strip()
            dd = parse_date_flexible(cand)
            if dd:
                extracted["payment_due"] = dd

    # For statement date fallback: check typical header patterns "Statement Date: dd/mm/yyyy" or "For period 14 Aug, 2025 To 13 Sep, 2025"
    if not extracted.get("statement_date"):
        m = re.search(r"(Statement Date|Statement Period|Statement for)\s*[:\-]?\s*([A-Za-z0-9 ,/]{6,40})", text_all, re.IGNORECASE)
        if m:
            cand = m.group(2).strip()
            sd = parse_date_flexible(cand)
            if sd:
                extracted["statement_date"] = sd
            else:
                # try to find a date inside cand
                dd = re.search(r"(\d{2}/\d{2}/\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s*,?\s*\d{4})", cand)
                if dd:
                    parsed = parse_date_flexible(dd.group(1))
                    if parsed:
                        extracted["statement_date"] = parsed

    # 3) When still missing fields: use numeric cluster heuristics
    # Build a flat list of candidate numbers with context
    flat_candidates = []
    for pidx, raw, nums in candidate_numeric_clusters:
        for n in nums:
            flat_candidates.append({"page": pidx, "raw": raw, "value": float(n)})

    # If total_due still missing, try to pick closing/total number heuristically:
    if not extracted.get("total_due") and flat_candidates:
        # Strategy:
        # - If any cluster line contains keywords 'closing'/'total' -> pick the largest numeric from that line
        for entry in flat_candidates:
            low = entry["raw"].lower()
            if any(k in low for k in ["closing", "closing balance", "total due", "amount due", "balance due"]):
                # extract highest number from that raw
                nums = re.findall(r"[\d,]+\.\d{2}", entry["raw"])
                if nums:
                    vals = [to_float(clean_num_str(n)) for n in nums]
                    vals = [v for v in vals if v is not None]
                    if vals:
                        val = max(vals)
                        extracted["total_due"] = fmt_amount(val)
                        break
        # otherwise as fallback: choose the largest number seen in the first pages (commonly closing/limit)
        if not extracted.get("total_due"):
            vals = [c["value"] for c in flat_candidates]
            if vals:
                # often closing total is not the maximum (credit limit could be higher) - but closing often near mid-high
                # choose median-high: 75th percentile
                vals_sorted = sorted(vals)
                idx = int(len(vals_sorted) * 0.75)
                idx = min(max(idx, 0), len(vals_sorted)-1)
                candidate = vals_sorted[idx]
                extracted["total_due"] = fmt_amount(candidate)

    # If minimum missing: try to find smallest positive amount less than total_due (if total known)
    if not extracted.get("minimum") and flat_candidates:
        # collect unique numeric values (positive)
        all_vals = sorted(set([c["value"] for c in flat_candidates if c["value"] >= 0]))
        # try label proximity first
        min_found = None
        for entry in flat_candidates:
            low = entry["raw"].lower()
            if any(k in low for k in ["minimum", "min amount", "min due", "minimum payable"]):
                min_found = entry["value"]
                break
        if min_found is not None:
            extracted["minimum"] = fmt_amount(min_found)
        else:
            # if total known, pick number significantly smaller than total (<= total)
            total_val = to_float(clean_num_str(extracted.get("total_due"))) if extracted.get("total_due") else None
            candidate = None
            if total_val:
                # find largest value smaller than total but commonly much smaller
                smaller = [v for v in all_vals if v <= total_val + 0.0001]
                if smaller:
                    # pick the maximum of the smaller ones that is substantially smaller (not equal)
                    # or prefer small ones (< total/2)
                    less_half = [v for v in smaller if v <= total_val * 0.5]
                    if less_half:
                        candidate = min(less_half)  # minimum payable tends to be relatively small
                    else:
                        candidate = min(smaller)
            # fallback: pick smallest positive numeric on page (could be fee)
            if candidate is None and all_vals:
                candidate = all_vals[0]
            if candidate is not None:
                extracted["minimum"] = fmt_amount(candidate)

    # Final normalization: ensure keys present
    out = {
        "Statement date": extracted.get("statement_date") or "N/A",
        "Payment due date": extracted.get("payment_due") or "N/A",
        "Minimum payable": extracted.get("minimum") or "N/A",
        "Total Dues": extracted.get("total_due") or "N/A"
    }

    if debug:
        st.markdown("#### Debug — candidate numeric clusters (first 10 shown):")
        import pandas as pd
        dbg = []
        for pidx, raw, nums in candidate_numeric_clusters[:20]:
            dbg.append({"page":pidx, "raw": raw, "numbers": nums})
        st.dataframe(pd.DataFrame(dbg))
        st.write("Direct regex extracted (intermediate):", extracted)

    return out

# ------------------------------
# Streamlit UI
# ------------------------------
st.sidebar.header("Options")
debug_mode = st.sidebar.checkbox("Show debug candidates", value=False)

uploaded = st.file_uploader("Upload one or more unlocked PDF credit card statements", type=["pdf"], accept_multiple_files=True)
if uploaded:
    for f in uploaded:
        st.markdown(f"### 📄 {f.name}")
        with st.spinner("Extracting summary..."):
            summary = extract_summary_from_pdf(f, debug=debug_mode)
        # display summary cards (compact)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("📅 Statement date", summary["Statement date"])
        c2.metric("⏰ Payment due date", summary["Payment due date"])
        c3.metric("⚠️ Minimum payable", summary["Minimum payable"])
        c4.metric("💰 Total Dues", summary["Total Dues"])
else:
    st.info("Please upload unlocked PDF credit card statements (HDFC/BOB/AMEX/ICICI etc.).")

# Footer hint
st.markdown("---")
st.markdown("Tips: If a statement is scanned (image PDF) the text extraction will fail — provide an unlocked (text) PDF. Use the debug toggle to see candidate numeric clusters if a value is wrong; then paste that cluster and I will tune heuristics.")
