"""
validator.py
============
Applies GST business rules to extracted invoice data and decides a final
status. This is where "accurate or flagged" actually gets enforced: nothing
silently passes just because Gemini returned valid-looking JSON.

TAX STRUCTURE HANDLING:
Invoices vary in how they break down tax:
  - CGST + SGST (intra-state)
  - IGST only (inter-state)
  - A single combined "GST" / "Tax" / "VAT" line with no breakdown at all
The extractor normalizes all of these into cgst_amount/sgst_amount/
igst_amount/total_gst_amount, but which of those sub-fields are populated
genuinely differs by invoice. Rule 4 below only checks CGST+SGST against
total_gst_amount when CGST/SGST were actually present - it does not treat a
combined-tax invoice (CGST/SGST both null) as an error.

CURRENCY HANDLING:
Per current policy, all amounts are validated as INR regardless of what
currency the invoice is actually in. Rule 6 doesn't block on a non-INR
currency - it adds an informational note so a human reviewing NEEDS_REVIEW
rows can see at a glance which invoices were actually foreign-currency,
since the numbers in those rows are not truly rupee amounts even though
they're being treated that way.
"""

import re
from datetime import datetime


def _parse_amount(val) -> float | None:
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").replace("₹", "").strip())
    except (ValueError, TypeError):
        return None


def _parse_date(val, formats: list[str]) -> datetime | None:
    if not val:
        return None
    for fmt in formats:
        try:
            return datetime.strptime(str(val).strip(), fmt)
        except ValueError:
            continue
    return None


def _normalize_currency_token(val: str) -> str:
    v = val.strip().upper()
    mapping = {
        "RS": "INR", "RS.": "INR", "₹": "INR", "INR": "INR",
        "$": "USD", "USD": "USD",
        "€": "EUR", "EUR": "EUR",
        "£": "GBP", "GBP": "GBP",
    }
    return mapping.get(v, v)


def validate_invoice(extracted: dict, schema: dict, page_confidence: float,
                      extraction_method: str) -> dict:
    """
    Returns a result dict:
      {
        "status": "PASSED" | "WARNING" | "FAILED" | "NEEDS_MANUAL_REVIEW",
        "issues": [list of human-readable strings],
        "confidence": float
      }
    """
    issues: list[str] = []       # actual validation problems -> affect status
    notes: list[str] = []        # informational context -> shown but don't affect status
    rules = schema["validation_rules"]
    thresholds = schema["confidence_thresholds"]

    # 0. Informational context: why this extraction may be less trustworthy.
    if extracted.get("_extraction_note"):
        notes.append(extracted["_extraction_note"])

    if extraction_method == "needs_vision_ai":
        notes.append(
            "Source page had low OCR confidence (likely handwritten, stamped, "
            "rotated, or a degraded scan) — extracted values need a human check."
        )

    # 1. Required fields present
    missing = [f for f in schema["required_fields"] if not extracted.get(f)]
    if missing:
        issues.append(f"Missing required field(s): {', '.join(missing)}")

    # 2. GSTIN format
    # Only flag a format problem when a GSTIN was actually extracted. Many
    # legitimate invoices (foreign vendors, retail receipts, unregistered
    # small vendors) genuinely have no GSTIN - that's covered by rule 1
    # (missing required field) if vendor_gstin is required, not a format error.
    gstin_pattern = re.compile(rules["gstin_regex"])
    for gstin_field in ("vendor_gstin", "buyer_gstin"):
        val = extracted.get(gstin_field)
        if val and not gstin_pattern.match(str(val).strip()):
            issues.append(f"{gstin_field} '{val}' does not match valid GSTIN format")

    # 3. Amount reconciliation: total ≈ taxable + gst
    # total_gst_amount is the normalized "all tax, however labeled" figure
    # produced by the extractor (covers GST/CGST+SGST/IGST/VAT/plain Tax),
    # so this check works the same regardless of which terminology the
    # source invoice used.
    taxable = _parse_amount(extracted.get("taxable_amount"))
    gst = _parse_amount(extracted.get("total_gst_amount"))
    total = _parse_amount(extracted.get("total_amount"))
    tolerance = rules["amount_reconciliation_tolerance"]

    if taxable is not None and gst is not None and total is not None:
        expected_total = taxable + gst
        if abs(expected_total - total) > tolerance:
            issues.append(
                f"Amount mismatch: taxable ({taxable}) + GST/Tax ({gst}) = "
                f"{expected_total:.2f}, but total_amount is {total}"
            )
    elif taxable is None and gst is None and total is not None:
        # Some invoices (e.g. retail receipts, foreign invoices with tax
        # already baked in) show only a final total with no breakdown at
        # all. That's a missing-field issue already caught by rule 1 if
        # taxable_amount/total_gst_amount are required - not a separate
        # reconciliation failure, so nothing additional to flag here.
        pass

    # 4. CGST + SGST should equal total_gst_amount, but ONLY when the
    # invoice actually provided a CGST/SGST split. An invoice that just
    # shows one combined "GST"/"Tax"/"VAT" figure (cgst/sgst left null by
    # the extractor) is not an error - it's simply a different tax
    # structure, not a missing breakdown.
    cgst = _parse_amount(extracted.get("cgst_amount"))
    sgst = _parse_amount(extracted.get("sgst_amount"))
    igst = _parse_amount(extracted.get("igst_amount"))

    if cgst is not None and sgst is not None and gst is not None:
        if abs((cgst + sgst) - gst) > tolerance:
            issues.append(
                f"CGST ({cgst}) + SGST ({sgst}) does not equal total_gst_amount ({gst})"
            )
    elif igst is not None and gst is not None:
        if abs(igst - gst) > tolerance:
            issues.append(
                f"IGST ({igst}) does not equal total_gst_amount ({gst})"
            )
    # else: combined tax line only (no CGST/SGST/IGST breakdown available) -
    # nothing to cross-check, and that's expected, not a problem.

    # 5. Date sanity
    invoice_date = _parse_date(extracted.get("invoice_date"), rules["date_formats"])
    if extracted.get("invoice_date") and not invoice_date:
        issues.append(f"invoice_date '{extracted.get('invoice_date')}' could not be parsed")
    elif invoice_date and (datetime.now() - invoice_date).days > rules["max_invoice_age_days"]:
        issues.append(f"invoice_date is unusually old ({invoice_date.date()})")

    # 6. Currency check - informational only, per current policy of treating
    # every amount as INR regardless of actual currency. This does not affect
    # status; it just makes foreign-currency rows visible to a human so the
    # "INR" amounts on those rows aren't mistaken for verified rupee figures.
    currency = extracted.get("currency_code")
    if currency:
        normalized = _normalize_currency_token(str(currency))
        expected = rules.get("expected_currency", "INR")
        if normalized != expected:
            notes.append(
                f"Invoice currency detected as {currency} (not {expected}) - "
                f"amounts are being treated as {expected} per current policy; "
                f"figures are NOT converted."
            )

    # --- Decide final status ---
    has_missing_required = any(i.startswith("Missing required field") for i in issues)
    is_low_confidence = page_confidence < thresholds["needs_review_below"]
    is_unverified_source = extraction_method == "needs_vision_ai"

    if has_missing_required or is_low_confidence or is_unverified_source:
        status = "NEEDS_MANUAL_REVIEW"
    elif any("mismatch" in i or "does not equal" in i or "format" in i for i in issues):
        status = "FAILED" if len(issues) > 1 else "WARNING"
    elif issues:
        status = "WARNING"
    else:
        status = "PASSED"

    return {
        "status": status,
        "issues": notes + issues,
        "confidence": round(page_confidence, 1),
    }
