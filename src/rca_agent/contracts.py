"""Strict JSON contracts; no model or analysis implementation."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class ContractError(ValueError):
    """Untrusted data does not satisfy the contract."""


def _object(value: Any, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError(f"expected object fields: {sorted(fields)}")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError("expected nonempty string")
    return value


def _list(value: Any, *, nonempty: bool = True) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ContractError("expected list" + (" with at least one item" if nonempty else ""))
    return value


def _unique(values: list[str]) -> None:
    if len(set(values)) != len(values):
        raise ContractError("duplicate values")


@dataclass(frozen=True)
class Evidence:
    id: str
    content: str


@dataclass(frozen=True)
class RCAInput:
    case_id: str
    candidates: tuple[str, ...]
    evidence: tuple[Evidence, ...]

    @classmethod
    def parse(cls, raw: Any) -> "RCAInput":
        obj = _object(raw, {"case_id", "candidates", "evidence"})
        candidates = [_text(c) for c in _list(obj["candidates"])]
        evidence = []
        for item in _list(obj["evidence"]):
            item = _object(item, {"id", "content"})
            evidence.append(Evidence(_text(item["id"]), _text(item["content"])))
        _unique(candidates)
        _unique([e.id for e in evidence])
        return cls(_text(obj["case_id"]), tuple(candidates), tuple(evidence))


@dataclass(frozen=True)
class RankedCause:
    candidate: str
    explanation: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    result_summary: str


@dataclass(frozen=True)
class RCAResult:
    case_id: str
    ranking: tuple[RankedCause, ...]
    tool_calls: tuple[ToolCall, ...]

    @classmethod
    def parse(cls, raw: Any, task: RCAInput) -> "RCAResult":
        obj = _object(raw, {"case_id", "ranking", "tool_calls"})
        case_id = _text(obj["case_id"])
        if case_id != task.case_id:
            raise ContractError("case_id mismatch")
        ranking = []
        for item in _list(obj["ranking"]):
            item = _object(item, {"candidate", "explanation", "evidence_ids"})
            candidate = _text(item["candidate"])
            evidence_ids = [_text(e) for e in _list(item["evidence_ids"])]
            _unique(evidence_ids)
            if candidate not in task.candidates:
                raise ContractError("unknown candidate")
            if not set(evidence_ids) <= {e.id for e in task.evidence}:
                raise ContractError("unknown evidence ID")
            ranking.append(RankedCause(candidate, _text(item["explanation"]), tuple(evidence_ids)))
        _unique([r.candidate for r in ranking])
        calls = []
        for item in _list(obj["tool_calls"], nonempty=False):
            item = _object(item, {"name", "arguments", "result_summary"})
            if not isinstance(item["arguments"], dict):
                raise ContractError("tool arguments must be an object")
            calls.append(
                ToolCall(_text(item["name"]), item["arguments"], _text(item["result_summary"]))
            )
        return cls(case_id, tuple(ranking), tuple(calls))


class RCAAgent(Protocol):
    def analyze(self, task: RCAInput) -> RCAResult:
        """Future implementations provide a result for a validated task."""
        ...


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ContractError(f"invalid JSON constant: {value}")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                row = json.loads(line, object_pairs_hook=_pairs, parse_constant=_constant)
                if not isinstance(row, dict):
                    raise ContractError("expected object")
            except (ValueError, TypeError) as exc:
                raise ContractError(f"line {number}: {exc}") from exc
            rows.append(row)
    if not rows:
        raise ContractError("empty JSONL")
    return rows
