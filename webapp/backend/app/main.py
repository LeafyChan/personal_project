"""
main.py — see docstring body below for full documentation.
"""

import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_org
from app.db import get_org_scoped_db, SessionLocal, _session_with_org
from app.org_resolver import resolve_org_and_user
from app.invoice_store import save_invoice_row
from app import storage

_core_parent = os.environ.get("CORE_PIPELINE_PATH")
if not _core_parent:
    raise RuntimeError("CORE_PIPELINE_PATH is not set.")
sys.path.insert(0, _core_parent)
sys.path.insert(0, str(Path(_core_parent) / "core"))
import pipeline as core_pipeline   # noqa: E402
import drive_connector              # noqa: E402
from app import hsn_generator                 # noqa: E402
from app.drive_sync import sync_org_drive_folder  # noqa: E402

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", tempfile.gettempdir())) / "invoice_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SCHEMA_PATH = os.environ.get(
    "INVOICE_SCHEMA_PATH", str(Path(_core_parent) / "config" / "invoice_schema.json"))
DRIVE_POLL_SECRET = os.environ.get("DRIVE_POLL_SECRET")
_SORTABLE_COLUMNS = frozenset({
    "processed_at", "invoice_date", "file_name", "vendor_name",
    "total_amount", "confidence", "status"})
_EDITABLE_INVOICE_FIELDS = frozenset({
    "vendor_name", "vendor_gstin", "buyer_name", "buyer_gstin",
    "invoice_number", "invoice_date", "payment_due_date", "place_of_supply",
    "taxable_amount", "cgst_amount", "sgst_amount", "igst_amount",
    "total_gst_amount", "total_amount", "currency_code", "po_number"})

