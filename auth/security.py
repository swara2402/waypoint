"""Secure local authentication, JWT sessions, RBAC and tenant context."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import HTTPException, Request, status
from sqlalchemy import select

from config.settings import settings
from database.auth_models import Tenant, User
from database.session import AsyncSessionLocal

ALGORITHM = "HS256"
COOKIE_NAME = "prism_session"
ROLE_ORDER = {"viewer": 10, "engineer": 20, "admin": 30, "owner": 40}
PBKDF2_ROUNDS = 310_000

# Read through the validated settings object so a secret supplied in ``.env`` is
# honoured. ``os.getenv`` alone would silently miss it, because the settings
# loader never mutates ``os.environ``.
#
# ``signing_secret`` (not ``jwt_secret``) because it never returns an empty
# string: signing with "" raises InvalidKeyError inside PyJWT. Outside
# development/test, an unset or short secret already aborted startup in
# ``validate_production_secrets``; this only covers the local/test fallback.
JWT_SECRET = settings.signing_secret
SESSION_HOURS = settings.session_hours


# --------------------------------------------------------------------------
# Token revocation
# --------------------------------------------------------------------------
# JWTs are self-contained and therefore valid until ``exp``. A denylist keyed on
# the token's ``jti`` makes logout and forced revocation effective. Production
# uses Redis so every API replica observes the same revocation state. Local/test
# environments retain an in-memory fallback to avoid requiring infrastructure.
_REVOKED: dict[str, float] = {}
_REDIS_REVOCATION_CLIENT = None
_REDIS_REVOCATION_PREFIX = "waypoint:revoked:"

async def _revocation_redis():
    global _REDIS_REVOCATION_CLIENT
    if _REDIS_REVOCATION_CLIENT is None and settings.redis_url:
        import redis.asyncio as aioredis
        _REDIS_REVOCATION_CLIENT = aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
    return _REDIS_REVOCATION_CLIENT

async def revoke_jti(jti: str, ttl_seconds: float) -> None:
    if not jti or ttl_seconds <= 0:
        return
    client = await _revocation_redis()
    if client is not None:
        await client.set(f"{_REDIS_REVOCATION_PREFIX}{jti}", "1", ex=max(1, int(ttl_seconds)))
        return
    _REVOKED[jti] = time.time() + ttl_seconds
    _prune_revoked()

async def is_revoked(jti: str) -> bool:
    if not jti:
        return False
    client = await _revocation_redis()
    if client is not None:
        return bool(await client.exists(f"{_REDIS_REVOCATION_PREFIX}{jti}"))
    expires_at = _REVOKED.get(jti)
    if expires_at is None:
        return False
    if expires_at <= time.time():
        _REVOKED.pop(jti, None)
        return False
    return True

def _prune_revoked() -> None:
    if len(_REVOKED) < 256:
        return
    now = time.time()
    for key in [k for k, v in _REVOKED.items() if v <= now]:
        _REVOKED.pop(key, None)


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    tenant_id: str
    role: str
    tenant_name: str
    # Capability list for machine credentials; ``None`` for human sessions,
    # which are governed by role alone.
    scopes: Optional[tuple[str, ...]] = None

    def can(self, role: str) -> bool:
        # Unknown roles fail closed on both sides: an unrecognised held role
        # scores 0, an unrecognised required role demands more than any.
        return ROLE_ORDER.get(self.role, 0) >= ROLE_ORDER.get(role, 999)


def hash_password(password: str, salt: bytes | None = None) -> str:
    if len(password) < settings.password_min_length:
        raise ValueError(f"Password must be at least {settings.password_min_length} characters")
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


# Burned when no account matches so that "unknown email" and "wrong password"
# cost the same wall-clock time. Without it the login endpoint is a ~40ms user
# enumeration oracle.
_TIMING_EQUALISER_HASH = hash_password("timing-equaliser-" + secrets.token_urlsafe(16))


def create_access_token(user: User, tenant: Tenant) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "email": user.email,
        "tenant_id": user.tenant_id,
        "role": user.role,
        "tenant_name": tenant.name,
        "iat": now,
        "exp": now + timedelta(hours=SESSION_HOURS),
        "jti": secrets.token_hex(16),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    # The algorithm is pinned; it is never derived from the token's header, so
    # ``alg: none`` and RS/HS confusion are structurally impossible.
    return jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])


async def authenticate_login(
    email: str, password: str, workspace: Optional[str] = None
) -> Optional[Principal]:
    """Authenticate an email/password pair.

    ``workspace`` optionally disambiguates the same address existing in more
    than one workspace. When omitted, the address must resolve to exactly one
    active account; an ambiguous address is rejected rather than silently
    binding the caller to an arbitrary tenant.
    """
    async with AsyncSessionLocal() as session:
        stmt = (
            select(User, Tenant)
            .join(Tenant, Tenant.id == User.tenant_id)
            .where(
                User.email == email.strip().lower(),
                User.is_active.is_(True),
                Tenant.is_active.is_(True),
            )
        )
        if workspace:
            stmt = stmt.where(Tenant.name == workspace.strip())
        rows = (await session.execute(stmt)).all()
        if not rows:
            verify_password(password, _TIMING_EQUALISER_HASH)
            return None
        if len(rows) > 1:
            # Equal cost to a wrong password: an ambiguous address must not be
            # an enumeration or workspace-probing signal.
            verify_password(password, _TIMING_EQUALISER_HASH)
            return None
        user, tenant = rows[0]
        if not verify_password(password, user.password_hash):
            return None
        return Principal(user.id, user.email, user.tenant_id, user.role, tenant.name)


async def principal_from_request(request: Request) -> Principal:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        raise HTTPException(401, "Please sign in to PRISM", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = decode_access_token(token)
    except jwt.PyJWTError:
        raise HTTPException(
            401,
            "Your PRISM session has expired. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if await is_revoked(claims.get("jti", "")):
        raise HTTPException(401, "Your PRISM session has been revoked. Please sign in again.")
    user_id = claims.get("sub")
    tenant_id = claims.get("tenant_id")
    role = claims.get("role")
    if not user_id or not tenant_id or role not in ROLE_ORDER:
        raise HTTPException(401, "Invalid PRISM session")
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User, Tenant)
            .join(Tenant, Tenant.id == User.tenant_id)
            .where(
                User.id == user_id,
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
                Tenant.is_active.is_(True),
            )
        )
        row = result.first()
    if not row:
        raise HTTPException(401, "Account is no longer active")
    user, tenant = row
    if user.role != role:
        raise HTTPException(401, "Session permissions changed. Please sign in again.")
    return Principal(user.id, user.email, user.tenant_id, user.role, tenant.name)


def enforce_route_permissions(request: Request, principal: Principal) -> None:
    path = request.url.path.rstrip("/")
    method = request.method.upper()
    if method in {"POST", "PUT", "PATCH", "DELETE"} and not principal.can("engineer"):
        raise HTTPException(403, "Engineer permission required for this action")
    if (
        any(path.startswith(p) for p in ("/patterns/", "/kg/", "/agents/", "/predictions/"))
        and method != "GET"
        and not principal.can("admin")
    ):
        raise HTTPException(403, "Admin permission required for this action")
    if path.endswith("/approve") and not principal.can("admin"):
        raise HTTPException(403, "Admin permission required to approve patterns")
    scopes = getattr(principal, "scopes", None)
    if scopes is not None and "*" not in scopes:
        # Machine credentials carry an explicit capability list. Ignoring it
        # (as this previously did) meant a `viewer` service account passed the
        # same checks as a full one for every GET.
        needed = _required_scope(path, method)
        if needed and needed not in scopes:
            raise HTTPException(403, f"Service account lacks the '{needed}' scope")


def _required_scope(path: str, method: str) -> str:
    if path.startswith("/kg/"):
        return "kg.write" if method != "GET" else "kg.read"
    if path.startswith("/patterns/"):
        return "pattern.read"
    if path.startswith("/incidents/"):
        return "incident.read" if method == "GET" else "incident.investigate"
    if path.startswith("/predictions/") or path.startswith("/agents/"):
        return "incident.read"
    return ""


async def bootstrap_owner() -> None:
    email = os.getenv("PRISM_BOOTSTRAP_EMAIL", "").strip().lower()
    password = os.getenv("PRISM_BOOTSTRAP_PASSWORD", "")
    if not email or not password:
        return
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.role == "owner").limit(1))
        if result.scalar_one_or_none() is not None:
            return
        result = await session.execute(select(User).limit(1))
        if result.scalar_one_or_none() is not None:
            # A workspace exists but has no owner. Refuse to silently leave it
            # owner-less: an operator must either grant an owner or reset state.
            raise SystemExit(
                "PRISM_BOOTSTRAP_EMAIL/PASSWORD are still set but an owner already exists. "
                "Clear the bootstrap variables after first run."
            )
        tenant = Tenant(name=os.getenv("PRISM_BOOTSTRAP_TENANT_NAME", "My PRISM Workspace"))
        session.add(tenant)
        await session.flush()
        session.add(
            User(
                email=email,
                password_hash=hash_password(password),
                tenant_id=tenant.id,
                role="owner",
                is_active=True,
            )
        )
        await session.commit()
