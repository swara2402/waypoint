"""Human login/session and workspace membership endpoints."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from api.deps import require_tenant
from auth.security import (
    COOKIE_NAME,
    SESSION_HOURS,
    authenticate_login,
    create_access_token,
    decode_access_token,
    hash_password,
    principal_from_request,
    revoke_jti,
    Principal,
)
from config.settings import settings
from database.auth_models import ServiceAccount, Tenant, User
from database.session import AsyncSessionLocal
from utils.rate_limit import build_rate_limiter

router = APIRouter(prefix="/auth", tags=["auth"])
_login_limiter = build_rate_limiter("5/minute", settings.redis_url)
_account_limiter = build_rate_limiter("10/15minute", settings.redis_url)

# Roles are modelled in the type system so ``/openapi.json`` advertises the
# valid values and an invalid role yields a 422 field error rather than a
# hand-written 400. ``WorkspaceRole`` documents that only an owner may grant
# ``owner``.
WorkspaceRole = Literal["viewer", "engineer", "admin"]
OwnerRole = Literal["viewer", "engineer", "admin", "owner"]

# Capability strings for machine credentials. These are enforced (see
# ``enforce_route_permissions``); the column is not decorative.
SCOPES_BY_ROLE: dict[str, list[str]] = {
    "viewer": ["incident.read", "pattern.read", "kg.read"],
    "engineer": ["incident.read", "incident.investigate", "pattern.read", "kg.read", "kg.write"],
    "admin": ["*"],
    "owner": ["*"],
}


class RegistrationRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=256)
    workspace_name: str = Field(min_length=2, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)
    # Optional: disambiguates the same address existing in several workspaces.
    workspace: Optional[str] = Field(default=None, max_length=128)


class MemberCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: WorkspaceRole = "viewer"
    password: str = Field(min_length=12, max_length=256)


class RoleUpdate(BaseModel):
    role: OwnerRole


class ServiceAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    role: WorkspaceRole = "engineer"
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=3650)


class SessionUser(BaseModel):
    id: str
    email: str
    role: str


class SessionTenant(BaseModel):
    id: str
    name: str


class SessionPermissions(BaseModel):
    can_investigate: bool
    can_admin: bool
    can_manage_workspace: bool


class SessionOut(BaseModel):
    """One shape for both ``/auth/login`` and ``/auth/me``.

    They previously returned different keys, so a client could not read its own
    tenant id or permissions from the login response.
    """

    user: SessionUser
    tenant: SessionTenant
    permissions: SessionPermissions


def _session_out(p) -> SessionOut:
    return SessionOut(
        user=SessionUser(id=p.user_id, email=p.email, role=p.role),
        tenant=SessionTenant(id=p.tenant_id, name=p.tenant_name),
        permissions=SessionPermissions(
            can_investigate=p.can("engineer"),
            can_admin=p.can("admin"),
            can_manage_workspace=p.can("owner"),
        ),
    )


@router.post("/register", response_model=SessionOut, status_code=201)
async def register(body: RegistrationRequest, request: Request, response: Response) -> SessionOut:
    """Create a new workspace and its owner account."""
    if "@" not in body.email:
        raise HTTPException(422, "Enter a valid email address")
    email = body.email.strip().lower()
    workspace_name = " ".join(body.workspace_name.strip().split())
    ip = request.client.host if request.client else "unknown"
    allowed, retry = await _account_limiter.check(f"register:ip:{ip}")
    if not allowed:
        raise HTTPException(
            429,
            "Too many registration attempts. Try again shortly.",
            headers={"Retry-After": str(max(1, int(retry + 0.999)))},
        )

    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(select(User).where(User.email == email))
        ).scalars().first()
        if existing is not None:
            raise HTTPException(409, "An account with this email already exists")

        tenant = Tenant(name=workspace_name)
        session.add(tenant)
        await session.flush()
        user = User(
            email=email,
            password_hash=hash_password(body.password),
            tenant_id=tenant.id,
            role="owner",
            is_active=True,
        )
        session.add(user)
        try:
            await session.commit()
            await session.refresh(user)
            await session.refresh(tenant)
        except IntegrityError:
            await session.rollback()
            raise HTTPException(409, "An account with this email already exists")

    token = create_access_token(user, tenant)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
        max_age=SESSION_HOURS * 3600,
        path="/",
    )
    return _session_out(
        Principal(user.id, user.email, user.tenant_id, user.role, tenant.name)
    )


@router.post("/login", response_model=SessionOut)
async def login(body: LoginRequest, request: Request, response: Response) -> SessionOut:
    if "@" not in body.email:
        raise HTTPException(422, "Enter a valid email address")
    ip = request.client.host if request.client else "unknown"
    # Per-IP *and* per-account budgets: an IP-only limiter is trivial to
    # exhaust collectively and offers no defence against a distributed
    # credential-stuffing run against one account.
    allowed, retry = await _login_limiter.check(f"login:ip:{ip}")
    if not allowed:
        raise HTTPException(
            429,
            "Too many sign-in attempts. Try again shortly.",
            headers={"Retry-After": str(max(1, int(retry + 0.999)))},
        )
    account_key = f"login:acct:{body.email.strip().lower()}"
    allowed, retry = await _account_limiter.check(account_key)
    if not allowed:
        raise HTTPException(
            429,
            "Too many sign-in attempts for this account. Try again shortly.",
            headers={"Retry-After": str(max(1, int(retry + 0.999)))},
        )

    principal = await authenticate_login(body.email, body.password, body.workspace)
    if not principal:
        raise HTTPException(401, "Email or password is incorrect")

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(User, Tenant)
                .join(Tenant, Tenant.id == User.tenant_id)
                .where(User.id == principal.user_id)
            )
        ).first()
    if row is None:
        # The account can be deactivated between the credential check and this
        # re-read. Unpacking None here was a TypeError -> 500 with a traceback.
        raise HTTPException(401, "Email or password is incorrect")
    user, tenant = row

    token = create_access_token(user, tenant)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
        # Honours PRISM_SESSION_HOURS instead of a hardcoded 8 hours.
        max_age=SESSION_HOURS * 3600,
        path="/",
    )
    return _session_out(principal)


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    """Revoke the presented session, then clear the cookie.

    Deleting the cookie alone left the JWT valid for its remaining lifetime.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if token:
        try:
            claims = decode_access_token(token)
            exp = claims.get("exp")
            if exp:
                await revoke_jti(
                    claims.get("jti", ""), ttl_seconds=float(exp) - datetime.now(timezone.utc).timestamp()
                )
        except Exception:  # noqa: BLE001 - a malformed token is already unusable
            pass
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"logged_out": True}


