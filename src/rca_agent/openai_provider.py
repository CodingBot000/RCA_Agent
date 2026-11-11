"""Offline Responses API adapter. Network transports are intentionally forbidden."""

import json

import httpx
from openai import OpenAI
from openai.lib._parsing._responses import type_to_text_format_param
from pydantic import BaseModel, ConfigDict

from rca_agent.ipc_protocol import validate_model_payload
from rca_agent.llm_reasoner import Finish, Hypotheses, LogsQuery, MetricsQuery, TracesQuery
from rca_agent.model_broker import PreparedRequest, Pricing, ProviderResult, Usage


class QueryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: MetricsQuery | LogsQuery | TracesQuery | Finish


class OpenAIProvider:
    """Mock-only SDK adapter; it is not an isolated production broker."""

    def __init__(self, *, transport: httpx.MockTransport, pricing: Pricing):
        if not isinstance(transport, httpx.MockTransport):
            raise TypeError("offline mock transport required")
        if not isinstance(pricing, Pricing):
            raise TypeError("explicit pricing required")
        self.pricing = pricing

        def handle(request):
            if request.method != "POST" or str(request.url) not in {
                "https://api.openai.com/v1/responses",
                "https://api.openai.com/v1/responses/input_tokens",
            }:
                raise ValueError("unapproved endpoint")
            return transport.handle_request(request)

        client = httpx.Client(
            transport=httpx.MockTransport(handle), trust_env=False, follow_redirects=False
        )
        self.client = OpenAI(
            api_key="offline-test-key",
            base_url="https://api.openai.com/v1",
            max_retries=0,
            http_client=client,
        )

    def prepare(self, payload, *, model, max_output_tokens):
        validate_model_payload(payload)
        return PreparedRequest.from_payload(
            payload, model=model, max_output_tokens=max_output_tokens
        )

    def _common(self, request):
        payload = request.payload()
        validate_model_payload(payload)
        if request.model not in {"gpt-6-luna", "gpt-6.1-terra"}:
            raise ValueError("unsupported model")
        output_type = QueryOutput if payload["phase"] == "investigate" else Hypotheses
        body = {
            key: value for key, value in payload.items() if key not in {"instruction", "schema"}
        }
        common = {
            "model": request.model,
            "instructions": payload["instruction"],
            "input": json.dumps(body, ensure_ascii=False, allow_nan=False),
            "tools": [],
        }
        return common, output_type

    def input_upper_bound(self, request: PreparedRequest) -> int:
        common, output_type = self._common(request)
        response = self.client.responses.input_tokens.with_raw_response.count(
            **common, text={"format": type_to_text_format_param(output_type)}
        )
        data = response.http_response.json()
        if not isinstance(data, dict) or data.get("object") != "response.input_tokens":
            raise ValueError("invalid input token response")
        count = data.get("input_tokens")
        if type(count) is not int or count < 0:
            raise ValueError("invalid input token count")
        return count

    def complete(self, request: PreparedRequest) -> ProviderResult:
        common, output_type = self._common(request)
        payload = request.payload()
        try:
            raw = self.client.responses.with_raw_response.parse(
                **common,
                text_format=output_type,
                store=False,
                max_output_tokens=request.max_output_tokens,
            )
            raw_usage = raw.http_response.json().get("usage")
            if not isinstance(raw_usage, dict) or any(
                type(raw_usage.get(key)) is not int or raw_usage[key] < 0
                for key in ("input_tokens", "output_tokens", "total_tokens")
            ):
                return ProviderResult(None, None, "usage_unavailable")
            response = raw.parse()
        except Exception:  # noqa: BLE001 - no provider error body may escape
            return ProviderResult(None, None, "provider_error")
        usage = response.usage
        if usage is None:
            return ProviderResult(None, None, "usage_unavailable")
        inp, out = usage.input_tokens, usage.output_tokens
        if (
            type(inp) is not int
            or type(out) is not int
            or inp < 0
            or out < 0
            or type(usage.total_tokens) is not int
            or usage.total_tokens != inp + out
        ):
            return ProviderResult(None, None, "usage_unavailable")
        counted = Usage(inp, out, self.pricing.cost(inp, out))
        if response.status != "completed" or response.output_parsed is None:
            return ProviderResult(None, counted, "provider_error")
        parsed = response.output_parsed
        if payload["phase"] == "investigate":
            parsed = parsed.query
        return ProviderResult(parsed.model_dump_json(), counted)
