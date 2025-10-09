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
        return float(str(s).replace(",", "").strip())
    except:
        return None

# Scoring for candidate primary mapping (Credit Limit, Available Credit, Total Due, Minimum Due)
def score_primary_candidate(mapping, nums, raw=''):
    # mapping: dict with keys 'Credit Limit','Available Credit','Total Due','Minimum Due' -> floats
    # nums: original list of floats for this row
    # Returns numeric score (higher is better)
    cl = mapping.get("Credit Limit")
    av = mapping.get("Available Credit")
    td = mapping.get("Total Due")
    md = mapping.get("Minimum Due")
    if any(x is None for x in [cl, av, td, md]):
        return -1e6
    score = 0.0
    # basic sanity
    if cl < 0 or av < 0 or td < 0 or md < 0:
        return -1e6
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
    # prefer cl being the maximum of row
    if abs(cl - max(nums)) < 1e-6:
        score += 1.5
    # prefer available less than cl
    if av <= cl:
        score += 0.5
    # prefer total_due positive
    if td > 0:
        score += 0.5
    # prefer cl reasonably large ( > 1000 )
    if cl >= 1000:
        score += 0.5
    # penalize wildly inconsistent totals (total >> cl * 3)
    if cl > 0 and td > cl * 3:
        score -= 2.0
    # prefer td <= cl
    if td <= cl + 1e-6:
        score += 1.0
    else:
        score -= 2.0
    # penalize if md == td
    if abs(md - td) < 1e-6:
        score -= 3.0
    # penalize if av == md or av == td
    if abs(av - md) < 1e-6 or abs(av - td) < 1e-6:
        score -= 3.0
    # prefer md / td ~ 0.05
    if td > 0 and md / td < 0.1:
        score += 2.0
    else:
        score -= 2.0
    # penalize if 'cash' in raw
    if 'cash' in raw.lower():
        score -= 5.0
    # add log cl for larger cl
    if cl > 0:
        score += math.log(cl + 1) / 10
    return score

# Scoring for secondary candidate mapping (Total Payments, Other Charges, Total Purchases, Previous Balance)
def score_secondary_candidate(mapping):
    tp = mapping.get("Total Payments")
    oc = mapping.get("Other Charges")
    purch = mapping.get("Total Purchases")
    prev = mapping.get("Previous Balance")
    if any(x is None for x in [tp, oc,
