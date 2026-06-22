"""
extractor.py
============
Turns raw OCR text OR a page image into structured invoice JSON.

Two entry points:
  extract_from_text(text, schema)    -> for digital_text / ocr_printed pages
  extract_from_image(image, schema)  -> for needs_vision_ai pages (handwritten,
                                          messy scans, stamps, non-standard layouts)

TWO PROVIDERS, SPLIT BY TASK:
  - extract_from_text  -> Groq (llama-3.3-70b-versatile), using Structured
    Outputs (json_schema mode) so the response is guaranteed to match our
    schema - no more "almost valid JSON" failures on this path.
  - extract_from_image -> Gemini (gemini-2.5-flash-lite), since Groq's
    vision support is limited to specific preview models and isn't a
    reliable fit for production invoice scans/handwriting yet.
This split exists because free-tier daily quotas differ enormously by
provider and task: the vast majority of real invoices are digital_text (no
image needed at all), and Groq's free tier allows roughly 1,000 requests/day
on llama-3.3-70b-versatile vs. the ~20/day this project was hitting on
Gemini - so routing the bulk of the workload to Groq removes the daily-quota
wall almost entirely. Only the smaller needs_vision_ai slice still depends
on Gemini's much tighter daily allowance.

NORMALIZATION:
Real-world invoices don't all say "CGST"/"SGST"/"IGST". Some say "GST", some
say just "Tax", some say "VAT", some are foreign invoices in USD/EUR. Rather
than handling every label variant in the validator, we push the normalization
into the extraction prompt itself: the model maps whatever label/structure
the invoice actually uses onto our fixed schema fields, and separately
reports what the invoice originally called it (tax_label_raw) and what
currency it's actually in (currency_code), so nothing is silently lost even
though downstream validation currently treats every amount as INR.

RATE LIMITING:
Free tier quotas are per-project/per-provider and change without notice.
Each provider gets its own throttle state and MIN_SECONDS_BETWEEN_CALLS,
since Groq and Gemini have very different per-minute ceilings. _call_with_retry
adds exponential-backoff retries specifically for rate-limit-shaped errors
(HTTP 429 / "RESOURCE_EXHAUSTED" / "quota" / "rate_limit_exceeded"), since
those are transient and usually succeed later - unlike a genuinely bad
request, which won't fix itself by waiting. A DAILY quota, once exhausted,
won't be fixed by retrying within the same run - _is_daily_quota_error
detects this specific case (the error message says "Per Day" or similar)
and fails fast with zero retries instead of burning minutes retrying into a
wall that only opens tomorrow.

MALFORMED JSON HANDLING:
Models occasionally return JSON that's 99% correct but has one small syntax
issue (trailing comma, an unescaped quote inside a free-text field like a
line item description). _clean_json_response makes one repair attempt at
common issues before giving up. Groq's Structured Outputs mode should make
this a non-issue on the text path, but the repair step stays as a safety net
and still matters for the Gemini vision path.

DEMO_MODE: if no working API keys / network is available (e.g. inside a
sandboxed dev environment with no outbound internet), we fall back to a stub
that returns clearly-marked placeholder data so the rest of the pipeline can
still be exercised end-to-end. Swap DEMO_MODE off in a real deployment.
"""

import json
import os
import re
import sys
import time
from typing import Optional

from PIL import Image

DEMO_MODE = os.environ.get("INVOICE_OCR_DEMO_MODE", "1") == "1"

GEMINI_MODEL_NAME = "gemini-2.5-flash-lite"
# llama-3.3-70b-versatile does NOT support response_format=json_schema (confirmed
# via 400 error in production - "This model does not support response format
# json_schema"). Per Groq's docs, json_schema mode (with strict:true constrained
# decoding, which guarantees valid schema-matching output and never errors) is
# only available on a short list of models - gpt-oss-120b is the strongest of
# them and what we use here.
GROQ_MODEL_NAME = "openai/gpt-oss-120b"