app = FastAPI(title="Invoice Intelligence API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)


def get_request_context(clerk_ctx: dict = Depends(get_current_org)) -> dict:
    resolved = resolve_org_and_user(
        clerk_org_id=clerk_ctx["org_id"], clerk_user_id=clerk_ctx["user_id"])
    return {**clerk_ctx, **resolved}


def get_db_for_request(ctx: dict = Depends(get_request_context)):
    yield from get_org_scoped_db(ctx["org_id"])


# ── Activity log ──────────────────────────────────────────────────────────────
# General-purpose, org-scoped audit trail covering BOTH invoice edits and
# settings changes (Drive folder, business description, HSN profile),
# logged by either a human user or the AI pipeline. This is intentionally a
# separate table from edit_history (which is invoice-field-specific and
# already wired into the learning loop) rather than overloading that table's
# schema with nullable invoice_id - settings changes have no invoice_id at
# all, and forcing them into edit_history would mean either a schema change
# there or NULL-stuffing a column other code may assume is always present.
#
# IMPORTANT: the activity_log TABLE ITSELF is NOT created here. An earlier
# version of this file tried to CREATE TABLE at import time using the same
# connection pool every request uses - that connection authenticates as
# invoice_app, which per PROJECT_LOG.md's setup script only has USAGE on
# schema public plus SELECT/INSERT/UPDATE/DELETE on existing tables, not
# CREATE. Running DDL through that role fails with
# "permission denied for schema public" - confirmed in testing, this
# crashed the app on every startup. Granting CREATE to invoice_app would
# "fix" it but is the wrong fix: it would let a compromised app connection
# alter schema, undermining the exact least-privilege/NOBYPASSRLS posture
# the rest of this project is built around. The correct fix is a one-time
# migration file (see migration_add_activity_log.sql, run the same way as
# migration_add_tax_rate_percent.sql / migration_hsn_itc.sql - by hand,
# once, as the schema-owning role, not by the app). log_activity() below
# assumes that table already exists; if it doesn't, every call raises
# UndefinedTable, which is the correct failure mode (loud, not silent).
#
# actor_id REFERENCES users(user_id), same as edit_history.edited_by and
# invoices.last_edited_by - kept nullable for the same reason those are:
# an 'ai' actor_type action (e.g. applying a generated HSN profile) is
# triggered by a logged-in user, so it's actually populated in practice,
# but the column stays nullable in case a future genuinely-unattended
# AI action (e.g. the Drive-poll cron) needs to log without a human user
# in the request context.


def log_activity(
    db: Session,
    org_id: str,
    *,
    actor_type: str,
    action: str,
    entity_type: str,
    entity_id: str = None,
    field_name: str = None,
    old_value=None,
    new_value=None,
    summary: str = None,
    actor_id: str = None,
):
    """
    Inserts one activity_log row on the CALLER's already-org-scoped
    connection (db), so it commits or rolls back atomically with whatever
    change it's logging - a failed save never produces an orphan log row
    claiming something happened that didn't.

    actor_type: 'user' for a human-initiated edit, 'ai' for something the
    pipeline/LLM did on its own (e.g. HSN generation, OCR extraction).
    old_value/new_value are stringified here, not by the caller, so every
    call site is consistent about None -> NULL vs None -> "None".

    org_id/actor_id are cast explicitly with CAST(... AS uuid), same fix as
    the invoices UPDATE and edit_history INSERT above - SQLAlchemy's
    text() binds a Python str, and Postgres's inline "::uuid" syntax
    butted directly against a ":param" bind placeholder raises "syntax
    error at or near ':'" under psycopg2 (confirmed in testing - this
    exact pattern crashed every PATCH /invoices/{id} call). CAST(:x AS
    uuid) is unambiguous and works everywhere :x::uuid doesn't. actor_id
    stays NULL-safe (CAST(NULL AS uuid) is still NULL, not an error)
    since it's nullable in schema.sql.
    """
    db.execute(
        text(
            "INSERT INTO activity_log "
            "(org_id, actor_type, actor_id, action, entity_type, entity_id, "
            " field_name, old_value, new_value, summary) "
            "VALUES (CAST(:oid AS uuid), :atype, CAST(:aid AS uuid), :action, :etype, :eid, "
            " :field, :old, :new, :summary)"
        ),
        {
            "oid": org_id, "atype": actor_type, "aid": actor_id, "action": action,
            "etype": entity_type, "eid": entity_id, "field": field_name,
            "old": str(old_value) if old_value is not None else None,
            "new": str(new_value) if new_value is not None else None,
            "summary": summary,
        },
    )


def _reconcile_invoice_amounts(db: Session, invoice_id: str) -> list[str]:
    """
    Re-checks an invoice's amounts AFTER all edits in this request have been
    applied — line items and header fields, in whatever combination the
    caller sent — and returns a list of human-readable mismatch descriptions
    (empty list = reconciles fine).

    Deliberately re-reads everything fresh from the DB rather than working
    from any pre-edit Python variable still in scope: the whole point is to
    catch a case like "edit total_amount to 310, no matching line-item
    change" on one save, and then on a LATER save "edit one line item's
    amount from 100 to 150" and have the check evaluate against the NEW
    150, never the original 100 that prompted the first warning. Trusting
    an in-memory value across the chain of edits described in the project
    log would silently reintroduce exactly the bug this exists to catch.

    Reuses validator.py's existing amount_reconciliation_tolerance from the
    same schema.json the OCR pipeline already validates against (loaded via
    core_pipeline.load_schema(SCHEMA_PATH), same call used elsewhere in this
    file) rather than hardcoding a second, possibly-drifting tolerance value
    here. Mirrors validator.py's rule 3 (taxable + gst ≈ total) and adds the
    line-items-vs-taxable_amount check validator.py doesn't do today, since
    validator.py only ever sees freshly-extracted data, never a human edit
    that changed one line independently of the header total.
    """
    try:
        schema = core_pipeline.load_schema(SCHEMA_PATH)
        tolerance = schema["validation_rules"]["amount_reconciliation_tolerance"]
    except Exception:
        # Schema unreadable for some reason — fall back to a conservative
        # default rather than skipping the check entirely (silence here
        # would defeat the whole point: better a slightly-off tolerance
        # than no reconciliation check at all on a save).
        tolerance = 1.0

    row = db.execute(
        text("SELECT taxable_amount, total_gst_amount, total_amount "
             "FROM invoices WHERE invoice_id = :iid"),
        {"iid": invoice_id}).mappings().fetchone()
    if not row:
        return []

    line_sum = db.execute(
        text("SELECT COALESCE(SUM(amount), 0) FROM line_items WHERE invoice_id = :iid "
             "AND amount IS NOT NULL"),
        {"iid": invoice_id}).scalar()
    has_line_items = db.execute(
        text("SELECT COUNT(*) FROM line_items WHERE invoice_id = :iid AND amount IS NOT NULL"),
        {"iid": invoice_id}).scalar()

    taxable = row["taxable_amount"]
    gst = row["total_gst_amount"]
    total = row["total_amount"]
    issues = []

    # Line items (source of truth for what was actually bought) vs the
    # header's taxable_amount. Only checked when there's at least one line
    # item with a real amount — an invoice with no line items recorded at
    # all (e.g. extracted from a degraded scan) has nothing to reconcile
    # against, and that gap is already what NEEDS_MANUAL_REVIEW exists for.
    if has_line_items and taxable is not None:
        if abs(float(line_sum) - float(taxable)) > tolerance:
            issues.append(
                f"Line items sum to {line_sum:.2f}, but taxable_amount is {taxable:.2f}"
            )

    # taxable + gst ≈ total — same check validator.py already does at OCR
    # time (rule 3), now also enforced after a human edit, which previously
    # had zero reconciliation of any kind.
    if taxable is not None and gst is not None and total is not None:
        expected_total = float(taxable) + float(gst)
        if abs(expected_total - float(total)) > tolerance:
            issues.append(
                f"taxable_amount ({taxable:.2f}) + total_gst_amount ({gst:.2f}) = "
                f"{expected_total:.2f}, but total_amount is {total:.2f}"
            )

    return issues


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/me")
def whoami(ctx: dict = Depends(get_request_context)):
    return {"org_id": ctx["org_id"], "user_id": ctx["user_id"]}

@app.get("/invoices/_smoke_test")
def invoices_smoke_test(db: Session = Depends(get_db_for_request)):
    return {"invoice_count_for_this_org": db.execute(text("SELECT COUNT(*) FROM invoices")).scalar()}


# ── Upload ────────────────────────────────────────────────────────────────────

@app.post("/invoices/upload")
async def upload_invoice(
    file: UploadFile,
    ctx: dict = Depends(get_request_context),
    db: Session = Depends(get_db_for_request),
):
    """
    Runs OCR pipeline, then uploads bytes to Supabase Storage (if configured)
    for durable storage across Render deploys. Falls back to local disk if
    SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY are not set (local dev only).
    """
    if not file.filename.lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".tiff")):
        raise HTTPException(400, "Only PDF and common image formats are accepted")

    safe_name = f"{uuid.uuid4()}_{file.filename}"
    local_path = UPLOAD_DIR / safe_name
    contents = await file.read()
    local_path.write_bytes(contents)

    schema = core_pipeline.load_schema(SCHEMA_PATH)
    try:
        rows = core_pipeline.process_single_pdf(local_path, schema)
    except Exception as e:
        local_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Pipeline processing failed: {e}")

    if storage.is_configured():
        try:
            storage_path = storage.upload_file(
                ctx["org_id"], file.filename, contents,
                content_type=file.content_type or "application/octet-stream")
        except Exception as e:
            local_path.unlink(missing_ok=True)
            raise HTTPException(500, f"Storage upload failed: {e}")
        local_path.unlink(missing_ok=True)
    else:
        storage_path = str(local_path)

    saved = []
    for row in rows:
        row["file_name"] = file.filename
        invoice_id = save_invoice_row(
            db, ctx["org_id"], row, source_type="upload", storage_path=storage_path)
        saved.append({"invoice_id": invoice_id, "page": row.get("page"),
                      "status": row.get("status"), "confidence": row.get("confidence")})

    return {"file_name": file.filename, "pages_processed": len(saved), "results": saved}


