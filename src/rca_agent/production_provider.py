"""Separated production interfaces. No operator authorization is minted automatically."""

import os
import time
from dataclasses import dataclass

import httpx

from rca_agent.egress_policy import tls_context
from rca_agent.egress_proxy import EgressProxy
from rca_agent.openai_provider import OpenAIProvider


@dataclass(frozen=True)
class LivePermit:
    user_approved: bool = False
    os_policy_verified: bool = False
    development_provenance_verified: bool = False
    budget_and_retention_approved: bool = False

    def require(self):
        if not all(
            value is True
            for value in (
                self.user_approved,
                self.os_policy_verified,
                self.development_provenance_verified,
                self.budget_and_retention_approved,
            )
        ):
            raise RuntimeError("LIVE_BLOCKED")


class ProductionProvider:
    """Worker-side SDK formatter over a credential-free authenticated egress callback."""

    def __init__(self, transport, pricing, permit):
        self.permit = permit

        def guarded(request):
            self.permit.require()
            result = transport(request.method, str(request.url), request.content)
            return httpx.Response(result["status"], json=result["body"])

        # The existing Mock-only class is not weakened: no direct network transport is accepted.
        self.formatter = OpenAIProvider(transport=httpx.MockTransport(guarded), pricing=pricing)

    def prepare(self, payload, *, model, max_output_tokens):
        return self.formatter.prepare(payload, model=model, max_output_tokens=max_output_tokens)

    def input_upper_bound(self, request):
        self.permit.require()
        return self.formatter.input_upper_bound(request)

    def complete(self, request):
        self.permit.require()
        return self.formatter.complete(request)


class ProductionTLS(EgressProxy):
    """Proxy-only system-CA transport; deployment must wrap it with Budget and journal."""

    def __init__(self, *, permit, lease_provider, key_reader, audit_path, timeout):
        self.permit, self.lease_provider, self.key_reader = permit, lease_provider, key_reader
        self.context = tls_context()  # No custom CA/file/config parameter and no Mock root.
        self.path = audit_path
        self.forward_timeout = timeout
        if not 0 < timeout < float("inf"):
            raise ValueError("explicit timeout required")
        self.calls = self.models = 0
        self._credential = None

    def forward(self, operation, data):
        self.permit.require()
        if os.getuid() != 10005:
            raise RuntimeError("Proxy-only credential boundary")
        lease = self.lease_provider()
        if lease.mock or lease.port != 443:
            raise RuntimeError("LIVE_BLOCKED: production DNS lease required")
        address = lease.addresses[self.calls % len(lease.addresses)]
        lease.authorize(address, 443, time.time(), self.forward_timeout)
        self.ip, self.port = address, 443
        return super().forward(operation, data)

    def request_headers(self):
        self.permit.require()
        if os.getuid() != 10005:
            raise RuntimeError("Proxy-only credential boundary")
        if self._credential is None:
            value = self.key_reader()
            if (
                not isinstance(value, str)
                or not 1 <= len(value) <= 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in value)
            ):
                raise ValueError("invalid API credential format")
            self._credential = value
        return {"Content-Type": "application/json", "Authorization": "Bearer " + self._credential}
