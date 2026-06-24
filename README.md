# Invoice Intelligence System — Project Log

> Last updated: June 2026 · Status: MVP working end-to-end. Invoices sync from Drive, extract correctly, display in UI. PDF viewer uses Google Drive `/preview` embed directly (no Supabase Storage dependency). See Session 11 for the full startup sequence that currently works reliably.

---

## ⚠️ ROTATE THESE BEFORE ANY REAL CLIENT DATA

Every key below has been shared in chat history and must be treated as compromised.

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
    │   ├── schema.sql               # Postgres schema with RLS — apply to fresh DB only
    │   ├── verify_rls.py            # RLS isolation test — run before any real client data
    │   └── app/
    │       ├── auth.py              # Clerk JWT verification (RS256 via JWKS)
    │       ├── db.py                # SQLAlchemy + session-scoped RLS context setter
    │       ├── drive_sync.py        # Bridges Drive connector into multi-tenant Postgres (no Storage upload)
    │       ├── hsn_generator.py     # LLM-based HSN/SAC profile generation from biz description
    │       ├── invoice_store.py     # Saves pipeline output into Postgres (org-scoped)
    │       ├── main.py              # All FastAPI routes
    │       ├── org_resolver.py      # Clerk string IDs → internal UUIDs (JIT provisioning)
    │       └── storage.py           # Supabase Storage wrapper (upload + signed URLs) — only used for manual uploads now
    └── frontend/
        ├── .env.local
        ├── index.html
        ├── package.json
        ├── vite.config.js
        └── src/
            ├── main.jsx                 # React entry, ClerkProvider
            ├── App.jsx                  # Shell: login, org creation, top nav, tab routing
            ├── InvoiceList.jsx          # Invoice list + 3-state detail panel + ITC editor
            ├── InvoiceReviewUtils.js    # shouldAutoReview() — split out for Fast Refresh compliance
            ├── ReviewModal.jsx          # Document viewer + fill-in-the-blanks review popup
            ├── ActivityLog.jsx          # Activity Log page
            ├── Settings.jsx             # Drive folder + business description + HSN profile editor
            └── Itcsummary.jsx           # ITC summary view with vendor/HSN breakdowns
```

**Note on filename casing:** the live filesystem has `InvoiceReviewUtils.js` and `Itcsummary.jsx` (capital-then-lowercase) — imports elsewhere must match this exact casing, since Linux filesystems are case-sensitive.

---

## Environment Variables

### Backend — correct working start command (Session 11 verified)

```bash
cd ~/personal_project/webapp/backend

export GOOGLE_APPLICATION_CREDENTIALS="/home/leafy/personal_project/gdrive_key.json"
export GDRIVE_KEY_PATH="/home/leafy/personal_project/gdrive_key.json"
export GROQ_API_KEY="gsk_..."
export GEMINI_API_KEY="..."
export CLERK_JWKS_URL="https://ready-crane-41.clerk.accounts.dev/.well-known/jwks.json"
export CORE_PIPELINE_PATH="/home/leafy/personal_project"       # project ROOT, not core/
export DATABASE_URL="postgresql://postgres.wflguoqnrxijfvdeaxhb:PASSWORD@aws-1-ap-south-1.pooler.supabase.com:5432/postgres"
export INVOICE_OCR_DEMO_MODE="0"
export SUPABASE_URL="https://wflguoqnrxijfvdeaxhb.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="eyJ..."                      # must start with eyJ (JWT)
export INVOICE_SCHEMA_PATH="/home/leafy/personal_project/core/config/invoice_schema.json"

python -m uvicorn app.main:app --reload --port 8000
```

**Three things to check on every restart:**

1. **`CORE_PIPELINE_PATH` must point at the project root** (`/home/leafy/personal_project`), not `core/`. `hsn_generator.py` appends `/core` itself.
2. **`SUPABASE_SERVICE_ROLE_KEY` must start with `eyJ`** — found at Supabase → Settings → API → `service_role` → Reveal. Not `sk_test_...` (Clerk) and not `sb_secret_...`.
3. **`INVOICE_SCHEMA_PATH` must be set** pointing at `core/config/invoice_schema.json`. Without it, `main.py` looks in the empty root-level `config/` folder and every Drive sync fails with `FileNotFoundError`.

### Frontend — start

```bash
cd ~/personal_project/webapp/frontend
npm run dev
# Opens at http://localhost:3000
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
   ├── drive_sync.py    Drive → OCR → Postgres (no Storage upload; drive_file_id is the file ref)
   ├── hsn_generator.py LLM → HSN/SAC profile
   └── storage.py       Supabase Storage — only used for manual /invoices/upload now
        ▼
