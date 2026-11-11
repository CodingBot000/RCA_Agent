"""Two roles for isolated, fake-only IPC validation. No provider network code exists."""

import argparse
import json
import os
import socket
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from rca_agent.graph import Limits, ObservationLimits, run_agent
from rca_agent.ipc_protocol import (
    IPCError,
    ReplayCache,
    encode_frame,
    parse_frame,
    validate_request,
)
from rca_agent.llm_reasoner import MODEL_INSTRUCTION, QUERY, Hypotheses, LLMReasoner
from rca_agent.model_broker import (
    BrokerFailure,
    Budget,
    FakeProvider,
    OfflineBroker,
    PreparedRequest,
    Pricing,
    ProviderResult,
    Usage,
)
from rca_agent.telemetry import EvidenceRegistry, TelemetryTools


def _read(maximum):
    data = sys.stdin.buffer.readline(maximum + 1)
    if not data or not data.endswith(b"\n"):
        raise IPCError("invalid or incomplete frame")
    return parse_frame(data, maximum)


def _write(value, maximum):
    sys.stdout.buffer.write(encode_frame(value, maximum))
    sys.stdout.buffer.flush()


def _valid_payload(payload):
    expected = (
        QUERY.json_schema() if payload["phase"] == "investigate" else Hypotheses.model_json_schema()
    )
    if payload["instruction"] != MODEL_INSTRUCTION or payload["schema"] != expected:
        raise IPCError("unapproved instruction or schema")
    for obs in payload["observations"]:
        if set(obs["arguments"]) - {"service", "start", "end", "limit", "indicator"}:
            raise IPCError("unapproved observation arguments")


