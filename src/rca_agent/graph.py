"""Offline StateGraph orchestration with injected scripted reasoning."""

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

import pyarrow.compute as pc
import pyarrow.parquet as pq
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from rca_agent.contracts import RCAResult
from rca_agent.model_broker import BrokerFailure
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
class ObservationLimits:
    max_records: int
    max_chars: int

    def __post_init__(self):
        if (
            type(self.max_records) is not int
            or not 1 <= self.max_records <= 100
            or type(self.max_chars) is not int
            or self.max_chars < 1
        ):
            raise ValueError("explicit positive observation limits are required")


@dataclass(frozen=True)
class Failure:
    reason: str
    status: str = "failed"


@dataclass(frozen=True)
class CaseContext:
    case_id: str
    candidates: tuple[str, ...]
    services: tuple[str, ...]
    start: int
    end: int

    @classmethod
    def from_tools(cls, tools: TelemetryTools) -> "CaseContext":
        path = tools.case / "metrics.parquet"
        if path.is_symlink() or not path.is_file():
            raise ValueError("metrics data unavailable")
        bounds = pc.min_max(pq.read_table(path, columns=["time"])["time"]).as_py()
        if bounds["min"] is None or bounds["max"] is None:
            raise ValueError("metrics time range unavailable")
        return cls(
            tools.task.case_id,
            tools.task.candidates,
            tuple(sorted(tools.services)),
            int(bounds["min"]),
            int(bounds["max"]) + 1,
        )


@dataclass(frozen=True)
class Observation:
    case_id: str
    source: str
    call_id: str
    evidence_id: str
    arguments: dict[str, Any]
    records: tuple[dict[str, Any], ...]
    truncated: bool


class Reasoner(Protocol):
    def investigate(self, history: tuple[dict, ...], context: CaseContext) -> dict | None: ...
    def hypothesize(self, history: tuple[dict, ...], context: CaseContext) -> list[dict]: ...


class ScriptedReasoner:
    """Offline scripted queries and hypotheses; `$latest` references a successful call."""

    def __init__(self, queries: list[dict], hypotheses: list[dict]):
        self._queries = deepcopy(queries)
        self._hypotheses = deepcopy(hypotheses)

    def investigate(self, history: tuple[dict, ...], candidates: tuple[str, ...]) -> dict | None:
        return deepcopy(self._queries[len(history)]) if len(history) < len(self._queries) else None

    def hypothesize(self, history: tuple[dict, ...], candidates: tuple[str, ...]) -> list[dict]:
        latest = next(
            (
                item["evidence_id"]
                for item in reversed(history)
                if item.get("status") == "ok" and item.get("evidence_id")
            ),
            None,
        )
        hypotheses = deepcopy(self._hypotheses)
        for hypothesis in hypotheses:
            hypothesis["evidence_ids"] = [
                latest if eid == "$latest" else eid for eid in hypothesis.get("evidence_ids", [])
            ]
            for claim in hypothesis.get("claims", []):
                if claim.get("evidence_id") == "$latest":
                    claim["evidence_id"] = latest
                if claim.get("value") == "$observed":
                    obs = next(
                        (
                            item.get("observation")
                            for item in reversed(history)
                            if item.get("evidence_id") == claim.get("evidence_id")
                        ),
                        None,
                    )
                    index, field = claim.get("record_index"), claim.get("field")
                    if (
                        obs
                        and type(index) is int
                        and 0 <= index < len(obs.records)
                        and field in obs.records[index]
                    ):
                        claim["value"] = obs.records[index][field]

        return hypotheses


class State(TypedDict):
    history: list[dict]
    hypotheses: list[dict]
    calls: int
    iterations: int
    failure: str
    result: RCAResult | None
    evidence: list[str]


