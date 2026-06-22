"""
storage.py
==========
Supabase Storage wrapper. All uploaded/synced invoice files are stored here
rather than on Render's ephemeral local disk — local disk is wiped on every
redeploy, Supabase Storage persists.

Files are stored at: invoices/{org_id}/{uuid}_{original_filename}
This path structure means no cross-org reads are possible even if the
bucket's RLS were misconfigured — org_id is in the path, not just in
metadata. The backend reads/writes via the service_role key (bypasses
Storage RLS intentionally, since our application already enforces org
isolation one layer up via Postgres RLS). The frontend NEVER gets this key.

Signed URLs: GET /invoices/{invoice_id}/file-url returns a time-limited
signed URL for the viewer iframe. Supabase generates these server-side;
the frontend loads the file without needing the service_role key.

One-time Supabase setup (do this once in the dashboard):
  Storage → New bucket → name: "invoices" → uncheck Public → Create
  Settings → API → copy "service_role" secret (NOT anon key)

Env vars required:
  SUPABASE_URL=https://wflguoqnrxijfvdeaxhb.supabase.co
  SUPABASE_SERVICE_ROLE_KEY=<service_role key, starts with eyJ...>

If these aren't set, is_configured() returns False and callers fall back
to local disk storage_path (safe for local dev, not for production).
"""

import os
import uuid

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
BUCKET = "invoices"


def is_configured() -> bool:
    """Returns True only when both env vars are present and non-empty."""
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
    }


def upload_file(org_id: str, original_filename: str, file_bytes: bytes,
                content_type: str = "application/octet-stream") -> str:
    """
    Uploads file_bytes to Supabase Storage. Returns the storage_path string
    (bucket/org_id/uuid_filename) which is what gets saved in invoices.storage_path.

    Raises RuntimeError if the upload fails — callers should propagate this
    as an HTTP 500 rather than silently saving a broken storage_path.
    """
    safe_name = f"{uuid.uuid4()}_{original_filename}"
    path = f"{BUCKET}/{org_id}/{safe_name}"
    url = f"{SUPABASE_URL}/storage/v1/object/{path}"
    headers = {**_headers(), "Content-Type": content_type}
    r = requests.post(url, headers=headers, data=file_bytes, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"Supabase Storage upload failed (HTTP {r.status_code}): {r.text[:300]}"
        )
    return path


def get_signed_url(storage_path: str, expires_in: int = 3600) -> str:
    """
    Returns a time-limited signed URL for storage_path (valid for expires_in
    seconds, default 1 hour). Used by GET /invoices/{id}/file-url to give
    the frontend a URL it can load in an iframe without the service_role key.

    storage_path is the value stored in invoices.storage_path, e.g.
    "invoices/abc-123/uuid_invoice.pdf" — passed straight through.
    """
    url = f"{SUPABASE_URL}/storage/v1/object/sign/{storage_path}"
    r = requests.post(
        url,
        headers={**_headers(), "Content-Type": "application/json"},
        json={"expiresIn": expires_in},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"Signed URL generation failed (HTTP {r.status_code}): {r.text[:300]}"
        )
    data = r.json()
    signed = data.get("signedURL") or data.get("signedUrl")
    if not signed:
        raise RuntimeError(f"No signedURL in Supabase response: {data}")
    # Supabase returns a relative path — prefix with the project URL
    return f"{SUPABASE_URL}{signed}" if signed.startswith("/") else signed