@router.get("/me", response_model=SessionOut)
async def me(request: Request) -> SessionOut:
    p = await principal_from_request(request)
    return _session_out(p)


@router.get("/members", response_model=list[dict])
async def members(request: Request) -> list[dict]:
    p = await principal_from_request(request)
    if not p.can("admin"):
        raise HTTPException(403, "Admin permission required")
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(User)
                .where(User.tenant_id == p.tenant_id)
                .order_by(User.email)
                .limit(settings.max_pagination_limit)
            )
        ).scalars().all()
    return [{"id": u.id, "email": u.email, "role": u.role, "active": u.is_active} for u in rows]


@router.post("/members", response_model=dict, status_code=201)
async def add_member(body: MemberCreate, request: Request) -> dict:
    p = await principal_from_request(request)
    # Minting an admin must require the same authority as changing a role to
    # admin, otherwise an admin escalates by creating rather than editing.
    required = "owner" if body.role == "admin" else "admin"
    if not p.can(required):
        raise HTTPException(403, f"{required.title()} permission required to add a {body.role}")

    email = body.email.strip().lower()
    try:
        async with AsyncSessionLocal() as session:
            # Scoped to this workspace: an unscoped probe would confirm that an
            # address is registered in *some* tenant.
            exists = (
                await session.execute(
                    select(User).where(User.email == email, User.tenant_id == p.tenant_id)
                )
            ).scalar_one_or_none()
            if exists:
                raise HTTPException(409, "A user with that email already exists in this workspace")
            user = User(
                email=email,
                password_hash=hash_password(body.password),
                tenant_id=p.tenant_id,
                role=body.role,
                is_active=True,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
    except IntegrityError:
        # Two concurrent invites raced the check; the unique constraint is the
        # authority. Surface the intended 409 rather than a 500.
        raise HTTPException(409, "A user with that email already exists in this workspace")
    return {"id": user.id, "email": user.email, "role": user.role}


@router.patch("/members/{user_id}/role", response_model=dict)
async def change_role(user_id: str, body: RoleUpdate, request: Request) -> dict:
    p = await principal_from_request(request)
    if not p.can("owner"):
        raise HTTPException(403, "Workspace owner permission required")
    async with AsyncSessionLocal() as session:
        user = (
            await session.execute(
                select(User).where(User.id == user_id, User.tenant_id == p.tenant_id)
            )
        ).scalar_one_or_none()
        if not user:
            raise HTTPException(404, "Member not found")
        if user.id == p.user_id and body.role != "owner":
            raise HTTPException(400, "Owner cannot remove their own owner role")
        user.role = body.role
        await session.commit()
    return {"id": user.id, "email": user.email, "role": user.role}


@router.post("/service-accounts", response_model=dict, status_code=201)
async def create_service_account(
    body: ServiceAccountCreate, request: Request, tenant_id: str = Depends(require_tenant)
) -> dict:
    """Create a tenant-bound machine credential. The token is returned once."""
    p = await principal_from_request(request)
    required = "owner" if body.role == "admin" else "admin"
    if not p.can(required):
        raise HTTPException(403, f"{required.title()} permission required to create a {body.role} credential")

    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(ServiceAccount).where(ServiceAccount.tenant_id == tenant_id)
            )
        ).scalars().all()
        if len(existing) >= settings.max_service_accounts_per_tenant:
            raise HTTPException(409, "Service account limit reached for this workspace")
        token = "prism_sa_" + secrets.token_urlsafe(32)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
            if body.expires_in_days
            else None
        )
        account = ServiceAccount(
            tenant_id=tenant_id,
            name=body.name.strip(),
            token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            role=body.role,
            scopes=list(SCOPES_BY_ROLE[body.role]),
            expires_at=expires_at,
            is_active=True,
        )
        session.add(account)
        await session.commit()
        await session.refresh(account)
    return {
        "id": account.id,
        "name": account.name,
        "tenant_id": account.tenant_id,
        "role": account.role,
        "scopes": account.scopes,
        "expires_at": account.expires_at,
        "token": token,
        "warning": "Store this token securely. WayPoint will not show it again.",
    }


@router.post("/service-accounts/{account_id}/revoke", response_model=dict)
async def revoke_service_account(
    account_id: str, request: Request, tenant_id: str = Depends(require_tenant)
) -> dict:
    """Deactivate a service account. Previously a leaked token stayed valid
    until it expired because there was no revocation path at all."""
    p = await principal_from_request(request)
    if not p.can("admin"):
        raise HTTPException(403, "Admin permission required")
    async with AsyncSessionLocal() as session:
        account = (
            await session.execute(
                select(ServiceAccount).where(
                    ServiceAccount.id == account_id,
                    ServiceAccount.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if account is None:
            raise HTTPException(404, "Service account not found")
        account.is_active = False
        await session.commit()
    return {"revoked": True, "id": account_id}
