from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SCENARIOS = ROOT / "scenarios.json"

def _norm(value: Any) -> str:
    return " ".join(str(value or "").lower().replace("_", " ").split())

def _response_root(response: dict) -> str:
    root = response.get("root_cause") or {}
    if isinstance(root, dict):
        return str(root.get("root_cause") or "")
    return str(root)

def _candidates(response: dict) -> list[str]:
    root = response.get("root_cause") or {}
    values = [_response_root(response)]
    if isinstance(root, dict):
        for item in root.get("alternatives") or []:
            if isinstance(item, dict):
                values.append(str(item.get("cause") or ""))
    return [_norm(v) for v in values if _norm(v)]

def _matches(value: str, aliases: list[str]) -> bool:
    value = _norm(value)
    return any(_norm(alias) in value or value in _norm(alias) for alias in aliases if alias)

def evaluate(results: list[dict], scenarios: list[dict]) -> dict[str, Any]:
    by_id = {x["id"]: x for x in scenarios}
    rows = []
    for item in results:
        sid = item.get("id")
        scenario = by_id.get(sid)
        response = item.get("response")
        if not scenario:
            continue
        if not isinstance(response, dict):
            rows.append({"id": sid, "error": True, "top1": False, "top3": False, "abstention": False, "evidence": False, "latency": None})
            continue
        aliases = [scenario["ground_truth"], *(scenario.get("aliases") or [])]
        candidates = _candidates(response)
        root = candidates[0] if candidates else ""
        expected_abstention = scenario["ground_truth"] == "insufficient evidence"
        abstained = root in {"undetermined", "insufficient evidence", "insufficient evidence to determine root cause"}
        explanation = response.get("explanation") or {}
        evidence = explanation.get("evidence_used") if isinstance(explanation, dict) else []
        rows.append({
            "id": sid,
            "error": False,
            "top1": _matches(root, aliases),
            "top3": any(_matches(c, aliases) for c in candidates[:3]),
            "abstention": abstained == expected_abstention,
            "evidence": bool(evidence) if not expected_abstention else True,
            "latency": response.get("duration_seconds"),
        })

    total = len(rows) or 1
    successful = [r for r in rows if not r["error"]]
    latencies = [float(r["latency"]) for r in rows if isinstance(r["latency"], (int, float))]
    return {
        "scenarios_evaluated": len(rows),
        "top1_accuracy": round(sum(r["top1"] for r in rows) / total, 4),
        "top3_recall": round(sum(r["top3"] for r in rows) / total, 4),
        "abstention_accuracy": round(sum(r["abstention"] for r in rows) / total, 4),
        "evidence_attribution_coverage": round(sum(r["evidence"] for r in successful) / max(1, len(successful)), 4),
        "error_rate": round(sum(r["error"] for r in rows) / total, 4),
        "latency_p50_seconds": round(statistics.median(latencies), 3) if latencies else None,
        "latency_p95_seconds": round(sorted(latencies)[max(0, math.ceil(len(latencies) * 0.95) - 1)], 3) if latencies else None,
    }

def main(path: str) -> int:
    results = json.loads(Path(path).read_text(encoding="utf-8"))
    scenarios = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    metrics = evaluate(results, scenarios)
    print(json.dumps(metrics, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "eval-results.json"))