class FixtureProvider(FakeProvider):
    def __init__(self, pricing):
        super().__init__([], 1)
        self.pricing = pricing

    def complete(self, request):
        payload = request.payload()
        if payload["phase"] == "investigate":
            context = payload["context"]
            result = {
                "kind": "metrics",
                "service": "api",
                "start": context["start"],
                "end": context["end"],
                "limit": 1,
                "indicator": "api_cpu",
            }
        else:
            obs = payload["observations"][0]
            field = obs["arguments"].get("indicator")
            if not isinstance(field, str) or field not in obs["records"][0]:
                raise IPCError("fixture metric observation unavailable")
            result = {
                "hypotheses": [
                    {
                        "candidate": payload["context"]["candidates"][0],
                        "explanation": "synthetic IPC check",
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
        return ProviderResult(json.dumps(result), Usage(1, 1, self.pricing.cost(1, 1)))


class SessionProvider:
    """Nest a per-case ledger under the evaluation-wide ledger."""

    def __init__(self, case_budget, max_cases, pricing):
        self.case_budget, self.max_cases, self.pricing = case_budget, max_cases, pricing
        self.cases = {}

    def prepare(self, payload, *, model, max_output_tokens):
        return PreparedRequest.from_payload(
            payload, model=model, max_output_tokens=max_output_tokens
        )

    def input_upper_bound(self, request):
        return 1

    def complete(self, request):
        payload = request.payload()
        case_id = payload["context"]["case_id"]
        if case_id not in self.cases:
            if len(self.cases) >= self.max_cases:
                raise IPCError("case limit exhausted")
            self.cases[case_id] = OfflineBroker(
                FixtureProvider(self.pricing),
                provider_name="fake",
                model="ipc-fixture",
                code_version="ipc-test",
                budget=replace(self.case_budget, max_retries=0),
                pricing=self.pricing,
            )
        case = self.cases[case_id]
        before = case.summary()
        try:
            content = case.request(payload)
        except BrokerFailure:
            after = case.summary()
            if case.blocked:
                raise
            usage = Usage(
                after["input_tokens"] - before["input_tokens"],
                after["output_tokens"] - before["output_tokens"],
                Decimal(after["cost_usd"]) - Decimal(before["cost_usd"]),
            )
            return ProviderResult(
                None, usage, "retryable" if case._last_retryable else "nonretryable"
            )
        after = case.summary()
        usage = Usage(
            after["input_tokens"] - before["input_tokens"],
            after["output_tokens"] - before["output_tokens"],
            Decimal(after["cost_usd"]) - Decimal(before["cost_usd"]),
        )
        return ProviderResult(content, usage)


def _fork_denied():
    try:
        pid = os.fork()
    except PermissionError:
        pass
    except OSError:
        return False
    else:
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        return False
    try:
        pid = os.posix_spawn("/usr/bin/true", ["true"], {})
    except PermissionError:
        return True
    except OSError:
        return False
    os.waitpid(pid, 0)
    return False


def broker(
    maximum, capacity, max_cases, case_budget, budget, pricing, probe_path, key_path, blind_file
):
    session = SessionProvider(case_budget, max_cases, pricing)
    service = OfflineBroker(
        session,
        provider_name="fake",
        model="ipc-fixture",
        code_version="ipc-test",
        budget=budget,
        pricing=pricing,
    )
    cache = ReplayCache(capacity)
    private_denied, network_denied = _probe(probe_path, True)
    blind_denied, _ = _probe(blind_file, False)
    key_denied, _ = _probe(key_path, False)
    _write(
        {
            "version": 1,
            "type": "ready",
            "private_denied": private_denied,
            "key_denied": key_denied,
            "blind_denied": blind_denied,
            "network_denied": network_denied,
            "fork_denied": _fork_denied(),
        },
        maximum,
    )
    while True:
        try:
            request = _read(maximum)
            if request == {"version": 1, "type": "stop"}:
                return
            validate_request(request)
            _valid_payload(request["payload"])
            previous = cache.lookup(request)
            if previous is None:
                content = service.request(request["payload"])
                response = {
                    "version": 1,
                    "type": "response",
                    "request_id": request["request_id"],
                    "content": content,
                    "metadata": service.summary(),
                    "case_metadata": session.cases[
                        request["payload"]["context"]["case_id"]
                    ].summary(),
                }
                cache.store(request, response)
            else:
                response = previous
            _write(response, maximum)
        except Exception:  # noqa: BLE001 - never leak content or tracebacks across IPC
            _write({"version": 1, "type": "error", "reason": "broker_failure"}, maximum)
            return


class RemoteBroker:
    def __init__(self, maximum, budget):
        self.maximum, self.budget = maximum, budget
        self.latest = None
        self.metadata = {
            "provider": "fake",
            "model": "ipc-fixture",
            "code_version": "ipc-test",
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": "0",
            "failure": "",
        }

    def request(self, payload):
        request = {
            "version": 1,
            "type": "request",
            "request_id": os.urandom(16).hex(),
            "payload": payload,
        }
        self.latest = request
        return self._exchange(request)

    def _exchange(self, request):
        _write(request, self.maximum)
        value = _read(self.maximum)
        if (
            set(value) != {"version", "type", "request_id", "content", "metadata", "case_metadata"}
            or value["version"] != 1
            or value["type"] != "response"
            or value["request_id"] != request["request_id"]
            or not isinstance(value["content"], str)
            or not isinstance(value["metadata"], dict)
            or not isinstance(value["case_metadata"], dict)
        ):
            raise IPCError("invalid broker response")
        self.metadata = value["case_metadata"]
        return value["content"]

    def summary(self):
        return dict(self.metadata)

    def execution_limits(self):
        return self.budget.max_tool_calls, self.budget.max_iterations

    def begin_run(self):
        return True

    def end_run(self):
        pass


def _probe(path, network):
    denied_file = False
    try:
        with open(path, "rb"):
            pass
    except PermissionError:
        denied_file = True
    except OSError:
        denied_file = False
    denied_network = True
    if network:
        for address in ("127.0.0.1", "192.0.2.1"):
            try:
                s = socket.socket()
                s.settimeout(0.25)
                try:
                    s.connect((address, 9))
                    denied_network = False
                finally:
                    s.close()
            except PermissionError:
                pass
            except OSError:
                denied_network = False
    return denied_file, denied_network


def agent(
    maximum, budget, probe_path, key_path, blind_file, blind_dir, case_id, obs_records, obs_chars
):
    file_denied, network_denied = _probe(probe_path, True)
    key_denied, _ = _probe(key_path, False)
    try:
        with open(blind_file, "rb") as f:
            read_blind = f.read() == b"synthetic-blind"
    except OSError:
        read_blind = False
    fork_denied = _fork_denied()
    _write(
        {
            "version": 1,
            "type": "agent_ready",
            "file_denied": file_denied,
            "key_denied": key_denied,
            "network_denied": network_denied,
            "fork_denied": fork_denied,
            "blind_readable": read_blind,
        },
        maximum,
    )
    if not all((file_denied, key_denied, network_denied, fork_denied, read_blind)):
        return
    remote = RemoteBroker(maximum, budget)
    tools = TelemetryTools(Path(blind_dir), case_id, EvidenceRegistry(case_id))
    state = run_agent(
        tools,
        LLMReasoner(remote),
        Limits(budget.max_tool_calls, budget.max_iterations),
        ObservationLimits(obs_records, obs_chars),
    )
    before_replay = remote.summary()["calls"]
    replay = remote._exchange(remote.latest)
    after_replay = remote.summary()["calls"]
    _write(
        {
            "version": 1,
            "type": "done",
            "file_denied": file_denied,
            "key_denied": key_denied,
            "network_denied": network_denied,
            "fork_denied": fork_denied,
            "blind_readable": read_blind,
            "graph_success": state["result"] is not None,
            "replay_equal": bool(json.loads(replay).get("hypotheses")),
            "calls_before_replay": before_replay,
            "calls_after_replay": after_replay,
            "calls_total": remote.summary()["calls"],
        },
        maximum,
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("role", choices=("agent", "broker"))
    p.add_argument("--max-frame", required=True, type=int)
    p.add_argument("--capacity", required=True, type=int)
    p.add_argument("--max-cases", required=True, type=int)
    p.add_argument("--case-budget", required=True)
    p.add_argument("--evaluation-budget", required=True)
    p.add_argument("--pricing", required=True)
    p.add_argument("--probe-path", default="")
    p.add_argument("--key-path", default="")
    p.add_argument("--blind-file", default="")
    p.add_argument("--blind-dir", default="")
    p.add_argument("--case-id", default="")
    p.add_argument("--obs-records", required=True, type=int)
    p.add_argument("--obs-chars", required=True, type=int)
    args = p.parse_args()
    if min(args.max_frame, args.capacity, args.max_cases) <= 0:
        raise SystemExit(2)

    def read_budget(raw):
        data = parse_frame(raw.encode(), args.max_frame)
        data["max_cost_usd"] = Decimal(data["max_cost_usd"])
        return Budget(**data)

    case_budget = read_budget(args.case_budget)
    evaluation_budget = read_budget(args.evaluation_budget)
    price_data = parse_frame(args.pricing.encode(), args.max_frame)
    if set(price_data) != {"input_per_token", "output_per_token"}:
        raise SystemExit(2)
    pricing = Pricing(
        Decimal(price_data["input_per_token"]), Decimal(price_data["output_per_token"])
    )
    if (
        evaluation_budget.max_retries > case_budget.max_retries
        or evaluation_budget.output_tokens_per_call != case_budget.output_tokens_per_call
    ):
        raise SystemExit(2)
    if args.role == "broker":
        broker(
            args.max_frame,
            args.capacity,
            args.max_cases,
            case_budget,
            evaluation_budget,
            pricing,
            args.probe_path,
            args.key_path,
            args.blind_file,
        )
    else:
        agent(
            args.max_frame,
            case_budget,
            args.probe_path,
            args.key_path,
            args.blind_file,
            args.blind_dir,
            args.case_id,
            args.obs_records,
            args.obs_chars,
        )


if __name__ == "__main__":
    main()