Supabase Postgres (RLS = tenant isolation at DB engine level)
        ▲
Google Drive (service account, read-only)
   └── /preview embed used directly as PDF viewer (no Storage intermediary)
        ▼
core/pipeline.py → ocr_engine.py → extractor.py → validator.py
```

---

## What's Built (Complete Feature List)

### Core pipeline (`core/`)
- Tiered OCR: digital text (pdfplumber) → printed scan (Tesseract) → handwriting/bad scan (flagged for Vision AI)
- Extraction: Groq (`openai/gpt-oss-120b`, strict JSON Schema mode) primary; Gemini (`gemini-2.5-flash-lite`) fallback
- GST validation: GSTIN format, amount reconciliation, CGST+SGST vs total_gst (or IGST), date sanity, currency detection
- Tax normalization: GST/VAT/Tax/CGST+SGST/IGST all → fixed schema fields; `tax_label_raw` preserves original label; `tax_rate_percent` extracts printed % only
- Per-line tax breakdown: `tax_rate`, `tax_amount`, `gross_amount` on each line item
- Drive integration: service account, read-only, dedup via `drive_file_id`
- Rate limiting: per-provider throttling + exponential backoff; fails fast on daily quota errors
- `DEMO_MODE`: regex-based stand-in for live LLM calls

### Auth & multi-tenancy
- Clerk org-first auth; JWT verified server-side via JWKS (RS256)
- Every tenant table has `ENABLE` + `FORCE` RLS; `invoice_app` role has `NOBYPASSRLS`
- All RLS policies use `NULLIF(current_setting(...), '')` — fails closed when context missing
- `verify_rls.py` proves isolation

### Invoice management
- Upload PDF/image → OCR → extract → validate → save to Postgres
- Drive sync: per-org folder registration, incremental sync, org-scoped dedup; files stay in Drive (no Storage upload)
- Vendor matching: GSTIN-first, name fallback, NULL/NULL shared bucket
- PDF viewer: Google Drive `/preview` embed (primary); Supabase Storage signed URL (manual uploads only)
- Edit history: every human correction logged as `(field, old_value, new_value)` triples

### ITC apportionment (Rule 42/43)
- `business_use_percent` per line item (default 100%), editable in UI
- `GET /itc-summary`: total claimable ITC, breakdown by vendor and HSN profile status

### HSN/SAC profile
- Generate preview from business description → review diff → apply
- Manual add/delete with `source='manual'` (never touched by regeneration)
- Line items badged: ✓ expected / ? verify use / unknown

### Frontend
- Invoice list: filter, search, sort, paginate; 3-state detail panel (44px / 400px / 680px)
- Review Modal: Drive `/preview` PDF viewer (primary, no auth prompt, no new tab) + editable header fields + editable line items (including per-line tax %)
- Amount reconciliation warnings surfaced immediately after save
- Activity Log: chronological feed of all changes, filterable by entity/actor type
- ITC Summary: totals, vendor/HSN breakdowns, ambiguous line items list
- Settings: Drive folder, business description, HSN profile editor

---

## Bug Fix History (chronological)

### Bug 1 — `business_use_percent` resets to 100 on every save
**Cause:** `saveBup()` sent key `business_use_pct`; Pydantic silently dropped it.
**Fix:** corrected key name; fixed parallel read-side bug.

### Bug 2 — editing one line-item field silently wiped the others
**Cause:** line-item `UPDATE` set every column unconditionally; omitted fields defaulted to `None`.
**Fix:** rewrote `UPDATE` to use `COALESCE(:field, existing_column)` per field.

### Bug 3 — Vite Fast Refresh: `"shouldAutoReview" export is incompatible`
**Cause:** `ReviewModal.jsx` default-exported a component AND named-exported a plain function.
**Fix:** moved `shouldAutoReview` into `InvoiceReviewUtils.js`.

### Bug 4 — `Failed to resolve import "./ReviewModal"`
**Cause:** `ReviewModal.jsx` and `ActivityLog.jsx` not copied into `webapp/frontend/src/`.
**Fix:** files placed in `src/`.

### Bug 5 — `psycopg2.errors.SyntaxError: syntax error at or near ":"`
**Cause:** `:uid::uuid` fragile under SQLAlchemy + psycopg2.
**Fix:** replaced every `:param::type` with `CAST(:param AS type)`.

### Bug 6 — `psycopg2.errors.InsufficientPrivilege: permission denied for schema public`
**Cause:** app tried `CREATE TABLE IF NOT EXISTS activity_log` at startup using `invoice_app` role.
**Fix:** removed runtime DDL; shipped as `migration_add_activity_log.sql`.

### Bug 7 — `activity_log` table drifted from schema conventions
**Fix:** added FK on `actor_id`, removed inconsistent `WITH CHECK`, added missing index on `entity_type`.

### Bug 8 — Activity Log page blank (including nav)
**Cause:** `EntryRow` referenced `s` alias for `styles` only defined inside parent component, not inside `EntryRow`.
**Fix:** changed every `s.xxx` to `styles.xxx` inside `EntryRow`.

### Bug 9 — Review Modal PDF not loading; Drive fallback using auth-gated `/view` URL
**Cause (1):** `ReviewModal.jsx` discarded `drive_file_id` from file-url response — fallback never triggered.
**Cause (2):** fallback used `/view` (auth-gated, breaks out of iframes) instead of `/preview`.
**Fix:** read `drive_file_id`; use `/preview` exclusively.

### Bug 10 — `shouldAutoReview` duplicate export kept reappearing
**Fix:** fully removed from `ReviewModal.jsx`; lives only in `InvoiceReviewUtils.js`.

### Bug 11 — Vendor/buyer name extraction swallowing the entire seller address block
**Cause:** extraction prompt had no boundary instruction — model grabbed the full contiguous block.
**Fix:** added explicit boundary rules to `GENERAL RULES` with a worked TechVision/Jaipur example; two-column-layout rule; rule to capture non-standard Tax Id values as `vendor_gstin`/`buyer_gstin`.

### Bug 12 — No amount-reconciliation validation on `PATCH /invoices/{id}`
**Fix:** added `_reconcile_invoice_amounts()` in `main.py`. Reuses tolerance from `invoice_schema.json`. Saves anyway, flags `WARNING`, surfaces mismatch text. Auto-clears only when the same check passes and warning text matches this check's own phrasing.

### Bug 13 — Review Modal missing line items and per-line tax %
**Fix:** added full editable line-items table to `ReviewModal.jsx`, saving through the same partial-PATCH contract.

### Bug 14 — Sync reports success but no invoices appear / DB stays empty
**Root causes (in order of discovery):**
1. `SUPABASE_SERVICE_ROLE_KEY` empty — `storage.is_configured()` returned `False`, `drive_sync.py` exited before `save_invoice_row`.
2. `SUPABASE_SERVICE_ROLE_KEY` value was `sb_secret_...` not a real Supabase JWT (`eyJ...`).
3. `INVOICE_SCHEMA_PATH` not set — `main.py` looked in the empty root `config/` folder.
4. `groq` and `google-genai` not installed in venv — pipeline fell back to DEMO stubs.

### Bug 15 — PDF viewer "supabase.co refused to connect"
**Cause:** Supabase free-tier auto-pause. Not a code bug.
**Resolution:** Drive `/preview` fallback already handles this. Un-pause from Supabase dashboard to restore Storage-backed viewer for manual uploads.

### Bug 16 — Groq schema validation 400 on invoices missing optional fields
**Cause:** `_build_json_schema()` listed every field (required + optional) in the JSON Schema `"required"` array. Groq's strict mode rejected any invoice where `payment_due_date`, `po_number`, `currency_code`, `tax_label_raw`, or `tax_rate_percent` were genuinely absent — which is common on real invoices.
**Fix:** `"required"` array now only contains `schema["required_fields"]`. Optional fields remain in `"properties"` (so the model returns them when present) but omitting them no longer causes a 400.

### Bug 17 — Supabase Storage upload impractical long-term; PDF viewer popping new tab
**Decision:** stop uploading Drive-sourced files to Supabase Storage entirely. Files stay in Drive; `drive_file_id` is the reference. The PDF viewer uses `https://drive.google.com/file/d/{id}/preview` directly as primary path — embeddable, no sign-in prompt, no new tab. `storage_path` is saved as `None` for Drive-synced invoices. `storage.py` is still used for the manual `/invoices/upload` endpoint.
**Fix:** rewrote `drive_sync.py` to remove all Storage upload logic.

