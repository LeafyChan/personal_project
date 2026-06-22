-- schema.sql
-- ===========
-- Multi-tenant PostgreSQL schema with Row-Level Security (RLS).
--
-- WHY RLS, NOT JUST "remember to filter by org_id in every query":
-- Application-level filtering (WHERE org_id = ? in every query) is the
-- common approach, but it has one fatal property: a single forgotten
-- WHERE clause, in one endpoint, on one bad day, leaks every other
-- client's invoices to whoever's logged in. That's not a hypothetical -
-- it's the single most common cause of real multi-tenant SaaS data
-- breaches. RLS moves the isolation guarantee into the database engine
-- itself: even if application code has a bug, queries the database thinks
-- aren't allowed to see another tenant's rows simply return nothing for
-- those rows, full stop. Defense in depth: app code SHOULD still filter by
-- org_id (defense layer 1), but RLS (defense layer 2) means a missed
-- filter is a silent no-op, not a leak.
--
-- CRITICAL: ENABLE ROW LEVEL SECURITY alone is NOT enough. Postgres
-- silently bypasses RLS policies for a table's OWNER by default - and the
-- app's own database user typically owns every table it creates (exactly
-- what happened when this was first tested: invoice_app owned everything,
-- so every RLS policy was silently ignored and both test orgs could see
-- each other's data). FORCE ROW LEVEL SECURITY closes this specific gap by
-- applying policies to the owner too. Every table below has both
-- statements - if you ever add a new tenant table, you need both, not just
-- ENABLE.
--
-- ALSO NOTE: FORCE ROW LEVEL SECURITY applies to INSERT/UPDATE/DELETE as
-- well as SELECT, not just reads. The backend must set app.current_org_id
-- BEFORE every single database operation on every request - including
-- writes - or that operation is rejected outright (InsufficientPrivilege),
-- not silently scoped. This is the correct failure mode (a bug that tries
-- to write to the wrong org fails loudly instead of succeeding), but it
-- means there is no code path in the FastAPI backend that touches these
-- tables without first setting this session variable from the verified
-- Clerk org context.
--
-- ALSO NOTE: current_setting('app.current_org_id', true) returns '' (empty
-- string), not NULL, when the variable was never set or was RESET. Casting
-- ''::uuid directly throws a Postgres error rather than evaluating to a
-- clean false - found during testing when a RESET-context query crashed
-- instead of returning zero rows. Every policy below wraps this in
-- NULLIF(..., '') first, turning the empty string into a real NULL so the
-- org_id = NULL comparison correctly evaluates to "no rows match" instead
-- of erroring. This is what makes "forgot to set org context" fail closed
-- (empty result) rather than crashing - both are safe from a leak
-- standpoint, but only one is the intended behavior.
-- Every tenant-owned table has org_id. A Postgres SESSION variable
-- (app.current_org_id) is set once per request by the backend, right after
-- verifying the Clerk auth token. Every table's RLS policy checks rows
-- against that session variable. The application connection literally
-- cannot read another org's rows while that session var is set to a
-- different org - there's no query you can write, correct or buggy, that
-- bypasses it (short of using a separate superuser/bypass-RLS connection,
-- which the app's normal DB role does not have).

-- ============================================================
-- ORGS & USERS
-- ============================================================
-- orgs = "clients" in your terms - one row per company/client using the
-- product. Everything else hangs off org_id.
CREATE TABLE IF NOT EXISTS orgs (
    org_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clerk_org_id    TEXT UNIQUE NOT NULL,  -- Clerk's own org identifier - source of truth for membership
    name            TEXT NOT NULL,
    drive_folder_id TEXT UNIQUE,           -- nullable; UNIQUE prevents two orgs claiming the same Drive folder
    -- One-sentence description of what the business does (e.g. "furniture
    -- manufacturing and retail shop"). Free text the org owner writes once
    -- in Settings - this is the input to HSN profile generation below, not
    -- itself a compliance artifact.
    business_description TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- users = people who can log in. clerk_user_id is the link to Clerk's
-- identity - this table never stores a password or session token itself,
-- Clerk owns all of that.
CREATE TABLE IF NOT EXISTS users (
    user_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clerk_user_id   TEXT UNIQUE NOT NULL,
    org_id          UUID NOT NULL REFERENCES orgs(org_id),
    email           TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'member',  -- 'admin' | 'member', extend later if needed
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
CREATE POLICY users_isolation ON users
    USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);

-- ============================================================
-- VENDORS (tenant-scoped version of the original SQLite table)
-- ============================================================
CREATE TABLE IF NOT EXISTS vendors (
    vendor_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              UUID NOT NULL REFERENCES orgs(org_id),
    vendor_gstin        TEXT,
    vendor_name         TEXT,           -- nullable on purpose, see original database.py note
    first_seen_date     DATE,
    last_seen_date      DATE,
    invoice_count       INTEGER NOT NULL DEFAULT 0,
    total_amount        NUMERIC(14, 2) NOT NULL DEFAULT 0,
    gstr1_filing_status TEXT,
    gstr1_last_checked  TIMESTAMPTZ,
    UNIQUE (org_id, vendor_gstin, vendor_name)
);
ALTER TABLE vendors ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendors FORCE ROW LEVEL SECURITY;
CREATE POLICY vendors_isolation ON vendors
    USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);

-- ============================================================
-- HSN/SAC PROFILE (one row per code the org's business is expected to use)
-- ============================================================
-- Generated from business_description via an LLM call (core/hsn_generator.py),
-- but stored as individual rows - not a JSON blob - specifically so:
--   (a) manually added/removed codes survive a later regeneration untouched
--       (regeneration is preview-then-apply, never a silent overwrite - see
--       the /org/hsn-profile/generate vs /apply split in main.py)
--   (b) this table is what GET /itc-summary joins line_items against, so a
--       line item's hsn_code can be checked against "is this expected for
--       this business" without re-parsing a JSON column on every request.
CREATE TABLE IF NOT EXISTS hsn_profile_codes (
    hsn_profile_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES orgs(org_id),
    code            TEXT NOT NULL,
    code_type       TEXT NOT NULL DEFAULT 'HSN',   -- 'HSN' (goods) | 'SAC' (services)
    description     TEXT,                           -- what the code covers, shown as a tooltip/label
    -- 'expected'   - LLM is confident this is a normal code for this business
    -- 'ambiguous'  - LLM flagged this as needing human confirmation on first
    --                use (e.g. could be raw material or a fixed asset)
    -- 'manual'     - added directly by the user, never touched by regeneration
    confidence      TEXT NOT NULL DEFAULT 'expected',
    source          TEXT NOT NULL DEFAULT 'generated',  -- 'generated' | 'manual'
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, code)
);
ALTER TABLE hsn_profile_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE hsn_profile_codes FORCE ROW LEVEL SECURITY;
CREATE POLICY hsn_profile_codes_isolation ON hsn_profile_codes
    USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);

CREATE INDEX IF NOT EXISTS idx_hsn_profile_org ON hsn_profile_codes(org_id);

-- ============================================================
-- INVOICES
-- ============================================================
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id            UUID NOT NULL REFERENCES orgs(org_id),
    vendor_id         UUID REFERENCES vendors(vendor_id),

    -- source tracking: a file can arrive via direct upload OR Drive sync
    source_type       TEXT NOT NULL DEFAULT 'upload',  -- 'upload' | 'drive'
    drive_file_id     TEXT,           -- Drive's permanent file ID, dedup key when source_type='drive'
    storage_path      TEXT,           -- where the actual file bytes live (see storage notes below)
    file_name         TEXT NOT NULL,
    page              INTEGER,
    extraction_method TEXT,
    confidence        REAL,
    status            TEXT,           -- PASSED | WARNING | FAILED | NEEDS_MANUAL_REVIEW
    issues            TEXT,

    vendor_name       TEXT,
    vendor_gstin      TEXT,
    buyer_name        TEXT,
    buyer_gstin       TEXT,
    invoice_number    TEXT,
    invoice_date      DATE,
    payment_due_date  DATE,
    payment_date      DATE,
    place_of_supply   TEXT,
    taxable_amount    NUMERIC(14, 2),
    cgst_amount       NUMERIC(14, 2),
    sgst_amount       NUMERIC(14, 2),
    igst_amount       NUMERIC(14, 2),
    total_gst_amount  NUMERIC(14, 2),
    total_amount      NUMERIC(14, 2),
    currency_code     TEXT,
    tax_label_raw     TEXT,
    tax_rate_percent  NUMERIC(5, 2),  -- e.g. 12.00 for "VAT @ 12%"; null when no rate was printed
    po_number         TEXT,
    hsn_codes         JSONB,
    line_items_raw    JSONB,

    -- learning-loop hooks (Phase 5): every human correction is logged, not
    -- just overwritten, so corrections can later be used as training/
    -- few-shot signal without losing the original extracted value.
    is_user_verified  BOOLEAN NOT NULL DEFAULT false,
    last_edited_by    UUID REFERENCES users(user_id),
    last_edited_at    TIMESTAMPTZ,

    processed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (org_id, drive_file_id)  -- dedup scoped per-org, not global
);
ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices FORCE ROW LEVEL SECURITY;
CREATE POLICY invoices_isolation ON invoices
    USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);

