import copy
import json
from decimal import Decimal

import httpx
import pytest

from rca_agent.egress_proxy import validate_http
from rca_agent.llm_reasoner import MODEL_INSTRUCTION, QUERY
from rca_agent.model_broker import Pricing
from rca_agent.openai_provider import OpenAIProvider


def envelope(operation):
    seen = []

    def handler(request):
        seen.append(
            {"method": request.method, "url": str(request.url), "body": json.loads(request.content)}
        )
        return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 10})

    provider = OpenAIProvider(
        transport=httpx.MockTransport(handler), pricing=Pricing(Decimal(0), Decimal(0))
    )
    request = provider.prepare(
        {
            "instruction": MODEL_INSTRUCTION,
            "schema": QUERY.json_schema(),
            "phase": "investigate",
            "context": {
                "case_id": "synthetic",
                "services": ["api"],
                "candidates": ["api_cpu"],
                "start": 1,
                "end": 2,
            },
            "observations": [],
        },
        model="gpt-6-luna",
        max_output_tokens=1000,
    )
    if operation == "count":
        provider.input_upper_bound(request)
    else:
        provider.complete(request)
    return seen[0]


@pytest.mark.parametrize("operation", ["count", "complete"])
def test_sdk_requests_accepted_by_proxy(operation):
    assert validate_http(operation, envelope(operation)).startswith("/v1/responses")


@pytest.mark.parametrize(
    "change",
    ["ip", "host", "port", "scheme", "schema", "store", "tools", "extra", "stream", "operation"],
)
def test_proxy_rejects_unapproved_request(change):
    data = copy.deepcopy(envelope("complete"))
    if change == "ip":
        data["url"] = "https://127.0.0.1/v1/responses"
    elif change == "host":
        data["url"] = "https://example.invalid/v1/responses"
    elif change == "port":
        data["url"] = "https://api.openai.com:8443/v1/responses"
    elif change == "scheme":
        data["url"] = "http://api.openai.com/v1/responses"
    elif change == "schema":
        data["body"]["text"] = {}
    elif change == "store":
        data["body"]["store"] = True
    elif change == "tools":
        data["body"]["tools"] = [{"type": "web_search"}]
    elif change == "extra":
        data["body"]["headers"] = {"Authorization": "secret-canary"}
    elif change == "stream":
        data["body"]["stream"] = True
    with pytest.raises(ValueError):
        validate_http("count" if change == "operation" else "complete", data)


def proxy_state(tmp_path, cost="10", uncertain=False):
    from dataclasses import replace

    from rca_agent.docker_integration import fixture_budget
    from rca_agent.egress_proxy import EgressProxy, ProxyModelProvider
    from rca_agent.model_broker import OfflineBroker

    proxy = EgressProxy.__new__(EgressProxy)
    proxy.counts = {}
    proxy.spent = set()
    proxy.count_calls = 0
    proxy.blocked = False
    proxy.calls = proxy.models = 0
    proxy.pricing = Pricing(Decimal(".001"), Decimal(".002"))
    proxy.path = tmp_path / "audit.json"
    proxy.ledger = OfflineBroker(
        ProxyModelProvider(proxy),
        provider_name="egress-mock",
        model="gpt-6-luna",
        code_version="tls-proxy",
        budget=replace(fixture_budget("none"), max_cost_usd=Decimal(cost)),
        pricing=proxy.pricing,
    )
    sent = []

    def forward(operation, data):
        sent.append(operation)
        if operation == "count":
            return {"status": 200, "body": {"input_tokens": 10}}
        if uncertain:
            raise TimeoutError("private-body-must-not-be-logged")
        return {
            "status": 200,
            "body": {
                "status": "completed",
                "usage": {"input_tokens": 10, "output_tokens": 50, "total_tokens": 60},
            },
        }

    proxy.forward = forward
    # Only the ledger audit location is redirected for offline unit tests.
    import rca_agent.egress_proxy as module

    return proxy, sent, module


def test_proxy_independent_budget_blocks_before_network(tmp_path, monkeypatch):
    from rca_agent.model_broker import BrokerFailure

    proxy, sent, module = proxy_state(tmp_path, cost=".001")
    real = module.Path
    monkeypatch.setattr(
        module,
        "Path",
        lambda path: (
            tmp_path / "ledger.json" if str(path) == "/proxychannel/ledger.json" else real(path)
        ),
    )
    proxy("count", envelope("count"))
    with pytest.raises(BrokerFailure, match="cost_limit"):
        proxy("complete", envelope("complete"))
    assert sent == ["count"] and proxy.blocked


def test_uncertain_proxy_request_cannot_be_reauthorized(tmp_path, monkeypatch):
    from rca_agent.model_broker import BrokerFailure

    proxy, sent, module = proxy_state(tmp_path, uncertain=True)
    real = module.Path
    monkeypatch.setattr(
        module,
        "Path",
        lambda path: (
            tmp_path / "ledger.json" if str(path) == "/proxychannel/ledger.json" else real(path)
        ),
    )
    proxy("count", envelope("count"))
    with pytest.raises(BrokerFailure):
        proxy("complete", envelope("complete"))
    with pytest.raises(ValueError, match="blocked"):
        proxy("count", envelope("count"))
    assert sent == ["count", "complete"] and proxy.blocked and proxy.ledger.blocked


def test_successful_proxy_request_is_spent_for_new_ids(tmp_path, monkeypatch):
    proxy, sent, module = proxy_state(tmp_path)
    real = module.Path
    monkeypatch.setattr(
        module,
        "Path",
        lambda path: (
            tmp_path / "ledger.json" if str(path) == "/proxychannel/ledger.json" else real(path)
        ),
    )
    proxy("count", envelope("count"))
    proxy("complete", envelope("complete"))
    with pytest.raises(ValueError, match="spent"):
        proxy("count", envelope("count"))
    assert sent == ["count", "complete"] and proxy.ledger.summary()["cost_usd"] == "0.110"
