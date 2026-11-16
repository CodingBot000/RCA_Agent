"""Container roles for authenticated, offline Agent/Broker/Mock-Worker execution."""

import json
import socket
import sys
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import httpx

from rca_agent.egress_policy import validate_destination
from rca_agent.graph import Limits, ObservationLimits, run_agent
from rca_agent.ipc_protocol import (
    ReplayCache,
    validate_model_payload,
    validate_request,
    validate_response,
)
from rca_agent.ipc_worker import RemoteBroker, SessionProvider, _read, _write
from rca_agent.llm_reasoner import LLMReasoner
from rca_agent.model_broker import (
    Budget,
    OfflineBroker,
    PreparedRequest,
    Pricing,
    ProviderResult,
    Usage,
)
from rca_agent.openai_provider import OpenAIProvider
from rca_agent.secure_uds import ChannelClient, Credentials, serve
from rca_agent.telemetry import EvidenceRegistry, TelemetryTools

MAXIMUM = 262144
IDENTITY = ("offline-mock", "gpt-6-luna", "docker-session")


class SocketProvider:
    def __init__(self, channel, pricing):
        self.channel, self.pricing = channel, pricing
        self.counts = {}

    def prepare(self, payload, *, model, max_output_tokens):
        validate_model_payload(payload)
        return PreparedRequest.from_payload(
            payload, model=model, max_output_tokens=max_output_tokens
        )

    def input_upper_bound(self, request):
        if request not in self.counts:
            if len(self.counts) >= 8:
                raise ValueError("count cache full")
            result = self.channel.request("count", asdict(request))
            count = result["input_tokens"]
            if type(count) is not int or count < 0:
                raise ValueError("invalid count")
            self.counts[request] = count
        return self.counts[request]

    def complete(self, request):
        if request not in self.counts:
            raise ValueError("uncounted request")
        result = self.channel.request("complete", asdict(request))
        return ProviderResult(
            result["content"],
            Usage(result["input_tokens"], result["output_tokens"], Decimal(result["cost_usd"])),
            result["error"],
        )


class MockAPI:
    """Fake outputs backed by actual observations, never an accuracy evaluation."""

    def __init__(self, fault="none", audit_path=None):
        self.fault = fault
        self.audit_path = audit_path
        if audit_path:
            Path(audit_path).write_text('{"http_calls":0,"model_calls":0}')
        self.calls = 0
        self.model_calls = 0

    def __call__(self, request):
        validate_destination(str(request.url))
        body = json.loads(request.content)
        data = json.loads(body["input"])
        self.calls += 1
        is_count = request.url.path.endswith("/input_tokens")
        if not is_count:
            self.model_calls += 1
        if self.audit_path:
            temporary = Path(self.audit_path + ".tmp")
            temporary.write_text(
                json.dumps({"http_calls": self.calls, "model_calls": self.model_calls})
            )
            temporary.replace(self.audit_path)
        if is_count:
            if self.fault == "count_failure":
                return httpx.Response(500, json={"error": {"message": "mock"}})
            return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 10})
        if self.fault == "delay":
            time.sleep(5)
        if data["phase"] == "investigate":
            ctx = data["context"]
            indicator = ctx["candidates"][0]
            service = next(s for s in ctx["services"] if indicator.startswith(s + "_"))
            content = {
                "query": {
                    "kind": "metrics",
                    "service": service,
                    "start": ctx["start"],
                    "end": ctx["end"],
                    "limit": 1,
                    "indicator": indicator,
                }
            }
        else:
            obs = data["observations"][0]
            field = obs["arguments"]["indicator"]
            content = {
                "hypotheses": [
                    {
                        "candidate": field,
                        "explanation": "offline fixture observation",
                        "evidence_ids": [obs["evidence_id"]],
                        "claims": [
                            {
                                "evidence_id": obs["evidence_id"],
                                "record_index": 0,
                                "field": field,
                                "value": obs["records"][0][field],
                            }
                        ],
                    }
                ]
            }
        result = {
            "id": "resp_mock",
            "object": "response",
            "created_at": 0,
            "model": body["model"],
            "status": "completed",
            "output": [
                {
                    "id": "msg_mock",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": json.dumps(content), "annotations": []}
                    ],
                }
            ],
        }
        if self.fault != "unknown_usage":
            result["usage"] = {
                "input_tokens": 10,
                "output_tokens": 50,
                "total_tokens": 60,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            }
        return httpx.Response(200, json=result)


def worker_handler(config):
    price = Pricing(Decimal("0.001"), Decimal("0.002"))
    api = MockAPI(config.get("fault", "none"), config.get("audit_path"))
    provider = OpenAIProvider(transport=httpx.MockTransport(api), pricing=price)
    counted = set()

    def handle(operation, data):
        request = PreparedRequest(**data)
        if request.model != IDENTITY[1]:
            raise ValueError("model mismatch")
        if operation == "count":
            count = provider.input_upper_bound(request)
            counted.add(request)
            return {"input_tokens": count}
        if request not in counted:
            raise ValueError("count required")
        counted.remove(request)
        result = provider.complete(request)
        if result.usage is None:
            raise ValueError("usage unavailable")
        return {
            "content": result.content,
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cost_usd": str(result.usage.cost_usd),
            "error": result.error,
        }

    return handle