# ── File URL (viewer iframe) ──────────────────────────────────────────────────

@app.get("/invoices/{invoice_id}/file-url")
def get_file_url(invoice_id: str, db: Session = Depends(get_db_for_request)):
    """Returns a signed Supabase Storage URL for the document viewer iframe,
    plus drive_file_id as a fallback (the frontend can open the Drive viewer
    URL directly if Supabase Storage is unavailable/paused).

    Fallback priority:
      1. Supabase signed URL (if storage configured + storage_path present)
      2. {url: null, drive_file_id: "..."} — frontend can open
         https://drive.google.com/file/d/{drive_file_id}/view in the iframe
      3. {url: null} — no file viewer available
    """
    row = db.execute(
        text("SELECT storage_path, drive_file_id FROM invoices WHERE invoice_id = :iid"),
        {"iid": invoice_id}).fetchone()
    if not row:
        raise HTTPException(404, "Invoice not found")
    storage_path, drive_file_id = row[0], row[1]
    drive_fallback = {"drive_file_id": drive_file_id} if drive_file_id else {}

    if not storage_path or not storage.is_configured() or not storage_path.startswith("invoices/"):
        return {"url": None, **drive_fallback}
    try:
        return {"url": storage.get_signed_url(storage_path), **drive_fallback}
    except Exception as e:
        # Supabase project may be paused (common on free tier) — return the
        # Drive fallback URL so the viewer stays usable
        return {"url": None, "storage_error": str(e), **drive_fallback}


def _extract_drive_folder_id(raw: str) -> str:
    """Accepts either a bare Drive folder ID or a full folder URL
    (e.g. https://drive.google.com/drive/folders/<ID>?usp=sharing,
    including the /u/0/ variant) and returns just the ID. Raises
    ValueError if nothing ID-shaped can be found, so callers can turn
    that into a clean 400 instead of silently saving garbage."""
    raw = raw.strip()
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", raw)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{10,}", raw):
        return raw
    raise ValueError(
        "Could not find a Drive folder ID in that value. Paste either the "
        "folder ID itself or the full folder URL."
    )


# ── Org settings ──────────────────────────────────────────────────────────────

class _DriveFolderBody(BaseModel):
    folder_id: str

@app.put("/org/drive-folder")
def set_drive_folder(
    body: _DriveFolderBody,
    ctx: dict = Depends(get_request_context),
    db: Session = Depends(get_db_for_request),
):
    """
    Registers this org's Drive folder once, server-side. After this,
    POST /drive/sync reads folder_id from this column — never from the
    request — closing the cross-tenant Drive leak (see PROJECT_LOG.md).

    Accepts either a bare folder ID or a full Drive folder URL — parsed
    server-side so this is correct regardless of what any future client
    sends, not just the current frontend. orgs.drive_folder_id has a
    UNIQUE constraint: if another org already claimed this exact folder
    (e.g. its ID leaked somewhere), this fails with 409, not a silent
    cross-tenant share.
    """
    try:
        folder_id = _extract_drive_folder_id(body.folder_id)
    except ValueError as e:
        raise HTTPException(400, str(e))

    prior = db.execute(
        text("SELECT drive_folder_id FROM orgs WHERE org_id = :oid"),
        {"oid": ctx["org_id"]}).fetchone()
    prior_folder_id = prior[0] if prior else None

    try:
        db.execute(
            text("UPDATE orgs SET drive_folder_id = :fid WHERE org_id = :oid"),
            {"fid": folder_id, "oid": ctx["org_id"]})
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            409,
            "This Drive folder is already registered to another account. "
            "Each folder can only be linked to one organization.")

    log_activity(
        db, ctx["org_id"], actor_type="user", actor_id=ctx["user_id"],
        action="settings_update", entity_type="drive_folder", entity_id=folder_id,
        field_name="drive_folder_id", old_value=prior_folder_id, new_value=folder_id,
        summary=f"Drive folder set to {folder_id}",
    )
    return {"org_id": ctx["org_id"], "drive_folder_id": folder_id}

@app.get("/org/drive-folder")
def get_drive_folder(
    ctx: dict = Depends(get_request_context),
    db: Session = Depends(get_db_for_request),
):
    row = db.execute(
        text("SELECT drive_folder_id FROM orgs WHERE org_id = :oid"),
        {"oid": ctx["org_id"]}).fetchone()
    return {"org_id": ctx["org_id"], "drive_folder_id": row[0] if row else None}


# ── Business description (HSN profile generation input) ─────────────────────

class _OrgSettingsBody(BaseModel):
    business_description: str

@app.put("/org/settings")
def set_org_settings(
    body: _OrgSettingsBody,
    ctx: dict = Depends(get_request_context),
    db: Session = Depends(get_db_for_request),
):
    """Saves the one-sentence business description used as the input to HSN
    profile generation. Saving this does NOT itself trigger generation -
    that's a separate explicit step (POST /org/hsn-profile/generate), so
    editing the description never silently regenerates or discards an
    existing reviewed profile."""
    desc = body.business_description.strip()
    if not desc:
        raise HTTPException(400, "business_description cannot be empty")
    prior = db.execute(
        text("SELECT business_description FROM orgs WHERE org_id = :oid"),
        {"oid": ctx["org_id"]}).fetchone()
    prior_desc = prior[0] if prior else None
    db.execute(
        text("UPDATE orgs SET business_description = :d WHERE org_id = :oid"),
        {"d": desc, "oid": ctx["org_id"]})
    log_activity(
        db, ctx["org_id"], actor_type="user", actor_id=ctx["user_id"],
        action="settings_update", entity_type="business_description",
        field_name="business_description", old_value=prior_desc, new_value=desc,
        summary="Business description updated",
    )
    return {"org_id": ctx["org_id"], "business_description": desc}

@app.get("/org/settings")
def get_org_settings(
    ctx: dict = Depends(get_request_context),
    db: Session = Depends(get_db_for_request),
):
    row = db.execute(
        text("SELECT business_description FROM orgs WHERE org_id = :oid"),
        {"oid": ctx["org_id"]}).fetchone()
    return {"org_id": ctx["org_id"], "business_description": row[0] if row else None}


