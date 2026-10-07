"""Tenant-scoped customer connector and onboarding APIs."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from api.deps import require_api_key, require_tenant
from config.settings import settings
from database.models import Incident, RootCause, Resolution, LessonLearned
from database.repositories import create_incident, get_incident_by_idempotency_key
from database.service_models import CustomerConnector
from database.session import AsyncSessionLocal
from services.canonical import ALIASES, infer_mapping, normalize_evidence
from services.connectors import (
    ALLOWED_KINDS,
    ALLOWED_SOURCE_TYPES,
    connector_summary,
    decrypt_secret,
    encrypt_secret,
    fetch_connector,
    normalize_connector_record,
    records_from_payload,
    stable_idempotency_key,
    validate_endpoint,
)

router = APIRouter(prefix="/service", tags=["customer onboarding"])


class ConnectorIn(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    kind: str = "http_json"
    source_type: str = "incidents"
    endpoint_url: str | None = Field(default=None, max_length=2048)
    http_method: str = Field(default="GET", max_length=8)
    auth_token: str | None = Field(default=None, max_length=4096)
    headers: dict[str, str] = Field(default_factory=dict)
    payload_path: str | None = Field(default=None, max_length=512)
    mapping: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    schedule_seconds: int | None = Field(default=None, ge=60, le=86400)


class ConnectorOut(BaseModel):
    id: str
    name: str
    kind: str
    source_type: str
    endpoint_url: str | None
    http_method: str
    payload_path: str | None
    mapping: dict[str, str]
    enabled: bool
    schedule_seconds: int | None
    last_sync_at: datetime | None
    last_status: str | None
    last_error: str | None
    configured: bool
    webhook_path: str | None


class ConnectorTestOut(BaseModel):
    ok: bool
    status_code: int | None = None
    records: int = 0
    sample_mapping: dict[str, str] = Field(default_factory=dict)
    error: str | None = None


class SyncOut(BaseModel):
    connector_id: str
    status: str
    received: int
    created: int
    duplicates: int
    failed: int
    errors: list[str] = Field(default_factory=list)


class MappingProposalIn(BaseModel):
    sample: dict[str, Any] = Field(..., min_length=1)


class MappingProposalOut(BaseModel):
    mapping: dict[str, str]
    source: str


class WebhookIn(BaseModel):
    payload: dict[str, Any] = Field(..., min_length=1)


def _require_admin(request: Request) -> None:
    role = getattr(getattr(request.state, "principal", None), "role", "")
    if role not in {"admin", "owner"}:
        raise HTTPException(403, "Workspace administration permission required")


def _validate_connector(body: ConnectorIn) -> None:
    if body.kind not in ALLOWED_KINDS:
        raise HTTPException(422, "Unsupported connector kind")
    if body.source_type not in ALLOWED_SOURCE_TYPES:
        raise HTTPException(422, "Unsupported connector source type")
    method = body.http_method.upper()
    if method not in {"GET", "POST"}:
        raise HTTPException(422, "Connector HTTP method must be GET or POST")
    if body.kind == "http_json":
        if not body.endpoint_url:
            raise HTTPException(422, "Endpoint URL is required for HTTP connectors")
        try:
            validate_endpoint(body.endpoint_url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    elif body.endpoint_url:
        raise HTTPException(422, "Webhook connectors do not need an endpoint URL")


@router.get("/connectors", response_model=list[ConnectorOut])
async def list_connectors(
    _auth: str = Depends(require_api_key), tenant: str = Depends(require_tenant)
) -> list[ConnectorOut]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(CustomerConnector)
                .where(CustomerConnector.tenant_id == tenant)
                .order_by(CustomerConnector.created_at.desc())
            )
        ).scalars().all()
    return [ConnectorOut(**connector_summary(row)) for row in rows]


@router.post("/connectors", response_model=ConnectorOut, status_code=201)
async def create_connector(
    body: ConnectorIn,
    request: Request,
    _auth: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> ConnectorOut:
    _require_admin(request)
    _validate_connector(body)
    async with AsyncSessionLocal() as session:
        connector = CustomerConnector(
            tenant_id=tenant,
            name=" ".join(body.name.strip().split()),
            kind=body.kind,
            source_type=body.source_type,
            endpoint_url=body.endpoint_url.strip() if body.endpoint_url else None,
            http_method=body.http_method.upper(),
            auth_token_encrypted=encrypt_secret(body.auth_token.strip()) if body.auth_token and body.auth_token.strip() else None,
            headers_encrypted=encrypt_secret(__import__("json").dumps(body.headers)) if body.headers else None,
            payload_path=body.payload_path.strip() if body.payload_path else None,
            mapping=body.mapping,
            enabled=body.enabled,
            schedule_seconds=body.schedule_seconds,
        )
        session.add(connector)
        await session.commit()
        await session.refresh(connector)
    return ConnectorOut(**connector_summary(connector))


@router.delete("/connectors/{connector_id}", status_code=204)
async def delete_connector(
    connector_id: str,
    request: Request,
    _auth: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> None:
    _require_admin(request)
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(CustomerConnector).where(
                    CustomerConnector.id == connector_id,
                    CustomerConnector.tenant_id == tenant,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "Connector not found")
        await session.delete(row)
        await session.commit()


@router.post("/connectors/{connector_id}/test", response_model=ConnectorTestOut)
async def test_connector(
    connector_id: str,
    request: Request,
    _auth: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> ConnectorTestOut:
    _require_admin(request)
    connector = await _load_connector(connector_id, tenant)
    if connector.kind == "webhook":
        return ConnectorTestOut(ok=True, records=0, sample_mapping=connector.mapping or {})
    try:
        status_code, payload = await fetch_connector(connector)
        records = records_from_payload(payload)
        sample = records[0] if records else {}
        _, mapping = normalize_connector_record(sample, source_type=connector.source_type, mapping=connector.mapping)
        return ConnectorTestOut(ok=True, status_code=status_code, records=len(records), sample_mapping=mapping)
    except Exception as exc:
        return ConnectorTestOut(ok=False, error=str(exc)[:500])


@router.post("/connectors/{connector_id}/sync", response_model=SyncOut)
async def sync_connector(
    connector_id: str,
    request: Request,
    _auth: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> SyncOut:
    _require_admin(request)
    connector = await _load_connector(connector_id, tenant)
    if connector.kind == "webhook":
        raise HTTPException(422, "Webhook connectors are push-only")
    try:
        _, payload = await fetch_connector(connector)
    except Exception as exc:
        await _mark_connector(connector_id, tenant, "error", str(exc)[:1000])
        raise HTTPException(502, f"Connector fetch failed: {str(exc)[:300]}") from exc

    records = records_from_payload(payload)
    created = duplicates = failed = 0
    errors: list[str] = []

    async with AsyncSessionLocal() as session:
        for record in records[:100]:
            try:
                normalized, _ = normalize_connector_record(
                    record, source_type=connector.source_type, mapping=connector.mapping
                )
                idem = stable_idempotency_key(connector.id, normalized)
                existing = await get_incident_by_idempotency_key(session, idem, tenant_id=tenant)
                if existing is not None:
                    duplicates += 1
                    continue
                inc = await create_incident(
                    session,
                    tenant_id=tenant,
                    title=normalized["title"],
                    description=normalized.get("description"),
                    severity=normalized["severity"],
                    status="open",
                    incident_type=normalized.get("incident_type"),
                    affected_services=normalized.get("affected_services") or [],
                    raw_logs=normalized.get("raw_logs") or [],
                    metrics=normalized.get("metrics") or {},
                    traces=normalized.get("traces") or [],
                    topology=normalized.get("topology") or {},
                    context=normalized.get("context") or {},
                    started_at=normalized.get("started_at"),
                    idempotency_key=idem,
                )
                await session.flush()
                created += 1
            except Exception as exc:
                failed += 1
                if len(errors) < 10:
                    errors.append(str(exc)[:240])
        await session.commit()

    await _mark_connector(
        connector_id,
        tenant,
        "ok" if failed == 0 else "partial",
        None if failed == 0 else "; ".join(errors[:3]),
    )
    return SyncOut(
        connector_id=connector_id,
        status="ok" if failed == 0 else "partial",
        received=len(records),
        created=created,
        duplicates=duplicates,
        failed=failed,
        errors=errors,
    )


@router.post("/connectors/{connector_id}/webhook", response_model=SyncOut)
async def connector_webhook(
    connector_id: str,
    body: WebhookIn,
    request: Request,
    x_waypoint_secret: str | None = Header(default=None, alias="X-WayPoint-Secret"),
    tenant: str | None = None,
) -> SyncOut:
    connector = await _load_connector_public(connector_id)
    if connector.kind != "webhook":
        raise HTTPException(404, "Webhook connector not found")
    expected = decrypt_secret(connector.auth_token_encrypted)
    if not expected or not x_waypoint_secret or not __import__("hmac").compare_digest(expected, x_waypoint_secret):
        raise HTTPException(401, "Invalid connector secret")

    normalized, _ = normalize_connector_record(
        body.payload, source_type=connector.source_type, mapping=connector.mapping
    )
    idem = stable_idempotency_key(connector.id, normalized)
    async with AsyncSessionLocal() as session:
        existing = await get_incident_by_idempotency_key(session, idem, tenant_id=connector.tenant_id)
        if existing is not None:
            await _mark_connector(connector.id, connector.tenant_id, "ok", None)
            return SyncOut(connector_id=connector.id, status="ok", received=1, created=0, duplicates=1, failed=0)
        try:
            await create_incident(
                session,
                tenant_id=connector.tenant_id,
                title=normalized["title"],
                description=normalized.get("description"),
                severity=normalized["severity"],
                status="open",
                incident_type=normalized.get("incident_type"),
                affected_services=normalized.get("affected_services") or [],
                raw_logs=normalized.get("raw_logs") or [],
                metrics=normalized.get("metrics") or {},
                traces=normalized.get("traces") or [],
                topology=normalized.get("topology") or {},
                context=normalized.get("context") or {},
                started_at=normalized.get("started_at"),
                idempotency_key=idem,
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return SyncOut(connector_id=connector.id, status="ok", received=1, created=0, duplicates=1, failed=0)
    await _mark_connector(connector.id, connector.tenant_id, "ok", None)
    return SyncOut(connector_id=connector.id, status="ok", received=1, created=1, duplicates=0, failed=0)


@router.post("/schema-mapping/propose", response_model=MappingProposalOut)
async def propose_schema_mapping(
    body: MappingProposalIn,
    request: Request,
    _auth: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> MappingProposalOut:
    _require_admin(request)
    deterministic = infer_mapping(body.sample)
    try:
        from api.service import _get_config, _decrypt
        from utils.llm import LLMClient, generate_structured
        cfg = await _get_config(tenant)
        client = LLMClient(
            host=cfg.llm_base_url,
            model=cfg.llm_model,
            provider=cfg.llm_provider,
            api_key=_decrypt(cfg.llm_api_key_encrypted),
            timeout=15.0,
        )
        result = await generate_structured(
            "Propose a mapping from WayPoint canonical incident fields to the exact source keys in this sample. "
            "Only use keys that exist in the sample. Return JSON: {mapping:{canonical_field:source_key}}.",
            system="You are a data-integration mapper. Never invent source keys. Mapping is advisory and will be validated.",
            schema={"mapping": {"type": dict, "required": True, "default": {}}},
            evidence=__import__("json").dumps(body.sample, ensure_ascii=False),
            fallback={"mapping": deterministic},
            client=client,
        )
        candidate = result.get("mapping", {}) if result else {}
        if not isinstance(candidate, dict):
            candidate = {}
        allowed = set(body.sample.keys())
        mapping = {
            str(k): str(v)
            for k, v in candidate.items()
            if str(k) in ALIASES and str(v) in allowed
        }
        mapping = {**deterministic, **mapping}
        return MappingProposalOut(mapping=mapping, source="llm+deterministic")
    except Exception:
        return MappingProposalOut(mapping=deterministic, source="deterministic-fallback")


@router.get("/onboarding")
async def onboarding_status(
    _auth: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    from api.service import _get_config
    from sqlalchemy import func

    cfg = await _get_config(tenant)
    async with AsyncSessionLocal() as session:
        connectors = (
            await session.execute(
                select(CustomerConnector)
                .where(CustomerConnector.tenant_id == tenant)
            )
        ).scalars().all()
        incident_count = await session.scalar(
            select(func.count(Incident.id)).where(Incident.tenant_id == tenant)
        )
        analyzed_count = await session.scalar(
            select(func.count(Incident.id)).where(
                Incident.tenant_id == tenant,
                Incident.status.in_(["analyzed", "resolved"]),
            )
        )
        explained_count = await session.scalar(
            select(func.count(RootCause.id)).where(RootCause.tenant_id == tenant)
        )
        resolution_rows = (
            await session.execute(
                select(Resolution).where(Resolution.tenant_id == tenant)
            )
        ).scalars().all()
        learned_count = await session.scalar(
            select(func.count(LessonLearned.id)).where(LessonLearned.tenant_id == tenant)
        )

    mapping_ready = bool(cfg.schema_mapping)
    mapping_confirmed = bool(cfg.schema_mapping_confirmed_at)
    connector_ready = any(c.enabled for c in connectors)
    confirmed_count = sum(
        1
        for resolution in resolution_rows
        if isinstance(resolution.metadata_, dict)
        and str(resolution.metadata_.get("confirmed_root_cause") or "").strip()
    )
    return {
        "stages": [
            {"id": "connect", "label": "Connect", "complete": connector_ready},
            {"id": "understand", "label": "Understand schema", "complete": mapping_ready},
            {"id": "confirm", "label": "Confirm mapping", "complete": mapping_confirmed},
            {"id": "ingest", "label": "Ingest", "complete": bool(incident_count)},
            {"id": "investigate", "label": "Investigate", "complete": bool(analyzed_count)},
            {"id": "explain", "label": "Explain", "complete": bool(explained_count)},
            {"id": "confirm_outcome", "label": "Confirm outcome", "complete": bool(confirmed_count)},
            {"id": "learn", "label": "Learn", "complete": bool(learned_count)},
        ],
        "connectors": [connector_summary(c) for c in connectors],
        "mapping": cfg.schema_mapping or {},
    }


async def _load_connector(connector_id: str, tenant: str) -> CustomerConnector:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(CustomerConnector).where(
                    CustomerConnector.id == connector_id,
                    CustomerConnector.tenant_id == tenant,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Connector not found")
    return row


async def _load_connector_public(connector_id: str) -> CustomerConnector:
    async with AsyncSessionLocal() as session:
        row = await session.get(CustomerConnector, connector_id)
    if row is None or not row.enabled:
        raise HTTPException(404, "Connector not found")
    return row


async def _mark_connector(connector_id: str, tenant: str, status: str, error: str | None) -> None:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(CustomerConnector).where(
                    CustomerConnector.id == connector_id,
                    CustomerConnector.tenant_id == tenant,
                )
            )
        ).scalar_one_or_none()
        if row:
            row.last_status = status
            row.last_error = error
            row.last_sync_at = datetime.now(timezone.utc)
            await session.commit()
