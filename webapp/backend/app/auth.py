"""
auth.py
=======
Verifies Clerk-issued session JWTs and extracts the verified org_id that
db.py uses to set app.current_org_id. This file is the other half of the
security boundary: db.py guarantees isolation IF given a correct org_id;
this file is what guarantees the org_id it hands over was never forged.

KEY PRINCIPLE: never trust a claim from a JWT that hasn't had its signature
verified first. Clerk's tokens are RS256-signed - verified against Clerk's
public JWKS (JSON Web Key Set), fetched from Clerk's own well-known
endpoint and cached, not a secret we configure ourselves. This means a
forged or tampered token fails verification outright, regardless of what
org_id an attacker tries to put in the payload.

WHAT WE DO NOT DO: we do not read org_id from a request header, query
param, or request body anywhere in this backend. The ONLY source of
org_id for any database operation is this verified token's `o.id` claim.
If a future endpoint is tempted to accept an org_id as a parameter "just
this once" (e.g. for convenience in an admin tool), that bypasses this
entire security model - don't.
"""

import os
import time
from functools import lru_cache

import jwt
from fastapi import Header, HTTPException
from jwt import PyJWKClient

CLERK_JWKS_URL = os.environ.get("CLERK_JWKS_URL")
if not CLERK_JWKS_URL:
    raise RuntimeError(
        "CLERK_JWKS_URL is not set. Find it in the Clerk dashboard under "
        "Configure -> API Keys -> Show JWT Public Key -> JWKS URL, looks like "
        "https://your-instance.clerk.accounts.dev/.well-known/jwks.json"
    )

# Allowed origins for the azp (authorized party) claim check below - the
# frontend domain(s) permitted to mint tokens this backend will accept.
# Set via env so local dev (localhost) and production (real domain) both
# work without code changes.
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("CLERK_ALLOWED_ORIGINS", "http://localhost:3000").split(",")
]

_jwk_client = PyJWKClient(CLERK_JWKS_URL)


def _verify_token(token: str) -> dict:
    """Verifies signature + standard claims. Raises jwt exceptions on any
    failure - caller (get_current_org) converts these to HTTP 401."""
    signing_key = _jwk_client.get_signing_key_from_jwt(token)
    payload = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        options={"require": ["exp", "iat", "sub"]},
    )

    # azp check: confirms the token was issued for a frontend we recognize,
    # not replayed from a different application using the same Clerk
    # instance. Skip only if azp is genuinely absent (Clerk omits it in some
    # configurations) - never skip if it's present but doesn't match.
    azp = payload.get("azp")
    if azp is not None and azp not in ALLOWED_ORIGINS:
        raise jwt.InvalidTokenError(f"azp claim '{azp}' not in allowed origins")

    return payload


def get_current_org(authorization: str = Header(...)) -> dict:
    """
    FastAPI dependency. Extracts and verifies the Bearer token, returns the
    verified org context: {"org_id": ..., "user_id": ..., "role": ...}.

    Raises HTTP 401 for: missing header, malformed token, bad signature,
    expired token, or a user who isn't part of an active organization
    (Clerk's `sts: "pending"` case - happens when org membership is
    required but the user hasn't joined one yet).

    This function NEVER returns an org_id that wasn't cryptographically
    verified to belong to the token's signer.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()

    try:
        payload = _verify_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, f"Invalid session token: {e}")

    if payload.get("sts") == "pending":
        raise HTTPException(
            403, "User has not joined an organization yet - org membership required"
        )

    org_claim = payload.get("o")
    if not org_claim or "id" not in org_claim:
        raise HTTPException(
            403, "Token has no active organization - this app requires org membership"
        )

    return {
        "org_id": org_claim["id"],          # Clerk's org ID - maps to orgs.clerk_org_id, NOT orgs.org_id directly, see note in routes.py
        "user_id": payload["sub"],          # Clerk's user ID - maps to users.clerk_user_id
        "role": org_claim.get("rol"),       # Clerk's org-level role claim, if configured
    }