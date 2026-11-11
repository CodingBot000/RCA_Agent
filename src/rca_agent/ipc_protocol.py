"""Strict, bounded IPC messages and replay-safe response cache."""

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError


class StrictMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class IPCContext(StrictMessage):
    case_id: StrictStr
    candidates: list[StrictStr] = Field(min_length=1)
    services: list[StrictStr] = Field(min_length=1)
    start: StrictInt
    end: StrictInt


class IPCObservation(StrictMessage):
    case_id: StrictStr
    source: Literal["metrics", "logs", "traces"]
    call_id: StrictStr = Field(pattern=r"^[0-9a-f]{32}$")
    evidence_id: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    arguments: dict[str, Any]
    records: list[dict[str, Any]]
    truncated: StrictBool


class IPCPayload(StrictMessage):
    instruction: StrictStr
    phase: Literal["investigate", "hypothesize"]
    context: IPCContext
    observations: list[IPCObservation]
    schema_payload: dict[str, Any] = Field(alias="schema")


class IPCRequest(StrictMessage):
    version: Literal[1]
    type: Literal["request"]
    request_id: StrictStr = Field(pattern=r"^[0-9a-f]{32}$")
    payload: IPCPayload


class IPCResponse(StrictMessage):
    version: Literal[1]
    type: Literal["response"]
    request_id: StrictStr = Field(pattern=r"^[0-9a-f]{32}$")
    content: StrictStr
    metadata: dict[str, Any]
    case_metadata: dict[str, Any]


class IPCReady(StrictMessage):
    version: StrictInt
    type: Literal["ready"]
    private_denied: StrictBool
    key_denied: StrictBool
    blind_denied: StrictBool
    network_denied: StrictBool
    fork_denied: StrictBool


class IPCAgentReady(StrictMessage):
    version: StrictInt
    type: Literal["agent_ready"]
    file_denied: StrictBool
    key_denied: StrictBool
    network_denied: StrictBool
    fork_denied: StrictBool
    blind_readable: StrictBool


def validate_agent_ready(value: dict) -> None:
    try:
        status = IPCAgentReady.model_validate(value, strict=True)
    except (ValidationError, TypeError, ValueError):
        raise IPCError("invalid agent ready schema") from None
    if status.version != 1 or not all(
        (
            status.file_denied,
            status.key_denied,
            status.network_denied,
            status.fork_denied,
            status.blind_readable,
        )
    ):
        raise IPCError("agent isolation verification failed")


class IPCDone(StrictMessage):
    version: StrictInt
    type: Literal["done"]
    file_denied: StrictBool
    key_denied: StrictBool
    network_denied: StrictBool
    fork_denied: StrictBool
    blind_readable: StrictBool
    graph_success: StrictBool
    replay_equal: StrictBool
    calls_before_replay: StrictInt
    calls_after_replay: StrictInt
    calls_total: StrictInt


def validate_status(value: dict, *, ready: bool) -> None:
    try:
        status = (IPCReady if ready else IPCDone).model_validate(value, strict=True)
    except (ValidationError, TypeError, ValueError):
        raise IPCError("invalid status schema") from None
    if status.version != 1:
        raise IPCError("invalid status version")


class IPCError(ValueError):
    """Protocol is invalid; caller must stop rather than retry ambiguously."""


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise IPCError("duplicate key")
        value[key] = item
    return value


def parse_frame(data: bytes, maximum: int) -> dict:
    if type(maximum) is not int or maximum < 1 or len(data) > maximum:
        raise IPCError("frame too large")
    try:
        value = json.loads(
            data,
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(IPCError("invalid constant")),
        )
    except (ValueError, UnicodeError, RecursionError):
        raise IPCError("invalid JSON frame") from None
    if not isinstance(value, dict):
        raise IPCError("frame must be an object")
    return value


def encode_frame(value: dict, maximum: int) -> bytes:
    try:
        result = (
            json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
            + b"\n"
        )
    except (ValueError, TypeError, RecursionError):
        raise IPCError("invalid frame value") from None
    if len(result) > maximum:
        raise IPCError("frame too large")
    return result


def validate_request(value: dict) -> None:
    try:
        request = IPCRequest.model_validate(value, strict=True)
    except (ValidationError, TypeError, ValueError):
        raise IPCError("invalid request schema") from None
    if (
        type(value["version"]) is not int
        or request.payload.context.start >= request.payload.context.end
    ):
        raise IPCError("invalid request context")
    if any(
        item.case_id != request.payload.context.case_id for item in request.payload.observations
    ):
        raise IPCError("cross-case observation")


