"""
api.investigation
=================

The master pipeline endpoint.

POST /investigate
    Accepts an :class:`IncidentCreate` and runs the complete
    pipeline:

        orchestrator -> causal graph -> confidence propagation
        -> consensus -> explainability -> meta-reasoning

    Continuous learning is deliberately NOT run here: PRISM's consensus
    is an unverified hypothesis.  Learning is deferred to
    ``POST /incidents/{id}/resolve``, which requires a confirmed
    (ground-truth) root cause before updating patterns, memory,
    knowledge-graph edges, or agent reliability.

GET /incidents/{id}
    Returns the persisted incident + root cause + resolution + lessons.

POST /incidents/{id}/resolve
    Records an engineer-supplied resolution and — when a confirmed root
    cause is provided — triggers continuous learning against that
    ground truth.  This is the authoritative learning trigger.

GET /incidents
    Lists recent incidents.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
import json

from api.deps import require_api_key, require_tenant
from causal_graph.engine import CausalGraphBuilder
from confidence.engine import propagate
from consensus.engine import reach_consensus
from config.logging import get_logger
from config.settings import settings
from database.repositories import (
    create_incident,
    get_incident,
    get_incident_by_idempotency_key,
    get_resolution,
    get_root_cause,
    list_findings,
    list_incidents,
    save_resolution,
    upsert_root_cause,
    update_incident,
)
from explainability.engine import build_explanation
from investigation.tree import run_tree
from learning.continuous_learning import LearningInput, learn_from_incident
from meta_reasoning.engine import evaluate as meta_evaluate, persist_result as persist_meta_reasoning
from models.schemas import (
    AgentStatus,
    IncidentCreate,
    IncidentOut,
    InvestigationResult,
    JobOut,
    ResolutionCreate,
    ResolutionOut,
    RootCauseOut,
    ExplanationOut,
    MetaReasoningOut,
    AlternativeHypothesis,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from utils.rate_limit import build_rate_limiter, RedisConcurrencyLimiter

logger = get_logger(__name__)


def apply_graph_traversal_fallback(
    consensus: Any,
    candidates: List[Dict[str, Any]],
    enable_fallback: bool = False,
) -> Any:
    """Fall back to the highest-confidence graph candidate when consensus is
    undetermined.

    Only reachable when ``enable_fallback`` is true. Both call sites previously
    passed ``False``, making the body -- and the ``None`` concatenation in it --
    permanently dead code.
    """
    if enable_fallback and getattr(consensus, "root_cause", None) == "undetermined" and candidates:
        label = candidates[0].get("label")
        logger.warning("graph_traversal_fallback_applied", extra={"label": label})
        return consensus.__class__(
            root_cause=label or "undetermined",
            confidence=float(candidates[0].get("confidence") or 0.3),
            alternatives=consensus.alternatives,
            explanation=f"{consensus.explanation or ''} (fallback to graph traversal)",
            voter_breakdown=consensus.voter_breakdown,
        )
    return consensus

router = APIRouter(prefix="/incidents", tags=["incidents"])


@router.get("/stats")
async def incident_stats(
    _api_key: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> Dict[str, int]:
    """Return tenant-scoped incident totals for service dashboards."""
    from database.session import AsyncSessionLocal
    from database import models as dbm

    async with AsyncSessionLocal() as session:
        total = await session.scalar(select(func.count()).select_from(dbm.Incident).where(dbm.Incident.tenant_id == tenant))
        open_count = await session.scalar(select(func.count()).select_from(dbm.Incident).where(
            dbm.Incident.tenant_id == tenant, dbm.Incident.status.notin_(["resolved", "closed"])))
        critical = await session.scalar(select(func.count()).select_from(dbm.Incident).where(
            dbm.Incident.tenant_id == tenant, dbm.Incident.severity.in_(["P0", "P1", "critical", "CRITICAL"])))
        resolved = await session.scalar(select(func.count()).select_from(dbm.Incident).where(
            dbm.Incident.tenant_id == tenant, dbm.Incident.status.in_(["resolved", "closed"])))
    return {"total": int(total or 0), "open": int(open_count or 0), "critical": int(critical or 0), "resolved": int(resolved or 0)}

# Concurrency limiter for expensive investigations
_investigation_semaphore = asyncio.Semaphore(settings.max_concurrent_investigations)
_distributed_investigation_limiter = (
    RedisConcurrencyLimiter(
        settings.max_concurrent_investigations,
        settings.redis_url,
        lease_seconds=max(1800, int(settings.agent_timeout_seconds * settings.MAX_INVESTIGATION_STEPS + 60)),
    )
    if settings.redis_url else None
)
_ACQUIRE_TIMEOUT = 5.0

# Per-caller request rate limiter (settings.investigation_rate_limit).
# Redis-backed when settings.redis_url is set (shared across replicas).
_rate_limiter = build_rate_limiter(
    settings.investigation_rate_limit, settings.redis_url
)

# Reproducibility metadata for this WayPoint build.
_PRISM_VERSION = "2.0.0"

# Every finding type that means "this agent did not produce a usable signal".
# Previously only ``error`` was treated as a failure, so a timed-out agent
# (agents.base.BaseAgent sets ``timeout``) or a self-degraded one
# (``historical_analyzer`` sets ``degraded``) was reported ``ok`` and the run
# was advertised as ``completed``.
_DEGRADED_FINDING_TYPES = frozenset({"error", "timeout", "degraded", "empty"})


def _finding_field(finding: Any, field: str, default: Any = None) -> Any:
    """Read one field from a Finding dataclass or an equivalent dict.

    The duplicated ``isinstance(f, dict) else getattr(...)`` dance previously
    appeared five times with three different default values.
    """
    if isinstance(finding, dict):
        value = finding.get(field, default)
    else:
        value = getattr(finding, field, default)
    return default if value is None else value


def _redact_evidence(incident_in: IncidentCreate):
    """Scrub every user-supplied field. Runs in a worker thread.

    ``scrub`` is regex-bound CPU work and was previously called inline inside
    ``async def``; the email pattern backtracks super-linearly, so a single
    legal request could stall the event loop for seconds.
    """
    from utils.redaction import scrub, scrub_collection, scrub_iterable

    return (
        scrub_iterable(incident_in.raw_logs),
        scrub_collection(incident_in.metrics),
        scrub_collection(incident_in.traces),
        scrub_collection(incident_in.topology),
        scrub_collection(incident_in.context),
        scrub(incident_in.description or "") or None,
    )


def _build_graph_stage(
    findings: List[Any],
    affected_services: List[str],
    safe_logs: List[str],
    safe_metrics: Dict[str, Any],
    safe_traces: List[Dict[str, Any]],
):
    """Causal graph + confidence propagation + candidate ranking.

    Pure CPU work, executed via ``asyncio.to_thread`` so it cannot block the
    event loop shared by every other request in the process.
    """
    builder = CausalGraphBuilder()
    causal_graph = builder.build_from_findings(
        findings=findings,
        affected_services=affected_services,
        logs=safe_logs,
        metrics=safe_metrics,
        traces=safe_traces,
    )
    propagation = propagate(causal_graph, iterations=settings.propagation_iterations)
    candidates = causal_graph.find_root_causes(top_k=5)
    return causal_graph, propagation, candidates


async def _persist_incident(
    session: Any,
    incident_in: IncidentCreate,
    safe_description: Optional[str],
    safe_logs: List[str],
    safe_metrics: Dict[str, Any],
    safe_traces: List[Dict[str, Any]],
    safe_topology: Dict[str, Any],
    safe_context: Dict[str, Any],
    tenant_id: str,
    idempotency_key: Optional[str],
) -> tuple[str, bool]:
    """Create an incident or reuse a matching row without breaking idempotency."""
    from database.repositories import get_similar_incident

    existing = await get_similar_incident(
        session,
        title=incident_in.title,
        affected_services=incident_in.affected_services,
        started_at=incident_in.started_at,
        tenant_id=tenant_id,
    )
    if existing is not None:
        # Never replace another request's idempotency key. If this request is
        # the first keyed request for an otherwise-existing incident, it may
        # claim the empty slot.
        if idempotency_key and not getattr(existing, "idempotency_key", None):
            await update_incident(
                session, existing.id, tenant_id=tenant_id, idempotency_key=idempotency_key
            )
        return existing.id, True

    inc = await create_incident(
        session,
        title=incident_in.title,
        description=safe_description or None,
        severity=incident_in.severity,
        status="investigating",
        incident_type=incident_in.incident_type,
        affected_services=incident_in.affected_services,
        raw_logs=safe_logs,
        metrics=safe_metrics,
        traces=safe_traces,
        topology=safe_topology,
        context=safe_context,
        started_at=incident_in.started_at,
        idempotency_key=idempotency_key,
        tenant_id=tenant_id,
    )
    return inc.id, False


def _build_agent_statuses(findings: List[Any]) -> List[Dict[str, Any]]:
    """Reduce findings into per-agent execution status.

    ``error``, ``timeout``, ``degraded`` and ``empty`` all mark an agent that
    did not deliver a usable signal; anything else is a successful invocation.
    """
    statuses: List[Dict[str, Any]] = []
    for f in findings:
        name = _finding_field(f, "agent_name", "unknown")
        ftype = _finding_field(f, "finding_type", "analysis")
        latency = _finding_field(f, "latency_s", 0.0)
        statuses.append({
            "agent_name": name,
            "status": "failed" if ftype in _DEGRADED_FINDING_TYPES else "ok",
            "execution_ms": round(float(latency or 0.0) * 1000, 2),
            "finding_type": ftype,
        })
    return statuses


def _runtime_metadata(role: str = "engineer") -> Dict[str, Any]:
    """Reproducibility metadata.

    Internal infrastructure topology (LLM host, database host/port) is
    administrative detail. It is withheld from non-admin callers instead of
    being handed to every authenticated role as free reconnaissance.
    """
    runtime: Dict[str, Any] = {
        "prism_version": _PRISM_VERSION,
        "llm_model": settings.ollama_model,
        "embedding_model": settings.sentence_transformer_model,
        "learning_mode": settings.learning_mode,
        "learning_require_confirmation": settings.learning_require_confirmation,
        "mdv_threshold": settings.MDV_THRESHOLD,
        "max_investigation_steps": settings.MAX_INVESTIGATION_STEPS,
        "consensus_confidence_threshold": settings.consensus_confidence_threshold,
    }
    from auth.security import ROLE_ORDER

    if ROLE_ORDER.get(role, 0) >= ROLE_ORDER.get("admin", 999):
        runtime["llm_host"] = settings.ollama_host
        runtime["database_url_masked"] = _mask_database_url(settings.database_url)
    return runtime


def _mask_database_url(url: str) -> str:
    """Mask credentials in a database URL for reproducibility logs.

    Uses ``urlsplit`` rather than manual ``partition``: partitioning a
    scheme-less URL put the whole string in the "scheme" half and returned the
    password in cleartext, which is precisely what this function exists to
    prevent.
    """
    if not url:
        return "[redacted]"
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "[redacted]"
    if not parsed.scheme or not parsed.netloc:
        # Unparseable input must never be echoed verbatim.
        return "[redacted]"
    if parsed.password:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return f"{parsed.scheme}://***@{host}{parsed.path}"
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _rate_limit_key(request: Request, tenant: str, api_key: str) -> str:
    """Identity used for rate limiting: tenant > API-key hash > client IP.

    Single definition: the inline copy used by ``POST /investigate`` had
    already drifted from this helper.
    """
    if tenant:
        return f"tenant:{tenant}"
    if api_key:
        return f"apikey:{hashlib.sha256(api_key.encode()).hexdigest()[:16]}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


async def _acquire_investigation_slot(request: Request) -> str | None:
    """Take a global Redis slot when configured, else a process-local slot."""
    request_id = getattr(request.state, "request_id", None) or "unknown"
    owner = f"{request_id}:{uuid.uuid4().hex}"
    if _distributed_investigation_limiter is not None:
        deadline = time.monotonic() + _ACQUIRE_TIMEOUT
        while time.monotonic() < deadline:
            slot = await _distributed_investigation_limiter.acquire(owner)
            if slot:
                request.state.investigation_slot = (slot, owner)
                return slot
            await asyncio.sleep(0.05)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many concurrent investigations (global limit={settings.max_concurrent_investigations}). Try again shortly.",
        )
    try:
        await asyncio.wait_for(_investigation_semaphore.acquire(), timeout=_ACQUIRE_TIMEOUT)
        return None
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many concurrent investigations (limit={settings.max_concurrent_investigations}). Try again shortly.",
        )



async def _jobs_to_out(jobs: Sequence[Any], *, tenant_id: str) -> List[JobOut]:
    """Build JobOut for many jobs with a single batched incident fetch.

    The previous per-job version opened one DB session per job inside a list
    comprehension -- up to ``max_pagination_limit`` sequential round trips on a
    cheap GET, and the embedded incident was fetched unscoped.
    """
    from database.repositories import get_incidents_by_ids
    from database.session import AsyncSessionLocal

    ids = [j.result_incident_id for j in jobs if j.result_incident_id]
    incidents: Dict[str, Any] = {}
    if ids:
        async with AsyncSessionLocal() as session:
            for inc in await get_incidents_by_ids(session, ids, tenant_id=tenant_id):
                incidents[inc.id] = inc

    def _build(job: Any) -> JobOut:
        inc = incidents.get(job.result_incident_id) if job.result_incident_id else None
        return JobOut(
            id=job.id,
            tenant_id=job.tenant_id,
            status=job.status,
            progress=job.progress,
            attempts=job.attempts,
            attempts_max=job.attempts_max,
            result_incident_id=job.result_incident_id,
            error=job.error,
            created_at=job.created_at,
            updated_at=job.updated_at,
            incident=IncidentOut.model_validate(inc) if inc is not None else None,
        )

    return [_build(j) for j in jobs]


async def _job_to_out(job: Any, *, tenant_id: str) -> JobOut:
    """Build a :class:`JobOut` from a job row, embedding the incident when done."""
    from database.repositories import get_incident
    from database.session import AsyncSessionLocal

    incident_out = None
    if job.result_incident_id:
        async with AsyncSessionLocal() as session:
            inc = await get_incident(session, job.result_incident_id, tenant_id=tenant_id)
            incident_out = IncidentOut.model_validate(inc) if inc is not None else None
    return JobOut(
        id=job.id,
        tenant_id=job.tenant_id,
        status=job.status,
        progress=job.progress,
        attempts=job.attempts,
        attempts_max=job.attempts_max,
        result_incident_id=job.result_incident_id,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        incident=incident_out,
    )



# ---------------------------------------------------------------
# POST /incidents/investigate
# ---------------------------------------------------------------

@router.post("/investigate", response_model=InvestigationResult)
async def investigate(
    incident_in: IncidentCreate,
    request: Request,
    _api_key: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
    x_idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> InvestigationResult:
    """
    Run the full agentic investigation pipeline for a new incident.

    Sending the same ``Idempotency-Key`` header for the same incident returns
    the previously persisted investigation instead of re-running agent work.
    The tenant is taken from the authenticated credential; there is no
    client-supplied tenant parameter.
    """
    request_id = getattr(request.state, "request_id", None) or "unknown"
    role = getattr(getattr(request.state, "principal", None), "role", "engineer")

    idem_key = (x_idempotency_key or "").strip()[:128] or None

    allowed, retry_after = await _rate_limiter.check(_rate_limit_key(request, tenant, _api_key))
    if not allowed:
        logger.warning(
            "investigation_rate_limited",
            extra={"request_id": request_id, "rate_limit": settings.investigation_rate_limit},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Rate limit exceeded ({settings.investigation_rate_limit}). "
                "Try again shortly."
            ),
            headers={"Retry-After": str(max(1, int(retry_after + 0.999)))},
        )

    await _acquire_investigation_slot(request)
    try:
        return await _run_investigation(
            incident_in,
            request_id=request_id,
            idempotency_key=idem_key,
            tenant_id=tenant,
            principal_role=role,
        )
    finally:
        slot_state = getattr(request.state, "investigation_slot", None)
        if slot_state and _distributed_investigation_limiter is not None:
            await _distributed_investigation_limiter.release(*slot_state)
        else:
            _investigation_semaphore.release()


async def _run_investigation(
    incident_in: IncidentCreate,
    request_id: str = "unknown",
    idempotency_key: Optional[str] = None,
    tenant_id: Optional[str] = None,
    principal_role: str = "engineer",
) -> InvestigationResult:
    """
    Internal investigation pipeline (runs under the concurrency semaphore).

    ``tenant_id`` is required: every read and write below is tenant-scoped.
    """
    from database.session import AsyncSessionLocal
    from utils.redaction import scrub, scrub_collection, scrub_iterable

    if not tenant_id:
        raise HTTPException(403, "No tenant is bound to this credential")

    # 1. Redact sensitive data from evidence BEFORE persistence or LLM exposure.
    #    Scrubbing is regex-bound CPU work, so it runs off the event loop; the
    #    email pattern in particular backtracks super-linearly on long inputs.
    safe_logs, safe_metrics, safe_traces, safe_topology, safe_context, safe_description = (
        await asyncio.to_thread(
            _redact_evidence,
            incident_in,
        )
    )

    try:
        async with AsyncSessionLocal() as session:
            # Idempotency: if an investigation was already run under this key, do
            # not run the pipeline again — return the existing result.
            existing = None
            if idempotency_key:
                existing = await get_incident_by_idempotency_key(
                    session, idempotency_key, tenant_id=tenant_id
                )
            if existing is not None:
                logger.info(
                    "investigation_idempotent_hit",
                    extra={
                        "request_id": request_id,
                        "incident_id": existing.id,
                        "idempotency_key": idempotency_key,
                    },
                )
                incident_id = existing.id
            else:
                incident_id, reused = await _persist_incident(
                    session, incident_in, safe_description, safe_logs, safe_metrics,
                    safe_traces, safe_topology, safe_context, tenant_id, idempotency_key,
                )
            await session.commit()
    except IntegrityError:
        # A concurrent request claimed this idempotency key onto another
        # incident row — replay its result instead of failing with a 500.
        logger.info(
            "investigation_idempotency_race_replayed",
            extra={"request_id": request_id, "idempotency_key": idempotency_key},
        )
        if idempotency_key:
            async with AsyncSessionLocal() as _sess:
                existing = await get_incident_by_idempotency_key(
                    _sess, idempotency_key, tenant_id=tenant_id
                )
                if existing is not None:
                    return await _result_for_existing_incident(
                        existing.id, tenant_id=tenant_id, reused=True
                    )
        raise

    if existing is not None:
        return await _result_for_existing_incident(
            incident_id, tenant_id=tenant_id, reused=True
        )

    if reused:
        async with AsyncSessionLocal() as session:
            existing_root = await get_root_cause(session, incident_id, tenant_id=tenant_id)
            existing_incident = await get_incident(session, incident_id, tenant_id=tenant_id)
        if existing_root is not None:
            return await _result_for_existing_incident(
                incident_id, tenant_id=tenant_id, reused=True
            )
        if existing_incident is not None and existing_incident.status == "investigating":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A matching incident is already under investigation",
            )

    # 2. Build investigation context (already redacted). LLM credentials are
    # resolved server-side from tenant workspace configuration and are never
    # persisted in incident context.
    llm_config: Dict[str, Any] = {}
    try:
        from database.service_models import WorkspaceConfig
        from api.service import _decrypt
        async with AsyncSessionLocal() as cfg_session:
            cfg = await cfg_session.get(WorkspaceConfig, tenant_id)
        if cfg is not None:
            llm_config = {
                "provider": cfg.llm_provider,
                "model": cfg.llm_model,
                "base_url": cfg.llm_base_url,
                "api_key": _decrypt(cfg.llm_api_key_encrypted),
                "timeout": settings.llm_request_timeout,
            }
    except Exception:
        logger.exception("tenant_llm_config_load_failed", extra={"tenant_id": tenant_id})

    context: Dict[str, Any] = {
        # Explicit, not ambient. The ContextVar fallback is never populated in
        # the background worker, so relying on it silently disabled the
        # historical agent for every async job.
        "tenant_id": tenant_id,
        "logs": safe_logs,
        "metrics": safe_metrics,
        "traces": safe_traces,
        "topology": safe_topology,
        "affected_services": incident_in.affected_services,
        "incident_type": incident_in.incident_type,
        "_llm_config": llm_config,
    }

    start = time.perf_counter()

    # 3. Run dynamic investigation tree (which delegates to the orchestrator)
    tree_result = await run_tree(
        incident_id=incident_id,
        incident_type=incident_in.incident_type,
        context=context,
    )

    findings = tree_result.findings
    agents_used = tree_result.agents_used

    # 4-6. Causal graph, confidence propagation and root-cause candidates.
    #      Pure CPU work -- off the event loop so it cannot stall the server.
    causal_graph, propagation, candidates = await asyncio.to_thread(
        _build_graph_stage, findings, incident_in.affected_services, safe_logs,
        safe_metrics, safe_traces,
    )

    root_node_id = candidates[0]["node_id"] if candidates else None
    # Keyed by the graph node id, not its display label. The consensus engine
    # looks this up with a natural-language hypothesis, so a label key made the
    # entire graph evidence channel unreachable.
    graph_candidates = {
        key: float(candidate.get("confidence") or 0.0)
        for candidate in candidates
        for key in (str(candidate.get("node_id") or ""), str(candidate.get("label") or "").strip().lower())
        if key
    }

    # 7. Consensus engine
    consensus = await reach_consensus(
        findings=findings,
        reliability_scores=tree_result.reliability_scores,
        graph_candidates=graph_candidates,
    )

    # Graph traversal fallback, driven by configuration rather than hardcoded
    # off at both call sites.
    consensus = apply_graph_traversal_fallback(
        consensus, candidates, enable_fallback=settings.graph_traversal_fallback
    )

    # 8. Persist root cause (upsert: a deduplicated repeat run must not raise
    #    IntegrityError against the existing unique row).
    causal_chain = causal_graph.causal_chain_to(root_node_id) if root_node_id else []
    contributing: List[str] = [
        label
        for c in causal_chain
        if isinstance(label := c.get("label"), str) and label
    ]
    async with AsyncSessionLocal() as session:
        await upsert_root_cause(
            session,
            tenant_id=tenant_id,
            incident_id=incident_id,
            root_cause=consensus.root_cause,
            confidence=consensus.confidence,
            alternatives=[
                {"cause": a.cause, "confidence": a.confidence, "evidence": a.evidence}
                for a in consensus.alternatives
            ],
            explanation=consensus.explanation,
            causal_chain=causal_chain,
            contributing_factors=contributing,
        )
        await session.commit()

    # 9-10. Explainability and meta-reasoning (CPU-bound, off the loop).
    explanation = await asyncio.to_thread(
        build_explanation,
        incident_id=incident_id,
        findings=findings,
        consensus=consensus,
        causal_graph=causal_graph,
        propagation=propagation,
        root_cause_node_id=root_node_id,
    )
    meta = await asyncio.to_thread(
        meta_evaluate,
        incident_id=incident_id,
        findings=findings,
        agents_skipped=tree_result.agents_skipped,
        final_root_cause=consensus.root_cause,
        final_confidence=consensus.confidence,
        agents_used=agents_used,
        persist=False,
    )
    await persist_meta_reasoning(meta, tenant_id=tenant_id)

    # 11. Mark the investigation phase complete. Findings were already
    #     persisted by the orchestrator (with root_cause_hint provenance) so a
    #     later confirmed resolution can grade each agent against ground truth.
    #     "analyzed" (not "investigating") closes the deduplication window;
    #     leaving the row in a non-terminal state made it mergeable forever.
    async with AsyncSessionLocal() as session:
        await update_incident(
            session, incident_id, tenant_id=tenant_id, status="analyzed"
        )
        await session.commit()

    # 12. NO continuous learning here.  Consensus is an unverified hypothesis.
    #     Learning is deferred to POST /incidents/{id}/resolve.

    duration = time.perf_counter() - start

    agent_statuses = _build_agent_statuses(findings)
    failed = [a["agent_name"] for a in agent_statuses if a["status"] == "failed"]

    return InvestigationResult(
        incident_id=incident_id,
        root_cause=RootCauseOut(
            incident_id=incident_id,
            root_cause=consensus.root_cause,
            confidence=consensus.confidence,
            alternatives=[
                AlternativeHypothesis(
                    cause=a.cause,
                    confidence=a.confidence,
                    evidence=a.evidence,
                )
                for a in consensus.alternatives
            ],
            explanation=consensus.explanation,
            causal_chain=causal_chain,
            contributing_factors=contributing,
        ),
        explanation=ExplanationOut(
            incident_id=incident_id,
            evidence_used=explanation.evidence_used,
            confidence_breakdown=explanation.confidence_breakdown,
            graph_reasoning=explanation.graph_reasoning,
            alternative_root_causes=[
                AlternativeHypothesis(
                    cause=a["cause"],
                    confidence=a["confidence"],
                    evidence=a.get("evidence", []),
                )
                for a in explanation.alternative_root_causes
            ],
            final_explanation=explanation.final_explanation,
        ),
        meta_reasoning=MetaReasoningOut(
            incident_id=incident_id,
            useful_agents=meta.useful_agents,
            unnecessary_agents=meta.unnecessary_agents,
            optimal_path=meta.optimal_path,
            suggestions=meta.suggestions,
            agent_scores=meta.agent_scores,
        ),
        agents_used=agents_used,
        duration_seconds=round(duration, 3),
        agent_statuses=[AgentStatus(**a) for a in agent_statuses],
        status="completed_with_degraded_agents" if failed else "completed",
        runtime=_runtime_metadata(principal_role),
    )


async def _mark_investigation_failed(incident_id: Optional[str], tenant_id: str, error: Exception) -> None:
    """Move a persisted investigation to a terminal failed state after pipeline error."""
    if not incident_id:
        return
    try:
        from database.session import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            await update_incident(session, incident_id, tenant_id=tenant_id, status="failed")
            await session.commit()
    except Exception as persist_exc:
        logger.error(
            "investigation_failure_status_persist_failed",
            extra={"incident_id": incident_id, "error_type": type(error).__name__, "persist_error": repr(persist_exc)},
        )


async def _result_for_existing_incident(
    incident_id: str,
    *,
    tenant_id: str,
    reused: bool = False,
    principal_role: str = "engineer",
) -> InvestigationResult:
    """Rebuild an :class:`InvestigationResult` from a previously persisted run.

    Used for idempotent replays so a repeat ``POST /incidents/investigate``
    with the same ``Idempotency-Key`` never re-runs the agent pipeline.
    """
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        rc = await get_root_cause(session, incident_id, tenant_id=tenant_id)
        if rc is None:
            raise HTTPException(409, "Earlier investigation left no root cause to replay")
        findings = await list_findings(session, incident_id, tenant_id=tenant_id)

    agent_statuses = _build_agent_statuses(findings)
    failed = [a["agent_name"] for a in agent_statuses if a["status"] == "failed"]

    return InvestigationResult(
        incident_id=incident_id,
        root_cause=RootCauseOut(
            incident_id=incident_id,
            root_cause=rc.root_cause,
            confidence=rc.confidence,
            alternatives=[
                AlternativeHypothesis(
                    cause=a.get("cause", ""),
                    confidence=a.get("confidence", 0.0),
                    evidence=a.get("evidence", []),
                )
                for a in rc.alternatives
            ],
            explanation=rc.explanation,
            causal_chain=rc.causal_chain,
            contributing_factors=rc.contributing_factors,
        ),
        explanation=ExplanationOut(
            incident_id=incident_id,
            evidence_used=[],
            confidence_breakdown={},
            graph_reasoning=rc.explanation,
            alternative_root_causes=[],
            final_explanation=rc.explanation,
        ),
        meta_reasoning=MetaReasoningOut(
            incident_id=incident_id,
            useful_agents=[a["agent_name"] for a in agent_statuses if a["status"] == "ok"],
            unnecessary_agents=[],
            optimal_path="",
            suggestions=["Replayed from idempotent investigation."] if reused else [],
            agent_scores={},
        ),
        agents_used=[a["agent_name"] for a in agent_statuses],
        duration_seconds=0.0,
        agent_statuses=[AgentStatus(**a) for a in agent_statuses],
        status="completed_with_degraded_agents" if failed else "completed",
        runtime=_runtime_metadata(principal_role),
    )


async def _stream_investigation(
    incident_in: IncidentCreate,
    request_id: str = "unknown",
    idempotency_key: Optional[str] = None,
    tenant_id: Optional[str] = None,
    principal_role: str = "engineer",
):
    """
    Generator that executes the investigation pipeline while yielding SSE events.
    """
    if not tenant_id:
        raise HTTPException(403, "No tenant is bound to this credential")

    start = time.perf_counter()

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    yield sse("pipeline_started", {
        "message": "Investigation pipeline initialized",
        "request_id": request_id,
        "title": incident_in.title,
    })

    # 1. Redact sensitive data before persistence / LLM exposure.
    safe_logs, safe_metrics, safe_traces, safe_topology, safe_context, safe_description = (
        await asyncio.to_thread(_redact_evidence, incident_in)
    )

    # 1. Persist the incident - use repository's create_incident which has built-in deduplication
    from database.session import AsyncSessionLocal

    reused = False
    async with AsyncSessionLocal() as session:
        existing = None
        if idempotency_key:
            existing = await get_incident_by_idempotency_key(
                session, idempotency_key, tenant_id=tenant_id
            )
        if existing is not None:
            incident_id = existing.id
        else:
            incident_id, reused = await _persist_incident(
                session, incident_in, safe_description, safe_logs, safe_metrics,
                safe_traces, safe_topology, safe_context, tenant_id, idempotency_key,
            )
        await session.commit()

    if existing is not None or reused:
        async with AsyncSessionLocal() as session:
            existing_root = await get_root_cause(session, incident_id, tenant_id=tenant_id)
            existing_incident = await get_incident(session, incident_id, tenant_id=tenant_id)
        if existing_root is not None:
            yield sse("idempotent_replay", {
                "incident_id": incident_id,
                "detail": "Investigation previously completed for this incident.",
            })
            result = await _result_for_existing_incident(
                incident_id, tenant_id=tenant_id, reused=True, principal_role=principal_role
            )
            yield sse("investigation_result", result.model_dump())
            return
        if reused and existing_incident is not None and existing_incident.status == "investigating":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A matching incident is already under investigation",
            )

    yield sse("incident_persisted", {
        "incident_id": incident_id,
        "title": incident_in.title,
        "severity": incident_in.severity,
        "affected_services": incident_in.affected_services,
    })

    # 2. Build context (already redacted)
    context: Dict[str, Any] = {
        # Explicit: the ContextVar fallback is never populated in the streaming
        # generator, which silently disabled the historical agent.
        "tenant_id": tenant_id,
        "logs": safe_logs,
        "metrics": safe_metrics,
        "traces": safe_traces,
        "topology": safe_topology,
        "affected_services": incident_in.affected_services,
        "incident_type": incident_in.incident_type,
    }

    yield sse("agents_dispatched", {
        "incident_type": incident_in.incident_type,
        "affected_services": incident_in.affected_services,
        "message": f"Orchestrating investigation tree for {incident_in.incident_type} incident...",
    })

    # 3. Run tree
    tree_result = await run_tree(
        incident_id=incident_id,
        incident_type=incident_in.incident_type,
        context=context,
    )

    findings = tree_result.findings
    agents_used = tree_result.agents_used

    # Stream each agent finding
    for f in findings:
        agent_name = _finding_field(f, "agent_name") or _finding_field(f, "agent", "analyzer")
        finding_type = _finding_field(f, "finding_type", "analysis")
        conf_val = _finding_field(f, "confidence", 0.5)
        details = _finding_field(f, "details", {})
        summary = _finding_field(f, "summary", "")
        if not summary and isinstance(details, dict):
            summary = details.get("description", "")

        yield sse("agent_completed", {
            "agent": agent_name,
            "finding_type": finding_type,
            "confidence": round(float(conf_val or 0.5), 3),
            "summary": summary,
        })

    # 4-6. Causal graph, propagation and candidates (CPU-bound, off the loop).
    causal_graph, propagation, candidates = await asyncio.to_thread(
        _build_graph_stage, findings, incident_in.affected_services, safe_logs,
        safe_metrics, safe_traces,
    )

    nodes_list = causal_graph.nodes() if callable(causal_graph.nodes) else causal_graph.nodes
    edges_list = causal_graph.edges() if callable(causal_graph.edges) else causal_graph.edges

    yield sse("causal_graph_built", {
        "nodes_count": len(nodes_list),
        "edges_count": len(edges_list),
        "message": f"Constructed causal graph with {len(nodes_list)} nodes and {len(edges_list)} edges",
    })

    root_node_id = candidates[0]["node_id"] if candidates else None
    # Keyed by graph node id, not display label: the consensus engine looks this
    # up with a natural-language hypothesis, so a label key made the graph
    # evidence channel unreachable.
    graph_candidates = {
        key: float(candidate.get("confidence") or 0.0)
        for candidate in candidates
        for key in (str(candidate.get("node_id") or ""), str(candidate.get("label") or "").strip().lower())
        if key
    }

    yield sse("confidence_propagated", {
        "iterations": settings.propagation_iterations,
        "candidate_count": len(candidates),
        "top_candidate": candidates[0].get("label") if candidates else None,
    })

    # 7. Consensus
    consensus = await reach_consensus(
        findings=findings,
        reliability_scores=tree_result.reliability_scores,
        graph_candidates=graph_candidates,
    )
    consensus = apply_graph_traversal_fallback(
        consensus, candidates, enable_fallback=settings.graph_traversal_fallback
    )

    yield sse("consensus_reached", {
        "root_cause": consensus.root_cause,
        "confidence": round(float(consensus.confidence), 3),
        "alternatives_count": len(consensus.alternatives),
    })

    # Emit verdict event for backward compatibility with tests
    yield sse("verdict", {
        "root_cause": consensus.root_cause,
        "confidence": round(float(consensus.confidence), 3),
        "explanation": consensus.explanation,
    })

    # 8. Persist root cause (upsert: a deduplicated repeat run must not raise
    #    IntegrityError against the existing unique row).
    causal_chain = causal_graph.causal_chain_to(root_node_id) if root_node_id else []
    contributing: List[str] = [
        label
        for c in causal_chain
        if isinstance(label := c.get("label"), str) and label
    ]
    async with AsyncSessionLocal() as session:
        await upsert_root_cause(
            session,
            tenant_id=tenant_id,
            incident_id=incident_id,
            root_cause=consensus.root_cause,
            confidence=consensus.confidence,
            alternatives=[
                {"cause": a.cause, "confidence": a.confidence, "evidence": a.evidence}
                for a in consensus.alternatives
            ],
            explanation=consensus.explanation,
            causal_chain=causal_chain,
            contributing_factors=contributing,
        )
        await session.commit()

    # 9. Explainability (CPU-bound, off the loop)
    explanation = await asyncio.to_thread(
        build_explanation,
        incident_id=incident_id,
        findings=findings,
        consensus=consensus,
        causal_graph=causal_graph,
        propagation=propagation,
        root_cause_node_id=root_node_id,
    )

    yield sse("explanation_built", {
        "summary": consensus.root_cause,
    })

    # 10. Meta-reasoning (CPU-bound, off the loop)
    meta = await asyncio.to_thread(
        meta_evaluate,
        incident_id=incident_id,
        findings=findings,
        agents_skipped=tree_result.agents_skipped,
        final_root_cause=consensus.root_cause,
        final_confidence=consensus.confidence,
        agents_used=agents_used,
        persist=False,
    )
    await persist_meta_reasoning(meta, tenant_id=tenant_id)

    # 11. Mark the investigation phase complete; "analyzed" closes the
    #     deduplication window that a lingering "investigating" kept open.
    async with AsyncSessionLocal() as session:
        await update_incident(
            session, incident_id, tenant_id=tenant_id, status="analyzed"
        )
        await session.commit()

    # 12. NO learning here (unverified consensus).  Learning only runs after
    # the incident is resolved with a confirmed root cause.
    yield sse("learning_deferred", {
        "detail": (
            "Continuous learning is deferred until the incident is resolved "
            "with a confirmed (ground-truth) root cause."
        ),
    })

    duration = time.perf_counter() - start

    agent_statuses = _build_agent_statuses(findings)
    failed = [a["agent_name"] for a in agent_statuses if a["status"] == "failed"]

    final_result = InvestigationResult(
        incident_id=incident_id,
        root_cause=RootCauseOut(
            incident_id=incident_id,
            root_cause=consensus.root_cause,
            confidence=consensus.confidence,
            alternatives=[
                AlternativeHypothesis(
                    cause=a.cause,
                    confidence=a.confidence,
                    evidence=a.evidence,
                )
                for a in consensus.alternatives
            ],
            explanation=consensus.explanation,
            causal_chain=causal_chain,
            contributing_factors=contributing,
        ),
        explanation=ExplanationOut(
            incident_id=incident_id,
            evidence_used=explanation.evidence_used,
            confidence_breakdown=explanation.confidence_breakdown,
            graph_reasoning=explanation.graph_reasoning,
            alternative_root_causes=[
                AlternativeHypothesis(
                    cause=a["cause"],
                    confidence=a["confidence"],
                    evidence=a.get("evidence", []),
                )
                for a in explanation.alternative_root_causes
            ],
            final_explanation=explanation.final_explanation,
        ),
        meta_reasoning=MetaReasoningOut(
            incident_id=incident_id,
            useful_agents=meta.useful_agents,
            unnecessary_agents=meta.unnecessary_agents,
            optimal_path=meta.optimal_path,
            suggestions=meta.suggestions,
            agent_scores=meta.agent_scores,
        ),
        agents_used=agents_used,
        duration_seconds=round(duration, 3),
        agent_statuses=[AgentStatus(**a) for a in agent_statuses],
        status="completed_with_degraded_agents" if failed else "completed",
        runtime=_runtime_metadata(principal_role),
    )

    yield sse("investigation_result", final_result.model_dump())


# ---------------------------------------------------------------
# POST /incidents/investigate/stream (SSE)
# ---------------------------------------------------------------

@router.post("/investigate/stream")
async def investigate_stream(
    incident_in: IncidentCreate,
    request: Request,
    _api_key: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
    x_idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """
    Run the full agentic investigation pipeline while streaming real-time SSE events.
    """
    request_id = getattr(request.state, "request_id", None) or "unknown"
    role = getattr(getattr(request.state, "principal", None), "role", "engineer")
    idem_key = (x_idempotency_key or "").strip()[:128] or None

    # The SSE path bypassed the rate limiter entirely, so it was the cheapest
    # way to saturate the agent pool.
    allowed, retry_after = await _rate_limiter.check(
        _rate_limit_key(request, tenant, _api_key)
    )
    if not allowed:
        logger.warning(
            "investigation_rate_limited",
            extra={"request_id": request_id, "route": "/investigate/stream"},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Rate limit exceeded ({settings.investigation_rate_limit}). "
                "Try again shortly."
            ),
            headers={"Retry-After": str(max(1, int(retry_after + 0.999)))},
        )

    # Capacity is checked here, not inside the generator: once
    # StreamingResponse starts the body the status line is already sent, so an
    # over-capacity rejection could only ever be delivered as an SSE error
    # event on an already-200 response.
    await _acquire_investigation_slot(request)

    async def event_generator():
        incident_id: Optional[str] = None
        try:
            # Idempotency replay: return the already-persisted result as the
            # final SSE event without re-running any agents.
            if idem_key:
                from database.repositories import get_incident_by_idempotency_key
                from database.session import AsyncSessionLocal

                async with AsyncSessionLocal() as session:
                    existing = await get_incident_by_idempotency_key(
                        session, idem_key, tenant_id=tenant
                    )
                    existing_id = existing.id if existing is not None else None
                    incident_id = existing_id
                if existing_id is not None:
                    yield f"event: idempotent_replay\\ndata: {json.dumps({'incident_id': existing_id, 'detail': 'Investigation previously run under this Idempotency-Key.'})}\\n\\n"
                    result = await _result_for_existing_incident(
                        existing_id, tenant_id=tenant, reused=True, principal_role=role
                    )
                    yield f"event: investigation_result\\ndata: {json.dumps(result.model_dump())}\\n\\n"
                    return

            # Bridge the pipeline generator through an async queue so
            # individual stage events reach the client immediately while
            # heartbeats keep the connection alive during quiet periods.
            queue: asyncio.Queue = asyncio.Queue()
            sentinel = object()

            async def produce_pipeline():
                try:
                    async for chunk in _stream_investigation(
                        incident_in,
                        request_id=request_id,
                        idempotency_key=idem_key,
                        tenant_id=tenant,
                        principal_role=role,
                    ):
                        await queue.put(("chunk", chunk))
                except Exception as exc:
                    await queue.put(("error", exc))
                finally:
                    await queue.put(("done", sentinel))

            pipeline_task = asyncio.create_task(produce_pipeline())
            try:
                elapsed = 0.0
                while True:
                    try:
                        kind, value = await asyncio.wait_for(queue.get(), timeout=10.0)
                    except asyncio.TimeoutError:
                        elapsed += 10.0
                        yield f"event: heartbeat\\ndata: {json.dumps({'stage': 'pipeline_running', 'elapsed_seconds': elapsed, 'request_id': request_id})}\\n\\n"
                        continue

                    if kind == "chunk":
                        yield value
                    elif kind == "error":
                        raise value
                    else:
                        break
            finally:
                if not pipeline_task.done():
                    pipeline_task.cancel()
                await asyncio.gather(pipeline_task, return_exceptions=True)

        except HTTPException as exc:
            await _mark_investigation_failed(incident_id, tenant, exc)
            yield f"event: error\\ndata: {json.dumps({'error': exc.detail})}\\n\\n"
        except Exception as exc:
            await _mark_investigation_failed(incident_id, tenant, exc)
            logger.exception(
                "investigation_stream_failed",
                extra={"request_id": request_id, "error_type": type(exc).__name__, "error": repr(exc)},
            )
            yield f"event: error\\ndata: {json.dumps({'error': 'Investigation failed', 'request_id': request_id})}\\n\\n"
        finally:
            slot_state = getattr(request.state, "investigation_slot", None)
            if slot_state and _distributed_investigation_limiter is not None:
                await _distributed_investigation_limiter.release(*slot_state)
            else:
                _investigation_semaphore.release()


    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------
# POST /incidents/investigate/async  (background job queue)
# ---------------------------------------------------------------

@router.post("/investigate/async", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def investigate_async(
    incident_in: IncidentCreate,
    request: Request,
    _api_key: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
    x_idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> JobOut:
    """
    Enqueue an investigation and return immediately (``202 Accepted``).

    A worker drains the ``investigation_jobs`` table and runs the pipeline in
    the background; poll ``GET /incidents/jobs/{id}`` for status.  Reusing an
    ``Idempotency-Key`` returns the already-enqueued job instead of creating a
    second one.  The synchronous and SSE endpoints are unchanged.
    """
    from datetime import datetime, timezone

    from database.repositories import get_job_by_idempotency_key
    from database.session import AsyncSessionLocal
    from utils.job_queue import enqueue_investigation

    request_id = getattr(request.state, "request_id", None) or "unknown"
    idem_key = (x_idempotency_key or "").strip()[:128] or None

    allowed, retry_after = await _rate_limiter.check(
        _rate_limit_key(request, tenant, _api_key)
    )
    if not allowed:
        logger.warning(
            "investigation_rate_limited",
            extra={"request_id": request_id, "route": "/investigate/async"},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Rate limit exceeded ({settings.investigation_rate_limit}). "
                "Try again shortly."
            ),
            headers={"Retry-After": str(max(1, int(retry_after + 0.999)))},
        )

    payload = {
        "incident": incident_in.model_dump(mode="json"),
        "idempotency_key": idem_key,
        "tenant_id": tenant,
        "request_id": request_id,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        job = await enqueue_investigation(
            payload,
            idempotency_key=idem_key,
            tenant_id=tenant,
            attempts_max=settings.job_attempts_max,
        )
    except IntegrityError:
        # A concurrent enqueue claimed this idempotency key first — replay it.
        async with AsyncSessionLocal() as session:
            job = await get_job_by_idempotency_key(session, idem_key, tenant_id=tenant)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key already in use",
            )
        logger.info(
            "job_idempotency_replay",
            extra={"request_id": request_id, "job_id": job.id},
        )

    return await _job_to_out(job, tenant_id=tenant)


# ---------------------------------------------------------------
# GET /incidents/jobs and /incidents/jobs/{id}, cancel
# ---------------------------------------------------------------

@router.get("/jobs", response_model=List[JobOut])
async def list_jobs_endpoint(
    limit: int = Query(default=50, ge=1, le=settings.max_pagination_limit),
    _api_key: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> List[JobOut]:
    from database.repositories import list_jobs
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        jobs = await list_jobs(session, limit=limit, tenant_id=tenant)
    # One batched incident fetch for the whole page instead of one DB session
    # per job.
    return await _jobs_to_out(jobs, tenant_id=tenant)


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job_endpoint(
    job_id: str,
    _api_key: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> JobOut:
    from database.repositories import get_job
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        job = await get_job(session, job_id, tenant_id=tenant)
    if job is None:
        raise HTTPException(404, "Job not found")
    return await _job_to_out(job, tenant_id=tenant)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job_endpoint(
    job_id: str,
    _api_key: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> JobOut:
    from database.repositories import cancel_job, get_job
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        job = await get_job(session, job_id, tenant_id=tenant)
        if job is None:
            raise HTTPException(404, "Job not found")
        # cancel_job reports whether it actually performed the transition.
        # Cancelling an already-cancelled job is idempotent (200, true state);
        # cancelling one that already reached a terminal state is a genuine
        # conflict, so the caller is not told the cancel succeeded.
        cancelled, job = await cancel_job(session, job_id, tenant_id=tenant)
        if job.status not in {"queued", "running", "cancelled"}:
            await session.rollback()
            raise HTTPException(409, f"Job is not cancellable (status={job.status})")
        await session.commit()
        # The UPDATE expired every column on this instance, so reading
        # job.updated_at would trigger a lazy refresh outside greenlet
        # context (MissingGreenlet). Refresh explicitly, while attached.
        await session.refresh(job)
        return await _job_to_out(job, tenant_id=tenant)


# ---------------------------------------------------------------
# GET /incidents
# ---------------------------------------------------------------

@router.get("", response_model=List[IncidentOut])
async def list_recent_incidents(
    limit: int = Query(default=50, ge=1, le=settings.max_pagination_limit),
    _api_key: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> List[IncidentOut]:
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        rows = await list_incidents(session, limit=limit, tenant_id=tenant)
    return [IncidentOut.model_validate(r) for r in rows]


# ---------------------------------------------------------------
# GET /incidents/{id}
# ---------------------------------------------------------------

@router.get("/{incident_id}", response_model=IncidentOut)
async def get_incident_by_id(
    incident_id: str,
    _api_key: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> IncidentOut:
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        inc = await get_incident(session, incident_id, tenant_id=tenant)
    if inc is None:
        raise HTTPException(404, "Incident not found")
    return IncidentOut.model_validate(inc)


# ---------------------------------------------------------------
# GET /incidents/{id}/root-cause
# ---------------------------------------------------------------

@router.get("/{incident_id}/root-cause", response_model=RootCauseOut)
async def get_root_cause_for_incident(
    incident_id: str,
    _api_key: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> RootCauseOut:
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        inc = await get_incident(session, incident_id, tenant_id=tenant)
        if inc is None:
            raise HTTPException(404, "Incident not found")
        rc = await get_root_cause(session, incident_id, tenant_id=tenant)
    if rc is None:
        raise HTTPException(404, "Root cause not found")
    return RootCauseOut(
        incident_id=rc.incident_id,
        root_cause=rc.root_cause,
        confidence=rc.confidence,
        alternatives=[
            AlternativeHypothesis(
                cause=a.get("cause", ""),
                confidence=a.get("confidence", 0.0),
                evidence=a.get("evidence", []),
            )
            for a in rc.alternatives
        ],
        explanation=rc.explanation,
        causal_chain=rc.causal_chain,
        contributing_factors=rc.contributing_factors,
    )


# ---------------------------------------------------------------
# POST /incidents/{id}/resolve
# ---------------------------------------------------------------

@router.post("/{incident_id}/resolve", response_model=ResolutionOut)
async def resolve_incident(
    incident_id: str,
    body: ResolutionCreate,
    _api_key: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> ResolutionOut:
    """
    Record an engineer-supplied resolution.

    This is the **authoritative learning trigger**: when ``body`` includes
    a ``confirmed_root_cause`` (with ``ground_truth_source`` / ``confirmed_by``),
    continuous learning runs against that ground truth — updating patterns,
    memory, knowledge-graph edges, and agent reliability.  Without a
    confirmed root cause, nothing is learned (PRISM's own consensus is never
    treated as truth).
    """
    from datetime import datetime, timezone

    from database.session import AsyncSessionLocal

    async def _existing_out() -> ResolutionOut:
        """Build the response from an already-persisted resolution (idempotent replay)."""
        async with AsyncSessionLocal() as _s:
            existing = await get_resolution(_s, incident_id, tenant_id=tenant)
        meta = (existing.metadata_ or {}) if existing else {}
        return ResolutionOut(
            incident_id=incident_id,
            action=existing.action if existing else body.action,
            steps=(existing.steps or []) if existing else body.steps,
            verified=existing.verified if existing else body.verified,
            confirmed_root_cause=meta.get("confirmed_root_cause"),
            ground_truth_source=meta.get("ground_truth_source"),
            ground_truth_confidence=meta.get("ground_truth_confidence"),
            confirmed_by=meta.get("confirmed_by"),
        )

    try:
        async with AsyncSessionLocal() as session:
            inc = await get_incident(session, incident_id, tenant_id=tenant)
            if inc is None:
                raise HTTPException(404, "Incident not found")
            if inc.status == "resolved":
                # Idempotent replay: a repeated resolve returns the existing
                # resolution instead of failing with 409.
                return await _existing_out()
            rc = await get_root_cause(session, incident_id, tenant_id=tenant)
            findings = await list_findings(session, incident_id, tenant_id=tenant)
            affected_services = list(inc.affected_services or [])
            raw_logs = list(inc.raw_logs or [])
            rc_confidence = rc.confidence if rc else 0.5

            meta: Dict[str, Any] = {
                "confirmed_root_cause": body.confirmed_root_cause,
                "ground_truth_source": body.ground_truth_source,
                "ground_truth_confidence": body.ground_truth_confidence,
                "confirmed_by": body.confirmed_by,
                "confirmed_at": datetime.now(timezone.utc).isoformat() if body.confirmed_root_cause else None,
            }
            res = await save_resolution(
                session,
                tenant_id=tenant,
                incident_id=incident_id,
                action=body.action,
                steps=body.steps,
                verified=body.verified,
                metadata_=meta,
            )
            await update_incident(
                session,
                incident_id,
                tenant_id=tenant,
                status="resolved",
                resolved_at=datetime.now(timezone.utc),
            )
            await session.commit()
    except IntegrityError:
        # Concurrent resolve race: another request committed first.  The
        # unique(incident_id) constraint made us lose — the winner already
        # recorded the resolution and ran learning, so replay their result.
        return await _existing_out()

    # Continuous learning runs ONLY against confirmed ground truth.
    confirmed_rc = (body.confirmed_root_cause or "").strip()
    if confirmed_rc:
        learning_findings = [
            {
                "agent_name": f.agent_name,
                "root_cause_hint": (f.metadata_ or {}).get("root_cause_hint"),
                "confidence": f.confidence,
            }
            for f in findings
        ]
        learning_input = LearningInput(
            incident_id=incident_id,
            tenant_id=tenant,
            root_cause=confirmed_rc,
            confidence=body.ground_truth_confidence or rc_confidence,
            affected_services=affected_services,
            raw_logs=raw_logs,
            resolution=body.action,
            agents_used=[],
            findings=learning_findings,
            consensus={},
            lessons=[
                f"Resolution '{body.action}' resolved incident confirmed as "
                f"'{confirmed_rc}' ({body.ground_truth_source or 'confirmed'} "
                f"by {body.confirmed_by or 'unknown'})."
            ],
            ground_truth_root_cause=confirmed_rc,
            ground_truth_source=body.ground_truth_source,
            ground_truth_confidence=body.ground_truth_confidence,
            confirmed_by=body.confirmed_by,
            confirmed_at=datetime.now(timezone.utc),
        )
        try:
            await learn_from_incident(learning_input)
        except Exception as exc:
            logger.warning(
                "learning_failed",
                extra={"incident_id": incident_id, "error": repr(exc)},
            )
    else:
        logger.info(
            "learning_skipped_unconfirmed_resolution",
            extra={
                "incident_id": incident_id,
                "hint": "No confirmed_root_cause supplied; nothing was learned.",
            },
        )

    return ResolutionOut(
        incident_id=incident_id,
        action=res.action,
        steps=res.steps,
        verified=res.verified,
        confirmed_root_cause=meta.get("confirmed_root_cause"),
        ground_truth_source=meta.get("ground_truth_source"),
        ground_truth_confidence=meta.get("ground_truth_confidence"),
        confirmed_by=meta.get("confirmed_by"),
    )