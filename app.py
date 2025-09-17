def extract_summary_from_pdf(pdf_file):
    summary = {}
    text_all = ""

    try:
        with pdfplumber.open(pdf_file) as pdf:
            for i in range(min(3, len(pdf.pages))):
                page_text = pdf.pages[i].extract_text()
                if page_text:
                    text_all += "\n" + page_text

        lines = [l.strip() for l in text_all.split("\n") if l.strip()]

        # --- Step 1: Keyword-driven extraction (HDFC style) ---
        keyword_map = {
            "statement date": "Statement Date",
            "payment due date": "Payment Due Date",
            "total due": "Total Due",
            "total dues": "Total Due",
            "minimum amount due": "Minimum Due",
            "minimum due": "Minimum Due",
            "credit limit": "Credit Limit",
            "available credit": "Available Credit",
            "available cash": "Available Cash",
            "previous balance": "Previous Balance",
            "opening balance": "Previous Balance",
            "payments / credits": "Payments / Credits",
            "payment/credits": "Payments / Credits",
            "purchases / debits": "Purchases / Debits",
            "purchases/debits": "Purchases / Debits",
            "other charges": "Other Charges",
            "finance charges": "Finance Charges",
        }

        for line in lines:
            for key, field in keyword_map.items():
                if key in line.lower():
                    nums = re.findall(r"[\d,]+\.\d{2}", line)
                    if nums:
                        summary[field] = fmt_num(nums[-1])

        # --- Step 2: If still missing (BoB style numeric blocks) ---
        if not summary.get("Credit Limit"):
            numbers = []
            for line in lines:
                nums = re.findall(r"[\d,]+\.\d{2}", line)
                if len(nums) == 4:
                    numbers.append([float(n.replace(",", "")) for n in nums])

            if numbers:
                # First 4-number row → Credit info
                cl, av, td, md = numbers[0]
                summary["Credit Limit"] = fmt_num(cl)
                summary["Available Credit"] = fmt_num(av)
                summary["Total Due"] = fmt_num(td)
                summary["Minimum Due"] = fmt_num(md)

                if len(numbers) > 1:
                    # Second 4-number row → Payments/Purchases
                    pay, other, purch, prev = numbers[1]
                    summary["Payments / Credits"] = fmt_num(pay)
                    summary["Other Charges"] = fmt_num(other)
                    summary["Purchases / Debits"] = fmt_num(purch)
                    summary["Previous Balance"] = fmt_num(prev)

        # Regex fallback for Statement Date
        if "Statement Date" not in summary:
            m = re.search(r"(\d{2}\s+[A-Za-z]{3},\s*\d{4})", text_all)
            if m:
                summary["Statement Date"] = m.group(1)

    except Exception as e:
        st.error(f"⚠️ Error extracting summary: {e}")

    if not summary:
        summary = {"Info": "No summary details detected in PDF."}

    return summary