def digest_request(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


@dataclass
class ReplayCache:
    maximum: int

    def __post_init__(self):
        if type(self.maximum) is not int or self.maximum < 1:
            raise ValueError("explicit positive replay capacity required")
        self._entries: dict[str, tuple[str, dict]] = {}

    def lookup(self, request: dict) -> dict | None:
        validate_request(request)
        item = self._entries.get(request["request_id"])
        if item is None:
            if len(self._entries) >= self.maximum:
                raise IPCError("request capacity exhausted")
            return None
        if item[0] != digest_request(request):
            raise IPCError("request ID reused with different payload")
        return deepcopy(item[1])

    def store(self, request: dict, response: dict) -> None:
        if self.lookup(request) is not None:
            raise IPCError("request already completed")
        self._entries[request["request_id"]] = (digest_request(request), deepcopy(response))


def validate_model_payload(payload: dict) -> None:
    """Only the fixed instruction and approved schemas cross the broker boundary."""
    from rca_agent.llm_reasoner import MODEL_INSTRUCTION, QUERY, Hypotheses

    expected = (
        QUERY.json_schema() if payload["phase"] == "investigate" else Hypotheses.model_json_schema()
    )
    if payload["instruction"] != MODEL_INSTRUCTION or payload["schema"] != expected:
        raise IPCError("unapproved model instruction or schema")
    for obs in payload["observations"]:
        if set(obs["arguments"]) - {"service", "start", "end", "limit", "indicator"}:
            raise IPCError("unapproved observation arguments")


def validate_response(request: dict, response: dict) -> None:
    """Check response envelope, both budget summaries, schema and observed claims."""
    from pydantic import ValidationError

    from rca_agent.llm_reasoner import QUERY, Finish, Hypotheses, MetricsQuery, _parse_json
    from rca_agent.public_report import public_run_report

    try:
        IPCResponse.model_validate(response, strict=True)
    except (ValidationError, TypeError, ValueError):
        raise IPCError("invalid response schema") from None
    if (
        set(response) != {"version", "type", "request_id", "content", "metadata", "case_metadata"}
        or type(response["version"]) is not int
        or response["version"] != 1
        or response["type"] != "response"
        or response["request_id"] != request["request_id"]
        or not isinstance(response["content"], str)
    ):
        raise IPCError("invalid response envelope")
    for metadata in (response["metadata"], response["case_metadata"]):
        if (
            not isinstance(metadata, dict)
            or set(metadata)
            != {
                "provider",
                "model",
                "code_version",
                "calls",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cost_usd",
                "failure",
            }
            or metadata["provider"] != "fake"
            or metadata["model"] != "ipc-fixture"
            or metadata["code_version"] != "ipc-test"
            or metadata["failure"] != ""
        ):
            raise IPCError("invalid usage metadata")
        try:
            public_run_report(
                {"model_metadata": metadata, "calls": 0, "iterations": 0, "failure": ""}
            )
        except (TypeError, ValueError, KeyError):
            raise IPCError("invalid usage metadata") from None
    payload = request["payload"]
    try:
        data = _parse_json(response["content"])
        if payload["phase"] == "investigate":
            query = QUERY.validate_python(data, strict=True)
            if not isinstance(query, Finish):
                ctx = payload["context"]
                if (
                    query.service not in ctx["services"]
                    or query.start < ctx["start"]
                    or query.end > ctx["end"]
                    or query.start >= query.end
                ):
                    raise IPCError("query outside allowed context")
                if isinstance(query, MetricsQuery) and (
                    query.indicator not in ctx["candidates"]
                    or not query.indicator.startswith(query.service + "_")
                ):
                    raise IPCError("indicator outside allowed context")
        else:
            result = Hypotheses.model_validate(data, strict=True)
            observations = {item["evidence_id"]: item for item in payload["observations"]}
            if len(observations) != len(payload["observations"]):
                raise IPCError("duplicate observation")
            for hypothesis in result.hypotheses:
                if hypothesis.candidate not in payload["context"]["candidates"]:
                    raise IPCError("candidate outside allowed context")
                for claim in hypothesis.claims:
                    observation = observations.get(claim.evidence_id)
                    if (
                        observation is None
                        or claim.evidence_id not in hypothesis.evidence_ids
                        or claim.record_index >= len(observation["records"])
                    ):
                        raise IPCError("claim outside observations")
                    record = observation["records"][claim.record_index]
                    if (
                        claim.field not in record
                        or type(record[claim.field]) is not type(claim.value)
                        or record[claim.field] != claim.value
                    ):
                        raise IPCError("claim does not match observation")
    except (ValueError, ValidationError, TypeError, KeyError):
        raise IPCError("invalid model response") from None
