"""
causal_graph.engine
===================

Causal Graph Engine.

Builds a directed graph of *events* (log lines, metric anomalies,
trace spans, findings) and uses NetworkX graph traversal to find
candidate root causes — instead of relying on linear timelines.

A node carries:

* ``id``: stable hash
* ``kind``: ``log`` | ``metric`` | ``trace`` | ``finding`` | ``service`` | ``external``
* ``label``: human-readable description
* ``timestamp``: float epoch seconds (if known)
* ``confidence``: prior confidence [0..1]
* ``source_agent``: which agent produced this node
* ``data``: arbitrary payload

An edge ``A -> B`` means *A causes / precedes / explains B*.

Root-cause candidates = source nodes (in-degree == 0) or nodes with
the highest *causal influence* (sum of weighted descendants).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import networkx as nx
from pydantic import BaseModel, Field


# ---------------------------------------------------------------
# Node / Edge models
# ---------------------------------------------------------------

class CausalNode(BaseModel):
    id: str
    kind: str
    label: str
    timestamp: Optional[float] = None
    confidence: float = 0.5
    source_agent: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


class CausalEdge(BaseModel):
    source: str
    target: str
    weight: float = 1.0
    relation: str = "causes"  # causes | precedes | explains | correlates


# ---------------------------------------------------------------
# Engine
# ---------------------------------------------------------------

class CausalGraph:
    """Wraps a NetworkX ``DiGraph`` with causal-specific helpers."""

    def __init__(self) -> None:
        self.graph: nx.DiGraph = nx.DiGraph()

    # ---------- Construction ----------

    def add_node(self, node: CausalNode) -> None:
        self.graph.add_node(
            node.id,
            kind=node.kind,
            label=node.label,
            timestamp=node.timestamp,
            confidence=node.confidence,
            source_agent=node.source_agent,
            data=node.data,
        )

    def add_edge(self, edge: CausalEdge) -> None:
        self.graph.add_edge(
            edge.source,
            edge.target,
            weight=edge.weight,
            relation=edge.relation,
        )

    def add_or_merge_node(
        self,
        node: CausalNode,
    ) -> None:
        """Add node, or merge data/confidence if it already exists."""
        if node.id in self.graph:
            existing = self.graph.nodes[node.id]
            existing["confidence"] = max(existing.get("confidence", 0.0), node.confidence)
            existing["data"].update(node.data)
        else:
            self.add_node(node)

    # ---------- Queries ----------

    def nodes(self) -> List[str]:
        return list(self.graph.nodes())

    def edges(self) -> List[Tuple[str, str]]:
        return list(self.graph.edges())

    def node_data(self, node_id: str) -> Dict[str, Any]:
        return dict(self.graph.nodes[node_id])

    def predecessors(self, node_id: str) -> List[str]:
        return list(self.graph.predecessors(node_id))

    def descendants(self, node_id: str) -> List[str]:
        return list(nx.descendants(self.graph, node_id))

    def ancestors(self, node_id: str) -> List[str]:
        return list(nx.ancestors(self.graph, node_id))

    # ---------- Root-cause traversal ----------

    def find_root_causes(self, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        Identify candidate root-cause nodes.

        Strategy:
        1. Source nodes (in-degree == 0) are the strongest candidates.
        2. If no source nodes exist (cyclic graph), pick nodes with the
           highest *causal influence* = number of descendants weighted
           by confidence.
        3. Rank candidates by a composite score:

            score = 0.4 * confidence + 0.3 * causal_influence + 0.3 * earliness

           where ``earliness`` = 1 - (relative timestamp position).
        """
        if self.graph.number_of_nodes() == 0:
            return []

        candidates: List[str] = []

        in_deg = dict(self.graph.in_degree())
        sources = [n for n, d in in_deg.items() if d == 0]
        if sources:
            candidates = sources
        else:
            # Fall back to influence ranking
            influence = {
                n: len(nx.descendants(self.graph, n)) for n in self.graph.nodes()
            }
            max_inf = max(influence.values()) or 1
            candidates = [
                n for n, inf in influence.items() if inf >= 0.5 * max_inf
            ]

        # Compute timestamps range for earliness
        ts: Dict[str, float] = {}
        for n, d in self.graph.nodes(data=True):
            t = d.get("timestamp")
            if t is not None:
                ts[n] = t
        if ts:
            tmin, tmax = min(ts.values()), max(ts.values())
            trange = (tmax - tmin) or 1.0
        else:
            tmin, tmax, trange = 0.0, 0.0, 1.0

        scored: List[Dict[str, Any]] = []
        for n in candidates:
            data = self.node_data(n)
            conf = float(data.get("confidence", 0.5))
            influence = len(nx.descendants(self.graph, n))
            max_inf = max(
                (len(nx.descendants(self.graph, x)) for x in self.graph.nodes()),
                default=1,
            ) or 1
            influence_norm = influence / max_inf
            t = ts.get(n)
            earliness = 1.0 - ((t - tmin) / trange) if t is not None else 0.5
            score = 0.4 * conf + 0.3 * influence_norm + 0.3 * earliness
            scored.append(
                {
                    "node_id": n,
                    "label": data.get("label"),
                    "kind": data.get("kind"),
                    "confidence": conf,
                    "causal_influence": influence,
                    "earliness": round(earliness, 3),
                    "score": round(score, 4),
                    "source_agent": data.get("source_agent"),
                }
            )

        scored.sort(key=lambda x: -x["score"])
        return scored[:top_k]

    def causal_chain_to(self, leaf_node_id: str) -> List[Dict[str, Any]]:
        """Return the chain of ancestors leading to ``leaf_node_id``."""
        if leaf_node_id not in self.graph:
            return []
        chain = []
        try:
            # Longest path from any source to leaf
            ancestors = nx.ancestors(self.graph, leaf_node_id)
            sources = [n for n in ancestors if self.graph.in_degree(n) == 0]
            if not sources:
                sources = list(ancestors)[:1]
            best_path: List[str] = []
            max_depth = 10
            max_paths = 256
            relevant = self.graph.subgraph(ancestors | {leaf_node_id}).copy()

            # DAGs can be solved with dynamic programming without enumerating
            # every simple path. Cyclic graphs use a bounded generator so a
            # dense incident graph cannot explode CPU/memory usage.
            if nx.is_directed_acyclic_graph(relevant):
                best_to_leaf: Dict[str, List[str]] = {leaf_node_id: [leaf_node_id]}
                for node in reversed(list(nx.topological_sort(relevant))):
                    if node == leaf_node_id:
                        continue
                    candidates = [
                        [node] + best_to_leaf[child]
                        for child in relevant.successors(node)
                        if child in best_to_leaf
                    ]
                    if candidates:
                        best_to_leaf[node] = max(candidates, key=len)
                for src in sources:
                    path = best_to_leaf.get(src, [])
                    if len(path) > len(best_path):
                        best_path = path
            else:
                examined = 0
                for src in sources:
                    try:
                        for p in nx.all_simple_paths(
                            self.graph, src, leaf_node_id, cutoff=max_depth
                        ):
                            examined += 1
                            if len(p) > len(best_path):
                                best_path = p
                            if examined >= max_paths:
                                break
                    except nx.NetworkXError:
                        continue
                    if examined >= max_paths:
                        break
            if not best_path:
                best_path = [leaf_node_id]
            for nid in best_path:
                d = self.node_data(nid)
                chain.append(
                    {
                        "node_id": nid,
                        "label": d.get("label"),
                        "kind": d.get("kind"),
                        "confidence": d.get("confidence"),
                        "source_agent": d.get("source_agent"),
                    }
                )
        except nx.NetworkXError:
            chain = [
                {
                    "node_id": leaf_node_id,
                    "label": self.node_data(leaf_node_id).get("label"),
                    "kind": self.node_data(leaf_node_id).get("kind"),
                    "confidence": self.node_data(leaf_node_id).get("confidence"),
                }
            ]
        return chain

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [
                {"id": n, **dict(d)} for n, d in self.graph.nodes(data=True)
            ],
            "edges": [
                {"source": u, "target": v, **dict(d)}
                for u, v, d in self.graph.edges(data=True)
            ],
        }