# Free tier per-minute ceilings differ a lot by provider, so each gets its
# own pacing. These are intentionally a bit under the published RPM to leave
# margin (published numbers have proven unreliable for the actual per-project
# limits in practice - the error message itself is the real source of truth).
MIN_SECONDS_BETWEEN_CALLS = {
    "gemini": 4.5,   # ~13 RPM ceiling -> stay under it
    "groq": 2.5,     # ~30 RPM ceiling -> stay under it
}

# Retry behaviour for rate-limit-shaped errors. Other errors (bad API key,
# malformed request, etc.) are not retried since waiting won't fix them.
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 15

_RATE_LIMIT_MARKERS = (
    "429", "RESOURCE_EXHAUSTED", "quota", "rate limit", "RateLimit",
    "rate_limit_exceeded",
)
# If any of these also appear alongside a rate-limit marker, the limit is a
# DAILY one - retrying within the same run cannot succeed, since the reset
# is hours away rather than seconds. Fail fast instead of burning 4 retries
# (~4 minutes per invoice) into a wall that won't open until tomorrow.
_DAILY_QUOTA_MARKERS = ("PerDay", "per day", "RPD", "daily")

_last_call_time: dict[str, float] = {"gemini": 0.0, "groq": 0.0}


def _throttle(provider: str):
    """Sleep just enough to respect this provider's MIN_SECONDS_BETWEEN_CALLS
    since its last live call. Applies to every live call, not just retries,
    since the point is to avoid triggering the rate limit in the first place."""
    elapsed = time.monotonic() - _last_call_time[provider]
    remaining = MIN_SECONDS_BETWEEN_CALLS[provider] - elapsed
    if remaining > 0:
        time.sleep(remaining)
    _last_call_time[provider] = time.monotonic()


def _is_rate_limit_error(e: Exception) -> bool:
    text = str(e)
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


def _is_daily_quota_error(e: Exception) -> bool:
    text = str(e)
    return _is_rate_limit_error(e) and any(marker in text for marker in _DAILY_QUOTA_MARKERS)