# ── HSN/SAC profile ───────────────────────────────────────────────────────────
# Stored as individual rows in hsn_profile_codes (see schema.sql), not
# regenerated on every page load - the bug this fixes is that the frontend
# was previously calling endpoints that didn't exist at all, so nothing was
# ever actually persisted (see PROJECT_LOG.md). Generation is a strict
# preview-then-apply flow so a regeneration never silently destroys a
# previously-reviewed list or a manually-added code - the user explicitly
# confirms what to add/remove based on a diff against what's already saved.

def _load_hsn_profile(db: Session, org_id: str) -> dict:
    rows = db.execute(
        text("SELECT code, code_type, description, confidence, source, added_at "
             "FROM hsn_profile_codes WHERE org_id = :oid ORDER BY code"),
        {"oid": org_id}).mappings().all()
    expected = [dict(r) for r in rows if r["confidence"] != "ambiguous"]
    ambiguous = [dict(r) for r in rows if r["confidence"] == "ambiguous"]
    return {
        "org_id": org_id,
        "expected_hsn_codes": expected,
        "ambiguous_hsn_codes": ambiguous,
        "has_profile": len(rows) > 0,
    }

@app.get("/org/hsn-profile")
def get_hsn_profile(
    ctx: dict = Depends(get_request_context),
    db: Session = Depends(get_db_for_request),
):
    """Returns the SAVED profile - never regenerates. This is what Settings
    loads on mount; it was previously hitting a 404 (endpoint didn't exist)
    every single time, which is why the profile always looked empty/
    regenerated on reopen even though nothing was actually being lost."""
    return _load_hsn_profile(db, ctx["org_id"])


@app.post("/org/hsn-profile/generate")
def generate_hsn_profile_preview(
    ctx: dict = Depends(get_request_context),
    db: Session = Depends(get_db_for_request),
):
    """
    Calls the LLM and returns a PREVIEW diffed against the currently saved
    profile - does NOT save anything. The frontend shows this diff (new
    codes to add, previously-saved codes no longer suggested) and the user
    explicitly confirms via POST /org/hsn-profile/apply. This is the fix for
    "don't overwrite anything without asking" - generation and persistence
    are two separate steps on purpose.
    """
    org_row = db.execute(
        text("SELECT business_description FROM orgs WHERE org_id = :oid"),
        {"oid": ctx["org_id"]}).fetchone()
    desc = org_row[0] if org_row else None
    if not desc:
        raise HTTPException(400,
            "No business description saved yet. Save one in Settings first.")

    try:
        generated = hsn_generator.generate_hsn_profile(desc)
    except Exception as e:
        raise HTTPException(500, f"HSN generation failed: {e}")

    existing_rows = db.execute(
        text("SELECT code, confidence, source FROM hsn_profile_codes WHERE org_id = :oid"),
        {"oid": ctx["org_id"]}).mappings().all()
    existing_codes = {r["code"] for r in existing_rows}
    manual_codes = {r["code"] for r in existing_rows if r["source"] == "manual"}

    gen_expected = generated.get("expected_codes", [])
    gen_ambiguous = generated.get("ambiguous_codes", [])
    gen_codes = {c["code"] for c in gen_expected} | {c["code"] for c in gen_ambiguous}

    new_codes = [c for c in (gen_expected + gen_ambiguous) if c["code"] not in existing_codes]
    # Manually-added codes are never proposed for removal - regeneration only
    # ever affects previously-*generated* codes, per the module note in
    # hsn_generator.py and schema.sql's source column.
    removed_codes = sorted(existing_codes - gen_codes - manual_codes)
    unchanged_codes = sorted(existing_codes & gen_codes)

    return {
        "expected_codes": gen_expected,
        "ambiguous_codes": gen_ambiguous,
        "diff": {
            "new_codes": [c["code"] for c in new_codes],
            "codes_no_longer_suggested": removed_codes,
            "unchanged_codes": unchanged_codes,
        },
    }


class _HsnApplyBody(BaseModel):
    expected_codes: list[dict] = []
    ambiguous_codes: list[dict] = []
    remove_codes: list[str] = []  # codes the user confirmed removing from removed_codes above

@app.post("/org/hsn-profile/apply")
def apply_hsn_profile(
    body: _HsnApplyBody,
    ctx: dict = Depends(get_request_context),
    db: Session = Depends(get_db_for_request),
):
    """
    Commits a previously-previewed generation. expected_codes/ambiguous_codes
    are upserted with source='generated'; remove_codes are deleted (only
    ever codes the user explicitly confirmed from the preview's diff -
    main.py never deletes a code the frontend didn't list). Manually-added
    codes are untouched unless their exact code appears in remove_codes,
    which the frontend should never send for a manual-source code.
    """
    for c in body.expected_codes + body.ambiguous_codes:
        confidence = "ambiguous" if c in body.ambiguous_codes else "expected"
        db.execute(
            text("INSERT INTO hsn_profile_codes "
                 "(org_id, code, code_type, description, confidence, source) "
                 "VALUES (:oid, :code, :ctype, :desc, :conf, 'generated') "
                 "ON CONFLICT (org_id, code) DO UPDATE SET "
                 "  code_type = EXCLUDED.code_type, description = EXCLUDED.description, "
                 "  confidence = EXCLUDED.confidence "
                 "WHERE hsn_profile_codes.source = 'generated'"),
            {"oid": ctx["org_id"], "code": c["code"], "ctype": c.get("code_type", "HSN"),
             "desc": c.get("description"), "conf": confidence})

    for code in body.remove_codes:
        db.execute(
            text("DELETE FROM hsn_profile_codes WHERE org_id = :oid AND code = :code "
                 "AND source = 'generated'"),
            {"oid": ctx["org_id"], "code": code})

    log_activity(
        db, ctx["org_id"], actor_type="ai", actor_id=ctx["user_id"],
        action="hsn_profile_apply", entity_type="hsn_profile",
        summary=(
            f"Applied AI-generated HSN profile: {len(body.expected_codes)} expected, "
            f"{len(body.ambiguous_codes)} ambiguous, {len(body.remove_codes)} removed "
            f"(confirmed by user)"
        ),
    )
    return _load_hsn_profile(db, ctx["org_id"])


