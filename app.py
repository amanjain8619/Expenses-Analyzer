# app.py
import streamlit as st
import pandas as pd
import io
import os
import json
from datetime import datetime
from openai import OpenAI

# optional plotting
try:
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except Exception:
    MATPLOTLIB_AVAILABLE = False

# -------------------------
#  OpenAI client init
# -------------------------
api_key = os.getenv("OPENAI_API_KEY") or st.secrets.get("OPENAI_API_KEY", None)
if not api_key:
    st.error("OPENAI_API_KEY is not set. Set it in environment or Streamlit secrets.toml.")
    st.stop()

client = OpenAI(api_key=api_key)

# -------------------------
#  Utility functions
# -------------------------
def safe_get_openai_text(resp):
    """Support a couple of possible response shapes."""
    try:
        return resp.choices[0].message.content
    except Exception:
        try:
            return resp.choices[0].message["content"]
        except Exception:
            return str(resp)

def call_ai_extract_transactions(text_blob, model="gpt-4o-mini", max_tokens=1200, temperature=0.0):
    """
    Ask model to extract transactions from arbitrary table/text.
    Returns a list of dicts: [{'date': 'YYYY-MM-DD', 'description':'', 'amount': 123.45, 'type':'debit'/'credit'}...]
    """
    system = "You are a precise financial data extractor. Output only valid JSON: a list of transactions."
    user = (
        "Extract transactions from the following input. Return strictly JSON array. "
        "Each transaction must have: date (YYYY-MM-DD or ISO-like), description (string), amount (number), "
        "type (either 'debit' or 'credit'). If sign is in amount, derive type accordingly. "
        "If uncertain about date format, attempt ISO conversion. Now extract:\n\n"
        f"INPUT:\n{text_blob}\n\nReturn ONLY JSON."
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            temperature=temperature,
            max_tokens=max_tokens
        )
        text = safe_get_openai_text(resp)
        # sometimes the model includes explanation before JSON; find first '['
        first_brace = text.find('[')
        if first_brace != -1:
            text = text[first_brace:]
        data = json.loads(text)
        # normalize
        normalized = []
        for t in data:
            nt = {}
            # date normalization
            d = t.get("date") or t.get("txn_date") or t.get("transaction_date")
            try:
                nt['date'] = pd.to_datetime(d, dayfirst=False).strftime('%Y-%m-%d')
            except Exception:
                nt['date'] = str(d)
            nt['description'] = t.get("description") or t.get("narration") or t.get("remark") or ""
            amt = t.get("amount") or t.get("amt") or t.get("value") or 0
            try:
                amt_f = float(amt)
            except Exception:
                s = str(amt).replace(',','').replace('₹','').replace('INR','').strip()
                try:
                    amt_f = float(s)
                except Exception:
                    amt_f = 0.0
            nt['amount'] = round(abs(amt_f), 2)
            ttype = t.get("type") or t.get("dr_cr") or t.get("txn_type") or ""
            ttype = str(ttype).lower()
            if ttype in ['debit','dr','d','withdrawal','out']:
                nt['type'] = 'debit'
            elif ttype in ['credit','cr','c','deposit','in']:
                nt['type'] = 'credit'
            else:
                # infer from sign
                try:
                    nt['type'] = 'debit' if float(amt_f) < 0 else 'credit'
                except:
                    nt['type'] = 'debit'
            normalized.append(nt)
        return normalized
    except Exception as e:
        st.warning(f"AI extraction failed: {e}")
        return []

