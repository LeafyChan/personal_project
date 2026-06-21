"""
main.py
=======
Wires the full request chain together:
  Bearer token -> auth.get_current_org() [verify signature, extract Clerk IDs]
  -> org_resolver.resolve_org_and_user() [Clerk ID -> our UUID, JIT-provision]
  -> db.get_org_scoped_db() [SET app.current_org_id, RLS now active]
  -> route handler [only ever sees this org's rows, guaranteed by Postgres]

UPLOAD_DIR / CORE_PIPELINE_PATH:
This file imports the existing OCR/extraction pipeline from ../../core
(personal_project/core - the original local pipeline, unchanged) rather
than duplicating that logic here. Uploaded files are saved under
UPLOAD_DIR on local disk for now - swapping this for real cloud storage
(S3/Supabase Storage) is a planned upgrade, not done in this pass, since
the goal here was proving the org-scoped DB write path works, not storage
infrastructure.

Run locally:
    export DATABASE_URL="postgresql://invoice_app.PROJECT_REF:PASSWORD@aws-REGION.pooler.supabase.com:5432/postgres"
    export CLERK_JWKS_URL="https://your-instance.clerk.accounts.dev/.well-known/jwks.json"
    export CLERK_ALLOWED_ORIGINS="http://localhost:3000"
    export CORE_PIPELINE_PATH="/home/leafy/personal_project"   # parent of core/
    export GROQ_API_KEY="..."     # same keys the local pipeline already uses
    export GEMINI_API_KEY="..."
    export INVOICE_OCR_DEMO_MODE=0
    uvicorn app.main:app --reload --port 8000
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import get_current_org
from app.db import get_org_scoped_db
from app.org_resolver import resolve_org_and_user
from app.invoice_store import save_invoice_row

# Makes the EXISTING core/ pipeline importable without duplicating it.
# CORE_PIPELINE_PATH should be the personal_project directory itself (the
# one that directly contains core/), not core/ itself.
_core_parent = os.environ.get("CORE_PIPELINE_PATH")
if not _core_parent:
    raise RuntimeError(
        "CORE_PIPELINE_PATH is not set. Point it at your personal_project "
        "directory (the one containing core/), e.g. "
        "export CORE_PIPELINE_PATH=/home/leafy/personal_project"
    )
sys.path.insert(0, _core_parent)
sys.path.insert(0, str(Path(_core_parent) / "core"))
import pipeline as core_pipeline  # noqa: E402  (path setup must run first)

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", tempfile.gettempdir()) ) / "invoice_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

SCHEMA_PATH = os.environ.get(
    "INVOICE_SCHEMA_PATH", str(Path(_core_parent) / "config" / "invoice_schema.json")
)

app = FastAPI(title="Invoice Intelligence API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # tighten to real frontend domain(s) before production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_request_context(clerk_ctx: dict = Depends(get_current_org)) -> dict:
    """Combines token verification + org resolution into one dependency.
    This is what every tenant-data route should depend on."""
    resolved = resolve_org_and_user(
        clerk_org_id=clerk_ctx["org_id"],
        clerk_user_id=clerk_ctx["user_id"],
    )
    return {**clerk_ctx, **resolved}  # merges Clerk IDs + our internal UUIDs


def get_db_for_request(ctx: dict = Depends(get_request_context)):
    """The actual per-route DB dependency: org-scoped, RLS-active session."""
    yield from get_org_scoped_db(ctx["org_id"])


@app.get("/health")
def health():
    """Unauthenticated - just confirms the service is up. No DB access."""
    return {"status": "ok"}


@app.get("/me")
def whoami(ctx: dict = Depends(get_request_context)):
    """Authenticated smoke-test: proves token verification + org resolution
    work end to end, without touching tenant data yet."""
    return {
        "org_id": ctx["org_id"],
        "user_id": ctx["user_id"],
        "clerk_org_id": ctx["org_id"],
    }


@app.get("/invoices/_smoke_test")
def invoices_smoke_test(db: Session = Depends(get_db_for_request)):
    """
    Authenticated + RLS-scoped smoke test: proves the FULL chain, including
    the database isolation guarantee. Returns only this org's invoice count
    - if RLS were broken, this would return every org's invoices summed
    together, so this single endpoint is a meaningful live check, not just
    a connectivity ping.
    """
    result = db.execute(text("SELECT COUNT(*) FROM invoices")).scalar()
    return {"invoice_count_for_this_org": result}


@app.post("/invoices/upload")
async def upload_invoice(file: UploadFile, ctx: dict = Depends(get_request_context),
                          db: Session = Depends(get_db_for_request)):
    """
    Accepts a PDF/image upload, runs it through the EXISTING core/ OCR +
    extraction + validation pipeline (unchanged logic - same Groq/Gemini
    routing, same tax-terminology normalization, same confidence/review
    flagging), then saves every resulting page row into this org's
    Postgres rows via invoice_store.save_invoice_row.

    Multi-page files produce multiple invoice rows (one per page), matching
    process_single_pdf's existing behavior - this endpoint returns all of
    them, not just the first.
    """
    if not file.filename.lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".tiff")):
        raise HTTPException(400, "Only PDF and common image formats are accepted")

    # Unique on-disk name to avoid collisions between orgs/users uploading
    # files with the same original filename at the same time.
    safe_name = f"{uuid.uuid4()}_{file.filename}"
    local_path = UPLOAD_DIR / safe_name
    contents = await file.read()
    local_path.write_bytes(contents)

    schema = core_pipeline.load_schema(SCHEMA_PATH)
    try:
        rows = core_pipeline.process_single_pdf(local_path, schema)
    except Exception as e:
        raise HTTPException(500, f"Pipeline processing failed: {e}")

    saved = []
    for row in rows:
        row["file_name"] = file.filename  # store the ORIGINAL name, not the disk-safe one
        invoice_id = save_invoice_row(
            db, ctx["org_id"], row, source_type="upload", storage_path=str(local_path)
        )
        saved.append({
            "invoice_id": invoice_id,
            "page": row.get("page"),
            "status": row.get("status"),
            "confidence": row.get("confidence"),
        })

    return {"file_name": file.filename, "pages_processed": len(saved), "results": saved}


@app.get("/invoices")
def list_invoices(db: Session = Depends(get_db_for_request)):
    """
    Lists this org's invoices, most recent first. Deliberately minimal for
    now (no pagination/sorting params yet) - exists to prove uploads are
    actually retrievable, full sort/filter UI support comes with Phase 3.
    """
    rows = db.execute(
        text(
            "SELECT invoice_id, file_name, vendor_name, invoice_date, total_amount, "
            "status, confidence, processed_at "
            "FROM invoices ORDER BY processed_at DESC LIMIT 100"
        )
    ).mappings().all()
    return {"invoices": [dict(r) for r in rows]}