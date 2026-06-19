# Invoice OCR Engine — Tiered Reader for Indian GST Invoices

Handles the reality that invoices arrive in wildly different formats: clean
digital PDFs, flatbed scans, phone-camera photos, rotated pages, and
handwritten notes. Reads what it can, and **explicitly flags what it can't**
instead of guessing — so nothing bad slips into your database silently.

## How it decides what to do with each page

```
PDF page
   │
   ├─► Has a real embedded text layer? ────────────► TIER 1: pdfplumber
   │    (typed/native PDF, ~80% of business invoices)   confidence ~99
   │
   ├─► No text layer → rasterize page → Tesseract OCR
   │       │
   │       ├─► Tesseract confident (≥65) AND enough text ──► TIER 2: ocr_printed
   │       │      (clean scan of a printed invoice)            confidence = Tesseract's own score
   │       │
   │       └─► Tesseract NOT confident, or barely any text ─► TIER 3: needs_vision_ai
   │              (handwritten, stamped, rotated, blurry,       confidence = Tesseract's score (low)
   │               torn, photographed at an angle...)           → routed to Gemini Vision
   │                                                             → ALWAYS flagged NEEDS_MANUAL_REVIEW
   │                                                               regardless of what Gemini returns,
   │                                                               so a human spot-checks it
   ▼
Structured JSON extraction (Gemini, text or vision)
   ▼
GST validation rules (GSTIN format, amount reconciliation, date sanity,
required-field check)
   ▼
Final status: PASSED / WARNING / FAILED / NEEDS_MANUAL_REVIEW
   ▼
output/invoices_export.csv
```

The key design choice: **Tier 3 doesn't try to "fix" handwriting with better
OCR settings.** Tesseract is a character recognizer, not a document
understander — it has no idea "this scrawl is an invoice number." Gemini's
vision model reads the actual image and understands invoice semantics in one
step, the way a person would. But because handwriting/degraded scans are
inherently less reliable than typed text no matter how good the model is,
every Tier-3 page is unconditionally flagged for manual review — confidence
in the *source*, not just the extracted values, drives that.

## Project layout

```
invoice_ocr/
├── config/
│   └── invoice_schema.json     ← EDIT THIS to change what fields get read/validated
├── core/
│   ├── ocr_engine.py           ← tiered reading logic (the part you asked about)
│   ├── extractor.py            ← Gemini calls (text + vision), demo-mode fallback
│   ├── validator.py            ← GST rules + status decision
│   └── pipeline.py             ← orchestrates everything, writes the CSV
├── test_data/
│   ├── generate_test_invoices.py
│   └── sample_invoices/        ← 5 synthetic invoices covering every tier
└── output/
    └── invoices_export.csv
```

## Making it adjustable for different invoice formats

You don't touch the code to handle a new invoice layout. Edit
`config/invoice_schema.json`:
- `required_fields` / `optional_fields` — add/remove what gets extracted
- `validation_rules.gstin_regex` — GSTIN format check
- `validation_rules.amount_reconciliation_tolerance` — how strict the
  taxable+GST=total check is (₹ tolerance for rounding)
- `confidence_thresholds.needs_review_below` — raise this if you want more
  invoices flagged for human eyes, lower it if you trust the pipeline more
- `invoice_type_hints` — per-vendor-type field overrides (e.g. POS receipts
  that never have a PO number)

Because Gemini does semantic extraction (not fixed-position parsing), it
naturally handles vendors with different layouts, languages mixed in,
different field orderings, etc. — that adaptability comes from the model,
not from per-template code.

## Setup (run this once)

```bash
cd invoice_ocr
python3 -m venv ../venv
../venv/bin/pip install pdfplumber pymupdf pytesseract pillow pandas reportlab numpy google-genai

# Tesseract itself (the OCR binary, not the Python wrapper) — already present
# on this machine; on a fresh machine:
sudo apt-get install -y tesseract-ocr
```

## Running it for real (with live Gemini extraction)

```bash
export GEMINI_API_KEY="your-key-here"
export INVOICE_OCR_DEMO_MODE=0
../venv/bin/python core/pipeline.py /path/to/your/invoices_folder output/invoices_export.csv
```

## Running the demo (no API key needed — what we just ran)

```bash
export INVOICE_OCR_DEMO_MODE=1   # this is the default
../venv/bin/python test_data/generate_test_invoices.py   # regenerate sample PDFs if needed
../venv/bin/python core/pipeline.py test_data/sample_invoices output/invoices_export.csv
```

In demo mode, Tier 1/2 (text-based) pages run through a small regex
stand-in so you can see PASSED/WARNING/FAILED differentiation without a
network call. Tier 3 (image-based) pages correctly stay flagged — there's no
honest way to simulate Vision AI reading a handwritten page, so the demo
doesn't pretend to.

## What you saw in this test run

| File | Tier used | OCR confidence | Result |
|---|---|---|---|
| clean_digital_invoice.pdf | digital_text | 99.0 | **PASSED** |
| scanned_printed_invoice.pdf | ocr_printed | 93.6 | **PASSED** |
| degraded_scan_invoice.pdf (rotated/noisy) | needs_vision_ai | 0.0 | **NEEDS_MANUAL_REVIEW** |
| handwritten_style_invoice.pdf (simulated) | needs_vision_ai | 61.2 | **NEEDS_MANUAL_REVIEW** |
| broken_invoice.pdf (missing GSTIN, bad total) | digital_text | 99.0 | **NEEDS_MANUAL_REVIEW** |

## Next steps you'll likely want

1. Get a `GEMINI_API_KEY` from Google AI Studio, set `INVOICE_OCR_DEMO_MODE=0`, point
   it at your real 100 PDFs.
2. Add a per-vendor `invoice_type_hints` entry the first time you hit a new
   layout that trips up validation unnecessarily.
3. Build the manual-review queue: anything `NEEDS_MANUAL_REVIEW` should land in
   a simple sheet/UI for a human to confirm or correct before it touches your
   real database — this is the safety net for handwritten/illegible cases.
