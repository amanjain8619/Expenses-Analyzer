# app_no_ai.py
import streamlit as st
import pandas as pd
import io
import os
import re
from datetime import datetime

# optional plotting
try:
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except Exception:
    MATPLOTLIB_AVAILABLE = False

# optional OCR
try:
    import pdfplumber
except Exception:
    pdfplumber = None

try:
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
except Exception:
    pytesseract = None
    OCR_AVAILABLE = False

st.set_page_config(page_title="Expense Analyzer — Local (no OpenAI)", layout="wide")
st.title("Expense Analyzer — Local (no OpenAI)")
st.write("Parses CSV / XLSX / PDF bank statements locally using heuristics. OCR optional (pytesseract).")

# -------------------------
# Utilities
# -------------------------
def safe_float(v):
    try:
        return float(v)
    except:
        try:
            s = str(v).replace(',','').replace('₹','').replace('INR','').strip()
            if s == '':
                return 0.0
            return float(s)
        except:
            return 0.0

date_patterns = [
    # dd-mm-yyyy or dd/mm/yyyy or d-m-yy
    r'\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b',
    # yyyy-mm-dd
    r'\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b',
    # Monthname dd, yyyy  (e.g., Jun 5, 2025) or (June 5 2025)
    r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|January|February|March|April|June|July|August|September|October|November|December)[\w\s\.,-]{0,20}\d{1,2}[,\s]*\d{4}\b',
    # dd Monthname yyyy (e.g., 5 Jun 2025)
    r'\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|January|February|March|April|June|July|August|September|October|November|December)\s+\d{4}\b'
]

amount_pattern = r'(?:₹|\bINR\b)?\s*[-+]?\s*[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?'

def find_first_date(text):
    for pat in date_patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            s = m.group(0)
            # try to parse
            for fmt in ("%d-%m-%Y","%d/%m/%Y","%Y-%m-%d","%d-%m-%y","%d/%m/%y","%d %b %Y","%d %B %Y","%b %d, %Y","%B %d, %Y"):
                try:
                    dt = datetime.strptime(s.replace(',', ''), fmt)
                    return dt.strftime('%Y-%m-%d')
                except:
                    pass
            # fallback to pandas
            try:
                dt = pd.to_datetime(s, dayfirst=True, errors='coerce')
                if not pd.isna(dt):
                    return dt.strftime('%Y-%m-%d')
            except:
                pass
            return s
    return None

def find_amounts(line):
    matches = re.findall(amount_pattern, line, flags=re.IGNORECASE)
    cleaned = []
    for m in matches:
        s = re.sub(r'[^\d\.\-]','', m)
        if s == '':
            continue
        try:
            cleaned.append(float(s.replace(',','')))
        except:
            try:
                cleaned.append(float(s))
            except:
                pass
    return cleaned

def infer_type_from_line(line, amount_value):
    low = line.lower()
    if any(k in low for k in ['cr', 'credit', 'deposit', 'credited']):
        return 'credit'
    if any(k in low for k in ['dr', 'debit', 'withdrawal', 'withdrawn', 'debited', 'payment', 'pmt']):
        return 'debit'
    # if amount had minus sign
    if re.search(r'[-]\s*[0-9]', line):
        return 'debit'
    # fallback: positive = debit? For bank statements credit is often listed separately.
    # We'll assume positive -> credit if word deposit/credited present else debit if words like paid/withdrawn present
    return 'debit'

def parse_text_transactions(text):
    """
    Parse text by scanning lines to find date + amount. Return list of txns.
    """
    txns = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines:
        date = find_first_date(ln)
        amounts = find_amounts(ln)
        if date and amounts:
            # pick last amount as transaction amount
            amt = amounts[-1]
            ttype = infer_type_from_line(ln, amt)
            # description: line without date and amount snippets
            desc = re.sub('|'.join([re.escape(x) for x in re.findall(amount_pattern, ln, flags=re.IGNORECASE)]), '', ln)
            # remove date substring
            for pat in date_patterns:
                desc = re.sub(pat, '', desc, flags=re.IGNORECASE)
            desc = re.sub(r'\s{2,}', ' ', desc).strip(' -:,')
            txns.append({'date': date, 'description': desc if desc else 'NA', 'amount': round(abs(float(amt)),2), 'type': ttype})
    return txns

