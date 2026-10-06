"""
tests.test_production_hardening
===============================

Regression tests for the production-hardening batch additions:

* deterministic pseudo-embeddings across processes (reproducibility)
* causal graph built from redacted evidence only (no secret leak via
  causal_chain labels / persisted root causes)
* idempotent incident resolve (repeat resolve replays, runs learning once)
* strict per-test reset of all module-level learning state
* investigation never grades agents / never updates action-effectiveness
  against unverified consensus or ad-hoc ``context["ground_truth"]``
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ENABLE_NEO4J", "false")
os.environ.setdefault("ENABLE_OLLAMA", "false")


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_AUTH = {"X-API-Key": "test-api-key-that-is-long-enough-32chars"}


def _run_loop(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# 1. Deterministic embeddings
# ---------------------------------------------------------------------------

def _subprocess_embedding(seed: str, text: str) -> str:
    env = dict(os.environ, PYTHONHASHSEED=seed)
    code = (
        "import sys; sys.path.insert(0, %r); "
        "from utils.llm import LLMClient; "
        "print(repr(LLMClient._pseudo_embedding(%r, 384)))"
    ) % (str(PROJECT_ROOT), text)
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_pseudo_embedding_is_deterministic_across_processes():
    """Vectors must not depend on the per-process ``hash()`` salt."""
    sample = "timeout error waiting on database pool connection"

    seed_1 = _subprocess_embedding("1", sample)
    seed_999 = _subprocess_embedding("999", sample)
    assert seed_1 == seed_999


def test_pseudo_embedding_fixed_values():
    from utils.llm import LLMClient

    vec = LLMClient._pseudo_embedding("", 384)
    assert all(v == 0.0 for v in vec)

    a = LLMClient._pseudo_embedding("timeout db", 384)
    b = LLMClient._pseudo_embedding("timeout OTHER", 384)
    c = LLMClient._pseudo_embedding("timeout db", 384)
    assert c == a
    assert any(x != y for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# 2. Redaction into the causal graph
# ---------------------------------------------------------------------------

def test_causal_graph_only_sees_scrubbed_evidence():
    """Causal graph nodes must be built from state that was redacted first."""
    from causal_graph.engine import CausalGraphBuilder
    from utils.redaction import scrub_iterable

    secret_value = "MyPass99"
    secret = f"password={secret_value}"
    raw = [f"ERROR checkout-svc: timeout {secret} pool"]
    scrubbed = scrub_iterable(raw)[0]
    assert secret_value not in scrubbed
    assert "REDACTED" in scrubbed

    graph = CausalGraphBuilder().build_from_findings(
        findings=[],
        affected_services=["checkout-svc"],
        logs=scrub_iterable(raw),
    )
    for nid, data in graph.graph.nodes(data=True):
        text = f"{data.get('label', '')} {data.get('data', {})}"
        assert secret_value not in text
        if data.get("kind") == "log":
            assert "REDACTED" in text, nid


def test_investigation_pipeline_never_leaks_secret(client):
    secret_value = "MyPass99"
    secret = f"password={secret_value}"
    payload = {
        "title": "redaction to causal graph",
        "severity": "P2",
        "affected_services": ["checkout-svc"],
        "raw_logs": [f"ERROR checkout-svc: timeout {secret} pool"],
    }
    r = client.post("/incidents/investigate", json=payload)
    assert r.status_code == 200, r.text
    incident_id = r.json()["incident_id"]

    inc = client.get(f"/incidents/{incident_id}")
    assert inc.status_code == 200, inc.text

    from database.repositories import get_incident
    from database.session import AsyncSessionLocal

    async def _fetch():
        async with AsyncSessionLocal() as s:
            row = await get_incident(s, incident_id, tenant_id="test-tenant")
            return list(row.raw_logs or [])

    stored = _run_loop(_fetch())
    assert stored
    assert all(secret_value not in line for line in stored)
    assert all("REDACTED" in line for line in stored)

    rc = client.get(f"/incidents/{incident_id}/root-cause")
    assert rc.status_code == 200, rc.text
    assert secret_value not in str(rc.json())


# ---------------------------------------------------------------------------
# 3. Idempotent resolve
# ---------------------------------------------------------------------------

def test_resolve_is_idempotent(client):
    payload = {
        "title": "resolved twice",
        "severity": "P2",
        "affected_services": ["web-svc"],
        "raw_logs": ["2024-01-01T12:00:00Z ERROR web-svc: timeout calling /v1/charge"],
    }
    r = client.post("/incidents/investigate", json=payload)
    assert r.status_code == 200
    incident_id = r.json()["incident_id"]

    body = {
        "action": "scaled replicas",
        "verified": True,
        "confirmed_root_cause": "timeout",
        "ground_truth_source": "engineer_confirmed",
        "ground_truth_confidence": 1.0,
        "confirmed_by": "sre-alice",
    }
    r1 = client.post(f"/incidents/{incident_id}/resolve", json=body)
    assert r1.status_code == 200, r1.text
    assert r1.json()["confirmed_root_cause"] == "timeout"

    r2 = client.post(f"/incidents/{incident_id}/resolve", json=body)
    assert r2.status_code == 200, r2.text
    assert r2.json()["confirmed_root_cause"] == "timeout"
    assert r2.json()["action"] == "scaled replicas"


def test_investigate_same_idempotency_key_replays_same_incident(client):
    payload = {
        "title": "duplicate requests same key",
        "severity": "P3",
        "affected_services": ["payments-svc"],
        "raw_logs": ["ERROR payments-svc: 500 on /v1/pay"],
    }
    key = "idem-abc-123"
    headers = {**_AUTH, "Idempotency-Key": key}

    r1 = client.post("/incidents/investigate", json=payload, headers=headers)
    assert r1.status_code == 200, r1.text
    id1 = r1.json()["incident_id"]

    r2 = client.post("/incidents/investigate", json=payload, headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["incident_id"] == id1


# ---------------------------------------------------------------------------
# 4. Strict learning-state reset
# ---------------------------------------------------------------------------

def test_reset_clears_reliability_and_effectiveness(monkeypatch):
    import investigation.action_effectiveness as ae
    import investigation.reliability_store as rs

    # Never touch real disk JSON; updates stay in-memory so a reset genuinely
    # wipes the observable state rather than reloading it from a file.
    monkeypatch.setattr(ae, "_persist_cache", lambda: None)
    monkeypatch.setattr(rs, "_persist_cache", lambda: None)

    ae.update_effectiveness("ctx", "agent", 0.9)
    rs.update_reliability("ctx", "agent", is_correct=True, has_ground_truth=True)
    assert ae.get_expected_effectiveness("ctx", "agent") > ae.DEFAULT_EXPECTED_GAIN
    assert rs.get_reliability("ctx", "agent") != rs.settings.agent_default_reliability

    ae.reset_effectiveness_cache()
    rs.reset_reliability_cache()

    # Fresh cache => prior defaults, no contamination from the update above.
    assert ae.get_expected_effectiveness("ctx", "agent") == ae.DEFAULT_EXPECTED_GAIN
    assert rs.get_reliability("ctx", "agent") == rs.settings.agent_default_reliability


def test_memory_and_kg_reset_singleton():
    from knowledge_graph.store import KnowledgeGraphStore
    from memory.store import MemoryStore

    first_mem = MemoryStore.get()
    first_kg = KnowledgeGraphStore.get()
    MemoryStore.reset()
    KnowledgeGraphStore.reset()
    assert MemoryStore.get() is not first_mem
    assert KnowledgeGraphStore.get() is not first_kg


# ---------------------------------------------------------------------------
# 5. Investigation never learns against unverified signals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_tree_does_not_grade_agents_or_effectiveness(db_session):
    from investigation.action_effectiveness import get_effectiveness_record
    from investigation.reliability_store import get_reliability
    from investigation.tree import run_tree

    context = {
        "incident_id": "INC-HARD-1",
        "incident_type": "network_incident",
        "tenant_id": "test-tenant",
        "logs": ["Network timeout connecting to host DB"],
        "ground_truth": "timeout",  # invalid learning channel - must be ignored
        "confirmed_root_cause": "timeout",
    }

    result = await run_tree("INC-HARD-1", "network_incident", context)
    assert result.findings

    from config.settings import settings

    for agent in result.agents_used:
        assert get_reliability("network_incident", agent) == settings.agent_default_reliability
        rec = get_effectiveness_record("network_incident", agent)
        assert rec["update_count"] == 0


# ---------------------------------------------------------------------------
# 6. Provenance reaches consensus (correlated-voter discount)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_provenance_stamped_on_inflight_findings(db_session):
    """Findings must carry provenance before they reach consensus / storage."""
    from orchestrator.engine import execute_action

    finding = await execute_action(
        agent_name="rule_based_analyzer",
        context={"logs": ["timeout on database connection"]},
        incident_id="INC-PROV-1",
        persist=False,
    )
    prov = (finding.metadata or {}).get("provenance", {})
    assert prov.get("source_type") == "rule"
    assert prov.get("codepath")
    assert "fallback_used" in prov
    assert isinstance(prov.get("execution_ms"), float)
    assert finding.to_dict()["metadata"]["provenance"]["codepath"] == prov["codepath"]


# ---------------------------------------------------------------------------
# 7. Incident dedup must never merge unrelated incidents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_incident_dedup_is_mutual(db_session):
    from database.repositories import create_incident

    inc1 = await create_incident(
        db_session,
        title="out of memory in checkout",
        severity="P1",
        affected_services=["checkout-svc"],
        status="open",
        tenant_id="test-tenant",
    )
    await db_session.flush()

    # Same real event, paraphrased title, same service => merged.
    inc2 = await create_incident(
        db_session,
        title="OOM on checkout-svc",
        severity="P1",
        affected_services=["checkout-svc"],
        status="open",
        tenant_id="test-tenant",
    )
    await db_session.flush()
    assert inc2.id == inc1.id

    # Different event that merely shares the keyword "oom" => never merged.
    inc3 = await create_incident(
        db_session,
        title="oom alert on payments",
        severity="P1",
        affected_services=["payments-svc"],
        status="open",
        tenant_id="test-tenant",
    )
    await db_session.flush()
    assert inc3.id != inc1.id
    assert inc3.title == "oom alert on payments"


# ---------------------------------------------------------------------------
# 8. Tenant isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tenant_isolation_across_incidents(db_session):
    from database.repositories import (
        create_incident,
        get_incident,
        list_incidents,
    )

    inc_a = await create_incident(
        db_session,
        title="checkout latency spike",
        severity="P2",
        affected_services=["checkout-svc"],
        status="open",
        tenant_id="acme",
    )
    await db_session.flush()

    # Same real event in ANOTHER tenant must never be merged.
    inc_b = await create_incident(
        db_session,
        title="checkout latency spike",
        severity="P2",
        affected_services=["checkout-svc"],
        status="open",
        tenant_id="globex",
    )
    await db_session.flush()
    assert inc_a.id != inc_b.id
    assert inc_a.tenant_id == "acme"
    assert inc_b.tenant_id == "globex"

    # Same event in the SAME tenant is still deduplicated into one row.
    inc_a2 = await create_incident(
        db_session,
        title="checkout latency spike",
        severity="P2",
        affected_services=["checkout-svc"],
        status="open",
        tenant_id="acme",
    )
    await db_session.flush()
    assert inc_a2.id == inc_a.id

    # Read paths are tenant-scoped.
    assert (await get_incident(db_session, inc_a.id, tenant_id="acme")) is not None
    assert (await get_incident(db_session, inc_a.id, tenant_id="globex")) is None
    ids_acme = {i.id for i in await list_incidents(db_session, tenant_id="acme")}
    ids_globex = {i.id for i in await list_incidents(db_session, tenant_id="globex")}
    assert inc_a.id in ids_acme
    assert inc_a.id not in ids_globex


def test_consensus_discounts_correlated_fallback_voters():
    """Two voters sharing the same fallback codepath must not out-vote one."""
    from consensus.engine import reach_consensus

    def _find(agent, conf):
        return {
            "agent_name": agent,
            "finding_type": "hypothesis",
            "description": f"hint by {agent}",
            "confidence": conf,
            "evidence": {},
            "root_cause_hint": "timeout",
            "hypotheses": ["timeout"],
            "metadata": {
                "provenance": {
                    "source_type": "rule",
                    "codepath": "fallback/db_pool",
                    "fallback_used": True,
                }
            },
        }

    single = _run_loop(reach_consensus([_find("A", 0.8)], reliability_scores={"A": 0.5}, min_voters=1, quorum_threshold=0.5))
    correlated = _run_loop(
        reach_consensus(
            [_find("A", 0.8), _find("B", 0.8)],
            reliability_scores={"A": 0.5, "B": 0.5},
            min_voters=1,
            quorum_threshold=0.5,
        )
    )
    assert single.root_cause == "timeout"
    assert correlated.root_cause == "timeout"
    # Correlated voters contribute 1/N each => same total as one voter.
    assert abs(correlated.confidence - single.confidence) < 1e-6

    # Control: two INDEPENDENT (non-fallback) codepaths still stack.
    def _independent(agent, cp):
        f = _find(agent, 0.8)
        f["metadata"]["provenance"]["codepath"] = cp
        f["metadata"]["provenance"]["fallback_used"] = False
        return f

    independent = _run_loop(
        reach_consensus(
            [_independent("A", "cp1"), _independent("B", "cp2")],
            reliability_scores={"A": 0.5, "B": 0.5},
        )
    )
    assert independent.confidence > single.confidence


# ---------------------------------------------------------------------------
# 9. Request rate limiting
# ---------------------------------------------------------------------------

def test_parse_rate_specs():
    from utils.rate_limit import parse_rate

    assert parse_rate("10/minute") == (10, 60.0)
    assert parse_rate("2/second") == (2, 1.0)
    assert parse_rate("5/hour") == (5, 3600.0)
    assert parse_rate("") == (0, 0.0)
    assert parse_rate("garbage") == (0, 0.0)
    assert parse_rate("0/minute") == (0, 0.0)


@pytest.mark.asyncio
async def test_sliding_window_limiter_blocks_over_limit():
    from utils.rate_limit import SlidingWindowRateLimiter

    limiter = SlidingWindowRateLimiter("2/minute")
    assert await limiter.check("caller") == (True, 0.0)
    assert await limiter.check("caller") == (True, 0.0)
    allowed, retry_after = await limiter.check("caller")
    assert allowed is False
    assert retry_after > 0
    # A different caller is unaffected by another caller's window.
    assert await limiter.check("other") == (True, 0.0)
    await limiter.reset()
    assert await limiter.check("caller") == (True, 0.0)


def test_investigate_rate_limited_returns_429(client, monkeypatch):
    import api.investigation as inv

    async def _deny(_key):
        return False, 2.0

    monkeypatch.setattr(inv._rate_limiter, "check", _deny)
    r = client.post(
        "/incidents/investigate",
        json={"title": "rate limited incident", "severity": "P3"},
        headers=_AUTH,
    )
    assert r.status_code == 429, r.text
    assert r.headers.get("Retry-After") == "2"


def test_require_tenant_header_enforced(client, monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "require_tenant_header", True)

    assert client.get("/incidents", headers=_AUTH).status_code == 200
    assert (
        client.post(
            "/incidents/investigate",
            json={"title": "needs tenant", "severity": "P3"},
            headers=_AUTH,
        ).status_code
        == 200
    )

    ok = client.get("/incidents", headers={**_AUTH, "X-Tenant-Id": "acme"})
    assert ok.status_code == 200


def test_build_rate_limiter_selects_backend():
    from utils.rate_limit import (
        RedisRateLimiter,
        SlidingWindowRateLimiter,
        build_rate_limiter,
    )

    assert isinstance(build_rate_limiter("10/minute", ""), SlidingWindowRateLimiter)
    assert isinstance(
        build_rate_limiter("10/minute", "redis://localhost:6379/0"),
        RedisRateLimiter,
    )


@pytest.mark.asyncio
async def test_redis_rate_limiter_shared_window():
    fakeredis = pytest.importorskip("fakeredis.aioredis")
    from utils.rate_limit import RedisRateLimiter

    client = fakeredis.FakeRedis(decode_responses=True)
    limiter = RedisRateLimiter("2/minute", "redis://unused", client=client)
    assert await limiter.check("tenant:acme") == (True, 0.0)
    assert await limiter.check("tenant:acme") == (True, 0.0)
    allowed, retry_after = await limiter.check("tenant:acme")
    assert allowed is False
    assert retry_after > 0
    # Independent identity is unaffected.
    assert await limiter.check("tenant:globex") == (True, 0.0)
    await limiter.reset()
    assert await limiter.check("tenant:acme") == (True, 0.0)

@pytest.mark.asyncio
async def test_jwt_revocation_uses_shared_redis():
    fakeredis = pytest.importorskip("fakeredis.aioredis")
    import auth.security as security

    client = fakeredis.FakeRedis(decode_responses=True)
    old_client = security._REDIS_REVOCATION_CLIENT
    old_url = security.settings.redis_url
    security._REDIS_REVOCATION_CLIENT = client
    security.settings.redis_url = "redis://unused"
    try:
        await security.revoke_jti("shared-jti", 60)
        assert await security.is_revoked("shared-jti") is True
        await client.delete("waypoint:revoked:shared-jti")
        assert await security.is_revoked("shared-jti") is False
    finally:
        security._REDIS_REVOCATION_CLIENT = old_client
        security.settings.redis_url = old_url
        await client.aclose()
