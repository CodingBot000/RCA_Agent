import pytest

pytest_plugins = ("test_telemetry",)

from rca_agent.graph import Limits, ObservationLimits
from rca_agent.graph import run_agent as actual_run_agent


def run_agent(tools, reasoner, limits):
    return actual_run_agent(tools, reasoner, limits, ObservationLimits(100, 10000))


class Scripted:
    def __init__(self, mode="success"):
        self.mode = mode

    def investigate(self, history, candidates):
        if self.mode == "unknown_tool":
            return {"kind": "evil"}
        return {
            "kind": "metrics",
            "service": "api",
            "start": 100,
            "end": 103,
            "limit": 2,
            "indicator": "api_cpu",
        }

    def hypothesize(self, history, candidates):
        if self.mode == "forged" or self.mode == "retry" and len(history) == 1:
            evidence = "not-real"
        else:
            evidence = history[-1].get("evidence_id")
        claims = []
        if (
            history
            and history[-1].get("observation") is not None
            and evidence == history[-1].get("evidence_id")
        ):
            obs = history[-1]["observation"]
            claims = [
                {
                    "evidence_id": evidence,
                    "record_index": 0,
                    "field": "api_cpu",
                    "value": obs.records[0]["api_cpu"],
                }
            ]
        return [
            {
                "candidate": "api_cpu",
                "explanation": "scripted",
                "evidence_ids": [evidence],
                "claims": claims,
            }
        ]


def test_graph_success(tools):
    result = run_agent(tools, Scripted(), Limits(2, 2))
    assert result["result"].ranking[0].candidate == "api_cpu"
    assert result["calls"] == 1 and result["failure_envelope"] is None


def test_graph_reinvestigates_bad_evidence(tools):
    result = run_agent(tools, Scripted("retry"), Limits(2, 2))
    assert result["result"] is not None and result["calls"] == 2


def test_graph_rejects_forged_evidence(tools):
    result = run_agent(tools, Scripted("forged"), Limits(2, 2))
    assert result["result"] is None and result["failure_envelope"] is not None
    assert result["calls"] == 2


def test_graph_counts_invalid_tool_attempt(tools):
    result = run_agent(tools, Scripted("unknown_tool"), Limits(2, 2))
    assert result["calls"] == 1 and result["failure"] == "tool_error"


def test_limits_no_implicit_policy():
    with pytest.raises(ValueError):
        Limits(0, 1)


def test_call_budget_exhaustion(tools):
    result = run_agent(tools, Scripted("forged"), Limits(1, 5))
    assert result["failure"] == "tool_call_limit"
    assert result["calls"] == 1


def test_iteration_budget_exhaustion(tools):
    result = run_agent(tools, Scripted("forged"), Limits(5, 1))
    assert result["failure"] == "iteration_limit"
    assert result["iterations"] == 1


def test_preexisting_forged_record_is_not_accepted(tools):
    from rca_agent.contracts import ContractError
    from rca_agent.telemetry import EvidenceRecord

    record = EvidenceRecord("fake", tools.task.case_id, "made-up", "metrics", "invented")
    with pytest.raises(ContractError):
        tools.registry.register(record, status="ok")


def test_success_contains_tool_call_history(tools):
    result = run_agent(tools, Scripted(), Limits(1, 1))
    assert result["result"].tool_calls[0].name == "metrics"
    assert result["history"][0]["arguments"]["service"] == "api"


def test_external_tracing_is_disabled(tools, monkeypatch):
    from langsmith.run_helpers import get_tracing_context

    class TracingCheck(Scripted):
        def investigate(self, history, candidates):
            assert get_tracing_context()["enabled"] is False
            return super().investigate(history, candidates)

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    result = run_agent(tools, TracingCheck(), Limits(1, 1))
    assert result["result"] is not None


def test_tool_error_status_is_failure(tools):
    class InvalidService(Scripted):
        def investigate(self, history, candidates):
            request = super().investigate(history, candidates)
            request["service"] = "unmapped"
            return request

    result = run_agent(tools, InvalidService(), Limits(2, 2))
    assert result["failure"] == "tool_error" and result["calls"] == 1
    assert result["history"][0]["status"] == "error"


