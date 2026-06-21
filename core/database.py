"""
database.py
===========
Replaces CSV output with a real SQLite database, structured around vendors
rather than just flat invoice rows. This is the foundation everything else
(180-day reversal alerts, HSN analysis, GSTR-1 filing tracking) builds on -
those all become SQL queries over this schema rather than separate pipelines.

SCHEMA DESIGN:
  vendors    - one row per unique vendor_gstin (or vendor_name if no GSTIN).
               Tracks aggregate info that belongs to the VENDOR, not any one
               invoice: total invoices seen, total amount, first/last seen.
  invoices   - one row per processed invoice page, same fields as the old CSV
               plus a foreign key to vendors and a drive_file_id for Drive
               dedup (so a file already processed is never re-downloaded or
               re-extracted on the next poll).
  line_items - normalized out of the invoices.line_items JSON blob, since
               HSN-code analysis (Phase 2 of the roadmap) needs to query
               across line items, not just within one invoice's JSON.

Vendors are matched by GSTIN when available (the only reliable unique key -
names vary by spelling/branch), falling back to exact vendor_name match when
GSTIN is missing (foreign vendors, unregistered small vendors).
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).parent.parent / "output" / "invoices.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    vendor_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_gstin    TEXT,           -- nullable: foreign/unregistered vendors
    vendor_name     TEXT,           -- nullable: kept visible as a real gap, never defaulted to a placeholder
    first_seen_date TEXT,
    last_seen_date  TEXT,
    invoice_count   INTEGER NOT NULL DEFAULT 0,
    total_amount    REAL NOT NULL DEFAULT 0,
    -- Phase 3 hook: GSTR-1 filing status isn't extracted from invoices at all
    -- (it comes from the GST portal, a separate integration) - these columns
    -- exist now so that future feature is a column-fill, not a schema change.
    gstr1_filing_status TEXT,
    gstr1_last_checked  TEXT,
    UNIQUE(vendor_gstin, vendor_name)
);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id         INTEGER REFERENCES vendors(vendor_id),
    drive_file_id     TEXT UNIQUE,  -- Drive's permanent file ID; dedup key for polling
    file_name         TEXT NOT NULL,
    page              INTEGER,
    extraction_method TEXT,
    confidence        REAL,
    status            TEXT,
    issues            TEXT,

    vendor_name       TEXT,
    vendor_gstin      TEXT,
    buyer_name        TEXT,
    buyer_gstin       TEXT,
    invoice_number    TEXT,
    invoice_date      TEXT,         -- stored as ISO YYYY-MM-DD for sortability
    payment_due_date  TEXT,
    place_of_supply   TEXT,
    taxable_amount    REAL,
    cgst_amount       REAL,
    sgst_amount       REAL,
    igst_amount       REAL,
    total_gst_amount  REAL,
    total_amount      REAL,
    currency_code     TEXT,
    tax_label_raw     TEXT,
    po_number         TEXT,
    hsn_codes         TEXT,         -- JSON array, kept denormalized for convenience
    line_items_raw    TEXT,         -- original JSON blob, kept for audit/debug

    -- Phase 3 hook: when you actually pay an invoice, fill this in (manually
    -- for now, or from a future accounting-software integration) - the
    -- 180-day reversal check is `payment_date - invoice_date`, so this
    -- column existing is the only prerequisite for that feature later.
    payment_date      TEXT,

    processed_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS line_items (
    line_item_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id    INTEGER NOT NULL REFERENCES invoices(invoice_id),
    description   TEXT,
    hsn_code      TEXT,
    quantity      REAL,
    rate          REAL,
    amount        REAL
);

CREATE INDEX IF NOT EXISTS idx_invoices_vendor ON invoices(vendor_id);
CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(invoice_date);
CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_line_items_invoice ON line_items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_line_items_hsn ON line_items(hsn_code);
"""


@contextmanager
def connect(db_path: str = None):
    db_path = db_path or str(DEFAULT_DB_PATH)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str = None):
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def is_already_processed(drive_file_id: str, db_path: str = None) -> bool:
    """Used by the Drive poller to skip files already in the database,
    so re-running the poll never re-extracts (and re-spends API tokens on)
    a file it's already seen."""
    if not drive_file_id:
        return False
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM invoices WHERE drive_file_id = ? LIMIT 1", (drive_file_id,)
        ).fetchone()
        return row is not None


