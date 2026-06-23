# Invoice Intelligence System — Project Log

---

## ⚠️ ROTATE THESE BEFORE ANY REAL CLIENT DATA

Every key below has been shared in chat history (this log, and earlier chat sessions) and must be treated as compromised.

| Key | Rotate at |
|-----|-----------|
| Supabase `invoice_app` DB password | Supabase → Settings → Database → Reset password |
| Supabase `postgres` DB password | Same |
| Supabase service role key | Supabase → Settings → API → Regenerate |
| Groq API key | console.groq.com → API Keys |
| Gemini API key | aistudio.google.com → API Keys |
| Clerk secret key | Clerk dashboard → API Keys → Regenerate |
| `gdrive_key.json` service account | GCP → IAM → Service Accounts → Keys → Delete + Add new |
| `venv/` and `gdrive_key.json` in git | Remove from history: `git filter-repo --path venv --path gdrive_key.json --invert-paths` |

---

## Project Goal

Multi-tenant SaaS for Indian SMEs: extract invoice data from PDFs and images, validate against GST rules, track vendor compliance, and surface ITC-claimability risk. Full tenant data isolation via Postgres Row-Level Security.

---

## Directory Structure (confirmed live, June 2026)

```
personal_project/
├── config/                          # empty — schema actually lives at core/config/
├── core/                            # Standalone Python pipeline (no web dependency)
│   ├── config/
│   │   └── invoice_schema.json      # Drives extraction + validation — edit here, no code changes needed
│   ├── database.py                  # SQLite DB for local / offline pipeline runs
│   ├── drive_connector.py           # Google Drive listing + download (service account)
│   ├── extractor.py                 # LLM extraction: Groq primary, Gemini fallback
│   ├── ocr_engine.py                # Tiered OCR: digital text → Tesseract → Vision AI
│   ├── pipeline.py                  # Orchestrates OCR → extract → validate → save
│   └── validator.py                 # GST business rules: GSTIN format, amount reconciliation, etc.
├── invoices/                        # Temp download dir for Drive sync (scratch only)
├── output/
│   └── invoices.db                  # SQLite output for local pipeline runs
├── test_data/                       # Sample PDFs for local testing
├── venv/                            # ⚠ DO NOT COMMIT (already in repo — fix with filter-repo)
├── gdrive_key.json                  # ⚠ DO NOT COMMIT (already in repo — fix with filter-repo)
├── requirements.txt
└── webapp/
    ├── backend/
    │   ├── .env
    │   ├── gdrive_key.json          # ⚠ duplicate copy, also DO NOT COMMIT
    │   ├── schema.sql                # Postgres schema with RLS — apply to fresh DB only
    │   ├── verify_rls.py             # RLS isolation test — run before any real client data
    │   └── app/
    │       ├── auth.py               # Clerk JWT verification (RS256 via JWKS)
    │       ├── db.py                 # SQLAlchemy + session-scoped RLS context setter
    │       ├── drive_sync.py         # Bridges Drive connector into multi-tenant Postgres
    │       ├── hsn_generator.py      # LLM-based HSN/SAC profile generation from biz description
    │       ├── invoice_store.py      # Saves pipeline output into Postgres (org-scoped)
    │       ├── main.py               # All FastAPI routes
    │       ├── org_resolver.py       # Clerk string IDs → internal UUIDs (JIT provisioning)
    │       └── storage.py            # Supabase Storage wrapper (upload + signed URLs)
    └── frontend/
        ├── .env.local
        ├── index.html
        ├── package.json
        ├── vite.config.js
        └── src/
            ├── main.jsx                  # React entry, ClerkProvider
            ├── App.jsx                   # Shell: login, org creation, top nav, tab routing
            ├── InvoiceList.jsx           # Invoice list + 3-state detail panel + ITC editor
            ├── InvoiceReviewUtils.js     # shouldAutoReview() — split out for Fast Refresh compliance
            ├── ReviewModal.jsx           # Document viewer + fill-in-the-blanks review popup
            ├── ActivityLog.jsx           # Activity Log page (new)
            ├── Settings.jsx              # Drive folder + business description + HSN profile editor
            └── Itcsummary.jsx            # ITC summary view with vendor/HSN breakdowns
```

