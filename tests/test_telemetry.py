import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rca_agent.contracts import ContractError
from rca_agent.telemetry import EvidenceRecord, EvidenceRegistry, TelemetryTools


@pytest.fixture
def tools(tmp_path):
    cid = "re2-ob-v2-" + "a" * 32
    case = tmp_path / cid
    case.mkdir()
    (case / "task.json").write_text(
        json.dumps(
            {
                "case_id": cid,
                "candidates": ["api_cpu"],
                "evidence": [{"id": "base", "content": "metadata"}],
            }
        )
    )
    pq.write_table(
        pa.table({"time": [100, 101, 102], "api_cpu": [1.0, 2.0, 3.0]}), case / "metrics.parquet"
    )
    pq.write_table(
        pa.table(
            {"timestamp": [100, 101], "container_name": ["api", "api"], "message": ["a", "b"]}
        ),
        case / "logs.parquet",
    )
    pq.write_table(
        pa.table(
            {"startTime": [100000000, 101000000], "serviceName": ["api", "api"], "duration": [5, 6]}
        ),
        case / "traces.parquet",
    )
    return TelemetryTools(tmp_path, cid, EvidenceRegistry(cid))


def test_queries_and_evidence(tools):
    for response in (
        tools.query_metrics(tools.task.case_id, "api", 100, 103, 1, "api_cpu"),
        tools.query_logs(tools.task.case_id, "api", 100, 103, 1),
        tools.query_traces(tools.task.case_id, "api", 100, 103, 1),
    ):
        assert response.status == "ok" and response.evidence_id and response.truncated
    response = tools.query_logs(tools.task.case_id, "api", 100, 103, 2)
    result = {
        "case_id": tools.task.case_id,
        "ranking": [
            {"candidate": "api_cpu", "explanation": "x", "evidence_ids": [response.evidence_id]}
        ],
        "tool_calls": [],
    }
    assert tools.registry.validate_result(result, tools.task).ranking[0].candidate == "api_cpu"
    result["ranking"][0]["evidence_ids"] = ["missing"]
    with pytest.raises(ContractError):
        tools.registry.validate_result(result, tools.task)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"case_id": "other"},
        {"service": "db"},
        {"start": 99},
        {"end": 104},
        {"limit": 101},
        {"limit": True},
        {"indicator": "db_cpu"},
    ],
)
def test_invalid_queries_rejected(tools, kwargs):
    args = {
        "case_id": tools.task.case_id,
        "service": "api",
        "start": 100,
        "end": 103,
        "limit": 10,
        "indicator": "api_cpu",
    }
    args.update(kwargs)
    assert tools.query_metrics(**args).status == "error"
    assert not tools.registry._records


def test_empty_missing_and_failed_evidence(tools):
    assert tools.query_logs(tools.task.case_id, "api", 102, 103, 10).status == "empty"
    (tools.case / "logs.parquet").unlink()
    assert tools.query_logs(tools.task.case_id, "api", 100, 103, 10).status == "unavailable"
    record = EvidenceRecord("id", tools.task.case_id, "call", "logs", "content")
    with pytest.raises(ContractError):
        tools.registry.register(record, status="error")
    with pytest.raises(ContractError):
        tools.registry.register(
            EvidenceRecord("id", "other", "call", "logs", "content"), status="ok"
        )
    with pytest.raises(ContractError):
        tools.registry.register(record, status="ok")
    actual = tools.query_metrics(tools.task.case_id, "api", 100, 103, 1, "api_cpu")
    registered = tools.registry._records[actual.evidence_id]
    with pytest.raises(ContractError):
        tools.registry.register(registered, status="ok", _proof=tools.registry._capability)


def test_finalize_requires_observed_evidence(tools):
    raw = {
        "case_id": tools.task.case_id,
        "ranking": [{"candidate": "api_cpu", "explanation": "x", "evidence_ids": ["base"]}],
        "tool_calls": [],
    }
    assert tools.registry.finalize(raw, tools.task) == ("insufficient_evidence", None)
    response = tools.query_logs(tools.task.case_id, "api", 100, 103, 1)
    assert tools.registry.finalize(raw, tools.task) == ("insufficient_evidence", None)
    raw["ranking"][0]["evidence_ids"] = [response.evidence_id]
    assert tools.registry.finalize(raw, tools.task)[0] == "ok"
    raw["ranking"][0]["evidence_ids"] = ["missing"]
    assert tools.registry.finalize(raw, tools.task) == ("error", None)


def test_case_and_symlink_rejected(tools, tmp_path):
    with pytest.raises(ContractError):
        TelemetryTools(tmp_path, "../private", EvidenceRegistry("../private"))
    path = tools.case / "logs.parquet"
    path.unlink()
    path.symlink_to(tmp_path / "answers.jsonl")
    assert tools.query_logs(tools.task.case_id, "api", 100, 103, 5).status == "error"


def test_cross_case_result_rejected(tools):
    from rca_agent.contracts import RCAInput

    other = RCAInput.parse(
        {"case_id": "other", "candidates": ["api_cpu"], "evidence": [{"id": "x", "content": "x"}]}
    )
    with pytest.raises(ContractError):
        tools.registry.validate_result({}, other)