---

## Key RLS Bugs Already Fixed — Do Not Regress

1. `ENABLE ROW LEVEL SECURITY` alone is not enough — must also `FORCE` on every tenant table
2. Supabase's default `postgres` role has `rolbypassrls = true` — app must use `invoice_app` at runtime
3. `current_setting('app.current_org_id', true)` returns `''` (not NULL) when unset — `NULLIF(..., '')` wraps every policy
4. `SET app.current_org_id = :param` fails with SQLAlchemy — use `set_config('app.current_org_id', :org_id, false)`
5. `orgs` table must have RLS disabled — it has no `org_id` column and is the tenant root. Supabase linter flags this; it is intentional.
6. Session Pooler (not Transaction mode) — transaction mode resets `SET` between transactions
7. `:param::type` inline casts break under SQLAlchemy + psycopg2 — use `CAST(:param AS type)` everywhere

---

## API Endpoints (complete)

```
# Auth — all endpoints require: Authorization: Bearer <Clerk session JWT>

# Invoices
POST   /invoices/upload                   Upload PDF/image manually (still uses Supabase Storage)
GET    /invoices
GET    /invoices/{id}
PATCH  /invoices/{id}
GET    /invoices/{id}/file-url            Returns {url, drive_file_id}; url=null for Drive-synced invoices

# Line items
PATCH  /invoices/{id}/line-items/{lid}

# ITC
GET    /itc-summary

# Org settings
GET    /org/drive-folder
PUT    /org/drive-folder
GET    /org/settings
PUT    /org/settings

# HSN profile
GET    /org/hsn-profile
POST   /org/hsn-profile/generate
POST   /org/hsn-profile/apply
POST   /org/hsn-profile/codes
DELETE /org/hsn-profile/codes/{code}

# HSN classification memory
POST   /invoices/{id}/line-items/{lid}/classify

# Activity log
GET    /activity-log

# Drive sync
POST   /drive/sync

# Admin (cron)
POST   /admin/drive-poll-all              Requires DRIVE_POLL_SECRET header
```