def detect_bank_and_parse(df: pd.DataFrame):
    """
    Heuristic detection for common csv/xlsx table formats.
    """
    cols = [c.lower() for c in df.columns]
    # attempt HDFC-like
    if any('value date' in c for c in cols) and any('transaction' in c for c in cols):
        try:
            date_col = next(c for c in df.columns if 'value date' in c.lower())
            desc_col = next(c for c in df.columns if 'transaction' in c.lower())
        except StopIteration:
            return None
        debit_col = next((c for c in df.columns if 'debit' in c.lower()), None)
        credit_col = next((c for c in df.columns if 'credit' in c.lower()), None)
        txns = []
        for _, r in df.iterrows():
            try:
                d = pd.to_datetime(r[date_col]).strftime('%Y-%m-%d')
            except:
                d = str(r[date_col])
            desc = str(r[desc_col])
            amt = 0.0
            ttype = 'debit'
            if debit_col and pd.notna(r[debit_col]) and str(r[debit_col]).strip()!='':
                amt = safe_float(r[debit_col])
                ttype='debit'
            elif credit_col and pd.notna(r[credit_col]) and str(r[credit_col]).strip()!='':
                amt = safe_float(r[credit_col])
                ttype='credit'
            else:
                # fallback amount col
                a = next((c for c in df.columns if 'amount' in c.lower()), None)
                amt = safe_float(r[a]) if a else 0.0
                ttype = 'debit' if amt < 0 else 'credit'
            txns.append({'date':d,'description':desc,'amount':round(abs(amt),2),'type':ttype})
        return pd.DataFrame(txns)

    # ICICI-like
    if any('txn date' in c for c in cols) or any('narration' in c for c in cols):
        date_col = next((c for c in df.columns if 'txn date' in c.lower()), next((c for c in df.columns if 'date' in c.lower()), df.columns[0]))
        desc_col = next((c for c in df.columns if 'narration' in c.lower()), next((c for c in df.columns if 'description' in c.lower()), None))
        withdraw_col = next((c for c in df.columns if 'withdrawal' in c.lower()), None)
        deposit_col = next((c for c in df.columns if 'deposit' in c.lower()), None)
        txns = []
        for _, r in df.iterrows():
            try:
                d = pd.to_datetime(r[date_col]).strftime('%Y-%m-%d')
            except:
                d = str(r[date_col])
            desc = str(r[desc_col]) if desc_col else ' '.join(str(x) for x in r.values[:3])
            amt = 0.0; ttype='debit'
            if withdraw_col and pd.notna(r[withdraw_col]) and str(r[withdraw_col]).strip()!='':
                amt = safe_float(r[withdraw_col]); ttype='debit'
            elif deposit_col and pd.notna(r[deposit_col]) and str(r[deposit_col]).strip()!='':
                amt = safe_float(r[deposit_col]); ttype='credit'
            else:
                a = next((c for c in df.columns if 'amount' in c.lower()), None)
                amt = safe_float(r[a]) if a else 0.0
                ttype = 'debit' if amt < 0 else 'credit'
            txns.append({'date':d,'description':desc,'amount':round(abs(amt),2),'type':ttype})
        return pd.DataFrame(txns)

    # generic narration-amount pattern
    if any('narration' in c for c in cols) or any('narr' in c for c in cols):
        date_col = next((c for c in df.columns if 'value date' in c.lower()), next((c for c in df.columns if 'date' in c.lower()), df.columns[0]))
        desc_col = next((c for c in df.columns if 'narration' in c.lower()), next((c for c in df.columns if 'description' in c.lower()), None))
        amt_col = next((c for c in df.columns if 'amount' in c.lower()), None)
        debit_col = next((c for c in df.columns if 'debit' in c.lower()), None)
        credit_col = next((c for c in df.columns if 'credit' in c.lower()), None)
        txns=[]
        for _, r in df.iterrows():
            try:
                d = pd.to_datetime(r[date_col]).strftime('%Y-%m-%d')
            except:
                d = str(r[date_col])
            desc = str(r[desc_col]) if desc_col else ' '.join(str(x) for x in r.values[:3])
            amt=0.0; ttype='debit'
            if debit_col and pd.notna(r[debit_col]) and str(r[debit_col]).strip()!='':
                amt = safe_float(r[debit_col]); ttype='debit'
            elif credit_col and pd.notna(r[credit_col]) and str(r[credit_col]).strip()!='':
                amt = safe_float(r[credit_col]); ttype='credit'
            elif amt_col:
                amt = safe_float(r[amt_col]); ttype='debit' if amt<0 else 'credit'
            txns.append({'date':d,'description':desc,'amount':round(abs(amt),2),'type':ttype})
        return pd.DataFrame(txns)

    return None