def detect_bank_and_parse(df: pd.DataFrame):
    """
    Heuristic detection for a few banks and mapping to a standardized transactions DataFrame.
    Returns: transactions_df or None if unknown
    Required columns in returned df: ['date','description','amount','type']
    """
    cols = [c.lower() for c in df.columns]
    # HDFC examples
    if any('value date' in c for c in cols) and any('transaction description' in c or 'transaction details' in c or 'transaction' in c for c in cols):
        date_col = next(c for c in df.columns if 'value date' in c.lower())
        desc_col = next(c for c in df.columns if 'transaction' in c.lower())
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
            if debit_col and pd.notna(r[debit_col]) and str(r[debit_col]).strip() != '':
                try:
                    amt = float(str(r[debit_col]).replace(',',''))
                except:
                    amt = 0.0
                ttype = 'debit'
            elif credit_col and pd.notna(r[credit_col]) and str(r[credit_col]).strip() != '':
                try:
                    amt = float(str(r[credit_col]).replace(',',''))
                except:
                    amt = 0.0
                ttype = 'credit'
            else:
                amt_col = next((c for c in df.columns if 'amount' in c.lower()), None)
                if amt_col:
                    val = r[amt_col]
                    try:
                        amt = float(str(val).replace(',','').replace('₹','')) if pd.notna(val) else 0.0
                    except:
                        amt = 0.0
                    ttype = 'debit' if float(amt) < 0 else 'credit'
            txns.append({'date':d,'description':desc,'amount':abs(amt),'type':ttype})
        return pd.DataFrame(txns)

    # ICICI sample detection
    if any('txn date' in c for c in cols) or (any('txn' in c for c in cols) and any('withdrawal' in c or 'deposit' in c for c in cols)):
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
            amt = 0.0
            ttype = 'debit'
            if withdraw_col and pd.notna(r[withdraw_col]) and str(r[withdraw_col]).strip() != '':
                try:
                    amt = float(str(r[withdraw_col]).replace(',',''))
                except:
                    amt = 0.0
                ttype = 'debit'
            elif deposit_col and pd.notna(r[deposit_col]) and str(r[deposit_col]).strip() != '':
                try:
                    amt = float(str(r[deposit_col]).replace(',',''))
                except:
                    amt = 0.0
                ttype = 'credit'
            else:
                amt_col = next((c for c in df.columns if 'amount' in c.lower()), None)
                if amt_col:
                    val = r[amt_col]
                    try:
                        amt = float(str(val).replace(',','')) if pd.notna(val) else 0.0
                    except:
                        amt = 0.0
                    ttype = 'debit' if float(amt) < 0 else 'credit'
            txns.append({'date':d,'description':desc,'amount':abs(amt),'type':ttype})
        return pd.DataFrame(txns)

    # Axis / generic narration-based formats
    if any('narration' in c for c in cols) or any('narr' in c for c in cols):
        date_col = next((c for c in df.columns if 'value date' in c.lower()), next((c for c in df.columns if 'date' in c.lower()), df.columns[0]))
        desc_col = next((c for c in df.columns if 'narration' in c.lower()), next((c for c in df.columns if 'description' in c.lower()), None))
        amt_col = next((c for c in df.columns if 'amount' in c.lower()), None)
        debit_col = next((c for c in df.columns if 'debit' in c.lower()), None)
        credit_col = next((c for c in df.columns if 'credit' in c.lower()), None)
        txns = []
        for _, r in df.iterrows():
            try:
                d = pd.to_datetime(r[date_col]).strftime('%Y-%m-%d')
            except:
                d = str(r[date_col])
            desc = str(r[desc_col]) if desc_col else ' '.join(str(x) for x in r.values[:3])
            amt = 0.0
            ttype = 'debit'
            if debit_col and pd.notna(r[debit_col]) and str(r[debit_col]).strip() != '':
                try:
                    amt = float(str(r[debit_col]).replace(',',''))
                except:
                    amt = 0.0
                ttype = 'debit'
            elif credit_col and pd.notna(r[credit_col]) and str(r[credit_col]).strip() != '':
                try:
                    amt = float(str(r[credit_col]).replace(',',''))
                except:
                    amt = 0.0
                ttype = 'credit'
            elif amt_col:
                val = r[amt_col]
                try:
                    amt = float(str(val).replace(',','').replace('₹','')) if pd.notna(val) else 0.0
                except:
                    amt = 0.0
                ttype = 'debit' if float(amt) < 0 else 'credit'
            txns.append({'date':d,'description':desc,'amount':abs(amt),'type':ttype})
        return pd.DataFrame(txns)

    # Unknown format
    return None

