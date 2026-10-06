from __future__ import annotations

import json
from pathlib import Path

SCENARIOS = Path(__file__).with_name("scenarios.json")
MIN_SCENARIOS = 50
MIN_NEGATIVE = 10

def main() -> int:
    data = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("Benchmark must be a JSON list")
    if len(data) < MIN_SCENARIOS:
        raise SystemExit(f"Benchmark has {len(data)} scenarios; minimum is {MIN_SCENARIOS}")
    ids = [item.get("id") for item in data]
    if len(ids) != len(set(ids)) or any(not x for x in ids):
        raise SystemExit("Scenario IDs must be unique and non-empty")
    required = {"id", "title", "description", "affected_services", "logs", "metrics", "ground_truth"}
    for item in data:
        missing = required - item.keys()
        if missing:
            raise SystemExit(f"{item.get('id')}: missing {sorted(missing)}")
        if not item["affected_services"] or not item["logs"]:
            raise SystemExit(f"{item['id']}: affected_services and logs are required")
        if not isinstance(item["metrics"], dict):
            raise SystemExit(f"{item['id']}: metrics must be an object")
    negatives = [x for x in data if x.get("negative") or x["ground_truth"] == "insufficient evidence"]
    if len(negatives) < MIN_NEGATIVE:
        raise SystemExit(f"Benchmark has only {len(negatives)} negative/abstention scenarios")
    categories = sorted({x["ground_truth"] for x in data})
    print(f"Benchmark OK: {len(data)} scenarios, {len(negatives)} negative/abstention, {len(categories)} RCA labels")
    print("Labels:", ", ".join(categories))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