# simple categorization
def categorize_transactions(txn_df: pd.DataFrame):
    rule_map = {
        'groceries': ['grocery','supermarket','big bazaar','dmart','reliance fresh','kirana','grocery store'],
        'fuel': ['petrol','diesel','fuel','hpcl','bharat petroleum','bpcl'],
        'rent': ['rent','landlord','house rent'],
        'salary': ['salary','payroll','salary credit'],
        'utility': ['electricity','water bill','phone bill','internet','broadband'],
        'entertainment': ['netflix','prime','spotify','movie','cinema','bookmyshow'],
        'dining': ['restaurant','cafe','dominos','zomato','swiggy','food','dine','canteen'],
        'shopping': ['amazon','flipkart','myntra','ajio','shopping'],
        'emi': ['emi','equated','installment'],
        'transfer': ['upi','neft','imps','transfer','rtgs','paytm']
    }
    def rule_cat(desc):
        d = desc.lower()
        for cat, kwlist in rule_map.items():
            for kw in kwlist:
                if kw in d:
                    return cat
        return 'other'
    txn_df['category'] = txn_df['description'].apply(lambda x: rule_cat(str(x)))
    return txn_df

def create_summary(txn_df: pd.DataFrame):
    txn_df['amount_signed'] = txn_df.apply(lambda r: -r['amount'] if r['type']=='debit' else r['amount'], axis=1)
    total_spent = txn_df[txn_df['type']=='debit']['amount'].sum()
    total_received = txn_df[txn_df['type']=='credit']['amount'].sum()
    by_cat = txn_df.groupby('category')['amount'].sum().reset_index().sort_values(by='amount', ascending=False)
    recent = txn_df.sort_values(by='date', ascending=False).head(10)
    return {'total_spent':round(total_spent,2),'total_received':round(total_received,2),'by_category':by_cat,'recent':recent}

def to_excel_bytes(dfs: dict):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        for sheet_name, df in dfs.items():
            df.to_excel(writer, sheet_name=sheet_name[:31], index=False)
    return output.getvalue()

# -------------------------
# UI
# -------------------------
with st.sidebar:
    st.header("Options")
    do_ocr = st.checkbox("Attempt OCR on scanned PDFs (requires Tesseract & pytesseract)", value=False)
    st.markdown("If OCR fails, ensure Tesseract is installed and in PATH.")
    st.markdown("This app runs fully locally (no external AI).")

uploaded_files = st.file_uploader("Upload Bank Statements (PDF / CSV / XLSX) — multi-select", accept_multiple_files=True, type=['pdf','csv','xlsx','xls'])
if not uploaded_files:
    st.info("Upload one or more files to start.")
    st.stop()

all_txns = []