# ---------------------------------------------------------------
# Builder: from findings -> CausalGraph
# ---------------------------------------------------------------

def _stable_id(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class CausalGraphBuilder:
    """Builds a :class:`CausalGraph` from investigation findings + raw evidence."""

    graph: CausalGraph = field(default_factory=CausalGraph)

    def add_log_node(
        self,
        line: str,
        confidence: float = 0.5,
        agent: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> str:
        nid = _stable_id("log", line)
        self.graph.add_or_merge_node(
            CausalNode(
                id=nid,
                kind="log",
                label=line[:200],
                confidence=confidence,
                source_agent=agent,
                timestamp=timestamp,
                data={"raw": line},
            )
        )
        return nid

    def add_metric_node(
        self,
        metric_name: str,
        value: float,
        confidence: float = 0.5,
        agent: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> str:
        nid = _stable_id("metric", metric_name)
        self.graph.add_or_merge_node(
            CausalNode(
                id=nid,
                kind="metric",
                label=f"{metric_name}={value}",
                confidence=confidence,
                source_agent=agent,
                timestamp=timestamp,
                data={"metric": metric_name, "value": value},
            )
        )
        return nid

    def add_trace_node(
        self,
        service: str,
        operation: str,
        duration_ms: float,
        status: str = "ok",
        confidence: float = 0.5,
        agent: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> str:
        nid = _stable_id("trace", service, operation)
        self.graph.add_or_merge_node(
            CausalNode(
                id=nid,
                kind="trace",
                label=f"{service}.{operation} ({duration_ms}ms, {status})",
                confidence=confidence,
                source_agent=agent,
                timestamp=timestamp,
                data={
                    "service": service,
                    "operation": operation,
                    "duration_ms": duration_ms,
                    "status": status,
                },
            )
        )
        return nid

    def add_service_node(
        self,
        service: str,
        confidence: float = 0.5,
        agent: Optional[str] = None,
    ) -> str:
        nid = _stable_id("service", service)
        self.graph.add_or_merge_node(
            CausalNode(
                id=nid,
                kind="service",
                label=service,
                confidence=confidence,
                source_agent=agent,
                data={"service": service},
            )
        )
        return nid

    def add_finding_node(
        self,
        finding_id: str,
        description: str,
        confidence: float = 0.5,
        agent: Optional[str] = None,
        root_cause_hint: Optional[str] = None,
    ) -> str:
        nid = _stable_id("finding", finding_id)
        self.graph.add_or_merge_node(
            CausalNode(
                id=nid,
                kind="finding",
                label=description[:200],
                confidence=confidence,
                source_agent=agent,
                data={"root_cause_hint": root_cause_hint, "description": description},
            )
        )
        return nid

    def link(self, source_id: str, target_id: str, weight: float = 1.0, relation: str = "causes") -> None:
        self.graph.add_edge(
            CausalEdge(source=source_id, target=target_id, weight=weight, relation=relation)
        )

    def build_from_findings(
        self,
        findings: List[Dict[str, Any]],
        affected_services: Sequence[str] = (),
        logs: Sequence[str] = (),
        metrics: Dict[str, Any] | None = None,
        traces: Sequence[Dict[str, Any]] = (),
    ) -> CausalGraph:
        """
        Build a complete causal graph by ingesting findings + raw evidence.

        Heuristics:
        * Each finding becomes a node.
        * Each affected service becomes a node.
        * Each compressed-evidence anomaly log becomes a node.
        * Edges:
            - log -> service it mentions (causes)
            - metric anomaly -> service (causes)
            - trace error -> service (causes)
            - finding -> service it implicates (explains)
            - finding -> log it cites (explains)
        """
        metrics = metrics or {}

        # 1. Services
        svc_ids: Dict[str, str] = {}
        for svc in affected_services:
            svc_ids[svc] = self.add_service_node(svc, confidence=0.4)

        # 2. Logs (only anomalies, capped)
        log_ids: List[str] = []
        for line in list(logs)[:50]:
            nid = self.add_log_node(line, confidence=0.4)
            log_ids.append(nid)
            mentioned = [s for s in affected_services if s.lower() in line.lower()]
            for svc in mentioned:
                if svc in svc_ids:
                    self.link(nid, svc_ids[svc], weight=0.7, relation="causes")

        # 3. Metrics
        metric_ids: Dict[str, str] = {}
        for name, values in metrics.items():
            try:
                last_val = float(values[-1]) if isinstance(values, list) and values else float(values)
            except (TypeError, ValueError):
                continue
            nid = self.add_metric_node(name, last_val, confidence=0.5)
            metric_ids[name] = nid
            for svc in affected_services:
                if svc.lower() in name.lower() and svc in svc_ids:
                    self.link(nid, svc_ids[svc], weight=0.6, relation="causes")

        # 4. Traces
        trace_ids: List[str] = []
        for span in list(traces)[:30]:
            svc = span.get("service") or span.get("service_name") or "unknown"
            op = span.get("operation") or span.get("operation_name") or "unknown"
            duration = float(span.get("duration_ms", 0) or 0)
            status = str(span.get("status", "ok")).lower()
            nid = self.add_trace_node(
                svc, op, duration, status,
                confidence=0.8 if status in ("error", "failed", "timeout") else 0.4,
            )
            trace_ids.append(nid)
            if svc in svc_ids:
                self.link(nid, svc_ids[svc], weight=0.8 if status != "ok" else 0.4, relation="causes")

        # 5. Findings
        finding_ids: List[str] = []
        for f in findings:
            fid = f.get("agent_name", "") + ":" + f.get("finding_type", "") + ":" + str(f.get("description", ""))[:50]
            nid = self.add_finding_node(
                finding_id=fid,
                description=f.get("description", ""),
                confidence=float(f.get("confidence", 0.5)),
                agent=f.get("agent_name"),
                root_cause_hint=f.get("root_cause_hint"),
            )
            finding_ids.append(nid)

            # Link finding -> services it mentions
            ev = f.get("evidence", {}) or {}
            for svc in ev.get("services", []) or list(affected_services):
                if svc in svc_ids:
                    self.link(nid, svc_ids[svc], weight=0.5, relation="explains")

            # Link finding -> first anomaly log
            critical = (ev.get("critical_events") or ev.get("anomalies") or [])[:1]
            for c in critical:
                if isinstance(c, str):
                    cid = _stable_id("log", c)
                    if cid in self.graph.graph:
                        self.link(nid, cid, weight=0.4, relation="explains")

            # Link finding -> root cause hint (as virtual target node)
            hint = f.get("root_cause_hint")
            if hint:
                hint_id = _stable_id("hint", hint)
                self.graph.add_or_merge_node(
                    CausalNode(
                        id=hint_id,
                        kind="hypothesis",
                        label=hint[:200],
                        confidence=float(f.get("confidence", 0.5)),
                        source_agent=f.get("agent_name"),
                        data={"hint": hint},
                    )
                )
                self.link(nid, hint_id, weight=0.8, relation="explains")

        return self.graph
