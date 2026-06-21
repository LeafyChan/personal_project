"""
org_resolver.py
================
Bridges Clerk's identity (clerk_org_id, clerk_user_id - strings like
"org_2abc...") to our internal schema's UUID org_id/user_id. This has to
run AFTER auth.py's signature verification and BEFORE db.py's
app.current_org_id is set - it's the missing link between "this token is
genuinely signed by Clerk" and "here is the UUID our RLS policies actually
key on."

JUST-IN-TIME PROVISIONING:
The first time a given Clerk org/user is seen, a row is created for them
here rather than requiring a separate signup-webhook flow. This keeps
Phase 2 simple (no webhook endpoint needed yet) at the cost of doing a
lookup-or-create on every request rather than a pure lookup. At real
production scale, a Clerk webhook (organization.created /
user.created) that pre-populates these rows would be more efficient -
noted as a future optimization, not required for correctness now.

CRITICAL: this lookup/create step runs on the UNSCOPED admin connection
(get_admin_db), not an org-scoped one - by definition, we don't know the
internal org_id yet, that's literally what we're resolving. This is the
ONE legitimate use of the unscoped connection in the whole backend. It is
safe specifically because orgs/users rows being created here are scoped by
clerk_org_id/clerk_user_id, which came from a cryptographically verified
JWT (auth.py) - not from anything a request could forge.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import SessionLocal


def resolve_org_and_user(clerk_org_id: str, clerk_user_id: str, email: str = None) -> dict:
    """
    Returns {"org_id": <our UUID>, "user_id": <our UUID>}, creating rows if
    this org/user hasn't been seen before. Runs on an unscoped connection
    deliberately - see module docstring.
    """
    session: Session = SessionLocal()
    try:
        org_row = session.execute(
            text("SELECT org_id FROM orgs WHERE clerk_org_id = :cid"),
            {"cid": clerk_org_id},
        ).fetchone()

        if org_row:
            org_id = org_row[0]
        else:
            org_id = str(uuid.uuid4())
            session.execute(
                text(
                    "INSERT INTO orgs (org_id, clerk_org_id, name) "
                    "VALUES (:oid, :cid, :name)"
                ),
                {"oid": org_id, "cid": clerk_org_id, "name": f"Org {clerk_org_id}"},
                # name is a placeholder - Phase 2 endpoint or webhook should
                # update it with the real org name from Clerk's API/webhook
                # payload once that's wired up. Not blocking for now.
            )

        # check-then-insert is not atomic across requests/retries - if an
        # earlier attempt partially committed a users row (e.g. crashed
        # right after this insert but before the caller saw success), a
        # plain INSERT here hits a duplicate-key error even though "the
        # user doesn't exist yet" was true a moment ago. ON CONFLICT DO
        # UPDATE makes this idempotent: existing row wins on user_id, but
        # org_id is refreshed in case it changed, and we always read back
        # the real row's user_id rather than trusting a freshly generated
        # uuid that might not be the one actually stored.
        new_user_id = str(uuid.uuid4())
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"),
            {"oid": str(org_id)},
        )
        result = session.execute(
            text(
                "INSERT INTO users (user_id, clerk_user_id, org_id, email) "
                "VALUES (:uid, :cid, :oid, :email) "
                "ON CONFLICT (clerk_user_id) DO UPDATE SET org_id = EXCLUDED.org_id "
                "RETURNING user_id"
            ),
            {"uid": new_user_id, "cid": clerk_user_id, "oid": org_id, "email": email or ""},
        )
        user_id = result.scalar()

        session.commit()
        # This connection returns to the same shared pool db.py's sessions
        # use - reset the context before releasing it, so a different
        # request that happens to check out this exact connection next
        # doesn't briefly inherit this org's context before its own SET runs.
        session.execute(text("RESET app.current_org_id"))
        return {"org_id": org_id, "user_id": user_id}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()