def test_evidence_from_before_run_cannot_authorize_result(tools):
    old = tools.query_metrics(tools.task.case_id, "api", 100, 103, 1, "api_cpu")

    class OldEvidence(Scripted):
        def hypothesize(self, history, candidates):
            return [
                {"candidate": "api_cpu", "explanation": "old", "evidence_ids": [old.evidence_id]}
            ]

    result = run_agent(tools, OldEvidence(), Limits(1, 1))
    assert result["result"] is None and result["failure_envelope"] is not None


def test_missing_telemetry_never_creates_evidence(tools):
    (tools.case / "metrics.parquet").unlink()
    result = run_agent(tools, Scripted(), Limits(2, 2))
    assert result["result"] is None and result["calls"] == 0
    assert result["failure"] == "case_context_error"


def test_unavailable_logs_are_counted_and_do_not_make_evidence(tools):
    class MissingLogs(Scripted):
        def investigate(self, history, candidates):
            return {"kind": "logs", "service": "api", "start": 100, "end": 103, "limit": 2}

    (tools.case / "logs.parquet").unlink()
    result = run_agent(tools, MissingLogs(), Limits(1, 1))
    assert result["calls"] == 1 and result["history"][0]["status"] == "unavailable"
    assert result["result"] is None and not tools.registry._records


def test_builtin_scripted_reasoner(tools):
    from rca_agent.graph import ScriptedReasoner

    reasoner = ScriptedReasoner(
        [
            {
                "kind": "metrics",
                "service": "api",
                "start": 100,
                "end": 103,
                "limit": 2,
                "indicator": "api_cpu",
            }
        ],
        [
            {
                "candidate": "api_cpu",
                "explanation": "synthetic",
                "evidence_ids": ["$latest"],
                "claims": [
                    {
                        "evidence_id": "$latest",
                        "record_index": 0,
                        "field": "api_cpu",
                        "value": "$observed",
                    }
                ],
            }
        ],
    )
    result = run_agent(tools, reasoner, Limits(1, 1))
    assert result["result"] is not None and result["calls"] == 1


def test_forged_registry_record_does_not_match_observed_call(tools):
    import hashlib

    from rca_agent.telemetry import EvidenceRecord

    class ForgingReasoner(Scripted):
        def hypothesize(self, history, candidates):
            content = "fabricated"
            evidence_id = hashlib.sha256(content.encode()).hexdigest()
            forged = EvidenceRecord(
                evidence_id, tools.task.case_id, "fake-call", "metrics", content
            )
            tools.registry.register(forged, status="ok", _proof=tools.registry._capability)
            return [{"candidate": "api_cpu", "explanation": "fake", "evidence_ids": [evidence_id]}]

    state = run_agent(tools, ForgingReasoner(), Limits(1, 1))
    assert state["result"] is None and state["failure_envelope"] is not None


def test_case_context_uses_actual_case_bounds(tools):
    from rca_agent.graph import CaseContext

    context = CaseContext.from_tools(tools)
    assert context.case_id == tools.task.case_id
    assert context.candidates == ("api_cpu",)
    assert context.services == ("api",)
    assert (context.start, context.end) == (100, 103)


def test_limits_are_explicit_and_observation_is_bounded(tools):
    from rca_agent.graph import ObservationLimits

    class Inspect(Scripted):
        def hypothesize(self, history, context):
            assert context.case_id == tools.task.case_id
            assert (context.start, context.end) == (100, 103)
            observation = history[-1]["observation"]
            assert len(observation.records) == 1 and observation.truncated
            return super().hypothesize(history, context)

    state = actual_run_agent(tools, Inspect(), Limits(1, 1), ObservationLimits(1, 1000))
    assert state["result"] is not None
    with pytest.raises(ValueError):
        ObservationLimits(101, 1)
    with pytest.raises(ValueError):
        ObservationLimits(1, 0)


def test_observation_that_cannot_fit_fails(tools):
    from rca_agent.graph import ObservationLimits

    state = actual_run_agent(tools, Scripted(), Limits(1, 1), ObservationLimits(1, 1))
    assert state["failure"] == "observation_limit" and state["result"] is None


def test_id_only_or_false_observation_claim_is_rejected(tools):
    class FalseClaim(Scripted):
        def hypothesize(self, history, context):
            return [
                {
                    "candidate": "api_cpu",
                    "explanation": "unfounded",
                    "evidence_ids": [history[-1]["evidence_id"]],
                    "claims": [
                        {
                            "evidence_id": history[-1]["evidence_id"],
                            "record_index": 0,
                            "field": "api_cpu",
                            "value": 999.0,
                        }
                    ],
                }
            ]

    state = run_agent(tools, FalseClaim(), Limits(1, 1))
    assert state["result"] is None and state["failure_envelope"] is not None


