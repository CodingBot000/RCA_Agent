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

    def __init__(self, fault="none", audit_path=None, signals=None):
        self.fault = fault
        self.signals = signals or ["metrics"]
        self.audit_path = audit_path
        if audit_path:
            Path(audit_path).write_text('{"http_calls":0,"model_calls":0}')
        self.calls = 0
        self.model_calls = 0

    def record(self, path):
        self.calls += 1
        is_count = path.endswith("/input_tokens")
        if not is_count:
            self.model_calls += 1
        if self.audit_path:
            temporary = Path(self.audit_path + ".tmp")
            temporary.write_text(
                json.dumps({"http_calls": self.calls, "model_calls": self.model_calls})
            )
            temporary.replace(self.audit_path)
        return is_count

    def __call__(self, request):
        validate_destination(str(request.url))
        body = json.loads(request.content)
        data = json.loads(body["input"])
        is_count = self.record(request.url.path)
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
            seen = {item["source"] for item in data["observations"]}
            kind = next((signal for signal in self.signals if signal not in seen), "metrics")
            query = {
                "kind": kind,
                "service": service,
                "start": ctx["start"],
                "end": ctx["end"],
                "limit": 1,
            }
            if kind == "metrics":
                query["indicator"] = indicator
            content = {"query": query}
        else:
            observations = data["observations"]
            if not set(self.signals) <= {item["source"] for item in observations}:
                content = {"hypotheses": []}
            else:
                candidate = data["context"]["candidates"][0]
                claims = []
                for obs in observations:
                    field = (
                        obs["arguments"]["indicator"]
                        if obs["source"] == "metrics"
                        else "message"
                        if obs["source"] == "logs"
                        else "duration"
                    )
                    claims.append(
                        {
                            "evidence_id": obs["evidence_id"],
                            "record_index": 0,
                            "field": field,
                            "value": obs["records"][0][field],
                        }
                    )
                content = {
                    "hypotheses": [
                        {
                            "candidate": candidate,
                            "explanation": "offline multi-signal fixture",
                            "evidence_ids": [obs["evidence_id"] for obs in observations],
                            "claims": claims,
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
    if config.get("live_mode") and not config.get("egress"):
        raise ValueError("LIVE_BLOCKED: production Worker requires authenticated Proxy")
    price = Pricing(
        Decimal(config.get("pricing", {}).get("input_per_token", "0.001")),
        Decimal(config.get("pricing", {}).get("output_per_token", "0.002")),
    )
    api = MockAPI(config.get("fault", "none"), config.get("audit_path"), config.get("signals"))
    transport = api
    if config.get("egress"):
        client_type = ReplayTestClient if config["egress"].get("replay") else ChannelClient
        channel = client_type(
            "/proxychannel/proxy.sock",
            Credentials(config["egress"]["session"], bytes.fromhex(config["egress"]["key"])),
            timeout=config.get("transport_timeout", 0) + 2 if config.get("live_mode") else 2,
            server_uid=10005,
            server_gid=10002,
        )

        def transport(request):
            validate_destination(str(request.url))
            operation = "count" if api.record(request.url.path) else "complete"
            result = channel.request(
                operation,
                {
                    "method": request.method,
                    "url": str(request.url),
                    "body": json.loads(request.content),
                },
            )
            if type(result.get("status")) is not int or not 100 <= result["status"] <= 599:
                raise ValueError("invalid proxy HTTP status")
            return httpx.Response(result["status"], json=result["body"])

    if config.get("live_mode"):
        from rca_agent.production_provider import LivePermit, ProductionProvider

        def forward(method, url, content):
            response = transport(httpx.Request(method, url, content=content))
            return {"status": response.status_code, "body": response.json()}

        provider = ProductionProvider(forward, price, LivePermit(**config["permit"]))
    else:
        provider = OpenAIProvider(transport=httpx.MockTransport(transport), pricing=price)
    counted = set()

    def handle(operation, data):
        request = PreparedRequest(**data)
        if request.model != tuple(config.get("identity", IDENTITY))[1]:
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
    price = Pricing(
        Decimal(config.get("pricing", {}).get("input_per_token", "0.001")),
        Decimal(config.get("pricing", {}).get("output_per_token", "0.002")),
    )
    client_type = ReplayTestClient if config.get("fault") == "uds_replay" else ChannelClient
    client = client_type(
        "/channel/worker.sock",
        Credentials(config["session"], bytes.fromhex(config["key"])),
        timeout=config["case_budget"]["timeout_seconds"] + 2
        if config.get("live_mode")
        else 3
        if config.get("egress")
        else 1,
    )
    provider = SocketProvider(client, price)
    identity = tuple(config.get("identity", IDENTITY))
    case_budget, total_budget = budget(config["case_budget"]), budget(config["total_budget"])
    session = SessionProvider(
        case_budget,
        1,
        price,
        provider=provider,
        provider_name=identity[0],
        model=identity[1],
        code_version=identity[2],
    )
    ledger = OfflineBroker(
        session,
        provider_name=identity[0],
        model=identity[1],
        code_version=identity[2],
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
                validate_response(request, response, identity=identity)
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
                    "settled_usage": {
                        key: ledger.summary()[key]
                        for key in ("input_tokens", "output_tokens", "total_tokens", "cost_usd")
                    },
                },
                MAXIMUM,
            )
            return


class CheckedRemote(RemoteBroker):
    def __init__(self, maximum, budget, identity=IDENTITY):
        super().__init__(maximum, budget)
        self.identity = identity

    def _exchange(self, request):
        _write(request, self.maximum)
        response = _read(self.maximum)
        validate_response(request, response, identity=self.identity)
        self.metadata = response["case_metadata"]
        return response["content"]


def agent(config):
    checks = isolation(config, "agent")
    _write({"type": "ready", "checks": checks}, MAXIMUM)
    if not all(checks.values()):
        return
    b = budget(config["case_budget"])
    remote = CheckedRemote(MAXIMUM, b, tuple(config.get("identity", IDENTITY)))
    tools = TelemetryTools(Path("/blind"), config["case_id"], EvidenceRegistry(config["case_id"]))
    state = run_agent(
        tools,
        LLMReasoner(remote),
        Limits(b.max_tool_calls, b.max_iterations),
        ObservationLimits(**config["observation_limits"])
        if config.get("live_mode")
        else ObservationLimits(1, 12000 if len(config.get("signals", [])) > 1 else 4000),
    )
    observed = sorted(
        {
            item["observation"].source
            for item in state["history"]
            if item.get("observation") is not None
        }
    )
    if config.get("all_signals") and not {"metrics", "logs", "traces"} <= set(observed):
        state["failure"] = "invalid_or_insufficient_evidence"
        state["result"] = None
    if config.get("private_artifact"):
        _write(
            {
                "type": "artifact",
                "result": asdict(state["result"]) if state["result"] is not None else None,
                "history": [
                    {k: (asdict(v) if k == "observation" else v) for k, v in item.items()}
                    for item in state["history"]
                ],
                "model_metadata": remote.summary(),
                "failure": state["failure"],
            },
            MAXIMUM,
        )
    if state["result"] is None:
        _write({"type": "done", "success": False, "failure": state["failure"]}, MAXIMUM)
        return
    before = remote.summary()["calls"]
    if not config.get("live_mode"):
        remote._exchange(remote.latest)
    _write(
        {
            "type": "done",
            "success": True,
            "calls": remote.summary()["calls"],
            "replay_equal": remote.summary()["calls"] == before,
            "signals_observed": observed,
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
