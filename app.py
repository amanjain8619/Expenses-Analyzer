import streamlit as st
import pdfplumber
import re
import itertools
import math
from datetime import datetime
from io import BytesIO
import pandas as pd

st.set_page_config(layout="wide")
st.title("📋 Credit Card Statement — Robust Summary Extractor")
st.write("Extracts: Statement date, Payment due date, Minimum payable, Total Dues. Upload unlocked PDF CC statements (HDFC, BOB, AMEX, ICICI...).")

# -------------------------
# Utilities
# -------------------------
def clean_num_str(s):
    if s is None:
        return None
    s = str(s)
    s = s.replace("₹", "").replace("Rs.", "").replace("Rs", "").replace("INR", "")
    s = s.replace(",", "").strip()
    s = re.sub(r"\s*(DR|CR|Dr|Cr)$", "", s)
    m = re.search(r"-?\d+\.?\d*", s)
    return m.group(0) if m else None

def to_float(s):
    try:
        c = clean_num_str(s)
        return float(c) if c is not None else None
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
    s = str(s).strip().replace(",", "")
    fmts = ["%d/%m/%Y","%d-%m-%Y","%d %b %Y","%b %d %Y","%d %B %Y","%B %d %Y","%d %b, %Y","%d %B, %Y"]
    for f in fmts:
        try:
            return datetime.strptime(s, f).strftime("%d/%m/%Y")
        except:
            pass
    # search for common occurrences
    m = re.search(r"(\d{2}/\d{2}/\d{4})", s)
    if m:
        return m.group(1)
    m2 = re.search(r"(\d{1,2}\s+[A-Za-z]{3,9}\s*,?\s*\d{4})", s)
    if m2:
        try:
            return datetime.strptime(m2.group(1), "%d %b %Y").strftime("%d/%m/%Y")
        except:
            try:
                return datetime.strptime(m2.group(1), "%d %B %Y").strftime("%d/%m/%Y")
            except:
                pass
    return None

# -------------------------
# Bank detection
# -------------------------
def detect_bank(text):
    t = text.lower()
    if "hdfc" in t:
        return "HDFC"
    if "bobcard" in t or "bank of baroda" in t or "b o b" in t or "bob card" in t:
        return "BOB"
    if "american express" in t or "amex" in t:
        return "AMEX"
    if "icici" in t:
        return "ICICI"
    if "axis" in t:
        return "AXIS"
    return "GENERIC"