def categorize_transactions(txn_df: pd.DataFrame, use_ai=False, model="gpt-4o-mini"):
    """
    Add a 'category' column to txn_df.
    If use_ai is False, use rule-based keyword mapping.
    If use_ai True, call OpenAI to categorize descriptions.
    """
    if 'category' not in txn_df.columns:
        txn_df['category'] = ''

    # simple rule-map
    rule_map = {
        'groceries': ['grocery','supermarket','big bazaar','dmart','reliance fresh','more','kirana'],
        'fuel': ['petrol','diesel','fuel','indane','hpcl','bharat petroleum','bpcl'],
        'rent': ['rent','landlord','house rent'],
        'salary': ['salary','payroll','salary credit'],
        'utility': ['electricity','water bill','phone bill','internet','bill payment','broadband'],
        'entertainment': ['netflix','prime','spotify','movie','cinema','bookmyshow'],
        'dining': ['restaurant','cafe','dominos','zomato','swiggy','food'],
        'shopping': ['amazon','flipkart','myntra','ajio','shopping'],
        'emi': ['emi','equated','installment'],
        'transfer': ['upi','neft','imps','transfer','rtgs']
    }

    def rule_cat(desc):
        d = desc.lower()
        for cat, kwlist in rule_map.items():
            for kw in kwlist:
                if kw in d:
                    return cat
        return 'other'

    if not use_ai:
        txn_df['category'] = txn_df['description'].apply(rule_cat)
        return txn_df

    # use AI to categorize each unique description to reduce calls
    unique_desc = txn_df['description'].astype(str).unique().tolist()
    desc_to_cat = {}
    batch_prompt = (
        "You are a helpful assistant that maps transaction descriptions to one of these categories: "
        "groceries, fuel, rent, salary, utility, entertainment, dining, shopping, emi, transfer, other.\n\n"
        "Input: A JSON list of descriptions. Output: JSON object mapping each description to exactly one category (from the list)."
    )
    try:
        req_text = json.dumps(unique_desc, ensure_ascii=False)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role":"system","content":batch_prompt},{"role":"user","content":req_text}],
            temperature=0.0,
            max_tokens=800
        )
        text = safe_get_openai_text(resp)
        first = text.find('{')
        if first != -1:
            text = text[first:]
        mapping = json.loads(text)
        desc_to_cat = mapping
    except Exception as e:
        st.warning(f"AI categorization failed, falling back to rule-based: {e}")
        desc_to_cat = {}

    def ai_or_rule(desc):
        if desc in desc_to_cat:
            return desc_to_cat[desc]
        return rule_cat(desc)

    txn_df['category'] = txn_df['description'].apply(lambda x: ai_or_rule(str(x)))
    return txn_df

def create_summary(txn_df: pd.DataFrame):
    """
    Returns a summary dict and aggregated DataFrame by category
    """
    txn_df['amount_signed'] = txn_df.apply(lambda r: -r['amount'] if r['type']=='debit' else r['amount'], axis=1)
    total_spent = txn_df[txn_df['type']=='debit']['amount'].sum()
    total_received = txn_df[txn_df['type']=='credit']['amount'].sum()
    by_cat = txn_df.groupby('category')['amount'].sum().reset_index().sort_values(by='amount', ascending=False)
    recent = txn_df.sort_values(by='date', ascending=False).head(10)
    return {
        'total_spent': round(total_spent,2),
        'total_received': round(total_received,2),
        'by_category': by_cat,
        'recent': recent
    }

def to_excel_bytes(dfs: dict):
    """
    dfs: dict of sheet_name -> dataframe
    returns bytes buffer for xlsx
    """
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        for sheet_name, df in dfs.items():
            df.to_excel(writer, sheet_name=sheet_name[:31], index=False)
        # writer.save()  # not needed inside context
    return output.getvalue()

# -------------------------
#  Streamlit UI
# -------------------------
st.set_page_config(page_title="Expense Analyzer (Streamlit + OpenAI fallback)", layout="wide")
st.title("Expense Analyzer — Streamlit + OpenAI fallback")
st.markdown("Upload bank statements (PDF / CSV / XLSX). The app will try to parse automatically; for unknown formats it will call OpenAI to extract transactions.")

with st.sidebar:
    st.header("Options")
    ai_fallback = st.checkbox("Enable AI fallback for unknown formats", value=True)
    ai_categorize = st.checkbox("Use AI for categorization (instead of rule-based)", value=False)
    model_choice = st.selectbox("OpenAI model for extraction/categorization", options=["gpt-4o-mini","gpt-3.5-turbo"], index=0)
    st.markdown("Make sure OPENAI_API_KEY is set in environment or secrets.")

uploaded_files = st.file_uploader("Upload Bank Statements (PDF / CSV / XLSX)", accept_multiple_files=True, type=['pdf','csv','xlsx','xls'])
if not uploaded_files:
    st.info("Upload at least one file to continue.")
    st.stop()

all_txns = []

