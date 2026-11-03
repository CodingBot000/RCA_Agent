import copy

import pytest

from rca_agent.contracts import ContractError, RCAInput, RCAResult, read_jsonl

pytestmark = pytest.mark.contract


@pytest.fixture
def task_data():
    return {
        "case_id": "case-1",
        "candidates": ["api", "db"],
        "evidence": [{"id": "e1", "content": "latency"}],
    }


@pytest.fixture
def result_data():
    return {
        "case_id": "case-1",
        "ranking": [{"candidate": "db", "explanation": "slow", "evidence_ids": ["e1"]}],
        "tool_calls": [
            {"name": "metrics", "arguments": {"service": "db"}, "result_summary": "slow"}
        ],
    }


def test_valid_contract(task_data, result_data):
    task = RCAInput.parse(task_data)
    result = RCAResult.parse(result_data, task)
    assert result.ranking[0].candidate == "db"
    assert result.tool_calls[0].arguments == {"service": "db"}


@pytest.mark.parametrize(
    "change", ["unknown_candidate", "unknown_evidence", "duplicate", "case", "missing_explanation"]
)
def test_reject_bad_result(task_data, result_data, change):
    result = copy.deepcopy(result_data)
    rank = result["ranking"][0]
    if change == "unknown_candidate":
        rank["candidate"] = "not-allowed"
    elif change == "unknown_evidence":
        rank["evidence_ids"] = ["missing"]
    elif change == "duplicate":
        result["ranking"].append(copy.deepcopy(rank))
    elif change == "case":
        result["case_id"] = "other"
    else:
        del rank["explanation"]
    with pytest.raises(ContractError):
        RCAResult.parse(result, RCAInput.parse(task_data))


def test_reject_duplicate_evidence(task_data):
    task_data["evidence"].append(task_data["evidence"][0])
    with pytest.raises(ContractError):
        RCAInput.parse(task_data)


@pytest.mark.parametrize(
    "content", ['{"case_id":\n', "\n", "[]\n", '{"x": 1, "x": 2}\n', '{"x": NaN}\n', ""]
)
def test_reject_invalid_jsonl(tmp_path, content):
    path = tmp_path / "input.jsonl"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ContractError):
        read_jsonl(path)


def test_valid_jsonl(tmp_path):
    path = tmp_path / "input.jsonl"
    path.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
    assert read_jsonl(path) == [{"a": 1}, {"a": 2}]


def test_agent_interface_with_mock(task_data, result_data):
    from unittest.mock import Mock

    from rca_agent.contracts import RCAAgent

    task = RCAInput.parse(task_data)
    expected = RCAResult.parse(result_data, task)
    agent: RCAAgent = Mock(spec=RCAAgent)
    agent.analyze.return_value = expected
    assert agent.analyze(task) == expected
    agent.analyze.assert_called_once_with(task)