def _get_or_create_vendor(conn: sqlite3.Connection, vendor_name, vendor_gstin, invoice_date) -> int:
    """Matches by GSTIN when present (the only reliable unique key - vendor
    names vary by spelling, branch suffix, etc.), falling back to exact
    vendor_name match when GSTIN is missing entirely.

    Deliberately does NOT substitute a placeholder like "UNKNOWN VENDOR" when
    vendor_name is missing - that would make an incomplete extraction look
    like a real vendor record in every query/report downstream. A genuinely
    nameless vendor stays NULL so it's visible as a gap to fix (or revisit
    after a re-extraction), not buried as fake-looking data.
    """
    if not vendor_name and not vendor_gstin:
        # Neither identifier present - group these under one shared
        # "unidentified" bucket (NULL/NULL) rather than creating a fresh
        # vendor row per occurrence, which would make vendor_count
        # meaningless. These rows are exactly what NEEDS_MANUAL_REVIEW
        # should already be catching upstream in the validator.
        row = conn.execute(
            "SELECT vendor_id FROM vendors WHERE vendor_gstin IS NULL AND vendor_name IS NULL"
        ).fetchone()
        if row:
            return row["vendor_id"]
        cur = conn.execute(
            "INSERT INTO vendors (vendor_gstin, vendor_name, first_seen_date, last_seen_date) "
            "VALUES (NULL, NULL, ?, ?)",
            (invoice_date, invoice_date),
        )
        return cur.lastrowid

    if vendor_gstin:
        row = conn.execute(
            "SELECT vendor_id FROM vendors WHERE vendor_gstin = ?", (vendor_gstin,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT vendor_id FROM vendors WHERE vendor_gstin IS NULL AND vendor_name = ?",
            (vendor_name,),
        ).fetchone()

    if row:
        vendor_id = row["vendor_id"]
        # If this occurrence has a name and the stored record doesn't, fill
        # it in - a later invoice from the same GSTIN often succeeds where
        # an earlier one only partially extracted.
        if vendor_name:
            conn.execute(
                "UPDATE vendors SET vendor_name = COALESCE(vendor_name, ?) WHERE vendor_id = ?",
                (vendor_name, vendor_id),
            )
        conn.execute(
            """UPDATE vendors SET
                 last_seen_date = MAX(COALESCE(last_seen_date, ''), COALESCE(?, '')),
                 first_seen_date = MIN(COALESCE(first_seen_date, ?), COALESCE(?, first_seen_date))
               WHERE vendor_id = ?""",
            (invoice_date, invoice_date, invoice_date, vendor_id),
        )
        return vendor_id

    cur = conn.execute(
        """INSERT INTO vendors (vendor_gstin, vendor_name, first_seen_date, last_seen_date)
           VALUES (?, ?, ?, ?)""",
        (vendor_gstin, vendor_name, invoice_date, invoice_date),
    )
    return cur.lastrowid


def save_invoice_row(row: dict, db_path: str = None) -> int:
    """
    Inserts one invoice (one row from the pipeline's per-page output) into
    the database, creating/updating the vendor record and normalizing
    line_items into their own rows along the way. Returns the new invoice_id.

    `row` is expected in the same shape pipeline.py already produces per
    page (see process_single_pdf), with one addition: an optional
    'drive_file_id' key when the source PDF came from Drive.
    """
    with connect(db_path) as conn:
        vendor_id = _get_or_create_vendor(
            conn, row.get("vendor_name"), row.get("vendor_gstin"), row.get("invoice_date")
        )

        line_items_raw = row.get("line_items")
        # pipeline.py json.dumps()'s list/dict fields before they reach here
        # (see process_single_pdf) - parse back out so we can both store the
        # raw blob AND normalize into the line_items table.
        line_items = []
        if line_items_raw:
            try:
                parsed = json.loads(line_items_raw) if isinstance(line_items_raw, str) else line_items_raw
                if isinstance(parsed, list):
                    line_items = parsed
            except (json.JSONDecodeError, TypeError):
                pass

        cur = conn.execute(
            """INSERT INTO invoices (
                 vendor_id, drive_file_id, file_name, page, extraction_method,
                 confidence, status, issues, vendor_name, vendor_gstin,
                 buyer_name, buyer_gstin, invoice_number, invoice_date,
                 payment_due_date, place_of_supply, taxable_amount, cgst_amount,
                 sgst_amount, igst_amount, total_gst_amount, total_amount,
                 currency_code, tax_label_raw, po_number, hsn_codes, line_items_raw
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                vendor_id, row.get("drive_file_id"), row.get("file_name"), row.get("page"),
                row.get("extraction_method"), row.get("confidence"), row.get("status"),
                row.get("issues"), row.get("vendor_name"), row.get("vendor_gstin"),
                row.get("buyer_name"), row.get("buyer_gstin"), row.get("invoice_number"),
                row.get("invoice_date"), row.get("payment_due_date"), row.get("place_of_supply"),
                row.get("taxable_amount"), row.get("cgst_amount"), row.get("sgst_amount"),
                row.get("igst_amount"), row.get("total_gst_amount"), row.get("total_amount"),
                row.get("currency_code"), row.get("tax_label_raw"), row.get("po_number"),
                row.get("hsn_codes"), line_items_raw,
            ),
        )
        invoice_id = cur.lastrowid

        for item in line_items:
            if not isinstance(item, dict):
                continue
            conn.execute(
                """INSERT INTO line_items (invoice_id, description, hsn_code, quantity, rate, amount)
                   VALUES (?,?,?,?,?,?)""",
                (
                    invoice_id, item.get("description"), item.get("hsn_code"),
                    item.get("quantity"), item.get("rate"), item.get("amount"),
                ),
            )

        total_amount = row.get("total_amount")
        if total_amount is not None:
            try:
                conn.execute(
                    """UPDATE vendors SET
                         invoice_count = invoice_count + 1,
                         total_amount = total_amount + ?
                       WHERE vendor_id = ?""",
                    (float(total_amount), vendor_id),
                )
            except (ValueError, TypeError):
                conn.execute(
                    "UPDATE vendors SET invoice_count = invoice_count + 1 WHERE vendor_id = ?",
                    (vendor_id,),
                )
        else:
            conn.execute(
                "UPDATE vendors SET invoice_count = invoice_count + 1 WHERE vendor_id = ?",
                (vendor_id,),
            )

        return invoice_id