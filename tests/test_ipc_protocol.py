import json

import pytest

from rca_agent.ipc_protocol import (
    IPCError,
    ReplayCache,
    encode_frame,
    parse_frame,
    validate_request,
)


def payload():
    return {
        "instruction": "untrusted telemetry",
        "phase": "investigate",
        "context": {
            "case_id": "synthetic",
            "candidates": ["api_cpu"],
            "services": ["api"],
            "start": 1,
            "end": 2,
        },
        "observations": [],
        "schema": {},
    }


def request():
    return {"version": 1, "type": "request", "request_id": "a" * 32, "payload": payload()}


def test_bounded_frames_and_strict_json():
    frame = encode_frame(request(), 4096)
    assert parse_frame(frame, 4096) == request()
    for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b"[]", b"\xff"):
        with pytest.raises(IPCError):
            parse_frame(raw, 4096)
    with pytest.raises(IPCError):
        parse_frame(frame, 5)


def test_request_shape_and_cross_case_observation():
    valid = request()
    validate_request(valid)
    valid["payload"]["observations"] = [
        {
            "case_id": "other",
            "source": "logs",
            "call_id": "x",
            "evidence_id": "x",
            "arguments": {},
            "records": [],
            "truncated": False,
        }
    ]
    with pytest.raises(IPCError):
        validate_request(valid)
    invalid = request()
    invalid["payload"]["secret"] = "do-not-send"
    with pytest.raises(IPCError):
        validate_request(invalid)


def test_replay_cache_idempotence_and_collision():
    cache = ReplayCache(1)
    original = request()
    assert cache.lookup(original) is None
    response = {"ok": True}
    cache.store(original, response)
    assert cache.lookup(json.loads(json.dumps(original))) == response
    changed = request()
    changed["payload"]["context"]["case_id"] = "changed"
    with pytest.raises(IPCError, match="different payload"):
        cache.lookup(changed)
    changed = request()
    changed["request_id"] = "b" * 32
    with pytest.raises(IPCError, match="capacity"):
        cache.lookup(changed)


def test_unapproved_instruction_and_schema_rejected():
    from rca_agent.ipc_protocol import validate_model_payload

    with pytest.raises(IPCError, match="unapproved"):
        validate_model_payload(payload())


def test_cached_response_is_immutable_to_caller():
    cache = ReplayCache(1)
    original = request()
    response = {"ok": True, "nested": {"calls": 1}}
    cache.store(original, response)
    response["nested"]["calls"] = 999
    cached = cache.lookup(original)
    assert cached["nested"]["calls"] == 1
    cached["nested"]["calls"] = 888
    assert cache.lookup(original)["nested"]["calls"] == 1


def test_request_response_schema_rejects_extra_keys_and_types():
    from rca_agent.ipc_protocol import IPCRequest, IPCResponse, validate_response

    assert IPCRequest.model_json_schema()["additionalProperties"] is False
    assert IPCResponse.model_json_schema()["additionalProperties"] is False
    value = request()
    value["version"] = True
    with pytest.raises(IPCError):
        validate_request(value)
    response = {
        "version": 1,
        "type": "response",
        "request_id": "a" * 32,
        "content": "{}",
        "metadata": {},
        "case_metadata": {},
        "extra": "SYNTHETIC_PRIVATE",
    }
    with pytest.raises(IPCError):
        validate_response(request(), response)


def test_rejected_untrusted_message_is_absent_from_traceback():
    import traceback

    bad = request()
    bad["payload"]["context"]["start"] = "SYNTHETIC_PRIVATE_TELEMETRY"
    with pytest.raises(IPCError) as captured:
        validate_request(bad)
    assert "SYNTHETIC_PRIVATE_TELEMETRY" not in "".join(traceback.format_exception(captured.value))


def test_status_schema_rejects_integer_as_boolean():
    from rca_agent.ipc_protocol import validate_status

    value = {
        "version": 1,
        "type": "ready",
        "private_denied": 1,
        "key_denied": True,
        "blind_denied": True,
        "network_denied": True,
        "fork_denied": True,
    }
    with pytest.raises(IPCError, match="status schema"):
        validate_status(value, ready=True)


def test_relay_rejects_unobserved_claim_in_valid_json():
    from rca_agent.ipc_protocol import validate_model_payload, validate_response
    from rca_agent.llm_reasoner import MODEL_INSTRUCTION, Hypotheses

    value = request()
    value["payload"]["instruction"] = MODEL_INSTRUCTION
    value["payload"]["phase"] = "hypothesize"
    value["payload"]["schema"] = Hypotheses.model_json_schema()
    value["payload"]["observations"] = [
        {
            "case_id": "synthetic",
            "source": "metrics",
            "call_id": "c" * 32,
            "evidence_id": "e" * 64,
            "arguments": {"service": "api"},
            "records": [{"api_cpu": 1.0}],
            "truncated": False,
        }
    ]
    validate_request(value)
    validate_model_payload(value["payload"])
    meta = {
        "provider": "fake",
        "model": "ipc-fixture",
        "code_version": "ipc-test",
        "calls": 1,
        "input_tokens": 1,
        "output_tokens": 1,
        "total_tokens": 2,
        "cost_usd": "0",
        "failure": "",
    }
    model = {
        "hypotheses": [
            {
                "candidate": "api_cpu",
                "explanation": "x",
                "evidence_ids": ["e" * 64],
                "claims": [
                    {"evidence_id": "e" * 64, "record_index": 0, "field": "api_cpu", "value": 999.0}
                ],
            }
        ]
    }
    response = {
        "version": 1,
        "type": "response",
        "request_id": "a" * 32,
        "content": json.dumps(model),
        "metadata": meta,
        "case_metadata": meta,
    }
    with pytest.raises(IPCError, match="invalid model response"):
        validate_response(value, response)


def test_agent_preflight_rejects_missing_isolation_before_requests():
    from rca_agent.ipc_protocol import validate_agent_ready

    value = {
        "version": 1,
        "type": "agent_ready",
        "file_denied": True,
        "key_denied": True,
        "network_denied": False,
        "fork_denied": True,
        "blind_readable": True,
    }
    with pytest.raises(IPCError, match="isolation verification failed"):
        validate_agent_ready(value)
