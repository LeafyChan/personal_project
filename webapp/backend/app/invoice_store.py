"""
invoice_store.py
=================
Saves a processed invoice row (the dict shape pipeline.process_single_pdf
already produces) into the multi-tenant Postgres schema, instead of the
original SQLite database.py. Ports the vendor-matching design from that
file unchanged - GSTIN-first matching, no fake placeholder names, shared
NULL/NULL bucket for genuinely unidentified vendors - the logic itself was
already correct; only the storage engine and org-scoping are new here.

Every function in this file takes a SQLAlchemy Session that ALREADY has
app.current_org_id set (i.e. one yielded by db.get_org_scoped_db) - this
file does not set org context itself, by design, so it's impossible to
call it with a forgotten/wrong context without that being visible at the
call site (main.py's route handlers own that responsibility, in one place).
"""

import json
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session


def _parse_date(val) -> str | None:
    """Returns ISO YYYY-MM-DD or None. Invoice dates from extraction are
    DD-MM-YYYY per the extractor's prompt instructions."""
    if not val:
        return None
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(val).strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_amount(val):
    if val is None or val == "":
        return None
    try:
        return float(str(val).replace(",", "").replace("\u20b9", "").strip())
    except (ValueError, TypeError):
        return None


def _get_or_create_vendor(db: Session, org_id: str, vendor_name, vendor_gstin, invoice_date) -> str:
    """Same matching design as the original SQLite version: GSTIN-first,
    fall back to exact name match, never substitute a fake placeholder
    name, and group genuinely-unidentified invoices under one shared
    NULL/NULL vendor row per org rather than one row per occurrence."""
    if not vendor_name and not vendor_gstin:
        row = db.execute(
            text(
                "SELECT vendor_id FROM vendors "
                "WHERE org_id = :oid AND vendor_gstin IS NULL AND vendor_name IS NULL"
            ),
            {"oid": org_id},
        ).fetchone()
        if row:
            return str(row[0])
        result = db.execute(
            text(
                "INSERT INTO vendors (org_id, vendor_gstin, vendor_name, first_seen_date, last_seen_date) "
                "VALUES (:oid, NULL, NULL, :d, :d) RETURNING vendor_id"
            ),
            {"oid": org_id, "d": invoice_date},
        )
        return str(result.scalar())

    if vendor_gstin:
        row = db.execute(
            text("SELECT vendor_id FROM vendors WHERE org_id = :oid AND vendor_gstin = :g"),
            {"oid": org_id, "g": vendor_gstin},
        ).fetchone()
    else:
        row = db.execute(
            text(
                "SELECT vendor_id FROM vendors "
                "WHERE org_id = :oid AND vendor_gstin IS NULL AND vendor_name = :n"
            ),
            {"oid": org_id, "n": vendor_name},
        ).fetchone()

    if row:
        vendor_id = row[0]
        if vendor_name:
            db.execute(
                text(
                    "UPDATE vendors SET vendor_name = COALESCE(vendor_name, :n) "
                    "WHERE vendor_id = :vid"
                ),
                {"n": vendor_name, "vid": vendor_id},
            )
        db.execute(
            text(
                "UPDATE vendors SET "
                "  last_seen_date = GREATEST(COALESCE(last_seen_date, CAST(:d AS date)), COALESCE(CAST(:d AS date), last_seen_date)), "
                "  first_seen_date = LEAST(COALESCE(first_seen_date, CAST(:d AS date)), COALESCE(CAST(:d AS date), first_seen_date)) "
                "WHERE vendor_id = :vid"
            ),
            {"d": invoice_date, "vid": vendor_id},
        )
        return str(vendor_id)

    result = db.execute(
        text(
            "INSERT INTO vendors (org_id, vendor_gstin, vendor_name, first_seen_date, last_seen_date) "
            "VALUES (:oid, :g, :n, :d, :d) RETURNING vendor_id"
        ),
        {"oid": org_id, "g": vendor_gstin, "n": vendor_name, "d": invoice_date},
    )
    return str(result.scalar())


