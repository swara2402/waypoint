# WayPoint — Enterprise Agentic AI Incident Investigation Framework

WayPoint is a modular, production-ready Python framework that investigates
production incidents using **multiple cooperating intelligent agents**.

When an incident is reported, WayPoint runs a subset of specialized agents over
the available evidence, merges their findings into a causal graph, propagates
confidence, reaches a weighted consensus on the root cause, produces a
human-readable explanation, and — only after a root cause is **confirmed** by a
human or external source — learns from the outcome to improve future
investigations.

The design goal is that **no single agent is authoritative**: findings are
treated as voters, and each voter's influence is weighted by its own
confidence and its continuously learned **reliability score**.

---

## Document set

| Document | Purpose |
|----------|---------|
| [`README.md`](./README.md) | This file — overview, install, quick start |
| [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) | System design, investigation pipeline, core modules |
| [`docs/API.md`](./docs/API.md) | HTTP API reference (all endpoints) |
| [`docs/CONFIGURATION.md`](./docs/CONFIGURATION.md) | Every environment setting |
| [`docs/SECURITY.md`](./docs/SECURITY.md) | Auth, redaction, trust boundary, production posture |
| [`docs/DEVELOPMENT.md`](./docs/DEVELOPMENT.md) | Testing, extending WayPoint, contributing |

---

## Table of Contents

