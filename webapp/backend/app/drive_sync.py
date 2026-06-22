"""
drive_sync.py
=============
Bridges core/drive_connector.py (Drive listing + downloading) into the
multi-tenant Postgres backend. Dedup is org-scoped via
invoice_store.is_already_processed instead of drive_connector's own
SQLite check. Downloaded files are uploaded to Supabase Storage (durable
across Render redeploys) and the local temp copy is deleted immediately
after — local disk here is scratch space only, never the copy of record.
"""

from pathlib import Path

from app.invoice_store import is_already_processed, save_invoice_row
from app import storage


def sync_org_drive_folder(db, org_id: str, folder_id: str, core_pipeline_module,
                           drive_connector_module, schema: dict, download_dir: str,
                           modified_after: str = None) -> list[dict]:
    """
    Lists this org's registered Drive folder, skips files already processed
    for THIS org, downloads + OCRs + validates new ones, uploads the bytes
    to Supabase Storage, and saves results into this org's Postgres rows.
    Returns per-page result summaries, same shape /invoices/upload returns.

    modified_after: ISO 8601 timestamp from orgs.last_drive_sync_at. When set,
      Drive only returns files modified after that point (server-side filter),
      so each sync cycle only touches genuinely new files rather than
      re-listing the entire folder. The dedup check below stays as a safety
      net for edge cases (Drive bumps modifiedTime on metadata-only changes).
    """
    all_files = drive_connector_module.list_folder_files(folder_id, modified_after=modified_after)
    new_files = [f for f in all_files if not is_already_processed(db, org_id, f["id"])]

    results = []
    for f in new_files:
        try:
            local_path = drive_connector_module.download_file(f["id"], f["name"], download_dir)
        except Exception as e:
            results.append({
                "file_name": f["name"],
                "drive_file_id": f["id"],
                "status": "FAILED",
                "issues": f"Download failed: {e}",
            })
            continue

        rows = core_pipeline_module.process_single_pdf(
            Path(local_path), schema, drive_file_id=f["id"]
        )

        # Pipeline needs a real local path to read (pdfplumber/PyMuPDF/
        # Tesseract all expect a filesystem path). Once OCR is done, push
        # the bytes to Supabase Storage (durable) and delete the local
        # temp copy — Render's disk doesn't survive a redeploy.
        try:
            file_bytes = Path(local_path).read_bytes()
            remote_storage_path = storage.upload_file(org_id, f["name"], file_bytes)
        except Exception as e:
            results.append({
                "file_name": f["name"],
                "drive_file_id": f["id"],
                "status": "FAILED",
                "issues": f"Storage upload failed: {e}",
            })
            Path(local_path).unlink(missing_ok=True)
            continue
        finally:
            Path(local_path).unlink(missing_ok=True)

        for row in rows:
            row["file_name"] = f["name"]
            invoice_id = save_invoice_row(
                db, org_id, row, source_type="drive", storage_path=remote_storage_path
            )
            results.append({
                "invoice_id": invoice_id,
                "file_name": f["name"],
                "page": row.get("page"),
                "status": row.get("status"),
                "confidence": row.get("confidence"),
            })

    return results