def _call_with_retry(provider: str, fn, *args):
    """Runs fn(*args), throttling before every attempt and retrying with
    exponential backoff specifically on per-minute rate-limit errors. A
    daily-quota error fails immediately with no retries, since no amount of
    waiting within this run will help. Prints progress to stderr so a long
    batch run shows what's actually happening instead of going silent for
    minutes at a time."""
    backoff = INITIAL_BACKOFF_SECONDS
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle(provider)
        try:
            return fn(*args)
        except Exception as e:
            last_error = e
            if _is_daily_quota_error(e):
                print(
                    f"   [{provider} daily quota exhausted - no point retrying "
                    f"today] {e}",
                    file=sys.stderr,
                )
                raise
            if _is_rate_limit_error(e) and attempt < MAX_RETRIES:
                print(
                    f"   [{provider} rate limit hit, attempt {attempt}/{MAX_RETRIES} - "
                    f"waiting {backoff}s before retry] {e}",
                    file=sys.stderr,
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            # Not a rate-limit error, or out of retries - surface it and stop.
            print(f"   [{provider} call failed] {e}", file=sys.stderr)
            raise
    raise last_error


EXTRACTION_INSTRUCTIONS_TEMPLATE = """You are extracting structured data from an invoice, which may be an
Indian GST invoice OR a foreign/non-standard invoice. Return ONLY a JSON
object, no markdown fences, no commentary.

Required fields (use null if genuinely not present, never guess):
{required_fields}

Optional fields (include if visible, else null):
{optional_fields}

TAX NORMALIZATION (important - invoices use inconsistent terminology):
- Indian invoices may show CGST + SGST (intra-state) or just IGST (inter-state).
  Map these directly to cgst_amount / sgst_amount / igst_amount.
- Some invoices just say "GST" or "Tax" with one combined number and no
  CGST/SGST/IGST split. In that case: put the full tax amount into
  total_gst_amount, and leave cgst_amount/sgst_amount/igst_amount as null.
  Do NOT guess a 50/50 CGST/SGST split if the invoice doesn't show one.
- Some invoices say "VAT" instead of GST. Treat VAT the same way as a
  combined "Tax" line: put the amount into total_gst_amount.
- Whatever the invoice actually calls its tax line (e.g. "GST", "VAT",
  "Tax", "Sales Tax", "IGST @18%"), record that exact label text in
  tax_label_raw. If there are multiple tax lines, join their labels with
  "; ". If there is no tax line at all, set tax_label_raw to null.
- total_gst_amount should always be the sum of all tax shown on the
  invoice, however it's labeled, even when cgst/sgst/igst are null.
- If a tax percentage is printed anywhere near the tax line (e.g.
  "VAT @ 12%", "GST 18%", "IGST(18%)", "Tax Rate: 5%"), extract that
  number into tax_rate_percent as a plain number (12, 18, 5 - no "%"
  sign). This applies regardless of which tax label is used (GST, VAT,
  IGST, Sales Tax, etc.) - it is not GST-specific. If multiple tax lines
  show different rates (e.g. CGST 9% + SGST 9%), use the combined/
  effective rate if one is printed, otherwise the single rate that
  applies to the combined tax line. If no percentage is printed anywhere
  on the invoice, set tax_rate_percent to null - do not calculate or
  infer it from total_gst_amount / taxable_amount, since rounding in the
  source numbers makes a back-calculated rate unreliable as a compliance
  figure.

CURRENCY:
- Detect the currency symbol or code printed on the invoice (e.g. INR, Rs,
  ₹, USD, $, EUR, £) and put a short code in currency_code (use "INR" for
  Rs/₹, "USD" for $, "EUR" for €, "GBP" for £, or the literal text if you
  genuinely can't tell). If no currency is shown at all, set it to null.
- Still extract all amounts as plain numbers exactly as printed, regardless
  of currency. Do not convert or guess an exchange rate.

GENERAL RULES:
- vendor_gstin and buyer_gstin must be exactly as printed (15 characters, no
  spaces). If the invoice is foreign and has no GSTIN, leave it null - this
  is expected, not an error.
- All amounts as plain numbers (no currency symbols, no commas).
- invoice_date in DD-MM-YYYY.
- line_items as a list of objects: {{description, hsn_code, quantity, rate, amount, tax_rate, tax_amount, gross_amount}}.
  - rate: unit price (price per unit, before tax)
  - amount: net line total (quantity × rate, before any tax). This is what the extractor already captures.
  - tax_rate: the tax percentage applied to this line (e.g. 10.0 for VAT @ 10%, 18.0 for GST @ 18%).
    Use null if no per-line rate is printed — do NOT back-calculate from amounts.
    This field captures whatever the invoice calls it (VAT, GST, IGST, Sales Tax etc.), not just GST.
  - tax_amount: the actual tax rupee/currency amount on this line (e.g. if net is 1000 and VAT 10% = 100).
    If tax is only on the overall bill and not broken out per line, set this to null.
  - gross_amount: amount + tax_amount (the total for this line including tax). If tax_amount is null,
    gross_amount should also be null — do not guess.
- If the document is illegible, low quality, or you are not confident in a
  field, set that field to null rather than guessing. Do not fabricate values.
- Missing information is common and expected (e.g. a retail receipt with no
  buyer GSTIN, or a foreign invoice with no HSN codes). Null is always
  preferable to a guessed value.
"""


def _build_prompt(schema: dict) -> str:
    return EXTRACTION_INSTRUCTIONS_TEMPLATE.format(
        required_fields=", ".join(schema["required_fields"]),
        optional_fields=", ".join(schema["optional_fields"]),
    )


def _build_json_schema(schema: dict) -> dict:
    """
    Builds a JSON Schema for Groq's Structured Outputs (response_format
    json_schema mode), so the API guarantees a response matching this shape
    rather than us hoping the model's free-text JSON happens to parse. All
    fields are nullable strings/numbers since invoices routinely omit fields
    and we never want a schema-validation failure just because a field is
    genuinely absent on this particular invoice.
    """
    all_fields = schema["required_fields"] + schema["optional_fields"]
    field_types = schema.get("field_types", {})

    properties = {}
    for field in all_fields:
        if field == "line_items":
            line_item_props = {
                "description": {"type": ["string", "null"]},
                "hsn_code": {"type": ["string", "null"]},
                "quantity": {"type": ["number", "null"]},
                "rate": {"type": ["number", "null"]},
                "amount": {"type": ["number", "null"]},       # net (pre-tax)
                "tax_rate": {"type": ["number", "null"]},     # % e.g. 10.0
                "tax_amount": {"type": ["number", "null"]},   # tax rupee amount
                "gross_amount": {"type": ["number", "null"]}, # amount + tax_amount
            }
            properties[field] = {
                "type": ["array", "null"],
                "items": {
                    "type": "object",
                    "properties": line_item_props,
                    # Strict mode requires every nested object - not just the
                    # root - to list all its own properties as required and
                    # set additionalProperties: false. Missing this on a
                    # nested object causes a 400 even though the root schema
                    # looks correct.
                    "required": list(line_item_props.keys()),
                    "additionalProperties": False,
                },
            }
        elif field == "hsn_codes":
            properties[field] = {"type": ["array", "null"], "items": {"type": "string"}}
        elif field_types.get(field) == "amount":
            properties[field] = {"type": ["number", "null"]}
        else:
            properties[field] = {"type": ["string", "null"]}

    return {
        "type": "object",
        "properties": properties,
        "required": all_fields,
        "additionalProperties": False,
    }


def _clean_json_response(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.lower().startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as first_error:
        # Models occasionally emit JSON that's almost valid - most commonly
        # a trailing comma before a closing ] or }, or a stray control
        # character inside a free-text field (line item descriptions are the
        # usual culprit). Try cheap, targeted repairs before giving up -
        # this avoids burning another rate-limited API call just to fix a
        # one-character syntax slip.
        repaired = re.sub(r",(\s*[\]}])", r"\1", raw)          # trailing commas
        repaired = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", repaired)  # control chars
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            # Repair didn't work - raise the ORIGINAL error, since its line/
            # column position points at the real problem in the model's
            # actual output, not at our repaired-and-still-broken version.
            raise first_error


def _call_groq_text(prompt: str, document_text: str, schema: dict) -> dict:
    from groq import Groq

    client = Groq()  # picks up GROQ_API_KEY from env
    json_schema = _build_json_schema(schema)
    response = client.chat.completions.create(
        model=GROQ_MODEL_NAME,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"--- DOCUMENT TEXT ---\n{document_text}"},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "invoice_extraction",
                "strict": True,  # constrained decoding - guaranteed schema match, never errors
                "schema": json_schema,
            },
        },
    )
    return _clean_json_response(response.choices[0].message.content)


