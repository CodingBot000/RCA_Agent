import json
from decimal import Decimal

import httpx
import pytest

from rca_agent.llm_reasoner import MODEL_INSTRUCTION, QUERY
from rca_agent.model_broker import Pricing
from rca_agent.openai_provider import OpenAIProvider


def payload():
    return {
        "instruction": MODEL_INSTRUCTION,
        "phase": "investigate",
        "schema": QUERY.json_schema(),
        "context": {
            "case_id": "synthetic",
            "candidates": ["api_cpu"],
            "services": ["api"],
            "start": 1,
            "end": 2,
        },
        "observations": [],
    }


def response(status="completed", usage=True, content=None):
    result = {
        "id": "resp_fake",
        "object": "response",
        "created_at": 0,
        "model": "gpt-6-luna",
        "status": status,
        "output": [
            {
                "id": "msg_fake",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": content
                or [
                    {
                        "type": "output_text",
                        "text": '{"query":{"kind":"finish"}}',
                        "annotations": [],
                    }
                ],
            }
        ],
    }
    if usage:
        result["usage"] = {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
    return result


def make(handler):
    return OpenAIProvider(
        transport=httpx.MockTransport(handler),
        pricing=Pricing(Decimal("0.1"), Decimal("0.2")),
    )


def complete(provider, data, limit, *, model):
    return provider.complete(provider.prepare(data, model=model, max_output_tokens=limit))


def test_success_and_request_contract():
    seen = []

    def handler(request):
        seen.append(request)
        body = json.loads(request.content)
        assert request.url == "https://api.openai.com/v1/responses"
        assert body["store"] is False and body["max_output_tokens"] == 20
        assert body["tools"] == [] and body["text"]["format"]["strict"] is True
        return httpx.Response(200, json=response())

    result = complete(make(handler), payload(), 20, model="gpt-6-luna")
    assert json.loads(result.content) == {"kind": "finish"}
    assert result.usage.cost_usd == Decimal("2.0")
    assert len(seen) == 1


@pytest.mark.parametrize("status", [429, 500])
def test_http_errors_are_not_retried(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "sensitive"}})

    result = complete(make(handler), payload(), 20, model="gpt-6-luna")
    assert result.usage is None and result.error
    assert len(calls) == 1


def test_missing_usage_and_timeout():
    result = complete(
        make(lambda request: httpx.Response(200, json=response(usage=False))),
        payload(),
        20,
        model="gpt-6.1-terra",
    )
    assert result.usage is None

    def timeout(request):
        raise httpx.ReadTimeout("sensitive", request=request)

    assert complete(make(timeout), payload(), 20, model="gpt-6-luna").usage is None


def test_incomplete_and_refusal():
    for data in (
        response(status="incomplete"),
        response(content=[{"type": "refusal", "refusal": "no"}]),
    ):
        result = complete(
            make(lambda request, data=data: httpx.Response(200, json=data)),
            payload(),
            20,
            model="gpt-6-luna",
        )
        assert result.error and result.content is None


def test_non_mock_transport_forbidden():
    with pytest.raises(TypeError):
        OpenAIProvider(transport=httpx.HTTPTransport(), pricing=Pricing(Decimal(0), Decimal(0)))


def test_broker_accounts_usage_and_stops_when_usage_unknown():
    from rca_agent.model_broker import BrokerFailure, Budget, OfflineBroker

    pricing = Pricing(Decimal("0.01"), Decimal("0.02"))
    budget = Budget(3, 2, 2, 100, 80, 150, Decimal(10), 1, 0, 20, 2)

    def broker_with(handler):
        def routed(request):
            if request.url.path.endswith("/input_tokens"):
                return httpx.Response(
                    200, json={"object": "response.input_tokens", "input_tokens": 20}
                )
            return handler(request)

        provider = OpenAIProvider(transport=httpx.MockTransport(routed), pricing=pricing)
        return OfflineBroker(
            provider,
            provider_name="openai",
            model="gpt-6-luna",
            code_version="offline-test",
            budget=budget,
            pricing=pricing,
        )

    broker = broker_with(lambda request: httpx.Response(200, json=response()))
    assert json.loads(broker.request(payload())) == {"kind": "finish"}
    assert broker.summary()["cost_usd"] == "0.20"
    unknown = broker_with(lambda request: httpx.Response(500))
    with pytest.raises(BrokerFailure, match="usage_unavailable"):
        unknown.request(payload())
    with pytest.raises(BrokerFailure, match="usage_unavailable"):
        unknown.request(payload())


