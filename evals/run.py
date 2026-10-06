from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

BASE_URL = os.environ.get("PRISM_EVAL_BASE_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("PRISM_EVAL_API_KEY", "")
SCENARIOS = Path(__file__).with_name("scenarios.json")


def post(path: str, payload: dict) -> dict:
    headers = {"Content-Type": "application/json", "Idempotency-Key": f"eval-{uuid.uuid4().hex}"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    req = Request(BASE_URL + path, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urlopen(req, timeout=180) as response:
        return json.loads(response.read().decode())


def main() -> int:
    scenarios = json.loads(SCENARIOS.read_text())
    results = []
    for scenario in scenarios:
        payload = {k: v for k, v in scenario.items() if k not in {"id", "ground_truth"}}
        try:
            result = post("/incidents/investigate", payload)
            results.append({"id": scenario["id"], "ground_truth": scenario["ground_truth"], "response": result})
            print(f"{scenario['id']}: received response")
        except Exception as exc:
            results.append({"id": scenario["id"], "error": type(exc).__name__})
            print(f"{scenario['id']}: failed ({type(exc).__name__})")
    output = Path("eval-results.json")
    output.write_text(json.dumps(results, indent=2, default=str))

    from evaluate import evaluate
    scenarios_data = json.loads(SCENARIOS.read_text())
    metrics = evaluate(results, scenarios_data)
    metrics_path = Path("eval-metrics.json")
    metrics_path.write_text(json.dumps(metrics, indent=2))
    print(f"Wrote {output}")
    print(f"Wrote {metrics_path}")
    print(json.dumps(metrics, indent=2))
    return 0 if all("response" in item for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