for uf in uploaded_files:
    st.write(f"Processing `{uf.name}`")
    df = None
    extracted_text = None
    try:
        if uf.name.lower().endswith(('.xls','.xlsx')):
            df = pd.read_excel(uf, engine='openpyxl')
        elif uf.name.lower().endswith('.csv'):
            df = pd.read_csv(uf)
        elif uf.name.lower().endswith('.pdf'):
            # lazy import of pdfplumber to avoid hard failure if not installed
            try:
                import pdfplumber
            except Exception as e:
                st.error("pdfplumber is required to read PDF files. Install with: pip install pdfplumber")
                st.stop()
            with pdfplumber.open(uf) as pdf:
                pages = pdf.pages
                all_text = []
                for page in pages:
                    txt = page.extract_text()
                    if txt:
                        all_text.append(txt)
                extracted_text = "\n\n".join(all_text)
            st.info(f"Extracted text from PDF `{uf.name}` ({len(pages)} pages).")
        else:
            st.warning(f"Unsupported file type for `{uf.name}`. Skipping.")
            continue
    except Exception as e:
        st.error(f"Failed to read file {uf.name}: {e}")
        continue

    # If we have a DataFrame (CSV/XLSX), attempt heuristic parse first
    if df is not None:
        parsed = detect_bank_and_parse(df)
        if parsed is not None:
            st.success(f"Parsed `{uf.name}` using heuristic parser.")
            parsed['source_file'] = uf.name
            all_txns.append(parsed)
            continue
        else:
            st.warning(f"Could not detect bank format for `{uf.name}` (CSV/XLSX).")
            # prepare preview for AI fallback if enabled
            try:
                preview = df.head(60).to_csv(index=False)
            except Exception:
                preview = str(df.head(60))
            payload = f"Filename: {uf.name}\n\nColumns: {list(df.columns)}\n\nPreview:\n{preview}"
    else:
        # PDF path: use extracted text as payload
        payload = f"Filename: {uf.name}\n\nExtracted Text:\n{extracted_text or ''}"

    # If we reach here, we need AI fallback to parse the payload
    if ai_fallback:
        with st.spinner(f"Calling OpenAI to extract transactions from {uf.name}..."):
            extracted = call_ai_extract_transactions(payload, model=model_choice)
        if extracted:
            parsed_df = pd.DataFrame(extracted)
            parsed_df['source_file'] = uf.name
            st.success(f"AI extracted {len(parsed_df)} transactions from `{uf.name}`.")
            all_txns.append(parsed_df)
        else:
            st.error(f"AI could not extract transactions from `{uf.name}`. Try improving PDF quality or saving as CSV/XLSX.")
    else:
        st.info("AI fallback disabled — skipping file.")

# combine all transactions
if len(all_txns) == 0:
    st.error("No transactions parsed. Try enabling AI fallback or upload files in CSV/XLSX with clear columns.")
    st.stop()

txns_df = pd.concat(all_txns, ignore_index=True, sort=False)
# ensure required columns exist
for c in ['date','description','amount','type']:
    if c not in txns_df.columns:
        txns_df[c] = ''

# basic cleaning
txns_df['date'] = txns_df['date'].astype(str)
try:
    txns_df['date'] = pd.to_datetime(txns_df['date']).dt.strftime('%Y-%m-%d')
except:
    pass
txns_df['description'] = txns_df['description'].astype(str)
# ensure amount numeric
def safe_float(v):
    try:
        return float(v)
    except:
        try:
            s = str(v).replace(',','').replace('₹','').replace('INR','').strip()
            return float(s)
        except:
            return 0.0
txns_df['amount'] = txns_df['amount'].apply(safe_float)
txns_df['type'] = txns_df['type'].astype(str).apply(lambda x: x.lower() if x else 'debit')

# Categorize
with st.expander("Preview sample transactions (first 8 rows)"):
    st.dataframe(txns_df.head(8))

if st.button("Categorize transactions now"):
    with st.spinner("Categorizing..."):
        txns_df = categorize_transactions(txns_df, use_ai=ai_categorize, model=model_choice)
    st.success("Categorization complete.")

# Show summary
summary = create_summary(txns_df)
col1, col2, col3 = st.columns(3)
col1.metric("Total Spent (debits)", f"₹ {summary['total_spent']}")
col2.metric("Total Received (credits)", f"₹ {summary['total_received']}")
col3.metric("Net (received - spent)", f"₹ {round(summary['total_received'] - summary['total_spent'],2)}")

st.subheader("Spending by Category")
st.dataframe(summary['by_category'].rename(columns={'amount':'total_amount'}).style.format({'total_amount':'{:.2f}'}))

# plot: prefer matplotlib if available, otherwise use st.bar_chart
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

# export
st.subheader("Export")
sheets = {
    "transactions": txns_df.drop(columns=['amount_signed'], errors='ignore') if 'amount_signed' in txns_df.columns else txns_df,
    "by_category": summary['by_category'],
    "recent": summary['recent']
}
xlsx_bytes = to_excel_bytes(sheets)
st.download_button("Download full report (Excel)", data=xlsx_bytes, file_name="expense_report.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

st.markdown("---")
st.caption("Notes: PDF extraction quality depends on the PDF. If the PDF is a scanned image, pdfplumber may not extract text — OCR would be needed. AI extraction may be imperfect; verify important rows and categories manually.")