def isolation(config, role):
    private = True
    for path in config["private_paths"]:
        try:
            with open(path, "rb"):
                pass
            private = False
        except (FileNotFoundError, PermissionError):
            pass
    if role != "agent" and Path("/blind").exists():
        private = False
    network = True
    for host in ("1.1.1.1", "192.168.96.1"):
        try:
            with socket.create_connection((host, 443), timeout=0.3):
                network = False
        except OSError as exc:
            if exc.errno not in (101, 113, 1, 13):
                network = False
    return {
        "private_unavailable": private,
        "tcp_denied": network,
        "agent_channel_absent": role != "agent" or not Path("/channel").exists(),
        "agent_auth_absent": role != "agent" or "key" not in config,
    }


def budget(data):
    return Budget(**{**data, "max_cost_usd": Decimal(data["max_cost_usd"])})


class ReplayTestClient(ChannelClient):
    """Explicit successful-response replay test only; never retries an uncertain exchange."""

    def _exchange(self, body):
        first = super()._exchange(body)
        second = super()._exchange(body)
        if first != second:
            raise ValueError("worker replay mismatch")
        return first


def broker(config):
    price = Pricing(Decimal("0.001"), Decimal("0.002"))
    client_type = ReplayTestClient if config.get("fault") == "uds_replay" else ChannelClient
    client = client_type(
        "/channel/worker.sock",
        Credentials(config["session"], bytes.fromhex(config["key"])),
        timeout=1,
    )
    provider = SocketProvider(client, price)
    case_budget, total_budget = budget(config["case_budget"]), budget(config["total_budget"])
    session = SessionProvider(
        case_budget,
        1,
        price,
        provider=provider,
        provider_name=IDENTITY[0],
        model=IDENTITY[1],
        code_version=IDENTITY[2],
    )
    ledger = OfflineBroker(
        session,
        provider_name=IDENTITY[0],
        model=IDENTITY[1],
        code_version=IDENTITY[2],
        budget=total_budget,
        pricing=price,
    )
    cache = ReplayCache(8)
    _write({"type": "ready", "checks": isolation(config, "broker")}, MAXIMUM)
    while True:
        request = _read(MAXIMUM)
        if request == {"type": "stop"}:
            return
        try:
            validate_request(request)
            validate_model_payload(request["payload"])
            response = cache.lookup(request)
            if response is None:
                content = ledger.request(request["payload"])
                response = {
                    "version": 1,
                    "type": "response",
                    "request_id": request["request_id"],
                    "content": content,
                    "metadata": ledger.summary(),
                    "case_metadata": session.cases[
                        request["payload"]["context"]["case_id"]
                    ].summary(),
                }
                validate_response(request, response, identity=IDENTITY)
                cache.store(request, response)
            _write(response, MAXIMUM)
        except Exception:  # noqa: BLE001 - no telemetry or exception text across IPC
            _write(
                {
                    "type": "error",
                    "reason": "broker_failed",
                    "calls": ledger.summary()["calls"],
                    "channel_failure": client.failure,
                    "ledger_failure": ledger.failure,
                },
                MAXIMUM,
            )
            return


class CheckedRemote(RemoteBroker):
    def _exchange(self, request):
        _write(request, self.maximum)
        response = _read(self.maximum)
        validate_response(request, response, identity=IDENTITY)
        self.metadata = response["case_metadata"]
        return response["content"]


def agent(config):
    checks = isolation(config, "agent")
    _write({"type": "ready", "checks": checks}, MAXIMUM)
    if not all(checks.values()):
        return
    b = budget(config["case_budget"])
    remote = CheckedRemote(MAXIMUM, b)
    tools = TelemetryTools(Path("/blind"), config["case_id"], EvidenceRegistry(config["case_id"]))
    state = run_agent(
        tools,
        LLMReasoner(remote),
        Limits(b.max_tool_calls, b.max_iterations),
        ObservationLimits(1, 4000),
    )
    if state["result"] is None:
        _write({"type": "done", "success": False}, MAXIMUM)
        return
    before = remote.summary()["calls"]
    remote._exchange(remote.latest)
    _write(
        {
            "type": "done",
            "success": True,
            "calls": remote.summary()["calls"],
            "replay_equal": remote.summary()["calls"] == before,
        },
        MAXIMUM,
    )


def main():
    config = _read(MAXIMUM)
    role = sys.argv[1]
    if role == "worker":
        checks = isolation(config, role)
        _write({"type": "ready", "checks": checks}, MAXIMUM)
        if not all(checks.values()):
            return
        serve(
            "/channel/worker.sock",
            Credentials(config["session"], bytes.fromhex(config["key"])),
            worker_handler(config),
        )
    elif role == "broker":
        broker(config)
    elif role == "agent":
        agent(config)
    else:
        raise ValueError("invalid role")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - redact all role failure details
        _write({"type": "error", "reason": "role_failed"}, MAXIMUM)
        raise SystemExit(2) from None