def save_invoice_row(db: Session, org_id: str, row: dict, source_type: str = "upload",
                      storage_path: str = None) -> str:
    """
    Saves one processed-page row (same shape as pipeline.process_single_pdf
    produces) into the org-scoped invoices/vendors/line_items tables.
    Returns the new invoice_id.

    db must already have app.current_org_id set for this org (see module
    docstring) - this function trusts that and does not set it itself.
    """
    invoice_date = _parse_date(row.get("invoice_date"))
    vendor_id = _get_or_create_vendor(
        db, org_id, row.get("vendor_name"), row.get("vendor_gstin"), invoice_date
    )

    line_items_raw = row.get("line_items")
    line_items = []
    if line_items_raw:
        try:
            parsed = json.loads(line_items_raw) if isinstance(line_items_raw, str) else line_items_raw
            if isinstance(parsed, list):
                line_items = parsed
        except (json.JSONDecodeError, TypeError):
            pass

    hsn_codes_raw = row.get("hsn_codes")
    hsn_codes_json = None
    if hsn_codes_raw:
        try:
            hsn_codes_json = json.dumps(
                json.loads(hsn_codes_raw) if isinstance(hsn_codes_raw, str) else hsn_codes_raw
            )
        except (json.JSONDecodeError, TypeError):
            hsn_codes_json = None

    result = db.execute(
        text(
            "INSERT INTO invoices ("
            "  org_id, vendor_id, source_type, drive_file_id, storage_path, file_name, page,"
            "  extraction_method, confidence, status, issues,"
            "  vendor_name, vendor_gstin, buyer_name, buyer_gstin, invoice_number,"
            "  invoice_date, payment_due_date, place_of_supply,"
            "  taxable_amount, cgst_amount, sgst_amount, igst_amount, total_gst_amount, total_amount,"
            "  currency_code, tax_label_raw, tax_rate_percent, po_number, hsn_codes, line_items_raw"
            ") VALUES ("
            "  :org_id, :vendor_id, :source_type, :drive_file_id, :storage_path, :file_name, :page,"
            "  :extraction_method, :confidence, :status, :issues,"
            "  :vendor_name, :vendor_gstin, :buyer_name, :buyer_gstin, :invoice_number,"
            "  :invoice_date, :payment_due_date, :place_of_supply,"
            "  :taxable_amount, :cgst_amount, :sgst_amount, :igst_amount, :total_gst_amount, :total_amount,"
            "  :currency_code, :tax_label_raw, :tax_rate_percent, :po_number, :hsn_codes, :line_items_raw"
            ") RETURNING invoice_id"
        ),
        {
            "org_id": org_id,
            "vendor_id": vendor_id,
            "source_type": source_type,
            "drive_file_id": row.get("drive_file_id"),
            "storage_path": storage_path,
            "file_name": row.get("file_name"),
            "page": row.get("page"),
            "extraction_method": row.get("extraction_method"),
            "confidence": row.get("confidence"),
            "status": row.get("status"),
            "issues": row.get("issues"),
            "vendor_name": row.get("vendor_name"),
            "vendor_gstin": row.get("vendor_gstin"),
            "buyer_name": row.get("buyer_name"),
            "buyer_gstin": row.get("buyer_gstin"),
            "invoice_number": row.get("invoice_number"),
            "invoice_date": invoice_date,
            "payment_due_date": _parse_date(row.get("payment_due_date")),
            "place_of_supply": row.get("place_of_supply"),
            "taxable_amount": _parse_amount(row.get("taxable_amount")),
            "cgst_amount": _parse_amount(row.get("cgst_amount")),
            "sgst_amount": _parse_amount(row.get("sgst_amount")),
            "igst_amount": _parse_amount(row.get("igst_amount")),
            "total_gst_amount": _parse_amount(row.get("total_gst_amount")),
            "total_amount": _parse_amount(row.get("total_amount")),
            "currency_code": row.get("currency_code"),
            "tax_label_raw": row.get("tax_label_raw"),
            "tax_rate_percent": _parse_amount(row.get("tax_rate_percent")),
            "po_number": row.get("po_number"),
            "hsn_codes": hsn_codes_json,
            "line_items_raw": json.dumps(line_items) if line_items else line_items_raw,
        },
    )
    invoice_id = str(result.scalar())

    for item in line_items:
        if not isinstance(item, dict):
            continue
        db.execute(
            text(
                "INSERT INTO line_items (org_id, invoice_id, description, hsn_code, "
                "quantity, rate, amount, tax_rate, tax_amount, gross_amount) "
                "VALUES (:org_id, :invoice_id, :description, :hsn_code, "
                ":quantity, :rate, :amount, :tax_rate, :tax_amount, :gross_amount)"
            ),
            {
                "org_id": org_id,
                "invoice_id": invoice_id,
                "description": item.get("description"),
                "hsn_code": item.get("hsn_code"),
                "quantity": _parse_amount(item.get("quantity")),
                "rate": _parse_amount(item.get("rate")),
                "amount": _parse_amount(item.get("amount")),
                "tax_rate": _parse_amount(item.get("tax_rate")),
                "tax_amount": _parse_amount(item.get("tax_amount")),
                "gross_amount": _parse_amount(item.get("gross_amount")),
            },
        )

    total_amount = _parse_amount(row.get("total_amount"))
    db.execute(
        text(
            "UPDATE vendors SET invoice_count = invoice_count + 1, "
            "total_amount = total_amount + :amt WHERE vendor_id = :vid"
        ),
        {"amt": total_amount or 0, "vid": vendor_id},
    )

    return invoice_id


def is_already_processed(db: Session, org_id: str, drive_file_id: str) -> bool:
    """Drive-poll dedup check, scoped to this org - same drive_file_id
    could legitimately exist in two different orgs' Drive folders without
    that being a collision, so this must stay org-scoped, not global."""
    if not drive_file_id:
        return False
    row = db.execute(
        text(
            "SELECT 1 FROM invoices WHERE org_id = :oid AND drive_file_id = :did LIMIT 1"
        ),
        {"oid": org_id, "did": drive_file_id},
    ).fetchone()
    return row is not None