def extract_summary_from_pdf(pdf_file):
    summary = {}
    text_all = ""

    with pdfplumber.open(pdf_file) as pdf:
        for i in range(min(3, len(pdf.pages))):
            page_text = pdf.pages[i].extract_text()
            if page_text:
                text_all += "\n" + page_text

            tables = pdf.pages[i].extract_tables()
            for table in tables:
                if not table or len(table) < 2:
                    continue
                headers = [str(h).strip() for h in table[0] if h]
                row = [str(v).strip() for v in table[1] if v]

                # Case 1: HDFC style (headers + values)
                if any("Due Date" in h or "Total Dues" in h or "Credit Limit" in h for h in headers):
                    for h, v in zip(headers, row):
                        if not v or v.lower() in ["nan", ""]:
                            continue
                        if "Payment Due Date" in h:
                            summary["Payment Due Date"] = v.replace(",", "")
                        elif "Total Dues" in h or "Total Due" in h:
                            summary["Total Due"] = v.replace(",", "")
                        elif "Minimum" in h:
                            summary["Minimum Due"] = v.replace(",", "")
                        elif "Credit Limit" in h and "Available" not in h:
                            summary["Credit Limit"] = v.replace(",", "")
                        elif "Available Credit" in h:
                            summary["Available Credit"] = v.replace(",", "")
                        elif "Available Cash" in h:
                            summary["Available Cash"] = v.replace(",", "")
                        elif "Opening Balance" in h:
                            summary["Previous Balance"] = v.replace(",", "")
                        elif "Payment" in h:
                            summary["Total Payments"] = v.replace(",", "")
                        elif "Purchase" in h:
                            summary["Total Purchases"] = v.replace(",", "")
                        elif "Finance" in h:
                            summary["Finance Charges"] = v.replace(",", "")

                # Case 2: BoB style (row of 4 numbers)
                numbers = re.findall(r"[\d,]+\.\d{2}", " ".join(row))
                if len(numbers) == 4:
                    if not summary.get("Credit Limit"):
                        summary["Credit Limit"] = numbers[0].replace(",", "")
                        summary["Available Credit"] = numbers[1].replace(",", "")
                        summary["Total Due"] = numbers[2].replace(",", "")
                        summary["Minimum Due"] = numbers[3].replace(",", "")
                    else:
                        summary["Total Payments"] = numbers[0].replace(",", "")
                        summary["Other Charges"] = numbers[1].replace(",", "")
                        summary["Total Purchases"] = numbers[2].replace(",", "")
                        summary["Previous Balance"] = numbers[3].replace(",", "")

    # Regex fallback (Statement Date etc.)
    patterns = {
        "Statement Date": [
            r"Statement Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
            r"(\d{2}\s+[A-Za-z]{3},\s*\d{4})\s+To"
        ],
    }

    for key, regex_list in patterns.items():
        if key not in summary:
            for pattern in regex_list:
                m = re.search(pattern, text_all, re.IGNORECASE)
                if m:
                    summary[key] = m.group(1).replace(",", "")
                    break

    return summary
