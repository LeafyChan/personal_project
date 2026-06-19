"""
pipeline.py
===========
Orchestrates the full flow for a folder of invoice PDFs:

  PDF -> ocr_engine.read_pdf()            (per-page tiered text/image extraction)
       -> extractor.extract_from_*()      (structured JSON via Gemini, text or vision)
       -> validator.validate_invoice()    (GST rules + confidence -> status)
       -> rows appended to a pandas DataFrame -> CSV

Run:
    python core/pipeline.py /path/to/invoices_folder /path/to/output.csv
"""

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import ocr_engine
import extractor
import validator


def load_schema(schema_path: str) -> dict:
    with open(schema_path) as f:
        return json.load(f)


def process_single_pdf(pdf_path: Path, schema: dict) -> list[dict]:
    """A PDF can have multiple pages; each page becomes one row (most invoices
    are 1 page, but this keeps multi-page invoices and statements working)."""
    rows = []
    try:
        pages = ocr_engine.read_pdf(str(pdf_path))
    except Exception as e:
        return [{
            "file_name": pdf_path.name,
            "page": None,
            "status": "FAILED",
            "extraction_method": "error",
            "confidence": 0,
            "issues": f"Could not open/read file: {e}",
        }]

    for page in pages:
        if page.method_used == "needs_vision_ai":
            extracted = extractor.extract_from_image(page.image, schema)
        else:
            extracted = extractor.extract_from_text(page.raw_text, schema)

        result = validator.validate_invoice(
            extracted, schema, page.confidence, page.method_used
        )

        row = {
            "file_name": pdf_path.name,
            "page": page.page_number,
            "extraction_method": page.method_used,
            "confidence": result["confidence"],
            "status": result["status"],
            "issues": "; ".join(result["issues"]) if result["issues"] else "",
        }
        # flatten extracted fields (skip list-type ones for the CSV, keep as JSON string)
        for k, v in extracted.items():
            if k == "_extraction_note":
                continue
            row[k] = json.dumps(v) if isinstance(v, (list, dict)) else v
        rows.append(row)

    return rows


def run_pipeline(input_folder: str, output_csv: str, schema_path: str = None) -> pd.DataFrame:
    schema_path = schema_path or str(Path(__file__).parent.parent / "config" / "invoice_schema.json")
    schema = load_schema(schema_path)

    pdf_files = sorted(Path(input_folder).glob("*.pdf"))
    if not pdf_files:
        print(f"No PDF files found in {input_folder}")
        return pd.DataFrame()

    all_rows = []
    for pdf_path in pdf_files:
        print(f"Processing {pdf_path.name} ...")
        rows = process_single_pdf(pdf_path, schema)
        for r in rows:
            print(f"   page {r.get('page')}: {r['extraction_method']:<14} "
                  f"conf={r['confidence']:>5} -> {r['status']}")
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    df.to_csv(output_csv, index=False)
    print(f"\nWrote {len(df)} row(s) to {output_csv}")
    return df


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python pipeline.py <input_folder> <output_csv>")
        sys.exit(1)
    run_pipeline(sys.argv[1], sys.argv[2])
