"""Offline StateGraph orchestration with injected scripted reasoning."""

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from rca_agent.contracts import RCAResult
from rca_agent.telemetry import TelemetryTools


@dataclass(frozen=True)
class Limits:
    max_tool_calls: int
    max_iterations: int

    def __post_init__(self):
        if (
            type(self.max_tool_calls) is not int
            or type(self.max_iterations) is not int
            or min(self.max_tool_calls, self.max_iterations) < 1
        ):
            raise ValueError("explicit positive limits are required")


@dataclass(frozen=True)
class Failure:
    reason: str
    status: str = "failed"


class Reasoner(Protocol):
    def investigate(
        self, history: tuple[dict, ...], candidates: tuple[str, ...]
    ) -> dict | None: ...
    def hypothesize(self, history: tuple[dict, ...], candidates: tuple[str, ...]) -> list[dict]: ...


class State(TypedDict):
    history: list[dict]
    hypotheses: list[dict]
    calls: int
    iterations: int
    failure: str
    result: RCAResult | None
    evidence: list[str]


def run_agent(tools: TelemetryTools, reasoner: Reasoner, limits: Limits) -> dict[str, Any]:
    """No network/model calls; reasoning and tools are supplied by the caller."""
    if not isinstance(tools, TelemetryTools) or not isinstance(limits, Limits):
        raise TypeError("TelemetryTools and Limits required")
    registry = tools.registry
    # Never allow evidence that pre-dates this run to authorize a result.
    observed: dict[str, str] = {}

    def investigation(s: State):
        if s["iterations"] >= limits.max_iterations:
            return {"failure": "iteration_limit"}
        iteration = s["iterations"] + 1
        try:
            request = reasoner.investigate(
                tuple(dict(x) for x in s["history"]), tools.task.candidates
            )
        except Exception:  # noqa: BLE001 - reasoner/tool boundary returns a failure envelope
            return {"failure": "reasoner_error", "iterations": iteration}
        if request is None:
            return {"iterations": iteration, "failure": ""}
        if s["calls"] >= limits.max_tool_calls:
            return {"failure": "tool_call_limit", "iterations": iteration}
        calls = s["calls"] + 1
        try:
            if not isinstance(request, dict):
                raise TypeError("invalid query")
            kind = request.get("kind")
            method = {
                "metrics": tools.query_metrics,
                "logs": tools.query_logs,
                "traces": tools.query_traces,
            }.get(kind)
            if method is None:
                raise ValueError("unknown tool")
            args = {k: v for k, v in request.items() if k != "kind"}
            response = method(case_id=tools.task.case_id, **args)
            record = {
                "kind": kind,
                "call_id": response.call_id,
                "status": response.status,
                "arguments": args,
                "evidence_id": response.evidence_id,
            }
            if response.status == "ok" and response.evidence_id:
                observed[response.evidence_id] = response.call_id
            return {
                "iterations": iteration,
                "calls": calls,
                "history": [*s["history"], record],
                "failure": "tool_error" if response.status == "error" else "",
            }
        except Exception:  # noqa: BLE001 - reasoner/tool boundary returns a failure envelope
            return {
                "iterations": iteration,
                "calls": calls,
                "history": [*s["history"], {"status": "error", "evidence_id": None}],
                "failure": "tool_error",
            }

    def hypothesis(s: State):
        try:
            h = reasoner.hypothesize(tuple(dict(x) for x in s["history"]), tools.task.candidates)
            if not isinstance(h, list):
                raise TypeError("invalid hypotheses")
            return {"hypotheses": h}
        except Exception:  # noqa: BLE001 - reasoner/tool boundary returns a failure envelope
            return {"failure": "reasoner_error"}

    def validation(s: State):
        valid = bool(s["hypotheses"])
        used = []
        for h in s["hypotheses"]:
            if (
                not isinstance(h, dict)
                or h.get("candidate") not in tools.task.candidates
                or not isinstance(h.get("evidence_ids"), list)
                or not h["evidence_ids"]
                or not isinstance(h.get("explanation"), str)
                or not h["explanation"].strip()
            ):
                valid = False
                break
            for eid in h["evidence_ids"]:
                record = registry._records.get(eid) if isinstance(eid, str) else None
                if (
                    record is None
                    or eid not in observed
                    or record.case_id != tools.task.case_id
                    or record.call_id != observed[eid]
                    or hashlib.sha256(record.content.encode()).hexdigest() != eid
                ):
                    valid = False
                    break
                used.append(eid)
        failure = ""
        if not valid:
            failure = (
                "tool_call_limit"
                if s["calls"] >= limits.max_tool_calls
                else (
                    "iteration_limit"
                    if s["iterations"] >= limits.max_iterations
                    else "invalid_or_insufficient_evidence"
                )
            )
        return {"evidence": used if valid else [], "failure": failure}

    def ranking(s: State):
        calls = [
            {"name": x["kind"], "arguments": x["arguments"], "result_summary": x["status"]}
            for x in s["history"]
            if "kind" in x and "arguments" in x
        ]
        raw = {"case_id": tools.task.case_id, "ranking": s["hypotheses"], "tool_calls": calls}
        status, result = registry.finalize(raw, tools.task)
        return {"result": result, "failure": "" if status == "ok" else status}

    def failure(s: State):
        return {"result": None}

    graph = StateGraph(State)
    for name, node in (
        ("investigation", investigation),
        ("hypothesis", hypothesis),
        ("validation", validation),
        ("ranking", ranking),
        ("failure", failure),
    ):
        graph.add_node(name, node)
    graph.add_edge(START, "investigation")
    graph.add_conditional_edges(
        "investigation", lambda s: "failure" if s["failure"] else "hypothesis"
    )
    graph.add_conditional_edges("hypothesis", lambda s: "failure" if s["failure"] else "validation")
    graph.add_conditional_edges(
        "validation",
        lambda s: (
            "investigation"
            if s["failure"]
            and s["iterations"] < limits.max_iterations
            and s["calls"] < limits.max_tool_calls
            else ("failure" if s["failure"] else "ranking")
        ),
    )
    graph.add_conditional_edges("ranking", lambda s: "failure" if s["failure"] else END)
    graph.add_edge("failure", END)
    initial = {
        "history": [],
        "hypotheses": [],
        "calls": 0,
        "iterations": 0,
        "failure": "",
        "result": None,
        "evidence": [],
    }
    with tracing_context(enabled=False):
        state = graph.compile().invoke(
            initial, config={"recursion_limit": 5 * limits.max_iterations + 10}
        )
    return {**state, "failure_envelope": Failure(state["failure"]) if state["failure"] else None}
