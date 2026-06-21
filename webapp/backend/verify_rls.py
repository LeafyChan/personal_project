"""
verify_rls.py
=============
RUN THIS AGAINST A REAL POSTGRES INSTANCE BEFORE TRUSTING THE SCHEMA.

I could not install/run PostgreSQL in the sandbox this was built in (network
restrictions block the package mirror), so the RLS policies in schema.sql
have been checked for correct syntax and structural completeness, but NOT
verified to actually behave correctly against a live database. Given
security is the top priority here, treat that gap as real until this script
passes - don't deploy on the strength of "the SQL looks right" alone.

What this proves, concretely:
  1. Two orgs, each with one invoice.
  2. With app.current_org_id set to org A, a SELECT * FROM invoices returns
     ONLY org A's row - org B's invoice must be completely invisible, not
     just filtered by convention.
  3. Same check from org B's side.
  4. A query with NO org context set (simulating a bug where the backend
     forgot to set it) returns ZERO rows, not all rows - the fail-safe
     direction matters: a missing filter must fail closed, not open.

Usage:
    pip install psycopg2-binary
    python3 verify_rls.py "postgresql://user:pass@host:port/dbname"

Run this once against your real Render Postgres connection (after applying
schema.sql) before pointing any real client data at it.
"""

import sys
import uuid

import psycopg2


def run(conn_string: str):
    conn = psycopg2.connect(conn_string)
    conn.autocommit = True
    cur = conn.cursor()

    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    clerk_a = f"test_clerk_org_{org_a[:8]}"
    clerk_b = f"test_clerk_org_{org_b[:8]}"

    print("Setting up two test orgs with one invoice each...")
    # orgs table itself has RLS too (via users policy reference) but orgs
    # rows aren't org-scoped data in the same sense - inserting orgs/setup
    # rows happens before any app.current_org_id is meaningful, so these
    # need a privileged path in a real app (e.g. a separate signup flow
    # connection). For this test, we insert as the table owner context
    # implicitly bypassed RLS before FORCE was applied - now that FORCE is
    # on, every insert needs the matching org_id set first, including the
    # orgs bootstrap row itself if orgs ever gets RLS. orgs has no org_id
    # column (it's the tenant root), so it's unaffected - only the
    # invoices insert below needs the context set per-row.
    cur.execute(
        "INSERT INTO orgs (org_id, clerk_org_id, name) VALUES (%s, %s, 'Test Org A'), (%s, %s, 'Test Org B')",
        (org_a, clerk_a, org_b, clerk_b),
    )

    cur.execute("SET app.current_org_id = %s", (org_a,))
    cur.execute(
        "INSERT INTO invoices (org_id, file_name) VALUES (%s, 'org_a_invoice.pdf')", (org_a,)
    )
    cur.execute("SET app.current_org_id = %s", (org_b,))
    cur.execute(
        "INSERT INTO invoices (org_id, file_name) VALUES (%s, 'org_b_invoice.pdf')", (org_b,)
    )

    failures = []

    def check(label, expected_count, expected_file=None):
        cur.execute("SELECT file_name FROM invoices")
        rows = [r[0] for r in cur.fetchall()]
        ok = len(rows) == expected_count and (expected_file is None or rows == [expected_file])
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {label}: got {rows}")
        if not ok:
            failures.append(label)

    print("\nTest 1: app.current_org_id = org A -> should see ONLY org A's invoice")
    cur.execute("SET app.current_org_id = %s", (org_a,))
    check("org A sees only its own invoice", 1, "org_a_invoice.pdf")

    print("\nTest 2: app.current_org_id = org B -> should see ONLY org B's invoice")
    cur.execute("SET app.current_org_id = %s", (org_b,))
    check("org B sees only its own invoice", 1, "org_b_invoice.pdf")

    print("\nTest 3: app.current_org_id RESET (simulating a backend bug) -> should see ZERO rows, not all")
    cur.execute("RESET app.current_org_id")
    check("no org context set -> fails closed (zero rows)", 0)

    print("\nCleaning up test data...")
    cur.execute("SET app.current_org_id = %s", (org_a,))
    cur.execute("DELETE FROM invoices WHERE org_id = %s", (org_a,))
    cur.execute("SET app.current_org_id = %s", (org_b,))
    cur.execute("DELETE FROM invoices WHERE org_id = %s", (org_b,))
    cur.execute("RESET app.current_org_id")
    # orgs/users deletes need elevated privilege if RLS also blocks DELETE
    # without context - reconnect path below handles that generically.
    cur.execute("DELETE FROM orgs WHERE org_id IN (%s, %s)", (org_a, org_b))

    print()
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED: {failures}")
        print("DO NOT deploy with real client data until this passes.")
        sys.exit(1)
    else:
        print("RESULT: All RLS isolation checks passed.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print('Usage: python3 verify_rls.py "postgresql://user:pass@host:port/dbname"')
        sys.exit(1)
    run(sys.argv[1])