CREATE INDEX IF NOT EXISTS idx_invoices_org_status ON invoices(org_id, status);
CREATE INDEX IF NOT EXISTS idx_invoices_org_date ON invoices(org_id, invoice_date);
CREATE INDEX IF NOT EXISTS idx_invoices_org_vendor ON invoices(org_id, vendor_id);

-- ============================================================
-- LINE ITEMS
-- ============================================================
CREATE TABLE IF NOT EXISTS line_items (
    line_item_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        UUID NOT NULL REFERENCES orgs(org_id),  -- denormalized for RLS - see note below
    invoice_id    UUID NOT NULL REFERENCES invoices(invoice_id) ON DELETE CASCADE,
    description   TEXT,
    hsn_code      TEXT,
    quantity      NUMERIC(12, 3),
    rate          NUMERIC(14, 2),
    amount        NUMERIC(14, 2),
    -- This line's own tax rate, ONLY when the invoice actually prints a
    -- per-line rate that's genuinely different from the bill-level rate
    -- (e.g. a mixed-rate invoice with some 12% items and some 18% items).
    -- NULL means "no distinct per-line rate was printed" - the bill-level
    -- invoices.tax_rate_percent applies uniformly, which is mathematically
    -- identical to applying it per line when every line shares one rate.
    -- This column exists specifically for the case where that assumption
    -- is wrong (mixed-rate invoices) - see hsn_generator.py module note
    -- and validator.py's mixed-rate detection.
    line_tax_rate_percent NUMERIC(5, 2),
    -- Business-use share of this line, 0-100, default 100 (fully business
    -- use). The "200 screws, 100 personal" case: GST invoice ITC is
    -- claimable on the full GST-paid amount per law, but only the
    -- business-use portion should actually be CLAIMED - this is the field
    -- the user edits to make that split explicit and auditable, rather
    -- than silently claiming either the full amount or zero.
    business_use_percent NUMERIC(5, 2) NOT NULL DEFAULT 100,
    -- Phase 6 hook: ITC claimability depends on the HSN code's GST rate
    -- and category - this column exists now so that logic is a column-fill
    -- once the HSN rate-lookup table exists, not a schema change.
    itc_claimable    BOOLEAN,
    itc_reason       TEXT
);
-- org_id is denormalized onto line_items (not just inherited via invoice_id)
-- specifically so RLS can filter this table directly without a JOIN back to
-- invoices on every read - a JOIN-based RLS policy is more expensive and,
-- more importantly, isn't natively how Postgres RLS USING clauses work
-- (they can reference other tables in a subquery, but a denormalized
-- column is simpler to audit for correctness, which matters more here than
-- a few bytes of duplication).
ALTER TABLE line_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE line_items FORCE ROW LEVEL SECURITY;
CREATE POLICY line_items_isolation ON line_items
    USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);