# -------------------------
# Primary extraction function (bank-specific heuristics + fallbacks)
# -------------------------
def extract_summary_from_pdf(pdf_path, debug=False):
    """
    Returns dict:
      Statement date, Payment due date, Minimum payable, Total Dues, Used Limit (if deducible)
    """
    text_all = ""
    lines = []
    numeric_clusters = []  # (page, raw_line, [float numbers])
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages = pdf.pages
            for pidx in range(min(6, len(pages))):
                page = pages[pidx]
                txt = page.extract_text() or ""
                text_all += txt + "\n"
                for line in txt.split("\n"):
                    l = line.strip()
                    if l:
                        lines.append(l)
                        found = re.findall(r"₹?\s*[\d,]+\.\d{2}", l)
                        if found:
                            nums = []
                            for n in found:
                                f = to_float(n)
                                if f is not None:
                                    nums.append(f)
                            if nums:
                                numeric_clusters.append((pidx, l, nums))
    except Exception as e:
        st.error(f"Error reading PDF: {e}")
        return {"Statement date":"N/A","Payment due date":"N/A","Minimum payable":"N/A","Total Dues":"N/A","Used Limit":"N/A"}

    bank = detect_bank(text_all)

    # initialize
    result = {"Statement date":"N/A","Payment due date":"N/A","Minimum payable":"N/A","Total Dues":"N/A","Used Limit":"N/A"}

    # ---------- 1) Statement date ----------
    # Try bank-specific patterns
    stmt_patterns = [
        r"Statement Date\s*[:\-]?\s*([A-Za-z0-9 ,/]+)",
        r"Statement Period\s*[:\-]?\s*From\s*[A-Za-z0-9 ,/]+\s*To\s*([A-Za-z0-9 ,/]+)",
        r"Statement for the period\s*([A-Za-z0-9 ,/]+)\s*To\s*([A-Za-z0-9 ,/]+)",
        r"Billing Date\s*[:\-]?\s*([A-Za-z0-9 ,/]+)",
        r"Period\s*[:\-]?\s*([A-Za-z0-9 ,/]+)\s*To\s*([A-Za-z0-9 ,/]+)"
    ]
    for pat in stmt_patterns:
        m = re.search(pat, text_all, re.IGNORECASE)
        if m:
            # prefer second group if exists (the end date)
            group = m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(1)
            sd = parse_date_flexible(group)
            if sd:
                result["Statement date"] = sd
                break

    # ---------- 2) Payment due date ----------
    due_patterns = [
        r"Payment Due Date\s*[:\-]?\s*([A-Za-z0-9 ,/]+)",
        r"Payment Due On\s*[:\-]?\s*([A-Za-z0-9 ,/]+)",
        r"Due Date\s*[:\-]?\s*([A-Za-z0-9 ,/]+)",
        r"Due by\s*([A-Za-z0-9 ,/]+)",
        r"Pay by\s*([A-Za-z0-9 ,/]+)",
        r"Payment Due\s*[:\-]?\s*([A-Za-z0-9 ,/]+)",
        r"Payment Due Date\s*([0-9]{2}/[0-9]{2}/[0-9]{4})"
    ]
    for pat in due_patterns:
        m = re.search(pat, text_all, re.IGNORECASE)
        if m:
            pdv = parse_date_flexible(m.group(1))
            if pdv:
                result["Payment due date"] = pdv
                break

    # ---------- 3) Direct keyword numeric search for Total & Minimum ----------
    # Give top priority to explicit lines
    for l in lines:
        low = l.lower()
        # total dues / closing / amount due
        if any(k in low for k in ["total amount due","total due","total dues","closing balance","amount due","balance due","closing balance in rs"]):
            m = re.search(r"₹?\s*([\d,]+\.\d{2})", l)
            if m:
                result["Total Dues"] = fmt_amount(to_float(m.group(1)))
        # minimum payable
        if any(k in low for k in ["minimum amount due","minimum due","min amount due","minimum payment","minimum payable","minimum payable amount"]):
            m = re.search(r"₹?\s*([\d,]+\.\d{2})", l)
            if m:
                result["Minimum payable"] = fmt_amount(to_float(m.group(1)))
        # credit limit / available credit -> for used limit
        if "credit limit" in low and "available" not in low:
            m = re.search(r"₹?\s*([\d,]+\.\d{2})", l)
            if m:
                result.setdefault("_credit_limit", fmt_amount(to_float(m.group(1))))
        if "available credit" in low or "available credit limit" in low or "available credit lim" in low:
            m = re.search(r"₹?\s*([\d,]+\.\d{2})", l)
            if m:
                result.setdefault("_available_credit", fmt_amount(to_float(m.group(1))))

    # ---------- 4) Bank-specific tailored parsing (if direct didn't find) ----------
    # HDFC: often has table labels Payment Due Date, Total Dues, Minimum Amount Due
    if bank == "HDFC":
        # try tighter patterns
        m_td = re.search(r"Total Dues\s*[:\-]?\s*₹?\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE)
        if m_td:
            result["Total Dues"] = fmt_amount(to_float(m_td.group(1)))
        m_min = re.search(r"Minimum Amount Due\s*[:\-]?\s*₹?\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE)
        if m_min:
            result["Minimum payable"] = fmt_amount(to_float(m_min.group(1)))
        # payment due
        m_pd = re.search(r"Payment Due Date\s*[:\-]?\s*([A-Za-z0-9 ,/]+)", text_all, re.IGNORECASE)
        if m_pd:
            d = parse_date_flexible(m_pd.group(1))
            if d:
                result["Payment due date"] = d

    # BOB: sometimes labels appear as 'Total Due' next to numbers and minimum appears with DR/CR nearby
    if bank == "BOB":
        m_td = re.search(r"Closing Balance\s*[:\-]?\s*₹?\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE) or \
               re.search(r"Total Dues\s*[:\-]?\s*₹?\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE) or \
               re.search(r"Total Due\s*[:\-]?\s*₹?\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE)
        if m_td:
            result["Total Dues"] = fmt_amount(to_float(m_td.group(1)))
        m_min = re.search(r"Minimum Amount Due\s*[:\-]?\s*₹?\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE)
        if m_min:
            result["Minimum payable"] = fmt_amount(to_float(m_min.group(1)))
        # Payment due might be labelled differently
        m_pd = re.search(r"Due Date\s*[:\-]?\s*([A-Za-z0-9 ,/]+)", text_all, re.IGNORECASE)
        if m_pd:
            d = parse_date_flexible(m_pd.group(1))
            if d:
                result["Payment due date"] = d

    # AMEX: 'Closing balance' and 'Minimum payment due' are common
    if bank == "AMEX":
        m_td = re.search(r"Closing Balance\s*[:\-]?\s*₹?\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE)
        if m_td:
            result["Total Dues"] = fmt_amount(to_float(m_td.group(1)))
        m_min = re.search(r"Minimum Payment Due\s*[:\-]?\s*₹?\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE) \
                or re.search(r"Minimum Amount Due\s*[:\-]?\s*₹?\s*([\d,]+\.\d{2})", text_all, re.IGNORECASE)
        if m_min:
            result["Minimum payable"] = fmt_amount(to_float(m_min.group(1)))
        m_pd = re.search(r"Payment Due Date\s*[:\-]?\s*([A-Za-z0-9 ,/]+)", text_all, re.IGNORECASE)
        if m_pd:
            d = parse_date_flexible(m_pd.group(1))
            if d:
                result["Payment due date"] = d

    # ---------- 5) If still missing, use numeric-cluster heuristics ----------
    # Build flat list of numeric values from numeric_clusters
    flat_vals = []
    for pidx, raw, nums in numeric_clusters:
        for n in nums:
            flat_vals.append((pidx, raw, n))
    # Try to pick explicit lines first (already attempted). If total is missing, choose the numeric closest to keywords
    if result["Total Dues"] == "N/A":
        # search nearby in text for keywords then a number
        patterns = ["closing balance", "total amount due", "total due", "total dues", "amount due", "balance due"]
        for p, raw, n in flat_vals:
            low = raw.lower()
            if any(k in low for k in patterns):
                result["Total Dues"] = fmt_amount(n)
                break
    # If still missing, pick 75th percentile of first-page numeric values (commonly closing)
    if result["Total Dues"] == "N/A" and flat_vals:
        vals_sorted = sorted({n for (_,_,n) in flat_vals})
        idx = max(0, min(len(vals_sorted)-1, int(len(vals_sorted)*0.75)))
        result["Total Dues"] = fmt_amount(vals_sorted[idx])

    # Minimum payable: try keyword first, then pick smallest positive <= total/2, else smallest positive
    if result["Minimum payable"] == "N/A" and flat_vals:
        total_val = to_float(result["Total Dues"]) if result["Total Dues"] != "N/A" else None
        # look for lines containing minimum keywords first
        found_min = None
        for p, raw, n in flat_vals:
            if any(k in raw.lower() for k in ["minimum", "min amount", "minimum payable", "minimum payment"]):
                found_min = n
                break
        if found_min:
            result["Minimum payable"] = fmt_amount(found_min)
        else:
            # choose smallest positive but <= total/2 if total exists
            candidates = sorted({n for (_,_,n) in flat_vals if n > 0})
            if candidates:
                if total_val:
                    smaller = [v for v in candidates if v <= total_val*0.5 + 1e-9]
                    if smaller:
                        result["Minimum payable"] = fmt_amount(smaller[0])
                    else:
                        # fallback to smallest
                        result["Minimum payable"] = fmt_amount(candidates[0])
                else:
                    result["Minimum payable"] = fmt_amount(candidates[0])

    # ---------- 6) Used Limit (derive from Credit Limit & Available Credit if present in text) ----------
    # try to capture credit limit / available credit from lines (we parsed earlier into internal fields)
    # attempt to find explicit values if not captured earlier
    if result["Used Limit"] == "N/A":
        # search for "Credit Limit" and "Available Credit" lines
        credit_limit = None
        available = None
        for l in lines:
            ll = l.lower()
            if "credit limit" in ll and "available" not in ll:
                m = re.search(r"₹?\s*([\d,]+\.\d{2})", l)
                if m:
                    credit_limit = to_float(m.group(1))
            if "available credit" in ll or "available limit" in ll or "available credit limit" in ll:
                m = re.search(r"₹?\s*([\d,]+\.\d{2})", l)
                if m:
                    available = to_float(m.group(1))
        if credit_limit and available is not None:
            used = credit_limit - available
            result["Used Limit"] = fmt_amount(used)
        else:
            # fallback: if Total Dues found, use that as Used Limit (some statements use used=closing)
            td = to_float(result["Total Dues"])
            if td is not None:
                result["Used Limit"] = fmt_amount(td)

    # ---------- 7) Final sanity corrections ----------
    # If minimum > total, swap or adjust (sometimes mis-identified)
    try:
        md = to_float(result["Minimum payable"])
        td = to_float(result["Total Dues"])
        if md is not None and td is not None and md > td:
            # very likely we mis-assigned; choose smallest positive number as minimum from clusters
            all_nums = sorted({n for (_,_,n) in flat_vals}) if flat_vals else []
            if all_nums:
                # pick smallest positive (not zero)
                candidate = next((v for v in all_nums if v > 0), None)
                if candidate:
                    result["Minimum payable"] = fmt_amount(candidate)
    except:
        pass

    # If statement date missing, try to pick first dd/mm/yyyy occurrence
    if result["Statement date"] == "N/A":
        m = re.search(r"(\d{2}/\d{2}/\d{4})", text_all)
        if m:
            result["Statement date"] = m.group(1)

    # Debug display
    if debug:
        st.subheader("Debug: Detected bank & candidate clusters")
        st.write("Detected bank:", bank)
        dbg_rows = []
        for pidx, raw, nums in numeric_clusters[:50]:
            dbg_rows.append({"page": pidx, "raw": raw, "numbers": nums})
        if dbg_rows:
            st.dataframe(pd.DataFrame(dbg_rows))
        st.markdown("**Extracted (intermediate)**")
        st.json(result)

    return result

# -------------------------
# Streamlit UI
# -------------------------
st.sidebar.header("Options")
debug_mode = st.sidebar.checkbox("Debug (show candidate numeric clusters)", value=False)

uploaded = st.file_uploader("Upload unlocked credit-card statement PDFs (multiple allowed)", type=["pdf"], accept_multiple_files=True)
if uploaded:
    all_summaries = []
    for f in uploaded:
        st.markdown(f"### 📄 {f.name}")
        with st.spinner("Extracting summary..."):
            summary = extract_summary_from_pdf(f, debug=debug_mode)
        col1, col2, col3, col4, col5 = st.columns([2,2,2,2,2])
        col1.metric("📅 Statement date", summary.get("Statement date", "N/A"))
        col2.metric("⏰ Payment due date", summary.get("Payment due date", "N/A"))
        col3.metric("⚠️ Minimum payable", summary.get("Minimum payable", "N/A"))
        col4.metric("💰 Total Dues", summary.get("Total Dues", "N/A"))
        col5.metric("🏦 Used Limit", summary.get("Used Limit", "N/A"))
        all_summaries.append({"file": f.name, **summary})

    # show a summary table
    st.subheader("All summaries")
    st.dataframe(pd.DataFrame(all_summaries))
else:
    st.info("Upload unlocked PDF credit-card statements (text PDFs). Use Debug to inspect candidate clusters if a number looks wrong.")

st.markdown("---")
st.markdown("If a particular field is still incorrect for a PDF, enable Debug, upload that file, then copy/paste the debug table (or screenshot) here — I'll adjust the one-line heuristic for that layout.")