1. [Features](#features)
2. [Prerequisites](#prerequisites)
3. [Option A — Docker (recommended)](#option-a--docker-recommended)
4. [Option B — Local development build](#option-b--local-development-build)
5. [Environment configuration](#environment-configuration)
6. [Verify the installation](#verify-the-installation)
7. [The web console](#the-web-console)
8. [Running tests](#running-tests)
9. [Upgrading](#upgrading)
10. [Troubleshooting](#troubleshooting)
11. [License](#license)

---

## Features

- **Eight investigation agents** — rule-based, log, metric, trace, topology,
  historical (semantic memory), knowledge-graph, and LLM analyzers.
- **Adaptive orchestration** — an MDV/ADG (marginal diagnostic value / actual
  diagnostic gain) step loop picks the single highest-value action to run next
  instead of always executing every agent.
- **Causal graph + confidence propagation** — NetworkX causal graph with
  belief-propagation-style posteriors and root-cause ranking.
- **Weighted consensus** — multi-voter voting with reliability-weighted
  confidence, quorum bonuses, and structural correlation discounting.
- **Self-learning pattern library** — normalized log signatures that can
  short-circuit future investigations once approved.
- **Semantic incident memory** — FAISS-backed retrieval of similar past
  incidents (pure-Python cosine fallback).
- **Neo4j knowledge graph** — services, APIs, teams, incidents, patterns with
  idempotent Cypher writes (in-memory fallback).
- **Predictive analytics** — forecasts future incidents per
  (service, failure type) with ETA and impact.
- **Explainability + meta-reasoning** — every verdict comes with an evidence
  breakdown and per-agent usefulness analysis.
- **Ground-truth-gated learning** — WayPoint never learns from its own unverified
  consensus; only resolution with a confirmed root cause updates patterns,
  memory, knowledge-graph edges, or agent reliability.
- **Secure by default** — API-key auth (constant-time compare), PII/secret
  redaction, LLM trust boundary, request IDs, security headers, CORS fail-closed
  in production.

---

## Prerequisites

- **Python 3.11+** (the Docker image uses `python:3.11-slim`)
- **Docker + docker compose** (for the recommended path)
- **git** (to clone the repository)
- ~2 GB free disk space for Docker images and model files

The system is designed so **only PostgreSQL is strictly required** — Neo4j,
Ollama, and FAISS are optional but strongly recommended for full functionality.
See [Resilience & Degradation](./docs/ARCHITECTURE.md#resilience--degradation).

---

## Option A — Docker (recommended)

The compose stack provisions every dependency with a single command.

```bash
# 1. Clone and enter the project
git clone <your-repo-url> prism
cd prism

# 2. Create your environment file from the template
cp .env.example .env

# 3. Open .env and set strong secrets (see "Environment configuration" below)

# 4. Build and start everything
docker compose up --build -d
```

This brings up:

| Service    | Container      | Host ports                   | Purpose                                |
|------------|----------------|------------------------------|----------------------------------------|
| PostgreSQL | `prism_postgres`| `5433 → 5432`              | Primary persistence                    |
| Neo4j      | `prism_neo4j`  | `7474` (browser), `7687` (bolt) | Knowledge graph                   |
| Ollama     | `prism_ollama` | `11434`                      | Local LLM + embeddings                 |
| API        | `prism_api`    | `8000`                       | WayPoint application + console            |

> The API container overrides `DATABASE_URL` to point at the internal
> `postgres:5432` address; host tooling (psql, Alembic) should use the published
> `localhost:5433`.

### Pull the default LLM model (optional but recommended)

The `llm_analyzer` and embeddings expect models on Ollama. Pull them once the
stack is running:

```bash
docker exec prism_ollama ollama pull llama3.1:8b
docker exec prism_ollama ollama pull nomic-embed-text
```

Use the model names you set in `.env` (`OLLAMA_MODEL`, `OLLAMA_EMBED_MODEL`).

### Check the logs

```bash
docker compose logs -f api
```

---

## Option B — Local development build

### 1. Create and activate a virtual environment

```bash
cd prism
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Upgrade pip and install dependencies

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Configure the environment

```bash
cp .env.example .env
```

Edit `.env` with your local values (see the table in
[Environment configuration](#environment-configuration)). For a local-only
setup, either run managed dependencies via Docker:

```bash
docker compose up postgres neo4j ollama -d
```

or point `DATABASE_URL` at an existing PostgreSQL instance.

### 4. Validate the environment

```bash
python -c "
import fastapi
import sqlalchemy
import aiosqlite
import faiss
import sentence_transformers
import httpx
print('Environment OK - all dependencies installed')
"
```

### 5. Start the application

```bash
uvicorn main:app --reload
```

The app listens on `http://localhost:8000` (override with `APP_HOST` /
`APP_PORT`).

---

## Environment configuration

Start from `.env.example` and fill in real values.

```bash
cp .env.example .env
```

**Required in production** are `APP_ENV=production`, a strong `API_KEY`
(generate one with `openssl rand -hex 32`), and explicit `CORS_ORIGINS`.
WayPoint refuses to start in production with weak or missing secrets.

Key variables (the full reference lives in
[docs/CONFIGURATION.md](./docs/CONFIGURATION.md)):

| Variable                                | Default                    | Notes                                          |
|-----------------------------------------|----------------------------|------------------------------------------------|
| `APP_ENV`                               | `development`              | Set `production` in real deployments           |
| `API_KEY`                               | *(example value)*          | Required in production (`X-API-Key` header)    |
| `CORS_ORIGINS`                          | *(empty)*                  | Comma-separated origins; required in prod      |
| `DATABASE_URL`                          | `postgresql+asyncpg://…:5433/incident_db` | Async SQLAlchemy URL   |
| `DATABASE_SYNC_URL`                     | `postgresql+psycopg2://…:5433/incident_db` | Sync URL for migrations |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | `bolt://localhost:7687`, `neo4j`, … | Knowledge graph connection |
| `OLLAMA_HOST` / `OLLAMA_MODEL` / `OLLAMA_EMBED_MODEL` | `http://localhost:11434`, `llama3.1:8b`, `nomic-embed-text` | LLM + embeddings |
| `SENTENCE_TRANSFORMER_MODEL`            | `all-MiniLM-L6-v2`         | Embedding model for FAISS                     |
| `FAISS_INDEX_PATH`                      | `./data/faiss_index`       | FAISS index snapshot location                 |
| `MAX_CONCURRENT_INVESTIGATIONS`         | `3`                        | Concurrent investigation cap                  |
| `ENABLE_NEO4J` / `ENABLE_OLLAMA` / `ENABLE_FAISS` | `true`×3      | Per-backend feature flags                     |
| `LEARNING_MODE`                         | `online`                   | `frozen` keeps evaluation runs independent    |
| `LEARNING_REQUIRE_CONFIRMATION`         | `true`                     | Only learn from confirmed root causes         |

`DATABASE_URL` in `.env` is overridden **inside the API container** to
`@postgres:5432/…`; the `5433` host mapping is for local tooling only.

---

## Verify the installation

Once running:

```bash
curl http://localhost:8000/health
```

Expected shape — deliberately minimal, it never leaks infrastructure detail:

```json
{ "status": "ok" }
```

For a full subsystem report use the **authenticated** endpoint (requires the
`X-API-Key` that the other APIs use):

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8000/internal/health
```

```json
{
  "status": "ok",
  "env": "development",
  "version": "2.1.0",
  "memory_size": 0,
  "subsystems": {
    "database": "connected",
    "memory": "ready",
    "faiss": "ready",
    "neo4j": "connected",
    "ollama": "available"
  }
}
```

A **readiness probe** is available at `/ready` (503 until the DB responds and
memory is loaded):

```bash
curl http://localhost:8000/ready
```

### Smoke-test an investigation

```bash
curl -X POST http://localhost:8000/incidents/investigate \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "title": "Login latency spike",
    "severity": "P2",
    "incident_type": "latency",
    "affected_services": ["auth-service", "user-service"],
    "raw_logs": [
      "ERROR 2024-01-01T12:00:01 auth-service connection pool exhausted",
      "WARN  2024-01-01T12:00:02 user-service slow query 2800ms"
    ],
    "metrics": {"auth-service.latency_ms": [120, 300, 1400, 2600]}
  }'
```

> The `X-API-Key` header is only required when `API_KEY` is set in `.env`.

- Interactive API docs: `http://localhost:8000/docs`
- OpenAPI JSON: `http://localhost:8000/openapi.json`
- Service info: `http://localhost:8000/api/info`

---

## The web console

A bundled incident command console is served at the API root:

```
http://localhost:8000/
```

It uses the same origin as the API by default. If you deploy the console
separately from the API, define `window.WAYPOINT_API_URL` **before** loading
`static/app.js`, e.g.:

```html
<script>
  window.PRISM_API_URL = "https://api.example.com";
</script>
<script src="/static/app.js"></script>
```

When `API_KEY` is set, the console sends it with every request.

---

## Running tests

Tests execute **without any external services** (in-memory SQLite; Neo4j /
Ollama / FAISS disabled).

```bash
pip install -r requirements.txt
pytest -v
```

The suite covers agents, orchestrator selection, the MDV/ADG investigation
loop, causal graph, confidence propagation, consensus, knowledge graph,
pattern generation, continuous learning, meta-reasoning, security middleware,
and API smoke tests.

---

## Upgrading

```bash
# Docker
git pull                          # fetch new code
docker compose build api          # rebuild the image
docker compose up -d              # recreate containers

# Local
git pull                          # fetch new code
source .venv/bin/activate
pip install -r requirements.txt   # sync dependencies
uvicorn main:app --reload         # restart
```

> The app initializes its schema on startup (idempotent). For schema changes
> across versions, run an Alembic migration:

```bash
# Docker
docker compose exec api python -m alembic upgrade head

# Local
source .venv/bin/activate
DATABASE_SYNC_URL=postgresql+psycopg2://... python -m alembic upgrade head
```

---

## Troubleshooting

| Symptom                                     | Likely cause / fix                                                          |
|---------------------------------------------|-----------------------------------------------------------------------------|
| `POSTGRES_PASSWORD must be set` on `docker compose up` | `.env` has an empty `POSTGRES_PASSWORD`. Set it before starting.  |
| `app_startup ... db_init_failed` / container exits | PostgreSQL unreachable. Check `docker compose ps` and that `DATABASE_URL` points at `postgres:5432` inside the container. |
| `refused to start in production ...`        | Weak/missing secrets or CORS in `production`. Generate a strong `API_KEY`, set `CORS_ORIGINS`, change weak passwords. |
| `neo4j: fallback_mode` in `/internal/health` | Neo4j not running or `NEO4J_PASSWORD` mismatch. The in-memory fallback keeps the API functional. |
| `ollama: unavailable` in `/internal/health` | Ollama container down or model not pulled. Run the `ollama pull` commands in [Option A](#option-a--docker-recommended). |
| `429 Too Many Requests` on investigations  | Concurrent investigation cap (`MAX_CONCURRENT_INVESTIGATIONS`) hit. Reduce concurrency or lower the rate limit. |
| `413 Request body too large`               | Payload exceeds `MAX_REQUEST_BODY_BYTES`. Trim logs/traces or raise the limit. |
| `409` on `POST /incidents/{id}/resolve`    | Incident is already resolved. Resolutions are written once.                |
| Slow first investigation                    | Embedding model + FAISS index warm-up on first run; subsequent runs are faster. |
| Ports already in use (`5433`, `7474`, etc.)| Change the host-side port mapping in `docker-compose.yml` (e.g. `"5434:5432"`). |
| Tests fail on `faiss` import                | FAISS is macOS/Linux-only. It is optional for tests; `ENABLE_FAISS` is not required by the test env. |
### Reset local state

```bash
# Docker: remove containers and volumes (destroys persisted data)
docker compose down -v
```

---

## License

MIT

<!-- v2.1-ci-verified-source -->


<!-- v2.3-ui-ci -->


<!-- v2.4-responsive-ci -->
