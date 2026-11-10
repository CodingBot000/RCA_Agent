import pytest

pytest_plugins = ("test_telemetry",)

from rca_agent.graph import Limits, run_agent


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
        return [{"candidate": "api_cpu", "explanation": "scripted", "evidence_ids": [evidence]}]


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
    assert result["result"] is None and result["calls"] == 1
    assert result["history"][0]["status"] == "error"


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
        [{"candidate": "api_cpu", "explanation": "synthetic", "evidence_ids": ["$latest"]}],
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
