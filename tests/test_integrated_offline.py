from dataclasses import asdict
from decimal import Decimal

import pytest

from rca_agent.docker_integration import fixture_budget
from rca_agent.integrated_worker import IDENTITY, SocketProvider, worker_handler
from rca_agent.llm_reasoner import MODEL_INSTRUCTION, QUERY
from rca_agent.model_broker import BrokerFailure, OfflineBroker, Pricing


def payload():
    return {
        "instruction": MODEL_INSTRUCTION,
        "schema": QUERY.json_schema(),
        "phase": "investigate",
        "context": {
            "case_id": "synthetic",
            "candidates": ["api_cpu"],
            "services": ["api"],
            "start": 1,
            "end": 3,
        },
        "observations": [],
    }


class LocalChannel:
    def __init__(self, fault="none"):
        self.handler = worker_handler({"fault": fault})
        self.operations = []

    def request(self, operation, data):
        self.operations.append(operation)
        return self.handler(operation, data)


def broker(fault="none"):
    channel = LocalChannel(fault)
    price = Pricing(Decimal(".001"), Decimal(".002"))
    provider = SocketProvider(channel, price)
    ledger = OfflineBroker(
        provider,
        provider_name=IDENTITY[0],
        model=IDENTITY[1],
        code_version=IDENTITY[2],
        budget=fixture_budget(fault),
        pricing=price,
    )
    return ledger, channel


def test_actual_mock_count_reservation_model_accounting():
    ledger, channel = broker()
    content = ledger.request(payload())
    assert "metrics" in content
    assert channel.operations == ["count", "complete"]
    assert ledger.summary()["input_tokens"] == 10 and ledger.summary()["output_tokens"] == 50
    assert ledger.summary()["cost_usd"] == "0.110"


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("token_limit", "token_limit"),
        ("cost_limit", "cost_limit"),
        ("count_failure", "input_estimate_unavailable"),
    ],
)
def test_preflight_failures_never_call_model(fault, reason):
    ledger, channel = broker(fault)
    with pytest.raises(BrokerFailure, match=reason):
        ledger.request(payload())
    assert channel.operations == ["count"]
    assert ledger.summary()["calls"] == 0


def test_changed_prepared_request_cannot_reuse_count():
    price = Pricing(Decimal(".001"), Decimal(".002"))
    channel = LocalChannel()
    provider = SocketProvider(channel, price)
    request = provider.prepare(payload(), model=IDENTITY[1], max_output_tokens=1000)
    assert provider.input_upper_bound(request) == provider.input_upper_bound(request) == 10
    changed = payload()
    changed["context"]["end"] = 4
    other = provider.prepare(changed, model=IDENTITY[1], max_output_tokens=1000)
    with pytest.raises(ValueError, match="uncounted"):
        provider.complete(other)
    assert channel.operations == ["count"]
    assert asdict(request) != asdict(other)


def test_redirect_is_not_followed():
    import httpx

    from rca_agent.openai_provider import OpenAIProvider

    seen = []

    def redirect(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://example.invalid/v1/responses"})

    provider = OpenAIProvider(
        transport=httpx.MockTransport(redirect), pricing=Pricing(Decimal(0), Decimal(0))
    )
    request = provider.prepare(payload(), model=IDENTITY[1], max_output_tokens=1000)
    result = provider.complete(request)
    assert result.usage is None and len(seen) == 1
    assert seen[0] == "https://api.openai.com/v1/responses"


def test_real_environment_key_is_not_used(monkeypatch):
    import httpx

    from rca_agent.openai_provider import OpenAIProvider

    monkeypatch.setenv("OPENAI_API_KEY", "canary-secret-never-used")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/")
    headers = []

    def handler(request):
        headers.append(request.headers["authorization"])
        assert request.url.host == "api.openai.com"
        return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 10})

    provider = OpenAIProvider(
        transport=httpx.MockTransport(handler), pricing=Pricing(Decimal(0), Decimal(0))
    )
    prepared = provider.prepare(payload(), model=IDENTITY[1], max_output_tokens=1000)
    assert provider.input_upper_bound(prepared) == 10
    assert headers == ["Bearer offline-test-key"]


def test_known_provider_error_still_settles_usage():
    ledger, channel = broker()
    original = channel.handler

    def handler(operation, data):
        if operation == "complete":
            return {
                "content": None,
                "input_tokens": 10,
                "output_tokens": 50,
                "cost_usd": "0.110",
                "error": "provider_error",
            }
        return original(operation, data)

    channel.handler = handler
    with pytest.raises(BrokerFailure, match="provider_error"):
        ledger.request(payload())
    assert ledger.summary()["cost_usd"] == "0.110"
    assert ledger.summary()["output_tokens"] == 50
