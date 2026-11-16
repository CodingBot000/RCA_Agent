"""Offline-only authenticated egress boundary with pinned local TLS Mock endpoint."""

import http.client
import ipaddress
import json
import os
import socket
import ssl
import threading
from decimal import Decimal
from pathlib import Path

from openai.lib._parsing._responses import type_to_text_format_param

from rca_agent.egress_policy import tls_context, validate_destination
from rca_agent.integrated_worker import MAXIMUM, isolation
from rca_agent.ipc_protocol import parse_frame, validate_model_payload, validate_request
from rca_agent.ipc_worker import _read, _write
from rca_agent.llm_reasoner import MODEL_INSTRUCTION, QUERY, Hypotheses
from rca_agent.model_broker import (
    Budget,
    OfflineBroker,
    PreparedRequest,
    Pricing,
    ProviderResult,
    Usage,
)
from rca_agent.openai_provider import QueryOutput
from rca_agent.request_journal import RequestJournal
from rca_agent.secure_uds import Credentials, serve

MAX_RESPONSE = 262144


def validate_http(operation, data):
    if (
        set(data) != {"method", "url", "body"}
        or data["method"] != "POST"
        or not isinstance(data["body"], dict)
    ):
        raise ValueError("invalid egress envelope")
    validate_destination(data["url"])
    path = "/v1/responses/input_tokens" if operation == "count" else "/v1/responses"
    if operation not in ("count", "complete") or data["url"] != "https://api.openai.com" + path:
        raise ValueError("operation/path mismatch")
    body = data["body"]
    common = {"model", "instructions", "input", "tools", "text"}
    allowed = (
        common
        if operation == "count"
        else common | {"store", "max_output_tokens", "stream", "service_tier"}
    )
    if (
        set(body) - allowed
        or not common <= set(body)
        or body["model"] not in ("gpt-6-luna", "gpt-6.1-terra")
        or body["instructions"] != MODEL_INSTRUCTION
        or body["tools"] != []
        or body.get("service_tier", "default") != "default"
    ):
        raise ValueError("unapproved request body")
    if operation == "complete" and (
        body.get("store") is not False
        or body.get("stream", False) is not False
        or type(body.get("max_output_tokens")) is not int
        or not 1 <= body["max_output_tokens"] <= 128000
    ):
        raise ValueError("unsafe output/storage settings")
    payload = parse_frame(body["input"].encode(), MAX_RESPONSE)
    output_type = QueryOutput if payload["phase"] == "investigate" else Hypotheses
    if body["text"] != {"format": type_to_text_format_param(output_type)}:
        raise ValueError("unapproved output schema")
    payload = {
        **payload,
        "instruction": body["instructions"],
        "schema": QUERY.json_schema()
        if payload["phase"] == "investigate"
        else Hypotheses.model_json_schema(),
    }
    validate_request({"version": 1, "type": "request", "request_id": "0" * 32, "payload": payload})
    validate_model_payload(payload)
    return path


def sanitize_upstream(operation, status, result):
    if status >= 400:
        return {"error": {"type": "upstream_http_error"}}
    if operation == "count":
        if (
            result.get("object") != "response.input_tokens"
            or type(result.get("input_tokens")) is not int
            or not 0 <= result["input_tokens"] <= 922000
        ):
            raise ValueError("invalid count response")
        return {"object": "response.input_tokens", "input_tokens": result["input_tokens"]}
    if result.get("object") != "response" or not isinstance(result.get("output"), list):
        raise ValueError("invalid model response")
    allowed = {
        "id",
        "object",
        "created_at",
        "model",
        "status",
        "output",
        "usage",
        "incomplete_details",
        "service_tier",
    }
    filtered = {k: v for k, v in result.items() if k in allowed}
    # Refusal text is not an analysis result; never forward arbitrary provider diagnostics.
    for item in filtered["output"]:
        for part in item.get("content", []):
            if part.get("type") == "refusal":
                part["refusal"] = "provider_refusal"
    return filtered


class PinnedTLS(http.client.HTTPSConnection):
    def __init__(self, ip, port, context):
        address = ipaddress.ip_address(ip)
        if getattr(address, "ipv4_mapped", None):
            raise ValueError("mapped IPv6 not allowed")
        self.family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        self.endpoint = (str(address), port, 0, 0) if address.version == 6 else (str(address), port)
        super().__init__("api.openai.com", 443, context=context, timeout=1)

    def connect(self):
        # No DNS lookup, URL-derived endpoint, environment proxy or CONNECT tunnel.
        with_socket = socket.socket(self.family, socket.SOCK_STREAM)
        try:
            with_socket.settimeout(self.timeout)
            with_socket.connect(self.endpoint)
            self.sock = self._context.wrap_socket(
                with_socket, server_hostname="api.openai.com", do_handshake_on_connect=False
            )
            self.sock.do_handshake()
        except BaseException:
            with_socket.close()
            raise