def _call_gemini_text(prompt: str, document_text: str) -> dict:
    from google import genai

    client = genai.Client()  # picks up GEMINI_API_KEY from env
    response = client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=f"{prompt}\n\n--- DOCUMENT TEXT ---\n{document_text}",
    )
    return _clean_json_response(response.text)


def _call_gemini_vision(prompt: str, image: Image.Image) -> dict:
    from google import genai

    client = genai.Client()
    response = client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=[prompt, image],
    )
    return _clean_json_response(response.text)


def _demo_stub(reason: str, schema: dict) -> dict:
    """
    Placeholder used only when DEMO_MODE is on / no API key configured.
    Every field is explicitly null and flagged so it can never be mistaken
    for a real extraction in downstream validation or CSV output.
    """
    stub = {f: None for f in schema["required_fields"] + schema["optional_fields"]}
    stub["_extraction_note"] = f"DEMO_MODE stub — {reason}. No live Gemini call made."
    return stub


# DEMO-only regex patterns. These exist purely so the pipeline can be run
# end-to-end without a live Gemini API key. They cover several real-world
# label variants (GST vs CGST/SGST vs VAT vs plain "Tax") so the demo proves
# out the normalization logic, but this is NOT how production extraction
# works - that's Gemini's job via the prompt above, which can additionally
# handle layouts and phrasings no fixed regex set ever will.
_DEMO_FIELD_PATTERNS = {
    "vendor_name": r"(?:^|\n)([A-Z][A-Za-z .&]+(?:Ltd|Limited|Pvt Ltd|Traders|Enterprises|Stores|GmbH|Inc|Mart|Co))\n",
    "vendor_gstin": r"GSTIN:\s*([0-9A-Z]{15})",
    "invoice_number": r"Invoice No:\s*([A-Za-z0-9\-/]+)",
    "invoice_date": r"Invoice Date:\s*([0-9./-]+)",
    "place_of_supply": r"Place of Supply:\s*([A-Za-z ]+)",
    "buyer_name": r"Bill To:\s*([A-Za-z0-9 .&]+)",
    "buyer_gstin": r"Buyer GSTIN:\s*([0-9A-Z]{15})",
    "taxable_amount": r"Taxable (?:Amount|Value):\s*([0-9.,]+)",
    "cgst_amount": r"CGST[^:]*:\s*([0-9.,]+)",
    "sgst_amount": r"SGST[^:]*:\s*([0-9.,]+)",
    "igst_amount": r"IGST[^:]*:\s*([0-9.,]+)",
    "total_amount": r"Total Amount:\s*([0-9.,]+)",
}

