"""
investigation.tree
==================

Dynamic Investigation Tree.

Each *node* in the tree represents an investigation *step*.  Steps
are **not** hard-coded — they are generated at runtime based on the
incident type, available evidence, and the output of previous steps.

A step has:

* ``id``: stable identifier
* ``name``: human-readable label
* ``kind``: one of ``"gather" | "analyze" | "decide"``
* ``inputs``: list of context keys this step needs
* ``agent``: optional agent name to delegate to
* ``children``: list of child step ids (mutable — builder can append)

The :class:`InvestigationTreeBuilder` constructs the tree from an
incident context, then :func:`run_tree` executes it depth-first,
feeding each step's output back into the shared context so later
steps can use it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
from typing import Any, Dict, List, Optional

import networkx as nx
from pydantic import BaseModel, Field

from config.logging import get_logger

from investigation.state import InvestigationState
from copy import deepcopy
logger = get_logger(__name__)


# ---------------------------------------------------------------
# Step models
# ---------------------------------------------------------------

class StepSpec(BaseModel):
    """Specification of a single investigation step."""

    id: str
    name: str
    kind: str = Field(default="analyze")  # gather | analyze | decide
    inputs: List[str] = Field(default_factory=list)
    agent: Optional[str] = None
    description: str = ""
    children: List[str] = Field(default_factory=list)


class InvestigationTree(BaseModel):
    """The runtime-built investigation tree."""

    incident_id: str
    incident_type: Optional[str] = None
    steps: Dict[str, StepSpec] = Field(default_factory=dict)
    root_step: Optional[str] = None
    edges: List[Dict[str, str]] = Field(default_factory=list)

    def to_networkx(self) -> nx.DiGraph:
        g = nx.DiGraph()
        for sid, step in self.steps.items():
            g.add_node(sid, **step.model_dump())
        for e in self.edges:
            g.add_edge(e["source"], e["target"])
        return g

    def topological_order(self) -> List[str]:
        g = self.to_networkx()
        try:
            return list(nx.topological_sort(g))
        except nx.NetworkXUnfeasible:
            logger.warning("investigation_tree_has_cycle")
            return list(self.steps.keys())


# ---------------------------------------------------------------
# Builder
# ---------------------------------------------------------------

class InvestigationTreeBuilder:
    """
    Builds a different investigation tree per incident.

    Construction rules:

    * Always start with a "gather_evidence" root step.
    * Branch out into one analyze-step per available evidence type
      (logs, metrics, traces, topology).
    * Add a "consult_history" step if memory is available.
    * Add a "consult_kg" step if affected_services are known.
    * Add an "llm_deep_dive" step only if the cheap agents did not
      already produce a high-confidence root cause.
    * Finish with a "decide_root_cause" step.
    """

    def __init__(self) -> None:
        self.tree: Optional[InvestigationTree] = None

    def build(
        self,
        incident_id: str,
        incident_type: Optional[str],
        context: Dict[str, Any],
    ) -> InvestigationTree:
        tree = InvestigationTree(incident_id=incident_id, incident_type=incident_type)
        self.tree = tree

        # Root
        root = StepSpec(
            id="root",
            name="Gather evidence",
            kind="gather",
            inputs=["logs", "metrics", "traces", "topology"],
            description="Collect all available incident evidence.",
        )
        tree.steps[root.id] = root
        tree.root_step = root.id

        # Analyze steps
        step_id = 0

        def _next_id(prefix: str) -> str:
            nonlocal step_id
            step_id += 1
            return f"{prefix}_{step_id}"

        if context.get("logs"):
            sid = _next_id("logs")
            tree.steps[sid] = StepSpec(
                id=sid,
                name="Analyze logs",
                kind="analyze",
                inputs=["logs"],
                agent="log_analyzer",
                description="Run log-based anomaly detection.",
            )
            tree.edges.append({"source": root.id, "target": sid})
            root.children.append(sid)

            # Rule-based baseline runs alongside
            rid = _next_id("rules")
            tree.steps[rid] = StepSpec(
                id=rid,
                name="Rule-based triage",
                kind="analyze",
                inputs=["logs"],
                agent="rule_based_analyzer",
                description="Deterministic rule-based triage.",
            )
            tree.edges.append({"source": root.id, "target": rid})
            root.children.append(rid)

        if context.get("metrics"):
            sid = _next_id("metrics")
            tree.steps[sid] = StepSpec(
                id=sid,
                name="Analyze metrics",
                kind="analyze",
                inputs=["metrics"],
                agent="metric_analyzer",
                description="Detect metric anomalies.",
            )
            tree.edges.append({"source": root.id, "target": sid})
            root.children.append(sid)

        if context.get("traces"):
            sid = _next_id("traces")
            tree.steps[sid] = StepSpec(
                id=sid,
                name="Analyze traces",
                kind="analyze",
                inputs=["traces"],
                agent="trace_analyzer",
                description="Localize slow / failed spans.",
            )
            tree.edges.append({"source": root.id, "target": sid})
            root.children.append(sid)

        if context.get("topology"):
            sid = _next_id("topology")
            tree.steps[sid] = StepSpec(
                id=sid,
                name="Analyze topology",
                kind="analyze",
                inputs=["topology"],
                agent="topology_analyzer",
                description="Compute blast radius & SPOF.",
            )
            tree.edges.append({"source": root.id, "target": sid})
            root.children.append(sid)

        if context.get("affected_services"):
            hid = _next_id("history")
            tree.steps[hid] = StepSpec(
                id=hid,
                name="Consult incident memory",
                kind="analyze",
                inputs=["logs", "affected_services"],
                agent="historical_analyzer",
                description="Retrieve similar past incidents.",
            )
            tree.edges.append({"source": root.id, "target": hid})
            root.children.append(hid)

            kid = _next_id("kg")
            tree.steps[kid] = StepSpec(
                id=kid,
                name="Query knowledge graph",
                kind="analyze",
                inputs=["affected_services"],
                agent="knowledge_graph_analyzer",
                description="Look up service dependencies & recent changes.",
            )
            tree.edges.append({"source": root.id, "target": kid})
            root.children.append(kid)

        # Deep LLM dive (conditional — added but only executed if cheap agents fail)
        lid = _next_id("llm")
        tree.steps[lid] = StepSpec(
            id=lid,
            name="LLM deep dive (conditional)",
            kind="analyze",
            inputs=["logs"],
            agent="llm_analyzer",
            description="Run only if cheap agents produced confidence < threshold.",
        )
        # LLM dive runs after all other analyze steps
        for child in root.children:
            tree.edges.append({"source": child, "target": lid})

        # Decide
        did = "decide"
        tree.steps[did] = StepSpec(
            id=did,
            name="Decide root cause",
            kind="decide",
            inputs=[],
            description="Consensus engine aggregates all findings.",
        )
        tree.edges.append({"source": lid, "target": did})

        return tree


# ---------------------------------------------------------------
# Runner
# ---------------------------------------------------------------

@dataclass
class TreeRunResult:
    """Output of :func:`run_tree`."""

    tree: InvestigationTree
    findings: List[Dict[str, Any]] = field(default_factory=list)
    agents_used: List[str] = field(default_factory=list)
    reliability_scores: Dict[str, float] = field(default_factory=dict)
    agents_skipped: List[Dict[str, str]] = field(default_factory=list)
    duration_s: float = 0.0
    final_state: Optional[InvestigationState] = None
    stopping_reason: str = ""
    iterations: int = 0
    consensus_result: Optional[Any] = None


async def run_tree(
    incident_id: str,
    incident_type: Optional[str],
    context: Dict[str, Any],
    *,
    enable_fallback: bool = False,
) -> TreeRunResult:
    """
    Build an investigation tree and execute actions step-by-step using single-action MDV selection.
    """
    import time as _time
    from config.settings import settings
    from orchestrator.engine import execute_action
    from orchestrator.selector import select_agents, AgentSelection
    from investigation.state import InvestigationState
    from investigation.diagnostic_gain import compute_adg
    from investigation.experiment_logger import log_iteration


    start = _time.perf_counter()
    builder = InvestigationTreeBuilder()
    tree = builder.build(incident_id, incident_type, context)

    # Initialize InvestigationState
    state = InvestigationState(
        hypotheses=dict(context.get("hypotheses", {})),
        metadata={
            "incident_id": incident_id,
            "incident_type": incident_type or context.get("incident_type", "default"),
            "evidence_present": {
                k: bool(context.get(k)) for k in ["logs", "metrics", "traces", "topology"]
            },
            "findings": [],
        },
    )
    # Seed default uniform hypotheses if none provided
    if not state.hypotheses:
        state.hypotheses = {
            "network_failure": 0.25,
            "database_failure": 0.25,
            "application_failure": 0.25,
            "resource_exhaustion": 0.25,
        }
        # Recompute metrics after seeding
        state.recompute_metrics()
    else:
        state.recompute_metrics()

    all_findings: List[Dict[str, Any]] = []
    all_agents_used: List[str] = []
    all_reliability: Dict[str, float] = {}
    all_skipped: List[Dict[str, str]] = []
    executed_agent_names: List[str] = []
    iteration = 0
    inc_context = incident_type or context.get("incident_type") or "default"

    stopping_reason = "max_steps_or_no_candidates"

    while True:
        # Select & rank remaining actions using current state
        selection: AgentSelection = await select_agents(
            incident_type=incident_type,
            context=context,
            state=state,
            executed_actions=executed_agent_names,
        )

        all_reliability.update(selection.reliability_scores)
        for name, reason in selection.skipped:
            if not any(s.get("agent") == name for s in all_skipped):
                all_skipped.append({"agent": name, "reason": reason})

        if not selection.remaining_actions:
            stopping_reason = "no_remaining_candidates"
            break

        # The LLM is a reasoning layer over specialist evidence, not a
        # parallel evidence source. If a deterministic specialist still has
        # sufficient MDV, execute it before the LLM. The LLM receives the
        # accumulated evidence package through context["_evidence_package"].
        best_action = selection.remaining_actions[0]
        if best_action["agent_name"] == "llm_analyzer":
            specialist_actions = [
                action
                for action in selection.remaining_actions
                if action["agent_name"] != "llm_analyzer"
                and float(action.get("mdv", 0.0)) >= settings.MDV_THRESHOLD
            ]
            if specialist_actions:
                best_action = specialist_actions[0]

        best_mdv = best_action.get("mdv", 0.0)

        if best_mdv < settings.MDV_THRESHOLD:
            stopping_reason = f"mdv_below_threshold ({best_mdv:.3f} < {settings.MDV_THRESHOLD:.3f})"
            break

        if iteration >= settings.MAX_INVESTIGATION_STEPS:
            stopping_reason = f"max_investigation_steps_reached ({iteration})"
            break

        target_agent = best_action["agent_name"]

        # Snapshot Pre-State
        # Snapshot Pre-State using deepcopy to capture full state
        pre_state = deepcopy(state)

        # Execute ONE best action
        try:
            rel = best_action.get("reliability_score", 0.5)
            finding_payload = await asyncio.wait_for(
                execute_action(
                    agent_name=target_agent,
                    context=context,
                    incident_id=incident_id,
                    reliability=rel,
                ),
                timeout=max(1.0, float(settings.agent_timeout_seconds)),
            )
            finding_dict = finding_payload.to_dict()
            status = "success"
            err_msg = None
        except Exception as exc:
            logger.warning(f"action_execution_failed: agent={target_agent}, error={repr(exc)}")
            finding_dict = {
                "agent_name": target_agent,
                "finding_type": "error",
                "description": f"Execution error: {exc}",
                "confidence": 0.0,
                "evidence": {},
                "metadata": {"error": str(exc)},
            }
            status = "failed"
            err_msg = str(exc)

        # Update tracking lists
        all_findings.append(finding_dict)
        all_agents_used.append(target_agent)
        executed_agent_names.append(target_agent)

        # Make specialist evidence available to the LLM reasoning agent on
        # the real dynamic-tree path. Bound the package so a noisy log cannot
        # turn into an unbounded prompt.
        if target_agent != "llm_analyzer":
            package_parts = []
            for finding in all_findings:
                package_parts.append(
                    f"Agent: {finding.get('agent_name', 'unknown')}\\n"
                    f"Type: {finding.get('finding_type', 'unknown')}\\n"
                    f"Confidence: {float(finding.get('confidence', 0.0)):.3f}\\n"
                    f"Finding: {str(finding.get('description', ''))[:1200]}\\n"
                    f"Root-cause hint: {str(finding.get('root_cause_hint') or 'none')[:500]}\\n"
                    f"Evidence: {str(finding.get('evidence') or {})[:1800]}"
                )
            context["_evidence_package"] = "\\n\\n".join(package_parts)[:12000]

        # Update remaining_actions in state and record executed action
        state.pop_action(target_agent, finding_dict)

        if status == "success":
            state.update_from_finding(finding_dict, context)

        # Calculate REAL ADG between pre-state and post-state
        adg_result = compute_adg(pre_state, state)
        # Action-effectiveness EMA is a trusted learning signal.  It must
        # only be graded against confirmed ground truth (see
        # learning.continuous_learning.learn_from_incident), never sampled
        # from an unverified investigation step, so no write happens here.

        iteration += 1

        # Record action history & explainability log (Priority 13)
        log_iteration(
            {
                "incident_id": incident_id,
                "iteration": iteration,
                "action": target_agent,
                "agent": target_agent,
                "mdv": best_mdv,
                "mdv_components": {
                    "discrimination_score": best_action.get("discrimination_score", 0.0),
                    "expected_uncertainty_reduction": best_action.get("expected_uncertainty_reduction", 0.0),
                    "reliability_score": best_action.get("reliability_score", 0.5),
                    "execution_cost": best_action.get("execution_cost", 0.0),
                },
                "pre_state": {
                    "uncertainty": pre_state.hypothesis_uncertainty,
                    "disagreement": pre_state.agent_disagreement,
                    "coverage": pre_state.evidence_coverage,
                    "causal_consistency": pre_state.causal_consistency,
                },
                "execution_result": finding_dict,
                "post_state": {
                    "uncertainty": state.hypothesis_uncertainty,
                    "disagreement": state.agent_disagreement,
                    "coverage": state.evidence_coverage,
                    "causal_consistency": state.causal_consistency,
                },
                "adg": adg_result.total_gain if adg_result else 0.0,
                "status": status,
                "error": err_msg,
                "stopping_reason": "",
            }
        )

    # Preserve a deterministic evidence baseline for log-backed incidents.
    # Adaptive MDV may otherwise stop after an agent that produced no explicit
    # hypothesis, leaving the investigation with no gradeable provenance.
    # The rule engine is deterministic and is executed at most once.
    has_hint = any(
        finding.get("root_cause_hint") or (finding.get("hypotheses") or [])
        for finding in all_findings
    )
    if context.get("logs") and not has_hint and "rule_based_analyzer" not in executed_agent_names:
        try:
            baseline = await execute_action(
                agent_name="rule_based_analyzer",
                context=context,
                incident_id=incident_id,
            )
            baseline_dict = baseline.to_dict()
            all_findings.append(baseline_dict)
            all_agents_used.append("rule_based_analyzer")
            executed_agent_names.append("rule_based_analyzer")
        except Exception as exc:
            logger.warning("deterministic_baseline_failed", error=repr(exc))

    # Isolated fallback path: only executed if enable_fallback is explicitly set to True
    if not all_findings and enable_fallback:
        from orchestrator.engine import execute_multi_agent_fallback
        result = await execute_multi_agent_fallback(
            incident_id=incident_id,
            incident_type=incident_type,
            context=context,
        )
        all_findings = [f.to_dict() for f in result.findings]
        all_agents_used = result.agents_used
        all_reliability = result.reliability_scores
        all_skipped = result.agents_skipped


    duration = _time.perf_counter() - start

    # Reach consensus across accumulated single-action findings
    from consensus.engine import reach_consensus

    consensus_result = await reach_consensus(
        findings=all_findings,
        reliability_scores=all_reliability,
        incident_id=incident_id,
        incident_type=incident_type,
        quorum_threshold=settings.CONSENSUS_QUORUM_THRESHOLD if hasattr(settings, "CONSENSUS_QUORUM_THRESHOLD") else 0.0,
    )

    # Reliability is trusted learning state and may only be graded through
    # the confirmed-resolve channel (learning.continuous_learning) against
    # validated ground truth.  A raw ``context["ground_truth"]`` marker is
    # NOT a valid learning channel and must never mutate the reliability
    # store mid-investigation.
    ground_truth = context.get("ground_truth") or context.get("confirmed_root_cause")
    if ground_truth:
        logger.warning(
            "reliability_update_rejected_invalid_learning_channel",
            extra={
                "incident_id": incident_id,
                "hint": (
                    "Supplying ground_truth via investigation context is not a "
                    "supported learning channel; use POST /incidents/{id}/resolve."
                ),
            },
        )

    return TreeRunResult(
        tree=tree,
        findings=all_findings,
        agents_used=all_agents_used,
        reliability_scores=all_reliability,
        agents_skipped=all_skipped,
        duration_s=duration,
        final_state=deepcopy(state),
        stopping_reason=stopping_reason,
        iterations=iteration,
        consensus_result=consensus_result,
    )