for uf in uploaded_files:
    st.write(f"Processing `{uf.name}`")
    df = None
    extracted_text = None
    try:
        if uf.name.lower().endswith(('.xls','.xlsx')):
            df = pd.read_excel(uf, engine='openpyxl')
            st.info(f"Read spreadsheet `{uf.name}` with {len(df)} rows.")
        elif uf.name.lower().endswith('.csv'):
            df = pd.read_csv(uf)
            st.info(f"Read CSV `{uf.name}` with {len(df)} rows.")
        elif uf.name.lower().endswith('.pdf'):
            if pdfplumber is None:
                st.error("pdfplumber is required to read PDFs. Install with: pip install pdfplumber")
                st.stop()
            with pdfplumber.open(uf) as pdf:
                pages = pdf.pages
                texts = []
                for page in pages:
                    txt = page.extract_text()
                    if txt:
                        texts.append(txt)
                extracted_text = "\n\n".join(texts)
                st.info(f"Extracted text from PDF `{uf.name}` ({len(pages)} pages).")
                # if no text and OCR requested, try OCR
                if not extracted_text and do_ocr and OCR_AVAILABLE:
                    st.info("No text found in PDF pages — attempting OCR (this may be slow)...")
                    ocr_texts = []
                    for page in pages:
                        im = page.to_image(resolution=300).original
                        try:
                            txt = pytesseract.image_to_string(im)
                            if txt:
                                ocr_texts.append(txt)
                        except Exception as e:
                            st.warning(f"OCR failed on a page: {e}")
                    extracted_text = "\n\n".join(ocr_texts)
                    if extracted_text:
                        st.success("OCR succeeded on PDF pages.")
        else:
            st.warning(f"Unsupported file type for `{uf.name}`. Skipping.")
            continue
    except Exception as e:
        st.error(f"Failed to read file `{uf.name}`: {e}")
        continue

    if df is not None:
        parsed = detect_bank_and_parse(df)
        if parsed is not None and len(parsed)>0:
            st.success(f"Parsed `{uf.name}` using heuristic parser.")
            parsed['source_file'] = uf.name
            all_txns.append(parsed)
            continue
        else:
            st.info(f"Could not auto-detect structured columns in `{uf.name}` — attempting to infer from table.")
            # fallback: try to build txns by scanning rows for date+amount-like columns
            # attempt to find date col and numeric col
            date_cols = [c for c in df.columns if any(k in c.lower() for k in ['date','txn','value date','value_date'])]
            amt_cols = [c for c in df.columns if any(k in c.lower() for k in ['amount','amt','debit','credit','withdrawal','deposit'])]
            if not date_cols and len(df.columns)>0:
                # try first column
                date_cols = [df.columns[0]]
            txns = []
            for _, r in df.iterrows():
                # try date from any of date_cols
                dval = None
                for dc in date_cols:
                    try:
                        dval = pd.to_datetime(r[dc], errors='coerce')
                        if not pd.isna(dval):
                            dval = dval.strftime('%Y-%m-%d')
                            break
                    except:
                        pass
                # find amount: prefer debit/credit columns
                amt = None; ttype='debit'
                # check explicit debit/credit
                debit_col = next((c for c in df.columns if 'debit' in c.lower()), None)
                credit_col = next((c for c in df.columns if 'credit' in c.lower()), None)
                if debit_col and pd.notna(r[debit_col]) and str(r[debit_col]).strip()!='':
                    amt = safe_float(r[debit_col]); ttype='debit'
                elif credit_col and pd.notna(r[credit_col]) and str(r[credit_col]).strip()!='':
                    amt = safe_float(r[credit_col]); ttype='credit'
                else:
                    # try any amt col
                    for ac in amt_cols:
                        v = r[ac]
                        if pd.notna(v) and str(v).strip()!='':
                            amt = safe_float(v)
                            ttype = 'debit' if amt < 0 else 'credit'
                            break
                # description attempt
                desc_col = next((c for c in df.columns if 'narration' in c.lower()), next((c for c in df.columns if 'description' in c.lower()), None))
                desc = str(r[desc_col]) if desc_col else ' '.join(str(x) for x in r.values[:3])
                if dval and amt is not None:
                    txns.append({'date':dval,'description':desc,'amount':round(abs(float(amt)),2),'type':ttype})
            if txns:
                pdf_df = pd.DataFrame(txns)
                pdf_df['source_file'] = uf.name
                st.success(f"Inferred {len(pdf_df)} transactions from `{uf.name}`.")
                all_txns.append(pdf_df)
                continue
            else:
                st.warning(f"Could not infer transactions from table `{uf.name}`. Consider saving as CSV with clear headers.")
                continue

    # else: handle PDF text
    if extracted_text:
        parsed_txns = parse_text_transactions(extracted_text)
        if parsed_txns:
            dfp = pd.DataFrame(parsed_txns)
            dfp['source_file'] = uf.name
            st.success(f"Parsed {len(dfp)} transactions from PDF `{uf.name}` via text parsing.")
            all_txns.append(dfp)
        else:
            st.warning(f"No transaction-like lines found in PDF text for `{uf.name}`. Try enabling OCR or convert to CSV/XLSX.")
    else:
        st.warning(f"No usable content extracted from `{uf.name}`. Skipping.")

