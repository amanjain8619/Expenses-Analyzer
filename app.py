def extract_summary_from_pdf(pdf_path, debug=False):
    import pdfplumber, re
    from datetime import datetime

    def clean_num_str(s):
        if not s: return None
        s = str(s).replace("₹", "").replace("Rs.", "").replace("Rs", "").replace("INR", "").replace(",", "").strip()
        s = re.sub(r"\s*(DR|CR|Dr|Cr)\s*$", "", s)
        m = re.search(r"-?\d+\.?\d*", s)
        return m.group(0) if m else None

    def to_float(s):
        try: return float(clean_num_str(s))
        except: return None

    def fmt_amount(v):
        try: return f"₹{float(v):,.2f}"
        except: return "N/A"

    def parse_date(s):
        if not s: return None
        s = s.strip().replace(",", "")
        fmts = ["%d/%m/%Y","%d-%m-%Y","%d %b %Y","%b %d %Y","%d %B %Y","%B %d %Y"]
        for f in fmts:
            try: return datetime.strptime(s, f).strftime("%d/%m/%Y")
            except: pass
        m = re.search(r"(\d{1,2})\s*([A-Za-z]{3,9})\s*(\d{4})", s)
        if m:
            for fmt in ("%d %b %Y","%d %B %Y"):
                try: return datetime.strptime(f"{m[1]} {m[2]} {m[3]}", fmt).strftime("%d/%m/%Y")
                except: pass
        m = re.search(r"(\d{2}/\d{2}/\d{4})", s)
        return m.group(1) if m else None

    text_all = ""
    lines = []
    with pdfplumber.open(pdf_path) as pdf:
        for p in pdf.pages[:4]:
            txt = p.extract_text() or ""
            text_all += txt + "\n"
            lines += [l.strip() for l in txt.split("\n") if l.strip()]

    text_all = re.sub(r"\s+", " ", text_all)
    summary = {"Statement date":"N/A","Payment due date":"N/A","Minimum payable":"N/A","Total Dues":"N/A"}

    # 1️⃣ Find dates directly
    for pat in [
        r"Statement (?:Date|Period)[^:]*[:\-]?\s*([A-Za-z0-9 ,/]+)",
        r"Statement Period[^T]*To\s*([A-Za-z0-9 ,/]+)",
    ]:
        m = re.search(pat, text_all, re.IGNORECASE)
        if m:
            d = parse_date(m.group(1))
            if d:
                summary["Statement date"] = d
                break

    for pat in [
        r"Payment Due (?:Date|On)?[:\-]?\s*([A-Za-z0-9 ,/]+)",
        r"Due by\s*([A-Za-z0-9 ,/]+)",
        r"Pay by\s*([A-Za-z0-9 ,/]+)",
    ]:
        m = re.search(pat, text_all, re.IGNORECASE)
        if m:
            d = parse_date(m.group(1))
            if d:
                summary["Payment due date"] = d
                break

    # 2️⃣ Direct keyword-based numeric detection (more accurate than guessing)
    for line in lines:
        low = line.lower()
        if any(k in low for k in ["total amount due","total due","closing balance","amount due","total dues"]):
            m = re.search(r"₹?\s*([\d,]+\.\d{2})", line)
            if m:
                summary["Total Dues"] = fmt_amount(to_float(m.group(1)))
        if any(k in low for k in ["minimum amount due","minimum due","minimum payment"]):
            m = re.search(r"₹?\s*([\d,]+\.\d{2})", line)
            if m:
                summary["Minimum payable"] = fmt_amount(to_float(m.group(1)))

    # 3️⃣ AMEX-style fallbacks like "= 8,858.07"
    if summary["Total Dues"] == "N/A":
        m = re.search(r"=\s*([\d,]+\.\d{2})", text_all)
        if m: summary["Total Dues"] = fmt_amount(to_float(m.group(1)))

    # 4️⃣ Final numeric sanity check
    try:
        td = to_float(summary["Total Dues"])
        md = to_float(summary["Minimum payable"])
        if md and td and md > td:
            # swapped or misread; fix
            summary["Minimum payable"], summary["Total Dues"] = fmt_amount(td*0.05), fmt_amount(td)
    except: pass

    if debug:
        st.text_area("Raw text sample", text_all[:1500])
        st.json(summary)

    return summary