# Tax-line variants tried in priority order. The first one found wins for
# total_gst_amount / tax_label_raw, mirroring the "whatever the invoice
# actually calls it" instruction given to Gemini in production.
_DEMO_TAX_LABEL_PATTERNS = [
    ("Total GST", r"Total GST\b[^:\n]*:\s*([0-9.,]+)"),
    # Negative lookahead "GST(?!IN)" stops this from matching inside "GSTIN:" -
    # without it, "GSTIN: 27..." gets misread as a GST tax line of "27".
    ("GST", r"\bGST(?!IN)\b[^:\n]*:\s*([0-9.,]+)"),
    ("VAT", r"\bVAT\b[^:\n]*:\s*([0-9.,]+)"),
    ("Sales Tax", r"Sales Tax\b[^:\n]*:\s*([0-9.,]+)"),
    ("Tax", r"\bTax\b[^:\n]*:\s*([0-9.,]+)"),
]

# Tax-rate-percent variants, tried in priority order against the same
# label vocabulary as _DEMO_TAX_LABEL_PATTERNS above. Covers "VAT @ 12%",
# "GST 18%", "IGST(18%)", "Tax Rate: 5%" - the common phrasings where a
# percentage sits right next to the tax label. This is demo-only; in
# production the model reads the percentage directly off the rendered
# invoice the same way a human would, which a fixed regex set can't fully
# replicate (rates can appear anywhere near the line, not just adjacent).
_DEMO_TAX_RATE_PATTERNS = [
    r"Total GST[^%\n]*?\(?\s*(\d{1,2}(?:\.\d+)?)\s*%",
    r"\bGST(?!IN)\b[^%\n]*?\(?\s*(\d{1,2}(?:\.\d+)?)\s*%",
    r"\bVAT\b[^%\n]*?\(?\s*(\d{1,2}(?:\.\d+)?)\s*%",
    r"\bIGST\b[^%\n]*?\(?\s*(\d{1,2}(?:\.\d+)?)\s*%",
    r"Sales Tax[^%\n]*?\(?\s*(\d{1,2}(?:\.\d+)?)\s*%",
    r"Tax Rate[:\s]*(\d{1,2}(?:\.\d+)?)\s*%",
    r"\bTax\b[^%\n]*?\(?\s*(\d{1,2}(?:\.\d+)?)\s*%",
]

_DEMO_CURRENCY_PATTERNS = [
    ("INR", r"(?:₹|Rs\.?\s|INR)"),
    ("USD", r"(?:\$|USD)"),
    ("EUR", r"(?:€|EUR)"),
    ("GBP", r"(?:£|GBP)"),
]