---

## Must-Do Before Real Client Data

- [ ] Rotate all credentials listed at the top of this file
- [ ] Remove `venv/` and `gdrive_key.json` from git history: `git filter-repo --path venv --path gdrive_key.json --invert-paths`
- [ ] Run `verify_rls.py` against live Supabase after rotation
- [ ] Run `migration_add_activity_log.sql` in Supabase SQL Editor if not already done

## Production Deployment
- [ ] Set `CLERK_ALLOWED_ORIGINS` to the real frontend domain (currently hardcoded `localhost:3000`)
- [ ] Set `BACKEND_URL` and `DRIVE_POLL_SECRET` secrets in GitHub Actions for the `/admin/drive-poll-all` cron
- [ ] Get a Render deploy URL, update `VITE_API_BASE` in frontend env

## Current Known Bugs / Issues Left to Fix

- [ ] **`/invoices/upload` manual upload still uses Supabase Storage** — this is fine for now but means manually uploaded PDFs depend on Supabase not being paused. If you want full Drive-only operation, add a "upload to Drive folder" flow instead and retire the manual upload endpoint.
- [ ] **Supabase free tier auto-pause** — the database itself (not just Storage) can pause after inactivity, causing a `Connection timed out` mid-sync. No code fix; un-pause from the dashboard. Upgrading to a paid plan ($25/month) removes this entirely.
- [ ] **`last_drive_sync_at` incremental sync** — the column exists on `orgs` and `modified_after` is wired in `drive_sync.py`, but confirm the backend reads it before listing and writes it after a successful sync cycle. Until confirmed, every sync re-lists the full folder (the dedup check prevents double-processing, just wastes a Drive API call).
- [ ] **No error boundary in `App.jsx`** — a broken tab still blanks the entire page including nav. Add `<ErrorBoundary>` around tab content so failures show an error message instead.

