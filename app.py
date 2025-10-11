# app.py
import streamlit as st
import pdfplumber
import re
import itertools
import math
from datetime import datetime

st.set_page_config(layout="wide")
st.title("📋 CC Statement — Robust 4-field Summary Extractor")
st.write("Extracts: Statement date, Payment due date, Minimum payable, Total Dues. Upload unlocked PDF statements (HDFC, BOB, AMEX, ICICI... ).")

# --------------------------
# Helpers
# --------------------------
def clean_num_str(s):
    if s is None: 
        return None
    s = str(s)
    s = s.replace("₹", "").replace("Rs.", "").replace("Rs", "").replace("INR", "")
    s = s.replace(",", "").strip()
    # handle trailing DR/CR etc
    s = re.sub(r"\s*(?:DR|CR|Dr|Cr)\s*$", "", s)
    m = re.search(r"-?\d+\.?\d*", s)
    return m.group(0) if m else None

def to_float(s):
    try:
        cs = clean_num_str(s)
        return float(cs) if cs is not None else None
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
    # patterns like "14 Aug 2025 to 13 Sep 2025" -> take the last date if present
    m = re.findall(r"\d{1,2}\s+[A-Za-z]{3,9}\s*\d{4}|\d{2}/\d{2}/\d{4}", s)
    if m:
        # prefer last (end of period)
        for cand in reversed(m):
            try:
                return parse_date_flexible(cand)
            except:
                pass
    return None

# --------------------------
# Heuristics for mapping 4-number rows -> (Credit Limit, Available Credit, Total Due, Minimum)
# (Scoring adapted from earlier code)
# --------------------------
def score_primary_candidate(mapping, nums, raw=''):
    cl = mapping.get("Credit Limit")
    av = mapping.get("Available Credit")
    td = mapping.get("Total Due")
    md = mapping.get("Minimum Due")
    # require not None
    if any(x is None for x in [cl, av, td, md]):
        return -1e6
    # negative fail
    if any(x < -0.01 for x in [cl, av, td, md]):
        return -1e6
    score = 0.0
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
    # cl likely the max in row
    if abs(cl - max(nums)) < 1e-6:
        score += 1.5
    # prefer av <= cl
    if av <= cl:
        score += 0.5
    # total positive
    if td > 0:
        score += 0.5
    # cl reasonably large
    if cl >= 1000:
        score += 0.3
    # penalize if td >> cl
    if cl > 0 and td > cl * 3:
        score -= 2.0
    # prefer td <= cl
    if td <= cl + 1e-6:
        score += 1.0
    else:
        score -= 2.0
    # penalize md == td or av == md etc
    if abs(md - td) < 1e-6:
        score -= 3.0
    if abs(av - md) < 1e-6 or abs(av - td) < 1e-6:
        score -= 2.0
    # prefer md relatively small to td
    if td > 0 and md / td < 0.2:
        score += 1.5
    else:
        score -= 1.0
    # penalize if 'cash' in raw
    if 'cash' in raw.lower():
        score -= 1.0
    # slight boost for larger cl
    if cl > 0:
        score += math.log(cl + 1) / 20.0
    return score

def choose_best_primary_mapping(numeric_rows):
    """
    numeric_rows: list of tuples (nums_list floats, page_idx, table_idx, row_idx, raw_row_text)
    return (mapping_dict, index)
    """
    fields = ["Credit Limit", "Available Credit", "Total Due", "Minimum Due"]
    best = None
    best_score = -1e9
    best_idx = None
    best_perm = None
    for idx, (nums, p, t, r, raw) in enumerate(numeric_rows):
        # only consider rows with at least 4 numbers (if more, try first 4 combos)
        if len(nums) < 4:
            continue
        # map permutations of 4 distinct positions
        indices = list(range(len(nums)))
        # try limited permutations if too many
        perms = itertools.permutations(indices, 4)
        # to limit time, evaluate at most 200 perms per row
        count = 0
        for perm in perms:
            if count > 200:
                break
            cand = {}
            try:
                for i, field in enumerate(fields):
                    cand[field] = float(nums[perm[i]])
            except:
                continue
            s = score_primary_candidate(cand, nums, raw)
            if s > best_score:
                best_score = s
                best = cand.copy()
                best_idx = idx
                best_perm = perm
            count += 1
    if best is None:
        return None, None, None
    # format numbers as floats (not formatted string yet)
    return best, best_idx, best_perm