def binding(data):
    return json.dumps(
        {
            k: v
            for k, v in data["body"].items()
            if k not in ("store", "max_output_tokens", "stream", "service_tier")
        },
        sort_keys=True,
    )


class ProxyModelProvider:
    def __init__(self, proxy):
        self.proxy = proxy

    def prepare(self, data, *, model, max_output_tokens):
        if data["body"]["model"] != model or data["body"]["max_output_tokens"] != max_output_tokens:
            raise ValueError("session model/output mismatch")
        return PreparedRequest.from_payload(data, model=model, max_output_tokens=max_output_tokens)

    def input_upper_bound(self, request):
        return self.proxy.counts[binding(request.payload())]

    def complete(self, request):
        journal = getattr(self.proxy, "journal", None)
        digest = None
        if journal is not None:
            digest = journal.sent(
                request,
                self.proxy.request_ids[binding(request.payload())],
                self.input_upper_bound(request),
            )
        try:
            if getattr(self.proxy, "journal_fault", None) == "journal_before_send":
                os._exit(3)
            result = self.proxy.forward("complete", request.payload())
            if getattr(self.proxy, "journal_fault", None) == "journal_after_response":
                os._exit(3)
            raw = result["body"].get("usage")
            usage = None
            if (
                isinstance(raw, dict)
                and all(
                    type(raw.get(k)) is int and raw[k] >= 0
                    for k in ("input_tokens", "output_tokens", "total_tokens")
                )
                and raw["total_tokens"] == raw["input_tokens"] + raw["output_tokens"]
            ):
                usage = Usage(
                    raw["input_tokens"],
                    raw["output_tokens"],
                    self.proxy.pricing.cost(raw["input_tokens"], raw["output_tokens"]),
                )
            if journal is not None:
                if usage is None:
                    journal.uncertain(digest)
                else:
                    journal.completed(
                        digest,
                        {
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                            "cost_usd": str(usage.cost_usd),
                        },
                        response_id=result["body"].get("id"),
                    )
            if result["body"].get("status") == "completed":
                try:
                    for item in result["body"].get("output", []):
                        for part in item.get("content", []):
                            if part.get("type") == "output_text":
                                parse_frame(part["text"].encode(), MAX_RESPONSE)
                except (ValueError, TypeError, UnicodeError):
                    self.proxy.record_error("RESPONSE_INVALID")
                    return ProviderResult(None, usage, "invalid_provider_response")
            return ProviderResult(json.dumps(result), usage)
        except BaseException:
            if journal is not None and digest is not None:
                journal.uncertain(digest)
            raise