## Features In Progress / Incomplete
- [ ] Drive folder proof-of-control (random-token file before registration accepted)
- [ ] 180-day ITC reversal alert (`payment_date - invoice_date > 180`)
- [ ] GSTR-1 filing status (needs GST portal API integration)
- [ ] Rule 43 capital assets (5% per year reversal, separate from Rule 42 proportional)
- [ ] **ITC rule engine** — blocked-credits table for Section 17(5) items (motor vehicles, food & beverages, club memberships, works contracts for immovable property, etc.)
- [ ] Surface "triage only, not a legal determination" caveat in the UI — currently only in code comments

## Known Limitations
- Gemini capped at ~20 req/day on free tier; Groq hits ~200K tokens/day around invoice 70–95 in a large batch
- `tax_rate_percent` only extracts the printed % — does not back-calculate
- HSN code generation relies on model knowledge, not an official master list

---

## How HSN/SAC Claimability Actually Works — Read Before Filing Anything

**What the system does today:** HSN badges (✓ / ? / unknown) and `itc_claimable` flags are attention-directing signals — not a legal determination. There is no official HSN/SAC master list lookup and no rule table mapping HSN chapters to ITC eligibility under Section 17(5).

**The real gap:** motor vehicles are largely blocked under Section 17(5) regardless of `business_use_percent`. The system computes a claimable number anyway. Do not file GST returns based on these numbers without accountant review.

**What to add:** a blocked-credits table covering Section 17(5): motor vehicles outside specific exceptions, food & beverages, outdoor catering, beauty treatment, club memberships, works contracts for immovable property, personal consumption goods, goods lost/stolen/destroyed.

---

## Key Design Decisions

**Why RLS over app-level `WHERE org_id = ?`** — one forgotten `WHERE` leaks data. RLS moves isolation into the DB engine.

**Why Session Pooler not Transaction Pooler** — `SET app.current_org_id` is a session variable; transaction mode resets it between transactions.

**Why `set_config()` not `SET ... = :param`** — `SET` rejects bind parameters. `set_config()` is a normal SQL function.

**Why `CAST(:param AS uuid)` not `:param::uuid`** — `:param::uuid` is fragile under SQLAlchemy + psycopg2 compiled-statement caching.

**Why service account not OAuth for Drive** — runs unattended. OAuth tokens expire on password changes.

**Why Drive `/preview` not Supabase Storage for Drive-synced files** — files already exist in Drive; uploading a copy to Storage is redundant, adds cost, hits the free-tier 1GB cap, and breaks when the Supabase project auto-pauses. The `/preview` embed is auth-free and never opens a new tab.

**Why `vendor_name` is nullable** — a nameless vendor stays NULL so it's visible as a gap, not buried as fake data.

**Why HSN profile uses generate → preview → apply** — a bad LLM generation should not silently overwrite a reviewed profile. Manually-added codes (`source='manual'`) are never removed by regeneration.

**Why activity logging is app-side DML but table creation is a hand-run migration** — `invoice_app` role is restricted to `SELECT/INSERT/UPDATE/DELETE`. Granting `CREATE` would undermine `NOBYPASSRLS`.

---

## Session History

### Session 1 — Core pipeline
Built `core/`: tiered OCR, Groq/Gemini extraction, GST validator, SQLite output, Drive connector.

### Session 2 — Multi-tenant web backend
Postgres schema with RLS, `auth.py`, `db.py`, `org_resolver.py`, `invoice_store.py`. Fixed RLS bugs. `verify_rls.py` written.

### Session 3 — Frontend + file storage
`App.jsx`, `InvoiceList.jsx`, `Settings.jsx`, Clerk integration, `storage.py`. Supabase Storage upload + signed URLs. Drive sync UI.

### Session 4 — ITC + HSN profile
`Itcsummary.jsx`, `hsn_generator.py`, ITC apportionment (Rule 42/43). `business_use_percent` per line item. HSN profile generation, badges on line items.

### Session 5 — Bug fixes and amount display
`_coerce()` helper for Postgres `Decimal` → `float`. `::float` casts in list SELECT. `parseFloat()` in `fmtAmount`. `last_drive_sync_at` + incremental sync.