def _demo_text_parser(document_text: str, schema: dict) -> dict:
    """
    DEMO-ONLY stand-in for the Gemini text call, using plain regex so the
    pipeline can be exercised end-to-end without network/API access. This
    intentionally covers a handful of label/currency variants to prove the
    normalization logic works, but is NOT how production extraction works —
    that's Gemini's job in production (see _call_gemini_text below). Image-tier
    pages still correctly fall back to the null stub below, since simulating
    Vision AI's image understanding isn't something regex can responsibly
    stand in for.
    """
    result = {f: None for f in schema["required_fields"] + schema["optional_fields"]}

    for field, pattern in _DEMO_FIELD_PATTERNS.items():
        m = re.search(pattern, document_text)
        if m:
            result[field] = m.group(1).strip()

    # Combined GST: if CGST/SGST/IGST weren't individually found, fall back
    # through GST -> VAT -> Tax -> Sales Tax, in that order, and remember
    # which label actually matched.
    if result.get("cgst_amount") or result.get("sgst_amount") or result.get("igst_amount"):
        parts = [result.get("cgst_amount"), result.get("sgst_amount"), result.get("igst_amount")]
        total = sum(float(p) for p in parts if p) or None
        if total:
            result["total_gst_amount"] = f"{total:.2f}"
        result["tax_label_raw"] = "CGST/SGST/IGST"
    else:
        for label, pattern in _DEMO_TAX_LABEL_PATTERNS:
            m = re.search(pattern, document_text)
            if m:
                result["total_gst_amount"] = m.group(1).strip()
                result["tax_label_raw"] = label
                break

    for code, pattern in _DEMO_CURRENCY_PATTERNS:
        if re.search(pattern, document_text):
            result["currency_code"] = code
            break

    # Tax rate: tried independently of which label matched above, since the
    # percentage often sits on the same line as the label regardless of
    # whether it was GST/VAT/Tax/etc. First match wins - same "whatever's
    # actually printed" philosophy as the label/currency detection above.
    for pattern in _DEMO_TAX_RATE_PATTERNS:
        m = re.search(pattern, document_text, re.IGNORECASE)
        if m:
            result["tax_rate_percent"] = m.group(1).strip()
            break

    result["_extraction_note"] = (
        "DEMO_MODE regex stand-in for Gemini text extraction (no live API call). "
        "Production path calls Gemini directly — see _call_gemini_text()."
    )
    return result


def extract_from_text(document_text: str, schema: dict) -> dict:
    prompt = _build_prompt(schema)
    if DEMO_MODE:
        return _demo_text_parser(document_text, schema)

    groq_key_present = bool(os.environ.get("GROQ_API_KEY"))
    if groq_key_present:
        try:
            return _call_with_retry("groq", _call_groq_text, prompt, document_text, schema)
        except Exception as groq_error:
            print(
                f"   [Groq failed, falling back to Gemini for this invoice] {groq_error}",
                file=sys.stderr,
            )
            # fall through to Gemini below rather than giving up immediately -
            # Groq's free tier is generous but not infinite, and Gemini still
            # has its own (smaller) daily allowance worth using as backup.
    try:
        return _call_with_retry("gemini", _call_gemini_text, prompt, document_text)
    except Exception as e:
        return _demo_stub(f"live call failed ({e})", schema)


def extract_from_image(image: Image.Image, schema: dict) -> dict:
    # Groq's vision support is limited to specific preview models and isn't
    # a reliable fit for production invoice scans/handwriting yet, so the
    # image path stays on Gemini only.
    prompt = _build_prompt(schema)
    if DEMO_MODE:
        return _demo_stub("set INVOICE_OCR_DEMO_MODE=0 and configure GEMINI_API_KEY to run live", schema)
    try:
        return _call_with_retry("gemini", _call_gemini_vision, prompt, image)
    except Exception as e:
        return _demo_stub(f"live call failed ({e})", schema)