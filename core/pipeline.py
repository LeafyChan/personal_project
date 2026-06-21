"""
pipeline.py
===========
Orchestrates the full flow for invoice PDFs, from two possible sources:

  LOCAL FOLDER:
    PDF -> ocr_engine.read_pdf() -> extractor.extract_from_*() ->
    validator.validate_invoice() -> rows -> CSV  (run_pipeline, unchanged
    behavior - still useful for quick local testing without Drive set up)

  GOOGLE DRIVE (polling):
    drive_connector.list_new_files() -> download -> same OCR/extract/validate
    steps -> database.save_invoice_row()  (run_drive_poll)

Both paths share process_single_pdf() for the actual OCR/extraction/
validation work - only the input source and output destination differ.

Run (local folder, CSV output - original behavior):
    python core/pipeline.py /path/to/invoices_folder /path/to/output.csv

Run (Drive folder, polls continuously, writes to SQLite):
    python core/pipeline.py --drive-poll <drive_folder_id> [--interval 300]
"""

import json
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import ocr_engine
import extractor
import validator
import database
import drive_connector


def load_schema(schema_path: str) -> dict:
    with open(schema_path) as f:
        return json.load(f)


def process_single_pdf(pdf_path: Path, schema: dict, drive_file_id: str = None) -> list[dict]:
    """A PDF can have multiple pages; each page becomes one row (most invoices
    are 1 page, but this keeps multi-page invoices and statements working).
    drive_file_id, when provided, is carried through to the output row so
    database.py can use it as the dedup key for the Drive polling path."""
    rows = []
    try:
        pages = ocr_engine.read_pdf(str(pdf_path))
    except Exception as e:
        return [{
            "file_name": pdf_path.name,
            "drive_file_id": drive_file_id,
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
            "drive_file_id": drive_file_id,
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


def _run_one_drive_cycle(folder_id: str, schema: dict, db_path: str, download_dir: str) -> int:
    """One poll cycle: list new files, download+process+save each, return
    count processed. Each file is saved to the database immediately after
    processing (not batched at the end) so a crash partway through a large
    batch doesn't lose already-completed work."""
    new_files = drive_connector.list_new_files(folder_id, db_path)
    if not new_files:
        return 0

    print(f"Found {len(new_files)} new file(s) in Drive folder.")
    processed = 0
    for f in new_files:
        print(f"Downloading {f['name']} ...")
        try:
            local_path = drive_connector.download_file(f["id"], f["name"], download_dir)
        except Exception as e:
            print(f"   [download failed] {e} - skipping, will retry next cycle")
            continue

        rows = process_single_pdf(Path(local_path), schema, drive_file_id=f["id"])
        for row in rows:
            try:
                invoice_id = database.save_invoice_row(row, db_path)
                print(f"   page {row.get('page')}: {row['extraction_method']:<14} "
                      f"conf={row['confidence']:>5} -> {row['status']} "
                      f"(saved as invoice_id={invoice_id})")
            except Exception as e:
                # Multi-page files: if page 1 already saved with this
                # drive_file_id, a later page hitting the UNIQUE constraint
                # would otherwise silently lose data - print loudly instead.
                print(f"   [DB save failed for page {row.get('page')}] {e}")
        processed += 1
    return processed


def run_drive_poll(folder_id: str, interval_seconds: int = 300,
                    schema_path: str = None, db_path: str = None,
                    download_dir: str = None, once: bool = False):
    """
    Polls the given Drive folder every interval_seconds for new files,
    processing and saving each to the SQLite database. This is the practical
    version of "automatic" without deploying a public webhook endpoint -
    Drive push notifications require a publicly reachable HTTPS callback,
    which a script running on a personal machine doesn't have.

    once=True runs a single cycle and returns - useful for testing or for
    wiring into cron/Task Scheduler instead of this function's own loop.
    """
    schema_path = schema_path or str(Path(__file__).parent.parent / "config" / "invoice_schema.json")
    schema = load_schema(schema_path)
    db_path = db_path or str(Path(__file__).parent.parent / "output" / "invoices.db")
    download_dir = download_dir or str(Path(__file__).parent.parent / "invoices")

    database.init_db(db_path)
    print(f"Database ready at {db_path}")
    print(f"Polling Drive folder {folder_id} every {interval_seconds}s "
          f"(Ctrl+C to stop)...")

    while True:
        try:
            count = _run_one_drive_cycle(folder_id, schema, db_path, download_dir)
            if count == 0:
                print("No new files this cycle.")
        except Exception as e:
            # A bad cycle (e.g. Drive API hiccup) shouldn't kill the whole
            # poller - log it and try again next interval.
            print(f"[poll cycle error] {e}")

        if once:
            return
        time.sleep(interval_seconds)


def _parse_cli_args(argv: list[str]) -> dict:
    if "--drive-poll" in argv:
        idx = argv.index("--drive-poll")
        if idx + 1 >= len(argv):
            print("Usage: python pipeline.py --drive-poll <folder_id> [--interval seconds] [--once]")
            sys.exit(1)
        args = {"mode": "drive", "folder_id": argv[idx + 1], "interval": 300, "once": False}
        if "--interval" in argv:
            i = argv.index("--interval")
            args["interval"] = int(argv[i + 1])
        if "--once" in argv:
            args["once"] = True
        return args
    return {"mode": "local"}


if __name__ == "__main__":
    parsed = _parse_cli_args(sys.argv[1:])
    if parsed["mode"] == "drive":
        run_drive_poll(parsed["folder_id"], interval_seconds=parsed["interval"], once=parsed["once"])
    else:
        if len(sys.argv) < 3:
            print("Usage:")
            print("  python pipeline.py <input_folder> <output_csv>")
            print("  python pipeline.py --drive-poll <folder_id> [--interval seconds] [--once]")
            sys.exit(1)
        run_pipeline(sys.argv[1], sys.argv[2])