def run_agent(
    tools: TelemetryTools, reasoner: Reasoner, limits: Limits, observation_limits: ObservationLimits
) -> dict[str, Any]:
    """No network/model calls; reasoning and tools are supplied by the caller."""
    if (
        not isinstance(tools, TelemetryTools)
        or not isinstance(limits, Limits)
        or not isinstance(observation_limits, ObservationLimits)
    ):
        raise TypeError("TelemetryTools, Limits and ObservationLimits required")
    budget_limits = getattr(reasoner, "execution_limits", None)
    if callable(budget_limits):
        allowed_calls, allowed_iterations = budget_limits()
        if limits.max_tool_calls > allowed_calls or limits.max_iterations > allowed_iterations:
            metadata = reasoner.public_summary()
            metadata["failure"] = "execution_limit_mismatch"
            return {
                "history": [],
                "hypotheses": [],
                "calls": 0,
                "iterations": 0,
                "failure": "execution_limit_mismatch",
                "result": None,
                "evidence": [],
                "failure_envelope": Failure("execution_limit_mismatch"),
                "model_metadata": metadata,
            }
    try:
        context = CaseContext.from_tools(tools)
    except (OSError, ValueError, KeyError):
        return {
            "history": [],
            "hypotheses": [],
            "calls": 0,
            "iterations": 0,
            "failure": "case_context_error",
            "result": None,
            "evidence": [],
            "failure_envelope": Failure("case_context_error"),
            "model_metadata": (
                {**reasoner.public_summary(), "failure": "case_context_error"}
                if callable(getattr(reasoner, "public_summary", None))
                else None
            ),
        }
    registry = tools.registry
    # Never allow evidence that pre-dates this run to authorize a result.
    observed: dict[str, str] = {}
    observations: dict[str, Observation] = {}

    def investigation(s: State):
        if s["iterations"] >= limits.max_iterations:
            return {"failure": "iteration_limit"}
        iteration = s["iterations"] + 1
        try:
            request = reasoner.investigate(tuple(deepcopy(s["history"])), context)
        except BrokerFailure as exc:
            return {"failure": exc.reason, "iterations": iteration}
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
                entry = registry._records.get(response.evidence_id)
                if (
                    entry is None
                    or entry.case_id != context.case_id
                    or entry.call_id != response.call_id
                    or entry.source != kind
                    or hashlib.sha256(entry.content.encode()).hexdigest() != response.evidence_id
                ):
                    raise ValueError("invalid observation provenance")
                content = json.loads(entry.content)
                if (
                    content.get("records") != list(response.records)
                    or content.get("call_id") != response.call_id
                    or content.get("source") != kind
                    or any(
                        content.get(field) != args.get(field)
                        for field in ("service", "start", "end")
                    )
                    or content.get("truncated") != response.truncated
                ):
                    raise ValueError("observation does not match registry")
                rows = []
                for row in response.records:
                    if len(rows) >= observation_limits.max_records:
                        break
                    candidate = [*rows, row]
                    if (
                        len(json.dumps(candidate, ensure_ascii=False, allow_nan=False))
                        > observation_limits.max_chars
                    ):
                        break
                    rows = candidate
                if not rows:
                    return {
                        "iterations": iteration,
                        "calls": calls,
                        "history": [*s["history"], record],
                        "failure": "observation_limit",
                    }
                observation = Observation(
                    context.case_id,
                    kind,
                    response.call_id,
                    response.evidence_id,
                    deepcopy(args),
                    tuple(deepcopy(rows)),
                    response.truncated or len(rows) < len(response.records),
                )
                observations[response.evidence_id] = observation
                record["observation"] = observation
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
            h = reasoner.hypothesize(tuple(deepcopy(s["history"])), context)
            if not isinstance(h, list):
                raise TypeError("invalid hypotheses")
            return {"hypotheses": h}
        except BrokerFailure as exc:
            return {"failure": exc.reason}
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
            claims = h.get("claims")
            if not isinstance(claims, list) or not claims:
                valid = False
                break
            for claim in claims:
                if not isinstance(claim, dict) or set(claim) != {
                    "evidence_id",
                    "record_index",
                    "field",
                    "value",
                }:
                    valid = False
                    break
                eid = claim["evidence_id"]
                obs = observations.get(eid) if isinstance(eid, str) else None
                index = claim["record_index"]
                field = claim["field"]
                if (
                    obs is None
                    or eid not in h["evidence_ids"]
                    or type(index) is not int
                    or not 0 <= index < len(obs.records)
                    or not isinstance(field, str)
                    or field not in obs.records[index]
                    or type(obs.records[index][field]) is not type(claim["value"])
                    or obs.records[index][field] != claim["value"]
                ):
                    valid = False
                    break
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
        raw = {
            "case_id": tools.task.case_id,
            "ranking": [
                {k: h[k] for k in ("candidate", "explanation", "evidence_ids")}
                for h in s["hypotheses"]
            ],
            "tool_calls": calls,
        }
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
    begin_run = getattr(reasoner, "begin_run", None)
    end_run = getattr(reasoner, "end_run", None)
    if callable(begin_run) and (not callable(end_run) or not begin_run()):
        metadata = reasoner.public_summary()
        metadata["failure"] = "concurrency_limit"
        return {
            **initial,
            "failure": "concurrency_limit",
            "failure_envelope": Failure("concurrency_limit"),
            "model_metadata": metadata,
        }
    try:
        with tracing_context(enabled=False):
            state = graph.compile().invoke(
                initial, config={"recursion_limit": 5 * limits.max_iterations + 10}
            )
    finally:
        if callable(begin_run) and callable(end_run):
            end_run()
    metadata = None
    summary = getattr(reasoner, "public_summary", None)
    if callable(summary):
        raw_summary = summary()
        metadata = {
            k: raw_summary[k]
            for k in (
                "provider",
                "model",
                "code_version",
                "calls",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cost_usd",
                "failure",
            )
        }
        if state["failure"]:
            metadata["failure"] = state["failure"]
    return {
        **state,
        "failure_envelope": Failure(state["failure"]) if state["failure"] else None,
        "model_metadata": metadata,
    }