def test_reasoner_changes_hypothesis_when_observed_values_change(tmp_path):
    import json

    import pyarrow as pa
    import pyarrow.parquet as pq

    from rca_agent.graph import ObservationLimits
    from rca_agent.telemetry import EvidenceRegistry, TelemetryTools

    class ValueReasoner(Scripted):
        def hypothesize(self, history, context):
            obs = history[-1]["observation"]
            value = obs.records[0]["api_cpu"]
            return [
                {
                    "candidate": "api_cpu" if value > 5 else "api_mem",
                    "explanation": "synthetic comparison",
                    "evidence_ids": [obs.evidence_id],
                    "claims": [
                        {
                            "evidence_id": obs.evidence_id,
                            "record_index": 0,
                            "field": "api_cpu",
                            "value": value,
                        }
                    ],
                }
            ]

    outcomes = []
    for i, value in enumerate((1.0, 9.0)):
        root = tmp_path / str(i)
        cid = "re2-ob-v2-" + "a" * 32
        case = root / cid
        case.mkdir(parents=True)
        (case / "task.json").write_text(
            json.dumps(
                {
                    "case_id": cid,
                    "candidates": ["api_cpu", "api_mem"],
                    "evidence": [{"id": "base", "content": "metadata"}],
                }
            )
        )
        pq.write_table(
            pa.table(
                {"time": [100, 101, 102], "api_cpu": [value, 2.0, 3.0], "api_mem": [2.0, 2.0, 2.0]}
            ),
            case / "metrics.parquet",
        )
        tool = TelemetryTools(root, cid, EvidenceRegistry(cid))
        result = actual_run_agent(tool, ValueReasoner(), Limits(1, 1), ObservationLimits(2, 1000))
        outcomes.append(result["result"].ranking[0].candidate)
    assert outcomes == ["api_mem", "api_cpu"]


def test_id_only_hypothesis_is_not_sufficient(tools):
    from rca_agent.graph import ScriptedReasoner

    reasoner = ScriptedReasoner(
        [
            {
                "kind": "metrics",
                "service": "api",
                "start": 100,
                "end": 103,
                "limit": 1,
                "indicator": "api_cpu",
            }
        ],
        [{"candidate": "api_cpu", "explanation": "only ID", "evidence_ids": ["$latest"]}],
    )
    assert run_agent(tools, reasoner, Limits(1, 1))["result"] is None


def test_claim_cannot_reference_a_record_hidden_by_limit(tools):
    from rca_agent.graph import ObservationLimits

    class HiddenRecord(Scripted):
        def hypothesize(self, history, context):
            eid = history[-1]["evidence_id"]
            return [
                {
                    "candidate": "api_cpu",
                    "explanation": "hidden",
                    "evidence_ids": [eid],
                    "claims": [
                        {"evidence_id": eid, "record_index": 1, "field": "api_cpu", "value": 2.0}
                    ],
                }
            ]

    state = actual_run_agent(tools, HiddenRecord(), Limits(1, 1), ObservationLimits(1, 1000))
    assert state["result"] is None


def test_tampered_response_records_are_rejected(tools, monkeypatch):
    from dataclasses import replace

    original = tools.query_metrics

    def tampered(*args, **kwargs):
        response = original(*args, **kwargs)
        return replace(response, records=({"time": 100, "api_cpu": 999.0},))

    monkeypatch.setattr(tools, "query_metrics", tampered)
    state = run_agent(tools, Scripted(), Limits(1, 1))
    assert state["result"] is None and state["failure"] == "tool_error"


def test_reasoner_cannot_change_observed_values(tools):
    class MutatingReasoner(Scripted):
        def hypothesize(self, history, context):
            observation = history[-1]["observation"]
            observation.records[0]["api_cpu"] = 999.0
            return [
                {
                    "candidate": "api_cpu",
                    "explanation": "mutated",
                    "evidence_ids": [observation.evidence_id],
                    "claims": [
                        {
                            "evidence_id": observation.evidence_id,
                            "record_index": 0,
                            "field": "api_cpu",
                            "value": 999.0,
                        }
                    ],
                }
            ]

    assert run_agent(tools, MutatingReasoner(), Limits(1, 1))["result"] is None
