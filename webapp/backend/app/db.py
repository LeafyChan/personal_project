"""
db.py
=====
Database connection layer. This file exists specifically to make the RLS
guarantee from schema.sql/verify_rls.py impossible to forget in practice:
every request handler gets a connection that has ALREADY had
app.current_org_id set, via FastAPI's dependency injection, before any
query runs. There is no code path to a tenant-owned table that skips this.

WHY THIS SPECIFIC SHAPE:
- One SQLAlchemy engine, using a connection pool, but every individual
  request gets its OWN checked-out connection for the duration of that
  request (not reused across requests) - this matters because
  app.current_org_id is a SESSION variable; it would leak between requests
  if connections were shared/reused without resetting it first.
- get_org_scoped_db() is a FastAPI dependency: it checks out a connection,
  sets app.current_org_id from the verified org_id (which comes from
  Clerk auth - see auth.py), yields it for the request handler to use, and
  resets the variable + returns the connection to the pool when the
  request ends. Resetting on the way out is what prevents leakage to
  whatever request happens to reuse that pooled connection next.
- Uses Supabase's Session Pooler connection string (see PROJECT_LOG.md for
  why: WSL2/IPv6 connectivity + RLS needs session-persistent SET, which
  Transaction-mode pooling would break).
"""

import os
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Expected the Supabase Session Pooler URI, "
        "e.g. postgresql://invoice_app.PROJECT_REF:PASSWORD@aws-REGION.pooler.supabase.com:5432/postgres"
    )

# pool_pre_ping avoids handing out dead connections after Supabase idles one
# out; pool_size kept modest since the free tier has a limited connection
# budget and this is a single small backend, not a high-concurrency service.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=5)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


@contextmanager
def _session_with_org(org_id: str):
    """Core primitive: a DB session with app.current_org_id set for its
    entire lifetime, guaranteed reset on the way out even if the request
    raised an exception (the finally block runs regardless)."""
    session = SessionLocal()
    try:
        # IMPORTANT: plain "SET app.current_org_id = :org_id" does NOT work
        # with a bind parameter - SET is a configuration command, not a
        # data query, and Postgres rejects the driver's automatic type cast
        # suffix (e.g. '...'::uuid) with a syntax error right at the "::".
        # set_config() is a normal SQL function, not a special command, so
        # it DOES accept parameters safely and correctly - this is the
        # actual fix, not just a workaround. is_local=false (the third
        # arg) makes it session-scoped, matching the RESET-based cleanup
        # below; true would make it transaction-scoped instead, which
        # would reset prematurely on intermediate commits.
        session.execute(
            text("SELECT set_config('app.current_org_id', :org_id, false)"),
            {"org_id": str(org_id)},
        )
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        # RESET (not just setting to '') ensures the next request to reuse
        # this pooled connection starts with a clean slate - this is the
        # specific line that prevents session-variable leakage across
        # requests when connections are pooled and reused.
        session.execute(text("RESET app.current_org_id"))
        session.close()


def get_org_scoped_db(org_id: str):
    """FastAPI dependency factory. Used as:
        def endpoint(db: Session = Depends(lambda: get_org_scoped_db(org_id)))
    In practice, routes.py wraps this with the verified org_id from the
    Clerk auth dependency - see auth.py's get_current_org()."""
    with _session_with_org(org_id) as session:
        yield session


def get_admin_db():
    """
    UNSCOPED connection - no org_id, no RLS context set. This will see
    NOTHING in any tenant table (RLS fails closed without context, per
    verify_rls.py Test 3) unless used for genuinely cross-tenant admin
    operations that have their own separate authorization check.

    Deliberately named differently from get_org_scoped_db so it's never
    reached for by accident/autocomplete in a request handler that should
    be tenant-scoped. If you find yourself importing this in a normal
    invoice/vendor endpoint, that's almost certainly a mistake.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()