class EgressProxy:
    def __init__(self, config):
        self.ip, self.port, self.context = self.endpoint_context(config)
        b = Budget(
            **{**config["budget"], "max_cost_usd": Decimal(config["budget"]["max_cost_usd"])}
        )
        if b.max_retries != 0 or b.max_concurrency != 1:
            raise ValueError("unsupported Proxy concurrency/retry")
        self.pricing = Pricing(
            Decimal(config["pricing"]["input_per_token"]),
            Decimal(config["pricing"]["output_per_token"]),
        )
        self.forward_timeout = min(config.get("transport_timeout", 1.5), b.timeout_seconds)
        self.ledger = OfflineBroker(
            ProxyModelProvider(self),
            provider_name="openai-egress" if config.get("mode") == "production" else "egress-mock",
            model=config["model"],
            code_version="production-tls" if config.get("mode") == "production" else "tls-proxy",
            budget=b,
            pricing=self.pricing,
        )
        self.journal = (
            RequestJournal(config["journal_path"]) if config.get("journal_path") else None
        )
        self.journal_fault = config.get("fault")
        self.request_ids = {}
        self.counts = {}
        self.spent = set()
        self.count_calls = 0
        self.blocked = False
        self.calls = self.models = 0
        self.path = Path("/proxychannel/audit.json")
        self.path.write_text('{"http_calls":0,"model_calls":0}')

    def endpoint_context(self, config):
        if (
            config.get("mode") != "offline-local-tls"
            or not ipaddress.ip_address(config["ip"]).is_private
        ):
            raise ValueError("LIVE_BLOCKED: local Mock endpoint required")
        self.ip, self.port = config["ip"], config["port"]
        if self.port != 8443:
            raise ValueError("Mock TLS port required")
        self.context = tls_context()
        self.context.load_verify_locations("/trust/ca.pem")
        if config.get("fault") == "tls_version":
            self.context.minimum_version = ssl.TLSVersion.TLSv1_3
        return self.ip, self.port, self.context

    def handle_authenticated(self, body):
        self.request_id = body["id"]
        return self(body["operation"], body["data"])

    def record_error(self, code):
        Path("/proxychannel/error.json").write_text(json.dumps({"code": code}))

    def __call__(self, operation, data):
        validate_http(operation, data)
        if self.blocked or self.ledger.blocked:
            raise ValueError("egress session blocked")
        key = binding(data)
        if key in self.spent:
            raise ValueError("spent request cannot be reauthorized")
        if data["body"]["model"] != self.ledger.model:
            raise ValueError("model not authorized for session")
        try:
            if operation == "count":
                if self.count_calls >= self.ledger.budget.max_calls:
                    raise ValueError("count limit")
                self.count_calls += 1
                result = self.forward(operation, data)
                if result["status"] == 200:
                    n = result["body"].get("input_tokens")
                    if type(n) is not int or n < 0:
                        raise ValueError("invalid count")
                    self.counts[key] = n
                return result
            if key not in self.counts:
                raise ValueError("input count required")
            if getattr(self, "journal", None) is not None:
                self.request_ids[key] = self.request_id
            self.spent.add(
                key
            )  # Consume before any model side effect, including uncertain failures.
            try:
                return json.loads(self.ledger.request(data))
            finally:
                self.counts.pop(key, None)
                Path("/proxychannel/ledger.json").write_text(json.dumps(self.ledger.summary()))
        except Exception:
            self.blocked = True
            raise

    def request_headers(self):
        return {"Content-Type": "application/json"}

    def forward(self, operation, data):
        path = validate_http(operation, data)
        self.calls += 1
        self.models += int(operation == "complete")
        self.path.write_text(json.dumps({"http_calls": self.calls, "model_calls": self.models}))
        conn = PinnedTLS(self.ip, self.port, self.context)
        conn.timeout = min(conn.timeout, self.forward_timeout)
        expired = threading.Event()
        control = {"socket": None}

        def stop():
            expired.set()
            sock = control["socket"] or conn.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            conn.close()

        timer = threading.Timer(self.forward_timeout, stop)
        timer.daemon = True
        timer.start()
        try:
            encoded = json.dumps(data["body"], ensure_ascii=False, allow_nan=False).encode()
            conn.request("POST", path, body=encoded, headers=self.request_headers())
            # HTTP/1.0 may detach conn.sock while its response file still owns the FD.
            # An independent TCP descriptor lets the deadline interrupt that read too.
            control["socket"] = socket.socket(fileno=os.dup(conn.sock.fileno()))
            response = conn.getresponse()
            if 300 <= response.status < 400:
                self.record_error("REDIRECT_DENIED")
                raise ValueError("redirect forbidden")
            content = response.read(MAX_RESPONSE + 1)
            if expired.is_set():
                raise TimeoutError("Proxy deadline exceeded")
            result = parse_frame(content, MAX_RESPONSE)
            if response.status >= 400:
                self.record_error(
                    "HTTP_429"
                    if response.status == 429
                    else "HTTP_5XX"
                    if response.status >= 500
                    else "HTTP_ERROR"
                )
                self.blocked = True
            if operation == "complete" and response.status == 200:
                if not isinstance(result.get("usage"), dict):
                    self.record_error("USAGE_UNAVAILABLE")
                    self.blocked = True
                elif any(
                    part.get("type") == "refusal"
                    for item in result.get("output", [])
                    for part in item.get("content", [])
                ):
                    self.record_error("REFUSAL")
                    self.blocked = True
            result = sanitize_upstream(operation, response.status, result)
            credential = getattr(self, "_credential", None)
            if credential and credential in json.dumps(result):
                raise ValueError("credential reflection refused")
            return {"status": response.status, "body": result}
        except ssl.SSLCertVerificationError as exc:
            self.record_error("TLS_HOSTNAME" if exc.verify_code == 62 else "TLS_CERTIFICATE")
            raise
        except ssl.SSLError:
            self.record_error("TIMEOUT" if expired.is_set() else "TLS_PROTOCOL")
            raise
        except Exception as exc:
            if expired.is_set() or isinstance(exc, TimeoutError):
                self.record_error("TIMEOUT")
            raise
        finally:
            timer.cancel()
            conn.close()
            if control["socket"] is not None:
                control["socket"].close()


def main():
    try:
        config = _read(MAXIMUM)
        proxy = EgressProxy(config)
        checks = isolation(config, "proxy")
        _write({"type": "ready", "checks": checks}, MAXIMUM)
        if not all(checks.values()):
            raise ValueError("proxy isolation failed")
        serve(
            "/proxychannel/proxy.sock",
            Credentials(config["session"], bytes.fromhex(config["key"])),
            proxy,
            peer_uid=10001,
            peer_gid=10002,
        )
    except Exception:  # noqa: BLE001 - no TLS payload or authentication exception logging
        _write({"type": "error", "reason": "proxy_failed"}, MAXIMUM)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