class _HsnManualCodeBody(BaseModel):
    code: str
    code_type: str = "HSN"
    description: Optional[str] = None

@app.post("/org/hsn-profile/codes")
def add_manual_hsn_code(
    body: _HsnManualCodeBody,
    ctx: dict = Depends(get_request_context),
    db: Session = Depends(get_db_for_request),
):
    """Manually adds a single code, independent of generation. Marked
    source='manual' so it's never touched/removed by a later regeneration -
    this is what 'move forward with whatever list I have' means in
    practice: regeneration only ever proposes changes to its own
    previously-generated codes, never to anything added by hand here."""
    code = body.code.strip().upper()
    if not code:
        raise HTTPException(400, "code cannot be empty")
    if body.code_type not in ("HSN", "SAC"):
        raise HTTPException(400, "code_type must be 'HSN' or 'SAC'")
    db.execute(
        text("INSERT INTO hsn_profile_codes (org_id, code, code_type, description, confidence, source) "
             "VALUES (:oid, :code, :ctype, :desc, 'expected', 'manual') "
             "ON CONFLICT (org_id, code) DO UPDATE SET "
             "  code_type = EXCLUDED.code_type, description = EXCLUDED.description"),
        {"oid": ctx["org_id"], "code": code, "ctype": body.code_type, "desc": body.description})
    log_activity(
        db, ctx["org_id"], actor_type="user", actor_id=ctx["user_id"],
        action="hsn_code_add", entity_type="hsn_profile_code", entity_id=code,
        new_value=code, summary=f"Manually added HSN/SAC code {code}",
    )
    return _load_hsn_profile(db, ctx["org_id"])

@app.delete("/org/hsn-profile/codes/{code}")
def remove_hsn_code(
    code: str,
    ctx: dict = Depends(get_request_context),
    db: Session = Depends(get_db_for_request),
):
    """Removes any code (generated or manual) by exact match. This is the
    one place a generated code can be removed outside the generate/apply
    diff flow - an explicit per-code delete the user clicked, not a side
    effect of regeneration."""
    result = db.execute(
        text("DELETE FROM hsn_profile_codes WHERE org_id = :oid AND code = :code"),
        {"oid": ctx["org_id"], "code": code})
    if result.rowcount == 0:
        raise HTTPException(404, f"Code '{code}' not found in profile")
    log_activity(
        db, ctx["org_id"], actor_type="user", actor_id=ctx["user_id"],
        action="hsn_code_remove", entity_type="hsn_profile_code", entity_id=code,
        old_value=code, summary=f"Removed HSN/SAC code {code}",
    )
    return _load_hsn_profile(db, ctx["org_id"])


# ── Drive sync ────────────────────────────────────────────────────────────────

@app.post("/drive/sync")
def sync_drive(
    ctx: dict = Depends(get_request_context),
    db: Session = Depends(get_db_for_request),
):
    """
    Syncs this org's registered Drive folder. folder_id is read server-side
    from orgs.drive_folder_id — NOT from the request. Register first via
    PUT /org/drive-folder (or the Settings page in the UI).
    """
    row = db.execute(
        text("SELECT drive_folder_id FROM orgs WHERE org_id = :oid"),
        {"oid": ctx["org_id"]}).fetchone()
    folder_id = row[0] if row else None
    if not folder_id:
        raise HTTPException(400,
            "No Drive folder registered. Go to Settings and enter your folder ID first.")
    schema = core_pipeline.load_schema(SCHEMA_PATH)
    try:
        results = sync_org_drive_folder(
            db, ctx["org_id"], folder_id, core_pipeline, drive_connector,
            schema, str(UPLOAD_DIR))
    except Exception as e:
        raise HTTPException(500, f"Drive sync failed: {e}")
    return {"folder_id": folder_id, "new_files_processed": len(results), "results": results}


# ── Background poll (cron-triggered) ─────────────────────────────────────────

@app.post("/admin/drive-poll-all")
def drive_poll_all(x_poll_secret: str = Header(default="")):
    """
    Polls every org's registered Drive folder. Called by GitHub Actions cron,
    not by users. Guarded by DRIVE_POLL_SECRET.
    Why not an in-process loop: Render free tier spins down after ~15 min of
    inactivity — an in-process loop dies and never self-wakes. External cron
    is the only reliable option on this tier.
    """
    if not DRIVE_POLL_SECRET or x_poll_secret != DRIVE_POLL_SECRET:
        raise HTTPException(401, "Invalid or missing poll secret")

    admin = SessionLocal()
    try:
        orgs = admin.execute(
            text("SELECT org_id, drive_folder_id FROM orgs WHERE drive_folder_id IS NOT NULL")
        ).fetchall()
    finally:
        admin.close()

    schema = core_pipeline.load_schema(SCHEMA_PATH)
    summary = []
    for org_id, folder_id in orgs:
        try:
            with _session_with_org(str(org_id)) as db:
                results = sync_org_drive_folder(
                    db, str(org_id), folder_id, core_pipeline, drive_connector,
                    schema, str(UPLOAD_DIR))
            summary.append({"org_id": str(org_id), "new_files_processed": len(results)})
        except Exception as e:
            summary.append({"org_id": str(org_id), "error": str(e)})
    return {"orgs_polled": len(orgs), "results": summary}


# ── Invoice PATCH (edits + learning loop) ────────────────────────────────────

class _LineItemPatch(BaseModel):
    line_item_id: Optional[str] = None
    description: Optional[str] = None
    hsn_code: Optional[str] = None
    quantity: Optional[float] = None
    rate: Optional[float] = None
    amount: Optional[float] = None
    line_tax_rate_percent: Optional[float] = None
    business_use_percent: Optional[float] = None
    delete: bool = False