def map_secondary_rows(numeric_rows, exclude_index=None):
    fields_secondary = ["Total Payments", "Other Charges", "Total Purchases", "Previous Balance"]
    mapped = {}
    for idx, (nums, p, t, r, raw) in enumerate(numeric_rows):
        if idx == exclude_index:
            continue
        if len(nums) < 4:
            continue
        best_score = -1e9
        best_map = None
        for perm in itertools.permutations(range(len(nums)), 4):
            try:
                candidate = {fields_secondary[i]: float(nums[perm[i]]) for i in range(4)}
            except:
                continue
            # small heuristic scoring: non-negative and purchases >= payments
            tp = candidate["Total Payments"]; purch = candidate["Total Purchases"]; prev = candidate["Previous Balance"]
            if any(x < -0.01 for x in [tp, purch, prev]):
                continue
            score = 0
            if purch >= tp:
                score += 1.2
            if purch > 0:
                score += 0.5
            if prev >= 0:
                score += 0.3
            if score > best_score:
                best_score = score
                best_map = candidate
        if best_map:
            for k, v in best_map.items():
                if k not in mapped:
                    mapped[k] = v
    return mapped

# --------------------------
# Main summary extractor
# --------------------------
def extract_summary_from_pdf(pdf_path, debug=False):
    """
    Returns: dict with keys:
    'Statement date', 'Payment due date', 'Minimum payable', 'Total Dues'
    """
    # read text + tables from first few pages
    text_all = ""
    numeric_rows = []  # collects table/line rows with numeric tokens for permutation mapping
    line_number_clusters = []  # list of (page, raw_line, [numbers floats])
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages_to_scan = min(5, len(pdf.pages))
            for pidx in range(pages_to_scan):
                page = pdf.pages[pidx]
                txt = page.extract_text() or ""
                text_all += txt + "\n"
                # extract tables if present
                tables = page.extract_tables() or []
                for t_idx, table in enumerate(tables):
                    if not table:
                        continue
                    for ridx, row in enumerate(table):
                        # row is list of cells; join to raw text
                        raw = " ".join("" if c is None else str(c) for c in row).strip()
                        found = re.findall(r"[\d,]+\.\d{2}", raw)
                        if found:
                            nums = []
                            for n in found:
                                f = to_float(n)
                                if f is not None:
                                    nums.append(f)
                            if nums:
                                numeric_rows.append((nums, pidx, t_idx, ridx, raw))
                # also process raw lines to capture clusters in plain text
                for line in txt.split("\n"):
                    if not line.strip():
                        continue
                    found = re.findall(r"[\d,]+\.\d{2}", line)
                    if found:
                        nums = []
                        for n in found:
                            f = to_float(n)
                            if f is not None:
                                nums.append(f)
                        if nums:
                            line_number_clusters.append((pidx, line.strip(), nums))
                            # also add to numeric_rows (so lines are considered)
                            numeric_rows.append((nums, pidx, -1, -1, line.strip()))
    except Exception as e:
        st.error(f"PDF read error: {e}")
        return {"Statement date":"N/A","Payment due date":"N/A","Minimum payable":"N/A","Total Dues":"N/A"}

    text_norm = re.sub(r"\s+"," ", text_all).strip()

    # initialize result
    result = {"Statement date":"N/A","Payment due date":"N/A","Minimum payable":"N/A","Total Dues":"N/A"}

    # -------------------------
    # Direct regex attempts (many variants)
    # -------------------------
    # Statement date candidates
    stmt_patterns = [
        r"Statement Date\s*[:\-]?\s*([^\n]{6,40})",
        r"Statement Period\s*[:\-]?\s*From\s*([^\n]+?)\s*To\s*([^\n]+?)",
        r"Statement for the period\s*([^\n]+?)\s*To\s*([^\n]+?)",
        r"Billing Date\s*[:\-]?\s*([^\n]{6,40})"
    ]
    for pat in stmt_patterns:
        m = re.search(pat, text_all, re.IGNORECASE)
        if m:
            if "To" in pat or ("From" in pat and m.lastindex and m.lastindex >= 2):
                # take the 2nd group (end of period)
                group = m.group(2) if m.lastindex >= 2 else m.group(1)
                sd = parse_date_flexible(group)
                if sd:
                    result["Statement date"] = sd
                    break
            else:
                group = m.group(1)
                sd = parse_date_flexible(group)
                if sd:
                    result["Statement date"] = sd
                    break

    # Payment due date variants
    due_patterns = [
        r"Payment Due Date\s*[:\-]?\s*([^\n]{6,40})",
        r"Payment Due On\s*[:\-]?\s*([^\n]{6,40})",
        r"Due Date\s*[:\-]?\s*([^\n]{6,40})",
        r"Due by\s*([A-Za-z0-9 ,/]+)",
        r"Pay by\s*([A-Za-z0-9 ,/]+)",
        r"Payment Due\s*[:\-]?\s*([^\n]{6,40})"
    ]
    for pat in due_patterns:
        m = re.search(pat, text_all, re.IGNORECASE)
        if m:
            val = m.group(1)
            dd = parse_date_flexible(val)
            if dd:
                result["Payment due date"] = dd
                break

    # Total Dues / Closing variants
    total_patterns = [
        r"(Total Dues|Total Amount Due|Total Due|Closing Balance|Amount Due|Balance Due)\s*[:\-]?\s*₹?\s*([\d,]+\.\d{2})",
        r"Closing Balance\s*₹?\s*([\d,]+\.\d{2})",
        r"Total Dues\s*₹?\s*([\d,]+\.\d{2})"
    ]
    for pat in total_patterns:
        m = re.search(pat, text_all, re.IGNORECASE)
        if m:
            # some patterns have 2 groups; the numeric group may be group(2) or group(1)
            g = m.group(2) if len(m.groups()) >= 2 and re.search(r"[\d,]+\.\d{2}", str(m.group(2) or "")) else m.group(1)
            num = to_float(g)
            if num is not None:
                result["Total Dues"] = fmt_amount(num)
                break

    # Minimum payable patterns
    min_patterns = [
        r"(Minimum Amount Due|Minimum Due|Min Amount Due|Minimum Payment)\s*[:\-]?\s*₹?\s*([\d,]+\.\d{2})",
        r"Minimum Amount\s*₹?\s*([\d,]+\.\d{2})"
    ]
    for pat in min_patterns:
        m = re.search(pat, text_all, re.IGNORECASE)
        if m:
            g = m.group(2) if len(m.groups())>=2 and re.search(r"[\d,]+\.\d{2}", str(m.group(2) or "")) else m.group(1)
            num = to_float(g)
            if num is not None:
                result["Minimum payable"] = fmt_amount(num)
                break

    # -------------------------
    # If total or minimum not found, use numeric cluster permutation mapping
    # Prefer rows with exactly 4 numeric values (common layout: prev,payments,purchases,total)
    # or 4 numbers that can map to Credit Limit / Available / Total / Minimum
    # -------------------------
    cluster_rows = []
    for (nums, p, t, r, raw) in numeric_rows:
        # keep rows with 3..6 numbers (common)
        if len(nums) >= 3 and len(nums) <= 6:
            cluster_rows.append((nums, p, t, r, raw))

    # If any cluster rows exist, try to map a 4-number row with permutation scoring
    if cluster_rows and (result["Total Dues"] == "N/A" or result["Minimum payable"] == "N/A"):
        # normalize rows to floats
        numeric_rows_f = []
        for nums, p, t, r, raw in cluster_rows:
            numeric_rows_f.append((nums, p, t, r, raw))
        best_map, best_idx, perm = choose_best_primary_mapping(numeric_rows_f)
        if best_map:
            # best_map keys contain floats
            if result["Total Dues"] == "N/A" and "Total Due" in best_map:
                result["Total Dues"] = fmt_amount(best_map["Total Due"])
            if result["Minimum payable"] == "N/A" and "Minimum Due" in best_map:
                result["Minimum payable"] = fmt_amount(best_map["Minimum Due"])
            # also try to map secondary rows
            secondary = map_secondary_rows(numeric_rows_f, exclude_index=best_idx)
            # if still missing minimum, try secondary 'Previous Balance / Total Payments' mapping heuristics
            if result["Minimum payable"] == "N/A" and "Previous Balance" in secondary:
                # cannot be sure but skip
                pass

    # -------------------------
    # AMEX style fallback: find expression "= 8,858.07" or "Closing Balance = 8,858.07"
    # -------------------------
    if result["Total Dues"] == "N/A":
        m = re.search(r"=\s*([\d,]+\.\d{2})", text_all)
        if m:
            num = to_float(m.group(1))
            if num is not None:
                result["Total Dues"] = fmt_amount(num)

    # -------------------------
    # If Minimum still missing: try heuristic - smallest positive number <= total/2 or labeled 'Minimum'
    # -------------------------
    if result["Minimum payable"] == "N/A" and line_number_clusters:
        # flatten numbers
        all_vals = sorted(set([v for (_, _, nums) in line_number_clusters for v in nums]))
        if all_vals:
            total_val = None
            if result["Total Dues"] != "N/A":
                total_val = to_float(clean_num_str(result["Total Dues"]))
            # if total available choose smallest positive <= total (and > 0)
            candidate = None
            if total_val:
                smaller = [v for v in all_vals if v <= total_val + 1e-9]
                # prefer numbers significantly smaller than total (<= total*0.5)
                less_half = [v for v in smaller if v <= total_val * 0.5]
                if less_half:
                    candidate = min(less_half)
                elif smaller:
                    candidate = min(smaller)
            else:
                # pick the smallest positive number (often minimum payable is small)
                candidate = min([v for v in all_vals if v > 0], default=None)
            if candidate:
                result["Minimum payable"] = fmt_amount(candidate)

    # -------------------------
    # Final cleanup: ensure statement date present by trying more fallbacks (take first/last date from first page)
    # -------------------------
    if result["Statement date"] == "N/A":
        # try first page dates
        m = re.search(r"(\d{2}/\d{2}/\d{4})", text_all)
        if m:
            result["Statement date"] = m.group(1)

    # If still total missing but we have numeric clusters, pick 75th percentile strategy
    if result["Total Dues"] == "N/A" and line_number_clusters:
        flat_vals = sorted({v for (_, _, nums) in line_number_clusters for v in nums})
        if flat_vals:
            idx = int(len(flat_vals) * 0.75)
            idx = min(len(flat_vals)-1, max(0, idx))
            result["Total Dues"] = fmt_amount(flat_vals[idx])

    # Debug output
    debug_data = {
        "clusters": line_number_clusters[:30],
        "text_head": text_all[:2000]
    }
    if debug:
        st.subheader("Debug: candidate numeric clusters (first 30)")
        import pandas as pd
        dbg_rows = []
        for pidx, raw, nums in debug_data["clusters"]:
            dbg_rows.append({"page": pidx, "raw": raw, "numbers": nums})
        st.dataframe(pd.DataFrame(dbg_rows))
        st.markdown("**Chosen result (intermediate):**")
        st.json(result)

    return result

# --------------------------
# Streamlit UI
# --------------------------
debug_mode = st.sidebar.checkbox("Debug: show candidate clusters", value=False)
uploaded = st.file_uploader("Upload one or more unlocked credit-card statement PDFs", type=["pdf"], accept_multiple_files=True)

if uploaded:
    for f in uploaded:
        st.markdown(f"### {f.name}")
        with st.spinner("Parsing summary..."):
            summary = extract_summary_from_pdf(f, debug=debug_mode)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("📅 Statement date", summary.get("Statement date", "N/A"))
        c2.metric("⏰ Payment due date", summary.get("Payment due date", "N/A"))
        c3.metric("⚠️ Minimum payable", summary.get("Minimum payable", "N/A"))
        c4.metric("💰 Total Dues", summary.get("Total Dues", "N/A"))
else:
    st.info("Upload PDF statements (unlocked). Enable Debug to see clusters if values look off.")

# Footer
st.markdown("---")
st.markdown("If Minimum/Total are still incorrect for a specific PDF, enable Debug, upload that file, copy the candidate clusters table and paste it here — I will tune immediately.")