**Note on filename casing:** the live filesystem has `InvoiceReviewUtils.js` and `Itcsummary.jsx` (capital-then-lowercase) — imports elsewhere must match this exact casing, since Linux filesystems (and Vite's resolver) are case-sensitive. This has already been a source of one prior bug (`./ReviewModal` not resolving) and is worth double-checking again given the blank Activity page symptom — see Open Items.

---

## Environment Variables

### Backend — `webapp/backend/.env` (used by `app/`)

```env
# Supabase Session Pooler (NOT direct — WSL2/IPv6 issue; NOT transaction mode — breaks SET)
DATABASE_URL=postgresql://postgres.wflguoqnrxijfvdeaxhb:PASSWORD@aws-1-ap-south-1.pooler.supabase.com:5432/postgres

# Clerk
CLERK_JWKS_URL=https://ready-crane-41.clerk.accounts.dev/.well-known/jwks.json
CLERK_SECRET_KEY=sk_test_...
CLERK_ALLOWED_ORIGINS=http://localhost:3000

# LLM APIs
GROQ_API_KEY=gsk_...
GEMINI_API_KEY=...

# Supabase Storage
SUPABASE_URL=https://wflguoqnrxijfvdeaxhb.supabase.co
SUPABASE_SERVICE_ROLE_KEY=sb_secret_...

# Drive
GDRIVE_KEY_PATH=../../gdrive_key.json

# Drive poll cron auth
DRIVE_POLL_SECRET=<long random string>

# Core pipeline location (needed by hsn_generator.py)
CORE_PIPELINE_PATH=/home/leafy/personal_project
```

### Frontend — `webapp/frontend/.env.local`

```env
VITE_CLERK_PUBLISHABLE_KEY=pk_test_...
VITE_API_BASE=http://localhost:8000
```

### Supabase connection note

Always use the **Session Pooler** connection string (not direct, not transaction mode):
- Direct = IPv6 → WSL2 can't reach it
- Transaction mode = resets `SET app.current_org_id` between transactions, silently breaking RLS
- Session mode = IPv4-compatible + session-persistent `SET` = correct

Find at: Supabase → Settings → Database → Connection string → Session mode.

**If the document viewer says a Supabase URL "refused to connect":** this is very likely the Supabase project itself being paused (common on the free tier after a period of inactivity), not a code bug — see Open Items, item 3.

### Supabase roles

The app uses the `invoice_app` role (not `postgres`) with `NOBYPASSRLS`:

```sql
CREATE ROLE invoice_app WITH LOGIN PASSWORD '...' NOBYPASSRLS;
GRANT CONNECT ON DATABASE postgres TO invoice_app;
GRANT USAGE ON SCHEMA public TO invoice_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO invoice_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO invoice_app;
```

**Critically: `invoice_app` has no `CREATE` privilege on `public`.** Any new table must be created once, by hand, in the Supabase SQL Editor (which connects as a privileged role) — never via app-level DDL at import time. This was the root cause of one of the two server-crash bugs below.

---

## Starting and Stopping the Project

### Backend — start

```bash
cd ~/personal_project/webapp/backend

export GOOGLE_APPLICATION_CREDENTIALS="/home/leafy/personal_project/gdrive_key.json"
export GDRIVE_KEY_PATH="/home/leafy/personal_project/gdrive_key.json"
export GROQ_API_KEY="gsk_..."
export GEMINI_API_KEY="..."
export CLERK_JWKS_URL="https://ready-crane-41.clerk.accounts.dev/.well-known/jwks.json"
export CORE_PIPELINE_PATH="/home/leafy/personal_project/core"
export DATABASE_URL="postgresql://postgres.wflguoqnrxijfvdeaxhb:PASSWORD@aws-1-ap-south-1.pooler.supabase.com:5432/postgres"
export INVOICE_OCR_DEMO_MODE="0"
export SUPABASE_URL="https://wflguoqnrxijfvdeaxhb.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="<service_role JWT — starts with eyJ..., NOT sk_test_...>"

python -m uvicorn app.main:app --reload --port 8000
```

**Two things worth double-checking against this exact set of vars:**

- **`CORE_PIPELINE_PATH` now points at `core/`, not the project root.** Earlier in this log it was set to `/home/leafy/personal_project` (the project root). `hsn_generator.py` does `sys.path.insert(0, str(Path(_core_parent) / "core"))` — i.e. it appends `/core` itself — so pointing `CORE_PIPELINE_PATH` *at* `core/` directly would make it look for `core/core/`, which doesn't exist. Confirm which convention is actually live: if `CORE_PIPELINE_PATH=/home/leafy/personal_project/core`, `hsn_generator.py`'s import will fail with `ModuleNotFoundError` the first time `/org/hsn-profile/generate` is called, even though every other route works fine — because `main.py` separately does its own `sys.path.insert(0, str(Path(_core_parent) / "core"))` *and* `sys.path.insert(0, _core_parent)` using the same env var for two different purposes (see `main.py` lines 26-30). Setting it to the project root (not `core/`) is what the rest of the codebase expects.
- **`SUPABASE_SERVICE_ROLE_KEY` must be a Supabase JWT (starts with `eyJ...`), not a Clerk secret key (starts with `sk_test_...`).** If this value is actually a Clerk key, every Supabase Storage upload and signed-URL request (`storage.py`) will fail — which would itself explain a Review Modal PDF failing to load, separately from the two frontend bugs fixed in Session 9. Get the real value from Supabase → Settings → API → `service_role` secret, not from Clerk's dashboard.

### Backend — stop

```bash
# In the terminal running uvicorn:
Ctrl+C

# If it's running detached/backgrounded and you don't have the terminal:
pkill -f "uvicorn app.main:app"
```

### Frontend — start

```bash
# In a separate terminal
cd ~/personal_project/webapp/frontend
npm run dev
# Opens at http://localhost:3000
```

### Frontend — stop

```bash
# In the terminal running vite:
Ctrl+C

# If backgrounded:
pkill -f "vite"
```

### Local pipeline (no web stack at all — for quick OCR/extraction testing)

```bash
cd ~/personal_project
source venv/bin/activate
python core/pipeline.py test_data/ output/results.csv
```

### Drive polling pipeline (standalone, no web stack)

```bash
cd ~/personal_project
source venv/bin/activate
python core/pipeline.py --drive-poll <drive_folder_id> --interval 300
# --once runs a single cycle and exits, useful for testing or cron
```

---

## Architecture

```
Browser (React + Clerk)
        │ Bearer JWT (RS256, signed by Clerk)
        ▼
FastAPI (webapp/backend/app/main.py)
   ├── auth.py          verifies JWT via Clerk JWKS — never trusts unverified claims
   ├── org_resolver.py  Clerk IDs → internal UUIDs (just-in-time provisioning)
   ├── db.py            sets app.current_org_id on every connection before any query
   ├── invoice_store.py saves pipeline output → Postgres (org-scoped)
   ├── drive_sync.py    Drive → OCR → Storage → Postgres
   ├── hsn_generator.py LLM → HSN/SAC profile (Groq, same provider as extractor)
   └── storage.py       Supabase Storage (upload + signed URLs for frontend)
        ▼
Supabase Postgres (RLS = tenant isolation at DB engine level)
Supabase Storage (files at invoices/{org_id}/{uuid}_{filename})
        ▲
Google Drive (service account, read-only)
        ▼
core/pipeline.py → ocr_engine.py → extractor.py → validator.py
```

---

## What's Built (Complete Feature List)

### Core pipeline (`core/`)
- Tiered OCR: digital text (pdfplumber) → printed scan (Tesseract) → handwriting/bad scan (flagged for Vision AI)
- Extraction: Groq (`openai/gpt-oss-120b`, strict JSON Schema mode) primary; Gemini (`gemini-2.5-flash-lite`) fallback
- GST validation: GSTIN format, amount reconciliation (taxable + GST = total ± tolerance), CGST+SGST vs total_gst (or IGST vs total_gst), date sanity, currency detection
- Tax normalization: GST/VAT/Tax/CGST+SGST/IGST all → fixed schema fields; `tax_label_raw` preserves the original label exactly as printed; `tax_rate_percent` extracts the printed % only (never back-calculated — see Known Limitations)
- Per-line tax breakdown: `tax_rate`, `tax_amount`, `gross_amount` on each line item, independent of the bill-level tax fields, for mixed-rate invoices
- Drive integration: service account, read-only, dedup via `drive_file_id`
- Rate limiting: per-provider throttling + exponential backoff on transient 429s; fails fast (no retries) on a detected **daily** quota error, since waiting within the same run can't fix that
- `DEMO_MODE`: regex-based stand-in for live LLM calls, used when no API keys/network are configured, so the pipeline can be exercised end-to-end offline

### Auth & multi-tenancy (`auth.py`, `org_resolver.py`, `db.py`, `schema.sql`)
- Clerk org-first auth; JWT verified server-side via JWKS (RS256)
- Every tenant table has `ENABLE` + `FORCE` RLS; `invoice_app` role has `NOBYPASSRLS`
- All RLS policies use `NULLIF(current_setting(...), '')` — fails closed (zero rows) when context missing, never errors
- `set_config()` used instead of `SET ... = :param` (Postgres rejects bind params on `SET`)
- `orgs` table has RLS disabled — it is the tenant root, has no `org_id` column
- `verify_rls.py` proves isolation: org A cannot see org B's rows, unset context returns zero rows

### Invoice management (backend routes + `invoice_store.py`)
- Upload PDF/image → OCR → extract → validate → save to Postgres
- Drive sync: per-org folder registration, incremental (`modifiedTime` filter after first sync), org-scoped dedup
- Vendor matching: GSTIN-first, name fallback, NULL/NULL shared bucket for unidentified vendors
- File storage: Supabase Storage (`invoices/{org_id}/{uuid}_{filename}`), signed URLs for frontend viewer
- Edit history: every human correction logged as `(field, old_value, new_value)` triples in `edit_history`

### ITC apportionment (Rule 42/43)
- `business_use_percent` per line item (default 100%), editable in UI
- Claimable ITC = `line_tax × (business_use_percent / 100)` per line
- `line_tax_rate_percent` on `line_items` for mixed-rate invoices (where per-line rate differs from bill-level)
- `GET /itc-summary`: total claimable ITC, breakdown by vendor and HSN profile status, list of ambiguous lines needing review
- Date range filter on ITC summary

### HSN/SAC profile (`hsn_generator.py`, Settings UI)
- Org saves a one-sentence business description in Settings
- "Generate preview" calls Groq → returns `{ expected_codes, ambiguous_codes, diff }` (preview only, not saved)
- User reviews diff (new codes, codes no longer suggested with checkbox to remove), clicks "Apply & save"
- Apply calls `POST /org/hsn-profile/apply` → persists to `hsn_profile_codes` table
- Manual add: `POST /org/hsn-profile/codes` — manual codes marked `source='manual'`, never touched by regeneration
- Manual delete: `DELETE /org/hsn-profile/codes/{code}` — × button on each chip in UI
- Line items badged in invoice detail panel: ✓ expected / ? verify use / unknown
- All profile data is org-scoped via RLS — zero cross-tenant visibility
- **Important scope note** — see "How HSN/SAC Claimability Actually Works" below; this is profile *matching*, not legal ITC eligibility determination

### Frontend — Invoice list (`InvoiceList.jsx`)
- Filter by status, search vendor name, sort by any column, paginate
- Invoice detail panel with 3-state sizing: Collapsed (44px strip) / Normal (400px) / Expanded (680px)
- Field format: `LABEL :- value` or `LABEL :- N/A` (italic gray) for empty fields
- Two-column grid uses fixed 118px label column — values always get remaining space, no mid-word breaks
- Line items table with per-line `business_use_percent` editing + live ITC calculation
- HSN badges on line items (expected/ambiguous/unknown) using org's saved profile
- "🔍 Review document" manual button, plus auto-opening review modal for low-confidence/needs-review invoices (new — see Session 7)

### Frontend — Settings (`Settings.jsx`)
- Drive folder registration + "Sync now" button
- Business description textarea + save
- HSN profile: generate preview → review diff → apply (or discard)
- Saved profile displayed as coloured chips (green = expected, yellow = ambiguous)
- Manually added codes marked with an M badge
- Add manual code form (code, type HSN/SAC, optional description)
- Remove any code with × button

### Frontend — ITC Summary (`Itcsummary.jsx`)
- Total claimable ITC across all processed invoices
- Breakdown by HSN profile status (bar chart)
- Breakdown by vendor (bar chart)
- Ambiguous line items needing review listed explicitly
- Date range filter
- **Newly wired into nav** (Session 6) — it existed as a built, working component but was never reachable from `App.jsx` until now

### Frontend — Activity Log (`ActivityLog.jsx`) — new, Session 6
- Reads `GET /activity-log`, paginated, filterable by `entity_type` and `actor_type`
- Intended to show every invoice field edit, line-item edit/delete, Drive folder change, business-description save, and HSN profile change (add/remove/apply), tagged by whether the actor was a human user or the AI pipeline
- **Currently broken — see Open Items, item 1.** Page renders fully blank, including the top nav, which is a stronger symptom than a failed fetch alone would produce

### Frontend — Review Modal (`ReviewModal.jsx`) — new, Session 7
- Document viewer (iframe pointed at the Supabase signed URL from `GET /invoices/{id}/file-url`) alongside a "fill in the blanks" form for missing/low-confidence fields
- Auto-opens when `shouldAutoReview(invoice)` is true: status is `NEEDS_MANUAL_REVIEW`/`FAILED`, or numeric confidence < 70
- Manual "🔍 Review document" button available on every invoice regardless of confidence
- Saves through the same `PATCH /invoices/{id}` endpoint as the inline header-field editor, so a save here is reflected everywhere else immediately
- `shouldAutoReview()` lives in its own file, `InvoiceReviewUtils.js`, specifically because Vite's React Fast Refresh requires every export from a `.jsx` file to itself be a React component — a file that default-exports a component *and* named-exports a plain function breaks Fast Refresh with `"shouldAutoReview" export is incompatible"`. Moving the function to a plain `.js` file (which has no component exports to be inconsistent with) is the correct fix per the `vite-plugin-react` docs.
- **Currently broken — PDF does not load when the modal opens. Two independent suspects, not yet isolated — see Open Items, item 2.**

---

## Database Setup

### Fresh database (first time only)

```sql
-- In Supabase SQL Editor, run in order:
-- 1. schema.sql
-- 2. migration_add_tax_rate_percent.sql
-- 3. migration_hsn_itc.sql
-- 4. migration_add_activity_log.sql   (added Session 6 — see below)
```

Then run `verify_rls.py` against the live DB before any real data.

### Adding migrations

Never re-run `schema.sql` against a live DB. Only run the new migration file.

**`migration_add_activity_log.sql`** must be run by hand in the Supabase SQL Editor — `invoice_app` deliberately has no `CREATE` privilege on `public` (see Supabase roles, above), so the app itself cannot create this table at startup. An earlier version of this code tried to `CREATE TABLE IF NOT EXISTS` at FastAPI import time using the app's own restricted connection; that crashed the server outright with `psycopg2.errors.InsufficientPrivilege: permission denied for schema public`. **If you have not yet run this migration in Supabase, run it now** — this is the most likely explanation for the blank Activity page and for edits silently not persisting (see Open Items, item 1).

### Key RLS bugs already fixed — do not regress

1. `ENABLE ROW LEVEL SECURITY` alone is not enough — must also `FORCE` on every tenant table or the table owner bypasses it silently
2. Supabase's default `postgres` role has `rolbypassrls = true` — app must use `invoice_app` at runtime
3. `current_setting('app.current_org_id', true)` returns `''` (not NULL) when unset — `NULLIF(..., '')` wraps every policy
4. `SET app.current_org_id = :param` fails with SQLAlchemy — use `set_config('app.current_org_id', :org_id, false)` instead
5. `orgs` table must have RLS disabled — it has no `org_id` column and is the tenant root
6. Session Pooler (not Transaction mode) — transaction mode resets `SET` between transactions
7. **`:param::type` inline casts break under SQLAlchemy + psycopg2 in some compiled-statement paths** — e.g. `last_edited_by = :uid::uuid` throws `psycopg2.errors.SyntaxError: syntax error at or near ":"`. Use `CAST(:uid AS uuid)` instead, which is unambiguous regardless of dialect/paramstyle. This bit both the original `edit_history` insert and the new `activity_log` insert; both are now fixed to use `CAST(... AS uuid)`.

---

## API Endpoints (complete)

```
# Auth
All endpoints require: Authorization: Bearer <Clerk session JWT>

# Invoices
POST   /invoices/upload                              Upload PDF/image, run full pipeline, save
GET    /invoices                                      List invoices (filters: status, search, sort, page)
GET    /invoices/{id}                                 Full invoice detail + line items
PATCH  /invoices/{id}                                 Update extracted fields (user correction)
GET    /invoices/{id}/file-url                        Get time-limited signed URL for the file viewer
                                                       (now also returns drive_file_id as a fallback hint
                                                       and surfaces {error} when signing fails — see below)

# Line items
PATCH  /invoices/{id}/line-items/{lid}                Update business_use_percent (and other line fields)

# ITC
GET    /itc-summary                                    ITC totals (params: date_from, date_to)

# Org settings
GET    /org/drive-folder                               Get registered Drive folder ID
PUT    /org/drive-folder                               Register a Drive folder ID
GET    /org/settings                                    Get org settings (incl. business_description)
PUT    /org/settings                                    Save org settings

# HSN profile
GET    /org/hsn-profile                                 Get saved profile { expected_hsn_codes, ambiguous_hsn_codes, has_profile }
POST   /org/hsn-profile/generate                        Generate preview (NOT saved) → { expected_codes, ambiguous_codes, diff }
POST   /org/hsn-profile/apply                           Save preview to DB → returns same shape as GET
POST   /org/hsn-profile/codes                           Add a manual code
DELETE /org/hsn-profile/codes/{code}                    Remove a code

# HSN classification memory
POST   /invoices/{id}/line-items/{lid}/classify         Classify an ambiguous HSN code

# Activity log (new — Session 6)
GET    /activity-log                                    Paginated, filterable by entity_type / actor_type

# Drive sync
POST   /drive/sync                                      Trigger sync for this org's registered folder

# Admin (cron)
POST   /admin/drive-poll-all                            Poll all orgs' Drive folders (requires DRIVE_POLL_SECRET header)
```

---

## Bug Fix History (chronological — what was actually wrong, and what fixed it)

This section exists because several of these bugs look superficially similar and it matters which one you're actually looking at.

### Bug 1 — `business_use_percent` resets to 100 on every save
**Symptom:** editing a line item's business-use % shows "Saved" but the value reverts to 100 on next load.
**Cause:** `InvoiceList.jsx`'s `saveBup()` sent the key `business_use_pct` to the backend; the backend's Pydantic model only recognizes `business_use_percent`. FastAPI/Pydantic silently drops unrecognized keys by default — the PATCH returns `200 {"updated": true}` while doing nothing to that field, and the column falls back to its SQL default (100) on the next read.
**Fix:** corrected the key name in `saveBup()` to `business_use_percent`, and fixed a parallel read-side bug where the footer "Total claimable ITC" was reading `item.business_use_pct` (always `undefined`) instead of `item.business_use_percent`.

### Bug 2 — editing one line-item field silently wiped the others
**Symptom:** editing just the HSN code (or just the description) on a line item would also reset `quantity`, `rate`, and `business_use_percent` back to defaults.
**Cause:** the line-item `UPDATE` in `main.py` set every column unconditionally on every save. Since the frontend only sends the fields the user actually touched, Pydantic defaults the untouched fields to `None`, and the `UPDATE` then wrote those `None`s over real data.
**Fix:** rewrote the `UPDATE` to use `COALESCE(:field, existing_column)` per field, so an omitted field keeps its current DB value instead of being overwritten. (`0.0` for `business_use_percent`, meaning "fully personal use," is preserved correctly since the check is `is not None`, not Python truthiness.)

### Bug 3 — Vite Fast Refresh: `"shouldAutoReview" export is incompatible`
**Symptom:** dev server warning, full-page reload instead of HMR on every edit to `ReviewModal.jsx` / `InvoiceList.jsx`.
**Cause:** `ReviewModal.jsx` both default-exported a component and named-exported a plain function (`shouldAutoReview`). Fast Refresh requires every export from a `.jsx` file to be a component.
**Fix:** moved `shouldAutoReview` into `InvoiceReviewUtils.js`, a plain `.js` file with no component exports at all. `InvoiceList.jsx` now imports `ReviewModal` (default) from `./ReviewModal` and `shouldAutoReview` from `./InvoiceReviewUtils`.

### Bug 4 — `Failed to resolve import "./ReviewModal"`
**Symptom:** Vite 500 error on startup, frontend won't load at all.
**Cause:** `ReviewModal.jsx` (and later `ActivityLog.jsx`) were generated but never actually copied into `webapp/frontend/src/` — the import in `InvoiceList.jsx` pointed at a file that didn't exist on disk yet.
**Fix:** copy step — no code change needed, just placing the files. **Confirmed done as of this session** (`InvoiceReviewUtils.js`, `ReviewModal.jsx`, `ActivityLog.jsx` all present in the live `src/` listing).

### Bug 5 — `psycopg2.errors.SyntaxError: syntax error at or near ":"`
**Symptom:** every `PATCH /invoices/{id}` that touched any field returned 500; log showed `last_edited_by = :uid::uuid`.
**Cause:** SQLAlchemy's `:name` bind-parameter syntax is ambiguous when immediately followed by a Postgres `::type` cast in certain compiled-statement paths under psycopg2 — `:uid::uuid` does not reliably parse the way plain SQL would.
**Fix:** replaced every `:param::type` occurrence (both the pre-existing `edit_history` insert and the new `activity_log` insert) with `CAST(:param AS uuid)`.

### Bug 6 — `psycopg2.errors.InsufficientPrivilege: permission denied for schema public`
**Symptom:** server crashed on every restart with this error on `CREATE TABLE IF NOT EXISTS activity_log (...)`.
**Cause:** the app tried to create the new `activity_log` table at FastAPI import time, using the same restricted `invoice_app` DB role that powers normal requests. That role intentionally has no `CREATE` privilege (least-privilege design, see Supabase roles above) — granting it would undermine the whole point of `NOBYPASSRLS`.
**Fix:** removed the runtime `CREATE TABLE` entirely. Shipped as `migration_add_activity_log.sql` instead, matching the project's existing `migration_*.sql` pattern, to be run once by hand in the Supabase SQL Editor (a privileged context) — **not** by the app.

### Bug 7 — `activity_log` table definition drifted from house schema conventions
**Found by:** cross-checking the new table against the real `schema.sql` once it was actually provided.
**Issues found and fixed:**
- `actor_id` had no foreign key; every comparable column elsewhere (`edit_history.edited_by`, `invoices.last_edited_by`) references `users(user_id)` — added the FK.
- The RLS policy used `USING ... WITH CHECK ...`; every other table in the schema uses `USING` only. Removed `WITH CHECK` for consistency (the `org_id` bound into every insert already comes from the verified Clerk context, never from caller input, so it added no real protection here).
- Missing an index on the columns `GET /activity-log` actually filters by (`entity_type`); added `idx_activity_log_org_entity` alongside the existing `(org_id, created_at)` index, matching the "index what you filter by" pattern used elsewhere (`idx_invoices_org_status`, `idx_line_items_org_hsn`).

---

## Open Items — In Priority Order

### ✅ Resolved (Session 9 — verified by actually executing the code, not just reading it)

1. **Activity Log page rendering completely blank, including the nav — FIXED.** Root cause: `EntryRow` in `ActivityLog.jsx` referenced a local `s` alias for `styles` that was only ever defined inside the parent `ActivityLog` component, never inside `EntryRow` itself. Every real entry threw `ReferenceError: s is not defined`, and with no error boundary anywhere in `App.jsx`'s tree, that took down the whole app shell. Fixed by referencing `styles` directly; confirmed via a real headless render test with mocked API data.
2. **Review Modal not loading the PDF — partially fixed.** The backend was already correctly returning a Drive-fallback `drive_file_id`; `ReviewModal.jsx` was just never reading it. Fixed to embed it as an iframe `src`. **However, see Session 10 below — the fallback URL pattern used (`/view`) is itself wrong and needs a follow-up fix to `/preview`.**
3. **Supabase Storage domain refusing to connect — explained, not a code bug.** Most likely a paused free-tier Supabase project; resolved by un-pausing from the dashboard, or relying on the Drive fallback above.

### 🔴 Active (Session 10 — in progress, paused pending file upload)

4. **Vendor/buyer name extraction swallows the entire seller block (and possibly bleeds across the buyer boundary).** Confirmed this isn't a web-app bug — `main.py` only stores `vendor_name`/`buyer_name`, the actual extraction prompt is in `core/extractor.py`, which hasn't been uploaded in any session yet. **Blocked on receiving `core/extractor.py`.**
5. **No amount-reconciliation validation on `PATCH /invoices/{id}`.** Confirmed by reading the full handler: header fields and line items are saved completely independently with zero cross-check. Decision made: on mismatch, save anyway and flag `status` as `WARNING`/`NEEDS_MANUAL_REVIEW` with a specific message — never silently save a number that doesn't add up, but never hard-block an edit either. **Blocked on `core/validator.py`** to confirm whether to reuse its existing tolerance/status logic rather than reimplementing a parallel version.
6. **Review Modal missing line items entirely** (descriptions, quantities, rates, per-line tax %) — only header fields are editable there currently. Planned, not yet built.
7. **Review Modal's PDF viewer popping a new tab and prompting a Google sign-in.** The uploaded `ReviewModal.jsx` doesn't contain any code that would do this — it's confirmed to be an older version with no Drive-fallback logic at all. Most likely explanation: the live file on disk has drifted further than any uploaded copy, probably using Drive's auth-gated `/view` URL instead of the embeddable `/preview` URL. Fix planned once extraction/validation work lands, to avoid building the line-items UI twice against two different file versions.

See **Session 10** under Session History below for full detail on all four active items, and the action-item checklist at the end of that entry for exactly what happens next.

### Superseded — original Session 8 bug descriptions (kept for history, no longer accurate)

<details>
<summary>Click to expand original (now-resolved) bug writeups from Session 8</summary>

1. **Activity Log page renders completely blank — including the top navigation bar.**
   This is a stronger symptom than "the fetch failed and an error message should show but doesn't" — if the *entire* page including the nav (which is rendered by `App.jsx`, not `ActivityLog.jsx`) is blank, the most likely causes are:
   - An unhandled JS exception thrown during render, which React's lack of an error boundary around the tab content would turn into "nothing renders, including everything around it" rather than a contained error.
   - A casing/path mismatch in the import (`Itcsummary.jsx` vs `ItcSummary.jsx`-style naming has already bitten this project once) — worth double-checking `App.jsx`'s import statement against the actual filename casing on disk.
   - The `migration_add_activity_log.sql` migration genuinely not having been run yet, causing `GET /activity-log` to 500, **combined with** no error boundary to contain that failure to just the tab content.

2. **Review modal opens but the PDF does not load.**
   Two independent, not-yet-distinguished suspects:
   - **Supabase Storage is unreachable** (see item 3 below) — if the signed URL itself can't be reached, the iframe inside the modal would show the same connection failure as the standalone test, just inside a popup instead of a new tab.
   - **The modal never receives a URL to load in the first place** — if `GET /invoices/{id}/file-url` is returning `{"url": null}`, the iframe has nothing to render.

3. **`wflguoqnrxijfvdeaxhb.supabase.co` refused to connect.**
   Given that the backend's `get_file_url` only returns a real signed URL when `storage.is_configured()` and the stored path already starts with `"invoices/"` (i.e. it already passed validation server-side), a browser-level "refused to connect" on that exact domain most likely means the **Supabase project itself is paused** — free-tier Supabase projects pause automatically after a period of inactivity, and a paused project refuses all connections, including to Storage, until someone manually un-pauses it from the dashboard.

</details>

### Design gap — decided in Session 10

4. **No reconciliation check between line items and header totals; editing one doesn't validate against, or recompute, the other.**
   This was originally raised as just "editing `taxable_amount` doesn't change `total_gst_amount`" — Session 10 sharpened it into the full scenario: line items (100 + 200 = 300 taxable, 10% tax = 30, total 330) should be the source of truth; editing the header `total_amount` to 310 without a matching line-item change should be flagged as not adding up; editing a line item afterward (100 → 150) should make every downstream figure reflect the *new* value, not the original.
   **Decision made (Session 10):** on a save that doesn't reconcile, **do not reject it** — apply the edit, but flag the invoice's `status` as `WARNING`/`NEEDS_MANUAL_REVIEW` and surface the specific mismatch in `issues`. This matches `validator.py`'s existing "accurate or flagged, never silently wrong" philosophy at OCR time, just extended to cover human edits too, which currently have zero validation of any kind.
   **Status:** not yet implemented — blocked on `core/validator.py` (to confirm whether its existing tolerance/status conventions can be reused directly) and `core/extractor.py` (related — see Open Item #4 above, the extraction bug feeds into the same invoices this check would run against).

### Must-do before real client data
- [ ] Rotate all credentials listed at the top of this file
- [ ] Remove `venv/` and `gdrive_key.json` from git history: `git filter-repo --path venv --path gdrive_key.json --invert-paths`
- [ ] Run `verify_rls.py` against live Supabase after rotation and before any onboarding
- [ ] Run `migration_add_activity_log.sql` in the Supabase SQL Editor if not already done (see Open Items #1)

### Production deployment
- [ ] Set `CLERK_ALLOWED_ORIGINS` to the real frontend domain (currently hardcoded `localhost:3000` for CORS too)
- [ ] Set `BACKEND_URL` and `DRIVE_POLL_SECRET` secrets in GitHub Actions for the `/admin/drive-poll-all` cron
- [ ] Get a Render deploy URL, update `VITE_API_BASE` in frontend env

### Features in progress / incomplete
- [ ] Drive folder proof-of-control — currently first-claim-wins via a UNIQUE constraint; stronger: require a random-token file dropped in the folder before registration is accepted
- [ ] `last_drive_sync_at` — incremental sync is wired, column exists on `orgs`, but confirm the backend reads it before listing and writes it after a successful sync cycle
- [ ] Add an error boundary around tab content in `App.jsx` so a future broken tab fails visibly (an error message) instead of blanking the entire page including the nav — directly relevant to Open Item #1

### GST compliance features (design needed)
- [ ] 180-day ITC reversal alert: `payment_date - invoice_date > 180` → flag for reversal. Column `payment_date` already on `invoices`, just needs filling (manual or accounting integration) and a query/alert on top
- [ ] GSTR-1 filing status: columns `gstr1_filing_status` and `gstr1_last_checked` exist on `vendors` — needs GST portal API integration to populate
- [ ] Rule 43 (capital assets): `business_use_percent` handles Rule 42 (inputs/consumables). Capital assets need a different calculation (5% per year reversal, not proportional per invoice) — separate column and logic needed
- [ ] **ITC rule engine — see dedicated section below, this is the one with real penalty exposure**

### Known limitations
- Gemini capped at ~20 req/day on free tier — fails gracefully but batches > 20 pages have gaps
- Groq hits ~200K tokens/day around invoice 70–95 in a large batch — same graceful fail
- `tax_rate_percent` only extracts the printed % — does not back-calculate from amounts (deliberate: rounding makes back-calculation unreliable for compliance purposes)
- HSN code generation relies on the model's own knowledge, not an official master list — generated profiles are a starting point for review, not a verified compliance artifact (see next section — this is the important caveat)

---

## How HSN/SAC Claimability Actually Works — Read This Before Filing Anything

You asked directly how the system decides which HSN/SAC codes are claimable and which aren't, and flagged — correctly — that this carries real penalty risk if done wrong. Here's the honest, complete answer.

**What the system actually does today:**

1. You write a one-sentence description of your business in Settings.
2. `hsn_generator.py` sends that description to Groq, which — using its own general training knowledge of HSN/SAC code structure, not an official government master list — returns a list of HSN/SAC codes it thinks a business like yours would typically deal in, split into "expected" and "ambiguous."
3. You review that list (the generate → preview → apply flow exists specifically so a bad LLM guess never silently overwrites a profile you already reviewed) and choose what to keep, remove, or add manually.
4. When an invoice line item's HSN code matches something in your saved "expected" list, it's badged ✓; if it matches "ambiguous," it's badged ?; otherwise it's "unknown."
5. **Separately**, the ITC math itself (`itc_claimable`, `itc_reason` on `line_items`) is currently described in this codebase's own comments as **"triage only"** — meaning the system flags a line as worth a closer look, but does not apply real GST law to determine actual legal eligibility.

**What this means in practice:** the HSN badge and the `itc_claimable` flag are both *attention-directing* signals — "here's something worth you or your accountant looking at" — not a legal determination of what you're actually entitled to claim. There is currently no official HSN/SAC master list lookup, and no rule table mapping HSN chapters to actual ITC eligibility under Section 17(5) (blocked credits — things like motor vehicles, food/beverages, club memberships, works contracts for immovable property, etc., which are NOT claimable regardless of business use percentage, no matter what your HSN profile says).

**Concretely, here's a real gap that has financial consequences:** if your business buys, say, a company car (HSN code for motor vehicles) and your `business_use_percent` editor lets you set 70% business use, the system will happily calculate a 70% claimable ITC amount for it — but motor vehicle ITC is **blocked under Section 17(5)** in most cases regardless of business-use percentage, with narrow exceptions (further supply of vehicles, transportation of passengers, driving schools, etc.). The current system has no way to know that and would compute a claimable number anyway.

**What I'd recommend, in order of urgency:**
1. **Do not file GST returns based on this tool's `itc_claimable` numbers as-is.** Treat every number as a draft for your accountant or a GST practitioner to verify, not a finished figure — this matches what the code's own design intent already says ("triage only"), but it's worth saying plainly since the UI doesn't currently warn about this anywhere a user would see it.
2. **Add a blocked-credits table** — a hardcoded list of HSN chapters/codes covered by Section 17(5) is a finite, known list (motor vehicles outside specific exceptions, food & beverages, outdoor catering, beauty treatment, health services, cosmetic/plastic surgery, membership of clubs/health/fitness centres, travel benefits to employees, works contract services for immovable property except plant & machinery, goods/services for personal consumption, goods lost/stolen/destroyed/written off/disposed of as gifts or free samples). Cross-referencing every line item's HSN against this table before computing `itc_claimable` would close the actual legal gap, not just the UX one.
3. **Surface the "this is a starting point, not a verified compliance artifact" caveat directly in the UI** — right now it only lives in code comments and this log, not anywhere a user filing a return would see it.

I'm not a tax advisor and this isn't legal advice — but as a description of what the *code* currently does versus what GST law actually requires, this gap is real and worth closing before you rely on these numbers for an actual filing.

---

## Key Design Decisions

**Why RLS over app-level `WHERE org_id = ?`**
One forgotten `WHERE` clause leaks data. RLS moves isolation into the DB engine: even a buggy query returns nothing for other tenants. Both layers exist (defence in depth) but RLS is the backstop that makes a missed filter a silent no-op rather than a breach.

**Why Session Pooler not Transaction Pooler**
`SET app.current_org_id` is a session variable. Transaction mode resets it between transactions, silently breaking RLS. Session mode keeps it alive for the full request duration.

**Why `set_config()` not `SET ... = :param`**
`SET` is a Postgres configuration command, not a query — SQLAlchemy's bind parameter mechanism appends `::type` suffixes that Postgres rejects as a syntax error in that position. `set_config()` is a normal SQL function that accepts parameters correctly.

**Why `CAST(:param AS uuid)` not `:param::uuid`**
Functionally the same outcome in plain SQL, but `:param::uuid` is genuinely fragile under SQLAlchemy + psycopg2's compiled-statement caching in some code paths, throwing a raw syntax error. `CAST(... AS ...)` is unambiguous and dialect-agnostic — there's no reason to use the shorthand once you've been bitten by it once.

**Why service account not OAuth for Drive**
Runs unattended in a polling loop. OAuth user tokens expire on password changes and require human login. A service account key doesn't expire and is not tied to a human account.

**Why `business_use_percent` not a "claim quantity" field**
Percentage works correctly for both quantity-based (200 screws, 50% for business) and value-based (₹10,000 software subscription, 70% business) apportionment. GST law (Rule 42) frames the reversal calculation as a proportion, which maps directly to this field.

**Why `vendor_name` is nullable**
Not substituted with a placeholder like "UNKNOWN VENDOR." A genuinely nameless vendor stays NULL so it's visible as a gap requiring human review, not buried as fake-looking data in every downstream report.

**Why HSN profile uses generate → preview → apply (not generate → auto-save)**
A bad LLM generation should not silently overwrite a previously-reviewed profile. The preview step means the user always confirms before anything is saved, and manually-added codes (`source='manual'`) are never in scope for removal by a regeneration.

**Why activity logging is app-side DML but table creation is a hand-run migration**
The app's runtime DB role (`invoice_app`) is deliberately restricted to `SELECT/INSERT/UPDATE/DELETE` with no `CREATE` — that's the same least-privilege boundary that makes `NOBYPASSRLS` meaningful in the first place. Granting `CREATE` to fix a convenience problem would have been the wrong trade.

**Why `fieldRow: { display: "contents" }` in the detail panel grid**
The field grid uses a fixed 118px label column so the value column always gets the remaining width regardless of label length. `display: contents` makes the label and value spans participate directly in the parent grid without adding a wrapper div that would break the two-column alignment.

**Why 3-state detail panel (collapsed / normal / expanded)**
Collapsed (44px strip) lets users keep an invoice selected while reclaiming screen space for the list. Normal (400px) is the default working view. Expanded (680px) is for invoices with long GSTINs, vendor names, or many line items where the normal width clips content.

---

## Session History

### Session 1 — Core pipeline
- Built `core/`: tiered OCR, Groq/Gemini extraction, GST validator, SQLite output, Drive connector

### Session 2 — Multi-tenant web backend
- Postgres schema with RLS (`schema.sql`), `auth.py`, `db.py`, `org_resolver.py`, `invoice_store.py`
- Fixed RLS bugs: FORCE RLS, NULLIF for empty string, set_config vs SET, orgs table RLS disabled
- `verify_rls.py` written to prove isolation

### Session 3 — Frontend + file storage
- `App.jsx`, `InvoiceList.jsx`, `Settings.jsx`, Clerk integration, `storage.py`
- Supabase Storage upload + signed URLs
- Drive sync UI in Settings

### Session 4 — ITC + HSN profile
- `Itcsummary.jsx`, `hsn_generator.py`, ITC apportionment (Rule 42/43)
- `business_use_percent` per line item, `line_tax_rate_percent` for mixed-rate invoices
- HSN profile generation from business description, badges on line items

### Session 5 — Bug fixes and amount display
- `_coerce()` helper in `main.py` to convert Postgres `Decimal` to `float` before serialization
- `::float` casts in list endpoint SELECT
- `parseFloat()` in `fmtAmount` in `InvoiceList.jsx`
- `business_use_percent` added to `GET /invoices/{id}` line items SELECT
- `last_drive_sync_at` column added, incremental Drive sync wired

### Session 6 — UI fixes, Activity Log, Review Modal (largest session)
- **HSN profile field-name mismatch fixed**: `GET /org/hsn-profile` returns `expected_hsn_codes`/`ambiguous_hsn_codes`; `POST .../generate` returns `expected_codes`/`ambiguous_codes` (preview shape). Old `Settings.jsx` used `expected_codes` everywhere and never called `/apply`, so nothing was ever actually persisted. Both fixed.
- **HSN profile edit UI added**: × delete, manual-add form, M badge for manual codes, preview diff with add/remove checkboxes
- **Detail panel text cutoff fixed**: `gridTemplateColumns: "118px 1fr"` + `display: contents` on `fieldRow`
- **Detail panel 3-state sizing added**: collapsed/normal/expanded
- **Field format standardized**: `LABEL :- value`, italic gray `N/A` for empty
- **Bug 1 fixed**: `business_use_pct` → `business_use_percent` (see Bug Fix History)
- **Bug 2 fixed**: line-item partial-update data loss via `COALESCE` (see Bug Fix History)
- **Activity Log feature added**: `activity_log` table design, `log_activity()` helper, logging hooked into every mutating endpoint, `GET /activity-log`, `ActivityLog.jsx`, wired into nav
- **`Itcsummary.jsx` wired into nav** — existed since Session 4 but was never reachable
- **Schema cross-check**: once `schema.sql` was actually provided, fixed `activity_log`'s FK on `actor_id`, removed inconsistent `WITH CHECK`, added missing index (see Bug Fix History, Bug 7)

### Session 7 — Crash fixes from live logs, Review Modal
- **Bug 3 fixed**: Fast Refresh `shouldAutoReview` incompatibility → extracted to `InvoiceReviewUtils.js`
- **Bug 5 fixed**: `:uid::uuid` syntax error → `CAST(:uid AS uuid)` everywhere
- **Bug 6 fixed**: `permission denied for schema public` → removed runtime `CREATE TABLE`, shipped `migration_add_activity_log.sql` instead
- **Review Modal built**: document viewer + fill-in-the-blanks form, auto-opens for low-confidence/needs-review invoices, manual button always available
- **`file-url` endpoint extended**: now also returns `drive_file_id` so the frontend can fall back to opening the file directly from Google Drive when Supabase Storage is unreachable (backend half done; frontend wiring not yet added to `ReviewModal.jsx`)
- **Bug 4 identified and resolved**: `ReviewModal.jsx`/`ActivityLog.jsx` existed as generated files but were never copied into `webapp/frontend/src/` — confirmed now placed correctly

### Session 8 — Consolidation + open live bugs
- Consolidated the full project log from scratch, incorporating every fix and finding from Sessions 1–7 into one document
- Confirmed current live directory structure matches expectations, with two casing notes (`InvoiceReviewUtils.js`, `Itcsummary.jsx`) worth double-checking against import statements
- Flagged three live bugs as needing the real current files + browser console output to diagnose properly, rather than guessing further from transcript descriptions
- **Design gap raised, not yet decided**: editing `taxable_amount` doesn't recompute dependent GST fields — recommended an explicit "Recalculate" action over silent auto-recalculation for auditability
- **Answered directly**: how HSN/SAC claimability actually works today, and the real legal/penalty gap in the current "triage only" ITC logic

### Session 9 — Activity Log root-caused and fixed; Review Modal Drive-fallback wiring fixed
- **Confirmed via `curl`** that the `activity_log` table exists (migration had been run) — ruled out the missing-table theory from Session 8
- **Root-caused the blank-page bug by actually executing the code**, not just reading it: built a headless React render harness (esbuild + jsdom) with mocked `/activity-log` API data and rendered `ActivityLog.jsx` for real. Reproduced `ReferenceError: s is not defined` thrown inside `EntryRow`.
  - **The bug**: `EntryRow` (a separate top-level function, not nested inside `ActivityLog`) referenced `s.row`, `s.rowIcon`, `s.rowTop`, etc. throughout — but `s` was only ever defined as `const s = styles` *inside* the parent `ActivityLog` component, never inside `EntryRow` itself, and never at module scope. Every render of a real log entry threw.
  - **Why this blanked the whole page, nav included**: `App.jsx`'s `AppShell` renders `TopNav` and the active tab's component as plain siblings with no error boundary anywhere in the tree. An uncaught render exception in any child unmounts everything above the nearest boundary — here, that's the whole app root. A contained "this tab is broken" message was never possible with this structure; it's all-or-nothing.
  - **Fix**: changed every `s.xxx` reference inside `EntryRow` to `styles.xxx` (the real module-scope object). Re-ran the same render harness against the patched file and confirmed a clean, correct render of both icon, badge, and diff rows.
  - Checked `Itcsummary.jsx` and `Settings.jsx` for the same `s`/`styles` aliasing pattern in any function defined outside their main component — clean, this bug was isolated to `ActivityLog.jsx`.
- **Review Modal PDF not loading**: confirmed the backend (`main.py`'s `get_file_url`) was already correctly returning `drive_file_id` and `storage_error` as Supabase fallbacks, but `ReviewModal.jsx` only ever checked `d.url` and discarded everything else — so even with a working Drive fallback available, the modal just showed a generic "preview isn't available" message.
  - **Fix applied at the time**: read `drive_file_id` from the response and embed `https://drive.google.com/file/d/{id}/preview` in the iframe when no Supabase URL is present; surface `storage_error` text for diagnosis.
  - Also fully closed out the still-half-done Fast Refresh fix from Session 7: `shouldAutoReview` was still duplicated in both `ReviewModal.jsx` and `InvoiceReviewUtils.js`, and `InvoiceList.jsx` was still importing it from the wrong file. Removed the duplicate from `ReviewModal.jsx`, fixed the import.
- Flagged that `SUPABASE_SERVICE_ROLE_KEY` in the person's real `.env` looked like it might actually be a Clerk secret key (`sk_test_...` prefix) rather than a genuine Supabase service-role JWT (`eyJ...` prefix) — if so, every Storage upload/signed-URL call would fail silently, which is a plausible root cause for Storage-related symptoms independent of the two frontend bugs above.
- Updated the backend start command in this log to match the person's actual working invocation (`python -m uvicorn app.main:app --reload --port 8000`, run from `webapp/backend` with explicit per-var `export` lines rather than a single block) and flagged a second discrepancy: `CORE_PIPELINE_PATH` must point at the project root (`.../personal_project`), not at `core/` directly — `hsn_generator.py` appends `core/` itself when building its import path, so pointing the env var at `core/` would make it look for a nonexistent `core/core/`.

### Session 10 (in progress) — Extraction bug + missing reconciliation validation + Review Modal gaps
**Triggered by a real invoice screenshot** showing a two-column Seller/Client layout (seller: "TechVision Distributors Pvt Ltd", full Mumbai address, Tax ID, GSTIN; client: "Jaipur Smart Devices", full Jaipur address, Tax ID).

**Three issues raised, one fully scoped + planned, two blocked on missing files:**

1. **Vendor-name extraction swallows the entire seller block.** The reported symptom: the extracted `vendor_name` contains the full run-on string from "TechVision Distributors..." through to (and apparently overlapping into) "...Smart Devices" — i.e. the seller/buyer boundary isn't being respected, and far more than the company name (address, possibly bleeding toward the buyer's name) is landing in one field. **Root cause not yet found** — the actual extraction prompt lives in `core/extractor.py`, which has never been uploaded to any session so far (confirmed: `main.py` only stores/reads `vendor_name`/`buyer_name`, it has no extraction logic at all — that lives entirely in `core/`). **Blocked, awaiting upload of `core/extractor.py`** (and `core/validator.py`, for the related reconciliation logic below, since some of that may already partially exist there at the OCR-pipeline level rather than only at the web-app PATCH level).

2. **No amount-reconciliation validation anywhere.** Confirmed by reading the full `PATCH /invoices/{id}` handler in `main.py`: header-field edits and line-item edits are applied completely independently, with no check that `sum(line_items.amount) == taxable_amount` or `taxable_amount + total_gst_amount == total_amount` after a save. This is exactly the gap behind the requested scenario (edit total from 300→310 with no matching line-item change → should flag; then edit a line item from 100→150 → downstream figures should reflect the new value, not the original).
   - **Decision made**: on a save that doesn't reconcile, **do not reject the save** — apply it, but flag the invoice's `status` as `WARNING`/`NEEDS_MANUAL_REVIEW` and surface the specific mismatch (e.g. "line items sum to ₹250, but taxable_amount is ₹310") rather than blocking the edit outright. This matches the project's existing "accurate or flagged, never silently wrong" philosophy already used in `validator.py`'s pipeline-time checks — same posture, just also enforced on post-hoc human edits, which currently have none.
   - **Not yet implemented** — needs `core/validator.py` to confirm whether the reconciliation-tolerance logic and status-setting convention already used at OCR time can be reused as-is for the edit-time check, rather than reimplementing a parallel, possibly inconsistent version inside `main.py`.
3. **Review Modal gaps, independent of the above:**
   - Line items (descriptions, quantities, rates, **and the per-line tax %**) are entirely absent from the "fill in the blanks" form — only header-level fields are editable there. Plan: add an editable line-items table to the modal, mirroring the line-item editor already in `InvoiceList.jsx`'s detail panel rather than duplicating different logic.
   - The PDF viewer is popping a new browser tab and prompting a Google sign-in instead of embedding inline. **Diagnosed**: the `ReviewModal.jsx` actually uploaded this session is confirmed to be the **pre-Session-9 version** — it has no `drive_file_id` handling at all, and no `window.open`/new-tab code exists anywhere in it. The sign-in-prompting new-tab behavior described doesn't match anything in this file's logic; the most likely explanation is that the person's live `src/ReviewModal.jsx` has drifted to an even earlier/different state than any uploaded copy, specifically one using Google Drive's `/view` URL (auth-gated, breaks out of iframes, prompts sign-in) rather than `/preview` (embeddable, no auth prompt). **Fix planned**: rebuild the modal's Drive fallback to use `/preview` exclusively and confirm no code path ever opens a new tab.
   - **Not yet implemented** — paused before writing code, per the person's request to stop and wait for `extractor.py`/`validator.py` so the line-item and reconciliation work in the modal can be built against the same logic as the backend fix, rather than ahead of it.

**Action items for next session:**
- [ ] Receive `core/extractor.py` and `core/validator.py`
- [ ] Fix the seller/buyer extraction prompt so `vendor_name` captures only the company name (not the full address block, and not bleeding past the seller/buyer boundary)
- [ ] Add amount-reconciliation check to `PATCH /invoices/{id}` (warn-and-flag, not reject), reusing `validator.py`'s existing tolerance/status conventions if applicable
- [ ] Add cascading recalculation: editing a line item's `amount`/`quantity`/`rate`/`line_tax_rate_percent` should make header-level rollup checks re-evaluate against the *new* line value, not a stale prior one
- [ ] Add an editable line-items table (including per-line tax %) to `ReviewModal.jsx`
- [ ] Fix `ReviewModal.jsx`'s Drive fallback to use the embeddable `/preview` URL, confirm zero new-tab/sign-in behavior
- [ ] Double-check whether `SUPABASE_SERVICE_ROLE_KEY` in the live `.env` is a genuine Supabase JWT, not a Clerk key (flagged in Session 9, not yet confirmed resolved)