class _InvoicePatch(BaseModel):
    fields: dict = {}
    line_items: list[_LineItemPatch] = []

@app.patch("/invoices/{invoice_id}")
def patch_invoice(
    invoice_id: str,
    body: _InvoicePatch,
    ctx: dict = Depends(get_request_context),
    db: Session = Depends(get_db_for_request),
):
    """
    Saves human corrections. Per changed field: (1) updates the invoices row,
    (2) inserts an edit_history row with old/new values — the learning loop
    foundation. Line items: update, delete, or insert.
    """
    bad = set(body.fields.keys()) - _EDITABLE_INVOICE_FIELDS
    if bad:
        raise HTTPException(400, f"Non-editable fields: {', '.join(sorted(bad))}")

    current = db.execute(
        text("SELECT * FROM invoices WHERE invoice_id = :iid"),
        {"iid": invoice_id}).mappings().fetchone()
    if not current:
        raise HTTPException(404, "Invoice not found")

    if body.fields:
        set_clause = ", ".join(f"{k} = :{k}" for k in body.fields)
        db.execute(
            text(f"UPDATE invoices SET {set_clause}, is_user_verified = true, "
                 # NOTE: ":uid::uuid" (Postgres's inline cast syntax butted
                 # right up against a SQLAlchemy bind param) raises
                 # "syntax error at or near ':'" under psycopg2 - the
                 # colon-colon is ambiguous with the bind-param colon at
                 # the parameter-substitution layer, not at the Postgres
                 # parser layer. CAST(:uid AS uuid) means the exact same
                 # thing without that ambiguity, and works in every
                 # position. Same fix applied to every other :x::type
                 # occurrence below.
                 "last_edited_by = CAST(:uid AS uuid), last_edited_at = now() "
                 "WHERE invoice_id = :iid"),
            {**body.fields, "iid": invoice_id, "uid": ctx["user_id"]})
        for field, new_val in body.fields.items():
            old_val = current.get(field)
            if str(old_val or "") != str(new_val or ""):
                db.execute(
                    text("INSERT INTO edit_history "
                         "(org_id, invoice_id, edited_by, field_name, old_value, new_value, edit_reason) "
                         "VALUES (:oid, :iid, CAST(:uid AS uuid), :field, :old, :new, 'correction')"),
                    {"oid": ctx["org_id"], "iid": invoice_id, "uid": ctx["user_id"],
                     "field": field,
                     "old": str(old_val) if old_val is not None else None,
                     "new": str(new_val) if new_val is not None else None})
                log_activity(
                    db, ctx["org_id"], actor_type="user", actor_id=ctx["user_id"],
                    action="invoice_field_edit", entity_type="invoice", entity_id=invoice_id,
                    field_name=field, old_value=old_val, new_value=new_val,
                    summary=f"{field} changed on {current.get('file_name') or invoice_id}",
                )

    # IMPORTANT: line item updates must be PARTIAL, not full-row overwrites.
    # The frontend often sends only the field(s) actually being edited (e.g.
    # business_use_percent alone from the Biz-use% editor, or just
    # description/hsn_code from the row editor) - a blind "SET col = :val"
    # for every column would NULL out (or reset to a default) every column
    # the caller didn't intend to touch. COALESCE(:val, existing_col) means
    # "if the caller didn't send this field, keep what's already there."
    # business_use_percent still defaults to 100 specifically when the
    # CALLER explicitly clears it (not when they simply omit it), which is
    # why that one uses COALESCE(:bup, business_use_percent, 100) - the
    # innermost fallback only kicks in if the column itself was already NULL.
    for li in body.line_items:
        if li.delete and li.line_item_id:
            prior = db.execute(
                text("SELECT description, hsn_code FROM line_items WHERE line_item_id = :lid"),
                {"lid": li.line_item_id}).mappings().fetchone()
            db.execute(text("DELETE FROM line_items WHERE line_item_id = :lid"),
                       {"lid": li.line_item_id})
            log_activity(
                db, ctx["org_id"], actor_type="user", actor_id=ctx["user_id"],
                action="line_item_delete", entity_type="line_item", entity_id=li.line_item_id,
                old_value=prior["description"] if prior else None,
                summary=f"Deleted line item on {current.get('file_name') or invoice_id}"
                        + (f": {prior['description']}" if prior and prior["description"] else ""),
            )
        elif li.line_item_id:
            prior = db.execute(
                text("SELECT description, hsn_code, quantity, rate, amount, "
                     "line_tax_rate_percent, business_use_percent FROM line_items "
                     "WHERE line_item_id = :lid"),
                {"lid": li.line_item_id}).mappings().fetchone()
            db.execute(
                text(
                    "UPDATE line_items SET "
                    "  description = COALESCE(:desc, description), "
                    "  hsn_code = COALESCE(:hsn, hsn_code), "
                    "  quantity = COALESCE(:qty, quantity), "
                    "  rate = COALESCE(:rate, rate), "
                    "  amount = COALESCE(:amt, amount), "
                    "  line_tax_rate_percent = COALESCE(:ltrp, line_tax_rate_percent), "
                    "  business_use_percent = COALESCE(:bup, business_use_percent, 100) "
                    "WHERE line_item_id = :lid"
                ),
                {"lid": li.line_item_id, "desc": li.description, "hsn": li.hsn_code,
                 "qty": li.quantity, "rate": li.rate, "amt": li.amount,
                 "ltrp": li.line_tax_rate_percent, "bup": li.business_use_percent})
            if prior:
                changed = []
                for field, new_val in (
                    ("description", li.description), ("hsn_code", li.hsn_code),
                    ("quantity", li.quantity), ("rate", li.rate), ("amount", li.amount),
                    ("line_tax_rate_percent", li.line_tax_rate_percent),
                    ("business_use_percent", li.business_use_percent),
                ):
                    if new_val is not None and str(prior.get(field) or "") != str(new_val):
                        changed.append((field, prior.get(field), new_val))
                for field, old_val, new_val in changed:
                    log_activity(
                        db, ctx["org_id"], actor_type="user", actor_id=ctx["user_id"],
                        action="line_item_edit", entity_type="line_item", entity_id=li.line_item_id,
                        field_name=field, old_value=old_val, new_value=new_val,
                        summary=f"Line item {field} changed on {current.get('file_name') or invoice_id}",
                    )
        elif not li.delete:
            result = db.execute(
                text("INSERT INTO line_items "
                     "(org_id, invoice_id, description, hsn_code, quantity, rate, amount, "
                     " line_tax_rate_percent, business_use_percent) "
                     "VALUES (:oid, :iid, :desc, :hsn, :qty, :rate, :amt, :ltrp, :bup) "
                     "RETURNING line_item_id"),
                {"oid": ctx["org_id"], "iid": invoice_id, "desc": li.description,
                 "hsn": li.hsn_code, "qty": li.quantity, "rate": li.rate, "amt": li.amount,
                 "ltrp": li.line_tax_rate_percent,
                 "bup": li.business_use_percent if li.business_use_percent is not None else 100})
            new_lid = result.scalar()
            log_activity(
                db, ctx["org_id"], actor_type="user", actor_id=ctx["user_id"],
                action="line_item_add", entity_type="line_item", entity_id=str(new_lid),
                new_value=li.description,
                summary=f"Added line item on {current.get('file_name') or invoice_id}"
                        + (f": {li.description}" if li.description else ""),
            )

    # Re-check amount reconciliation AFTER every edit in this request has
    # been applied (header fields above, line items below all run first —
    # this must be the last thing before the function returns). Per
    # project decision: never reject the save outright — apply it, but
    # flag the invoice so the mismatch is visible rather than silently
    # wrong. A user correcting one field at a time (as in the project log's
    # walkthrough: edit total_amount, see the warning, then edit a line
    # item) needs every individual save to succeed; only the STATUS should
    # reflect whether the invoice currently adds up.
    reconciliation_issues = _reconcile_invoice_amounts(db, invoice_id)
    if reconciliation_issues:
        db.execute(
            text("UPDATE invoices SET status = 'WARNING', "
                 "issues = :issues WHERE invoice_id = :iid"),
            {"iid": invoice_id, "issues": "; ".join(reconciliation_issues)})
        log_activity(
            db, ctx["org_id"], actor_type="user", actor_id=ctx["user_id"],
            action="invoice_field_edit", entity_type="invoice", entity_id=invoice_id,
            field_name="status", old_value=current.get("status"), new_value="WARNING",
            summary=f"Flagged WARNING after edit on {current.get('file_name') or invoice_id}: "
                    + "; ".join(reconciliation_issues),
        )
    elif current.get("status") == "WARNING" and current.get("issues") and (
        "taxable_amount is" in current["issues"] or "total_amount is" in current["issues"]
    ):
        # Only auto-clear when the EXISTING warning text matches the
        # specific phrasing this function produces (see the two issue
        # strings above) — i.e. we can actually confirm the problem we're
        # about to clear is the one we just re-checked. A WARNING set for
        # an unrelated reason (e.g. validator.py flagging a GSTIN format
        # problem at OCR time) must never be silently cleared just because
        # this one PATCH happened not to touch amounts — this check has no
        # way to know whether THAT problem is still unresolved, so it has
        # no business clearing the flag for it.
        db.execute(
            text("UPDATE invoices SET status = 'PASSED', issues = NULL "
                 "WHERE invoice_id = :iid"),
            {"iid": invoice_id})

    return {
        "invoice_id": invoice_id,
        "updated": True,
        "reconciliation_issues": reconciliation_issues,
    }


