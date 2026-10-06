from __future__ import annotations

import os

# Keep the scenario deterministic and self-contained. These values are only
# used inside the test process and are never production credentials.
os.environ["PRISM_BOOTSTRAP_EMAIL"] = "owner@waypoint.test"
os.environ["PRISM_BOOTSTRAP_PASSWORD"] = "Waypoint-test-password-2026!"
os.environ["PRISM_BOOTSTRAP_TENANT_NAME"] = "WayPoint Checkout Test"


def test_checkout_latency_end_to_end():
    """Exercise login -> incident -> investigation -> RCA -> confirmation."""
    from fastapi.testclient import TestClient
    from main import app

    with TestClient(app) as client:
        register = client.post(
            "/auth/register",
            json={
                "email": "owner@waypoint.test",
                "password": "Waypoint-test-password-2026!",
                "workspace_name": "WayPoint Checkout Test",
            },
        )
        assert register.status_code == 201, register.text
        assert register.cookies.get("waypoint_session")

        payload = {
            "title": "Checkout API latency increased after deployment",
            "severity": "P1",
            "incident_type": "latency",
            "affected_services": ["checkout-api", "payment-svc"],
            "raw_logs": [
                "2026-09-28T12:00:00Z INFO deploy checkout-api version=2026.09.28.4",
                "2026-09-28T12:01:03Z WARN checkout-api p99=4800ms upstream=payment-svc",
                "2026-09-28T12:01:05Z ERROR checkout-api timeout calling payment-svc /v1/charge",
                "2026-09-28T12:01:08Z WARN checkout-api retry budget exhausted",
            ],
            "metrics": {
                "checkout_api_p99_latency_ms": [120, 125, 130, 128, 4700, 5100],
                "checkout_api_error_rate": [0.01, 0.01, 0.02, 0.02, 0.19, 0.24],
            },
            "traces": [
                {"service": "checkout-api", "operation": "checkout", "duration_ms": 5100, "status": "error"},
                {"service": "payment-svc", "operation": "charge", "duration_ms": 4900, "status": "error"},
            ],
            "topology": {
                "nodes": [{"id": "checkout-api"}, {"id": "payment-svc"}],
                "edges": [{"source": "checkout-api", "target": "payment-svc"}],
            },
        }

        investigation = client.post("/incidents/investigate", json=payload)
        assert investigation.status_code == 200, investigation.text
        result = investigation.json()
        incident_id = result["incident_id"]
        assert incident_id
        assert result.get("agents_used")
        assert result.get("root_cause")

        detail = client.get(f"/incidents/{incident_id}")
        assert detail.status_code == 200, detail.text
        incident = detail.json()
        assert incident["id"] == incident_id

        resolve = client.post(
            f"/incidents/{incident_id}/resolve",
            json={
                "action": "Rolled back checkout-api deployment",
                "steps": ["rollback to 2026.09.28.3", "verify p99 and error rate recovered"],
                "verified": True,
            },
        )
        assert resolve.status_code == 200, resolve.text
        assert resolve.json()["action"] == "Rolled back checkout-api deployment"

        me = client.get("/auth/me")
        assert me.status_code == 200, me.text
        assert me.json()["tenant"]["name"] == "WayPoint Checkout Test"

        logout = client.post("/auth/logout")
        assert logout.status_code == 200

        after_logout = client.get("/auth/me")
        assert after_logout.status_code == 401