@pytest.mark.parametrize("phase", ["investigate", "hypothesize"])
def test_wire_schema_is_closed_and_required(phase):
    from rca_agent.llm_reasoner import Hypotheses

    data = payload()
    data["phase"] = phase
    data["schema"] = (
        QUERY.json_schema() if phase == "investigate" else Hypotheses.model_json_schema()
    )

    seen = []

    def handler(request):
        body = json.loads(request.content)
        schema = body["text"]["format"]["schema"]
        assert schema["type"] == "object" and "anyOf" not in schema

        def visit(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node.get("additionalProperties") is False
                    assert set(node.get("required", [])) == set(node.get("properties", {}))
                assert "allOf" not in node
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)

        visit(schema)
        seen.append(schema)
        return httpx.Response(200, json=response())

    complete(make(handler), data, 20, model="gpt-6-luna")
    assert seen


def test_invalid_structured_output_fails_closed():
    invalid = response(
        content=[
            {
                "type": "output_text",
                "text": '{"query":{"kind":"finish","extra":1}}',
                "annotations": [],
            }
        ]
    )
    result = complete(
        make(lambda request: httpx.Response(200, json=invalid)), payload(), 20, model="gpt-6-luna"
    )
    assert result.content is None and result.error


def test_count_and_response_share_request_fields():
    from rca_agent.model_broker import Budget, OfflineBroker

    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 10})
        return httpx.Response(200, json=response())

    pricing = Pricing(Decimal("0.01"), Decimal("0.02"))
    provider = OpenAIProvider(transport=httpx.MockTransport(handler), pricing=pricing)
    b = OfflineBroker(
        provider,
        provider_name="openai",
        model="gpt-6-luna",
        code_version="test",
        budget=Budget(2, 2, 2, 100, 80, 150, Decimal(10), 1, 0, 20, 2),
        pricing=pricing,
    )
    assert json.loads(b.request(payload())) == {"kind": "finish"}
    assert len(seen) == 2
    count, completion = seen[0][1], seen[1][1]
    for key in ("model", "instructions", "input", "tools", "text"):
        assert count[key] == completion[key]
    assert "store" not in count and "max_output_tokens" not in count
    assert completion["store"] is False and completion["max_output_tokens"] == 20


@pytest.mark.parametrize("kind", ["failure", "timeout", "invalid", "boolean", "limit", "cost"])
def test_count_failures_and_budget_reject_before_model(kind):
    from rca_agent.model_broker import BrokerFailure, Budget, OfflineBroker

    calls = []

    def handler(request):
        calls.append(request.url.path)
        if kind == "timeout":
            raise httpx.ReadTimeout("private", request=request)
        if kind == "failure":
            return httpx.Response(500)
        count = (
            "invalid"
            if kind == "invalid"
            else (True if kind == "boolean" else (101 if kind == "limit" else 10))
        )
        return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": count})

    pricing = Pricing(Decimal("0.01"), Decimal("0.02"))
    budget = Budget(
        2, 2, 2, 100, 80, 150, Decimal("0.1") if kind == "cost" else Decimal(10), 1, 0, 20, 2
    )
    provider = OpenAIProvider(transport=httpx.MockTransport(handler), pricing=pricing)
    b = OfflineBroker(
        provider,
        provider_name="openai",
        model="gpt-6-luna",
        code_version="test",
        budget=budget,
        pricing=pricing,
    )
    with pytest.raises(BrokerFailure):
        b.request(payload())
    assert calls == ["/v1/responses/input_tokens"]


@pytest.mark.parametrize("change", ["model", "instructions", "schema", "observations"])
def test_counted_request_not_affected_by_mutation(change):
    from dataclasses import FrozenInstanceError

    original = payload()
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        if request.url.path.endswith("/input_tokens"):
            original["instruction"] = "changed"
            original["schema"] = {}
            original["observations"].append({"changed": True})
            return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 10})
        return httpx.Response(200, json=response())

    provider = make(handler)
    prepared = provider.prepare(original, model="gpt-6-luna", max_output_tokens=20)
    assert provider.input_upper_bound(prepared) == 10
    if change == "model":
        with pytest.raises(FrozenInstanceError):
            prepared.model = "gpt-6.1-terra"
    assert provider.complete(prepared).content is not None
    assert len(seen) == 2
    for key in ("model", "instructions", "input", "text"):
        assert seen[0][key] == seen[1][key]


def test_changed_or_unapproved_payload_is_rejected_before_http():
    from rca_agent.ipc_protocol import IPCError

    calls = []
    provider = make(lambda request: calls.append(request))
    data = payload()
    data["schema"] = {}
    with pytest.raises(IPCError):
        provider.prepare(data, model="gpt-6-luna", max_output_tokens=20)
    assert not calls


@pytest.mark.parametrize("bad", [True, "10", -1, None])
def test_malformed_usage_is_unknown(bad):
    data = response()
    data["usage"]["input_tokens"] = bad
    result = complete(
        make(lambda request: httpx.Response(200, json=data)), payload(), 20, model="gpt-6-luna"
    )
    assert result.usage is None and result.error