### Session 6 — UI fixes, Activity Log, Review Modal
HSN profile field-name mismatch fixed. HSN profile edit UI. Detail panel text cutoff + 3-state sizing. Bug 1 and Bug 2 fixed. Activity Log built. `Itcsummary.jsx` wired into nav. Bug 7 fixed.

### Session 7 — Crash fixes, Review Modal
Bug 3 fixed (Fast Refresh). Bug 5 fixed (`CAST` everywhere). Bug 6 fixed (runtime DDL removed). Review Modal built. `file-url` endpoint extended with `drive_file_id`. Bug 4 resolved.

### Session 8 — Consolidation + open live bugs
Full project log consolidated. Design gap raised: editing amounts doesn't validate against line items.

### Session 9 — Activity Log root-caused; Review Modal Drive-fallback wired
Bug 8 fixed: `ReferenceError: s is not defined` in `EntryRow` — confirmed via headless render harness. Bug 9 (part 1) fixed. `SUPABASE_SERVICE_ROLE_KEY` and `CORE_PIPELINE_PATH` convention flagged.

### Session 10 — Extraction bug, reconciliation validation, Review Modal rebuild
Bug 11 fixed: vendor/buyer name prompt rewritten. Bug 12 fixed: `_reconcile_invoice_amounts()` added. Bug 13 fixed: editable line-items in Review Modal. Bug 9 (part 2) fixed: `/view` → `/preview`. Bug 10 final.

### Session 11 — Full startup debugging; end-to-end sync working
Bug 14 diagnosed and fixed (in order): `SUPABASE_SERVICE_ROLE_KEY` empty → set it → wrong value → got real JWT → `INVOICE_SCHEMA_PATH` not set → added env var → `groq`/`google-genai` not installed → `pip install`, deleted stubs, re-synced. End result: invoices sync, extract, display correctly. Bug 15 documented (Supabase auto-pause, not a code bug).

### Session 12 — Groq schema fix; Drive-as-primary-storage
Bug 16 fixed: `_build_json_schema()` now only marks `schema["required_fields"]` as required — optional fields (`payment_due_date`, `po_number`, `currency_code`, `tax_label_raw`, `tax_rate_percent`) no longer cause a 400 when absent from the invoice.
Bug 17 fixed: `drive_sync.py` rewritten to skip Supabase Storage upload entirely. Files stay in Drive; `storage_path` saved as `None` for Drive-synced invoices. PDF viewer uses `drive.google.com/file/d/{id}/preview` as primary path — no auth prompt, no new tab, no Storage dependency.

---

## Future Vision: From Invoice Extraction Tool to GST Exception Management Platform

### The Final Product Architecture

**The unit of work: the Exception, not the invoice.** The software crunches 10,000 invoices down to ~100 actionable anomalies (Missing in 2B, Duplicate, Potential 17(5)). Humans make the final call.

**Deterministic matching:** reconciling purchase registers with GSTR-2B is purely mathematical — Found, Missing, Mismatch, or Duplicate. No AI used here.

**Restricted AI roles — strictly boxed:**
1. **Extraction:** parsing unstructured PDFs into rigid JSON schemas with confidence scores
2. **Vendor normalization:** grouping disparate names into unified canonical vendors
3. **History search:** surfacing similar past human decisions

**The human-in-the-loop moat:** the system never auto-claims ITC. Humans make every final call (`CLAIM_ITC`, `DO_NOT_CLAIM`, `CAPITALIZE`). These decisions build irreplaceable institutional memory over time.

### Pragmatic 3-Month Build Plan

**Month 1 — Ingestion & Visibility:** Drive sync, PDF extraction, Purchase Register UI.

**Month 2 — Compliance & Triage:** GSTR-2B import, deterministic matching engine (Found / Missing / Mismatch / Duplicate), Exception Queue.

**Month 3 — Workflow & Auditability:** Review Workflow, append-only Audit Trail, Reviewer Notes, threshold policies, precedent text search.

### Future Technical Agenda: Own OCR/Extraction Model

Train or fine-tune a document understanding model specifically on Indian GST invoices, replacing the current Groq/Gemini calls. The labeled dataset is being generated now by the MVP's human review workflow — every human correction is ground truth. Target: Month 4+, after 20,000+ invoices processed through the MVP.