# combine results
if len(all_txns) == 0:
    st.error("No transactions parsed from uploaded files. Try different files, enable OCR if needed, or save statements as CSV/XLSX with clear headers.")
    st.stop()

txns_df = pd.concat(all_txns, ignore_index=True, sort=False)
for c in ['date','description','amount','type']:
    if c not in txns_df.columns:
        txns_df[c] = ''

# normalize
try:
    txns_df['date'] = pd.to_datetime(txns_df['date'], errors='coerce').dt.strftime('%Y-%m-%d')
except:
    txns_df['date'] = txns_df['date'].astype(str)
txns_df['description'] = txns_df['description'].astype(str)
txns_df['amount'] = txns_df['amount'].apply(safe_float)
txns_df['type'] = txns_df['type'].astype(str).apply(lambda x: x.lower() if x else 'debit')

with st.expander("Preview sample transactions (first 10 rows)"):
    st.dataframe(txns_df.head(10))

if st.button("Categorize transactions now"):
    with st.spinner("Categorizing locally..."):
        txns_df = categorize_transactions(txns_df)
    st.success("Categorization complete.")

summary = create_summary(txns_df)
c1, c2, c3 = st.columns(3)
c1.metric("Total Spent (debits)", f"₹ {summary['total_spent']}")
c2.metric("Total Received (credits)", f"₹ {summary['total_received']}")
c3.metric("Net (received - spent)", f"₹ {round(summary['total_received'] - summary['total_spent'],2)}")

st.subheader("Spending by Category")
st.dataframe(summary['by_category'].rename(columns={'amount':'total_amount'}).style.format({'total_amount':'{:.2f}'}))

if MATPLOTLIB_AVAILABLE:
    try:
        fig, ax = plt.subplots(figsize=(8,4))
        cats = summary['by_category']['category'].tolist()
        vals = summary['by_category']['amount'].tolist()
        ax.barh(cats[::-1], vals[::-1])
        ax.set_xlabel("Amount")
        ax.set_title("Spending by Category")
        st.pyplot(fig)
    except Exception:
        st.bar_chart(summary['by_category'].set_index('category'))
else:
    st.bar_chart(summary['by_category'].set_index('category'))

st.subheader("Recent transactions")
st.dataframe(summary['recent'])

# Export
sheets = {
    "transactions": txns_df,
    "by_category": summary['by_category'],
    "recent": summary['recent']
}
xlsx_bytes = to_excel_bytes(sheets)
st.download_button("Download full report (Excel)", data=xlsx_bytes, file_name="expense_report_local.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

st.caption("Local parser uses heuristics. It works well for structured CSV/XLSX and readable PDFs. For scanned PDFs, enable OCR (requires Tesseract). If parsing fails, export the statement to CSV from your bank's website and upload CSV.")