CREATE INDEX IF NOT EXISTS idx_line_items_org_invoice ON line_items(org_id, invoice_id);
CREATE INDEX IF NOT EXISTS idx_line_items_org_hsn ON line_items(org_id, hsn_code);

-- ============================================================
-- EDIT HISTORY (Phase 5 learning-loop foundation)
-- ============================================================
-- Every human correction to an extracted field is logged here, separately
-- from the invoices table itself. This is what lets the "algo learns from
-- corrections" feature exist later without redesigning storage: corrections
-- are already structured as (field, wrong_value, corrected_value) triples,
-- ready to feed back into prompts or a fine-tuning set.
CREATE TABLE IF NOT EXISTS edit_history (
    edit_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES orgs(org_id),
    invoice_id      UUID NOT NULL REFERENCES invoices(invoice_id) ON DELETE CASCADE,
    edited_by       UUID REFERENCES users(user_id),
    field_name      TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    edit_reason     TEXT,  -- 'correction' | 'fill_missing' | 'new_row_added' etc.
    edited_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE edit_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE edit_history FORCE ROW LEVEL SECURITY;
CREATE POLICY edit_history_isolation ON edit_history
    USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid);

CREATE INDEX IF NOT EXISTS idx_edit_history_org_invoice ON edit_history(org_id, invoice_id);