# ── Activity log (read) ───────────────────────────────────────────────────────

@app.get("/activity-log")
def get_activity_log(
    db: Session = Depends(get_db_for_request),
    entity_type: Optional[str] = Query(default=None),
    actor_type: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    """
    Returns this org's activity log, newest first - covers invoice edits,
    line item edits/deletes/adds, and settings changes (Drive folder,
    business description, HSN profile). RLS scopes this to the calling
    org automatically; no org_id filter needed here beyond what db's
    session context already enforces.
    """
    filters, params = [], {}
    if entity_type:
        filters.append("entity_type = :etype")
        params["etype"] = entity_type
    if actor_type:
        if actor_type not in ("user", "ai"):
            raise HTTPException(400, "actor_type must be 'user' or 'ai'")
        filters.append("actor_type = :atype")
        params["atype"] = actor_type
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size

    count_p = {k: v for k, v in params.items() if k not in ("limit", "offset")}
    total = db.execute(text(f"SELECT COUNT(*) FROM activity_log {where}"), count_p).scalar()
    rows = db.execute(
        text(f"""SELECT log_id, actor_type, actor_id, action, entity_type, entity_id,
                        field_name, old_value, new_value, summary, created_at
                 FROM activity_log {where}
                 ORDER BY created_at DESC
                 LIMIT :limit OFFSET :offset"""),
        params).mappings().all()
    return {
        "total_count": total, "page": page, "page_size": page_size,
        "total_pages": max(1, -(-total // page_size)),
        "entries": [dict(r) for r in rows],
    }


# ── List / filter / sort / paginate ──────────────────────────────────────────

@app.get("/invoices")
def list_invoices(
    db: Session = Depends(get_db_for_request),
    status: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    sort_by: str = Query(default="processed_at"),
    sort_dir: str = Query(default="desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    col = sort_by if sort_by in _SORTABLE_COLUMNS else "processed_at"
    direction = "DESC" if sort_dir.strip().upper() == "DESC" else "ASC"
    filters, params = [], {}
    if status:
        if status not in {"PASSED", "WARNING", "FAILED", "NEEDS_MANUAL_REVIEW"}:
            raise HTTPException(400, f"Invalid status '{status}'")
        filters.append("status = :status"); params["status"] = status
    if search:
        filters.append("vendor_name ILIKE :search"); params["search"] = f"%{search}%"
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size
    count_p = {k: v for k, v in params.items() if k not in ("limit", "offset")}
    total = db.execute(text(f"SELECT COUNT(*) FROM invoices {where}"), count_p).scalar()
    rows = db.execute(
        text(f"""SELECT invoice_id, file_name, page, vendor_name, vendor_gstin,
                        invoice_number, invoice_date, total_amount, total_gst_amount,
                        taxable_amount, status, confidence, extraction_method,
                        issues, is_user_verified, processed_at
                 FROM invoices {where}
                 ORDER BY {col} {direction} NULLS LAST
                 LIMIT :limit OFFSET :offset"""),
        params).mappings().all()
    return {"total_count": total, "page": page, "page_size": page_size,
            "total_pages": max(1, -(-total // page_size)),
            "invoices": [dict(r) for r in rows]}


# ── ITC summary ───────────────────────────────────────────────────────────────
# "Everything": total claimable ITC across all invoices, with breakdowns by
# HSN-profile status (expected/ambiguous/unknown - joined against
# hsn_profile_codes) and by vendor, so a number alone isn't the only thing
# shown - the breakdown is what actually explains WHY the total is what it
# is and where to look if something seems off.
#
# Per-line claimable amount = line's tax amount x business_use_percent/100.
# Line tax amount: uses line_tax_rate_percent x line amount when the
# invoice printed a genuinely different per-line rate; otherwise falls back
# to the invoice's bill-level total_gst_amount apportioned by this line's
# share of the invoice's taxable_amount (this fallback is mathematically
# identical to a uniform per-line rate, which is the case schema.sql's
# line_tax_rate_percent note describes - it is NOT double-counted against
# line_tax_rate_percent, only one or the other applies per line).

@app.get("/itc-summary")
def itc_summary(
    db: Session = Depends(get_db_for_request),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
):
    """Returns total claimable ITC plus breakdowns. date_from/date_to filter
    by invoices.invoice_date (ISO YYYY-MM-DD), both optional."""
    filters, params = [], {}
    if date_from:
        filters.append("i.invoice_date >= :date_from"); params["date_from"] = date_from
    if date_to:
        filters.append("i.invoice_date <= :date_to"); params["date_to"] = date_to
    where = ("AND " + " AND ".join(filters)) if filters else ""

    rows = db.execute(
        text(f"""
            SELECT
                li.line_item_id, li.hsn_code, li.amount, li.business_use_percent,
                li.line_tax_rate_percent,
                i.invoice_id, i.vendor_name, i.taxable_amount, i.total_gst_amount,
                hp.confidence AS hsn_status
            FROM line_items li
            JOIN invoices i ON i.invoice_id = li.invoice_id
            LEFT JOIN hsn_profile_codes hp
                ON hp.org_id = li.org_id AND hp.code = li.hsn_code
            WHERE i.status != 'FAILED' {where}
        """), params).mappings().all()

    total_claimable = 0.0
    by_vendor: dict[str, float] = {}
    by_hsn_status = {"expected": 0.0, "ambiguous": 0.0, "manual": 0.0, "unknown": 0.0}
    line_count = 0
    flagged_ambiguous = []

    for r in rows:
        line_amount = float(r["amount"] or 0)
        taxable = float(r["taxable_amount"] or 0)
        bill_gst = float(r["total_gst_amount"] or 0)
        biz_pct = float(r["business_use_percent"] if r["business_use_percent"] is not None else 100) / 100

        if r["line_tax_rate_percent"] is not None:
            # Genuinely different per-line rate was printed - use it directly,
            # not the bill-level apportionment below.
            line_tax = line_amount * float(r["line_tax_rate_percent"]) / 100
        elif taxable > 0:
            # No distinct per-line rate printed - apportion the bill-level
            # GST by this line's share of the invoice's taxable amount.
            # Mathematically equals "apply the bill rate to every line"
            # when every line in fact shares one rate, which is the normal
            # case; this is just the apportionment math for it.
            line_tax = bill_gst * (line_amount / taxable)
        else:
            line_tax = 0.0

        claimable = line_tax * biz_pct
        total_claimable += claimable
        line_count += 1

        vendor = r["vendor_name"] or "(unidentified vendor)"
        by_vendor[vendor] = by_vendor.get(vendor, 0.0) + claimable

        status = r["hsn_status"] if r["hsn_status"] in by_hsn_status else "unknown"
        by_hsn_status[status] += claimable
        if status == "ambiguous":
            flagged_ambiguous.append({
                "invoice_id": str(r["invoice_id"]), "hsn_code": r["hsn_code"],
                "claimable": round(claimable, 2),
            })

    return {
        "total_claimable_itc": round(total_claimable, 2),
        "line_items_counted": line_count,
        "by_vendor": {k: round(v, 2) for k, v in sorted(
            by_vendor.items(), key=lambda kv: kv[1], reverse=True)},
        "by_hsn_status": {k: round(v, 2) for k, v in by_hsn_status.items()},
        "ambiguous_lines_needing_review": flagged_ambiguous,
        "note": (
            "Claimable amount = line tax x business-use %. Business-use % "
            "defaults to 100 and must be edited per line for any item with "
            "a personal/mixed-use portion (e.g. partial-quantity purchases) "
            "- this total does not itself know which lines need that edit."
        ),
    }

@app.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str, db: Session = Depends(get_db_for_request)):
    row = db.execute(
        text("SELECT * FROM invoices WHERE invoice_id = :iid"),
        {"iid": invoice_id}).mappings().fetchone()
    if not row:
        raise HTTPException(404, "Invoice not found")
    items = db.execute(
        text("SELECT line_item_id, description, hsn_code, quantity, rate, amount, "
             "line_tax_rate_percent, business_use_percent, "
             "itc_claimable, itc_reason FROM line_items WHERE invoice_id = :iid "
             "ORDER BY line_item_id"),
        {"iid": invoice_id}).mappings().all()
    return {**dict(row), "line_items": [dict(i) for i in items]}