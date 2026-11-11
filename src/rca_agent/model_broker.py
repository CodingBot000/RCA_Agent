"""Offline broker contract and fail-closed accounting. No external provider is included."""

import re
import time
from dataclasses import dataclass
from decimal import Decimal
from threading import BoundedSemaphore, Event, Lock, Thread
from typing import Protocol


class BrokerFailure(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Budget:
    max_calls: int
    max_tool_calls: int
    max_iterations: int
    max_input_tokens: int
    max_output_tokens: int
    max_total_tokens: int
    max_cost_usd: Decimal
    max_concurrency: int
    max_retries: int
    output_tokens_per_call: int
    timeout_seconds: float

    def __post_init__(self):
        positive = (
            self.max_calls,
            self.max_tool_calls,
            self.max_iterations,
            self.max_input_tokens,
            self.max_output_tokens,
            self.max_total_tokens,
            self.max_concurrency,
            self.output_tokens_per_call,
        )
        if (
            any(type(x) is not int or x < 1 for x in positive)
            or type(self.max_retries) is not int
            or self.max_retries < 0
        ):
            raise ValueError("explicit valid budget required")
        if (
            not isinstance(self.max_cost_usd, Decimal)
            or not self.max_cost_usd.is_finite()
            or self.max_cost_usd < 0
        ):
            raise ValueError("explicit finite Decimal cost required")
        if self.output_tokens_per_call > self.max_output_tokens:
            raise ValueError("per-call output exceeds output budget")
        if type(self.timeout_seconds) not in (int, float) or not 0 < self.timeout_seconds < float(
            "inf"
        ):
            raise ValueError("explicit finite positive timeout required")


@dataclass(frozen=True)
class Pricing:
    input_per_token: Decimal
    output_per_token: Decimal

    def __post_init__(self):
        if any(
            not isinstance(v, Decimal) or not v.is_finite() or v < 0
            for v in (self.input_per_token, self.output_per_token)
        ):
            raise ValueError("finite nonnegative Decimal pricing required")

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        return self.input_per_token * input_tokens + self.output_per_token * output_tokens


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class ProviderResult:
    content: str | None
    usage: Usage | None
    error: str | None = None


class Provider(Protocol):
    def input_upper_bound(self, payload: dict) -> int: ...
    def complete(self, payload: dict, max_output_tokens: int, *, model: str) -> ProviderResult: ...


class ModelBroker(Protocol):
    def request(self, payload: dict) -> str: ...
    def summary(self) -> dict: ...
    def execution_limits(self) -> tuple[int, int]: ...
    def begin_run(self) -> bool: ...
    def end_run(self) -> None: ...


def _bounded_call(function, timeout):
    if timeout <= 0:
        return False, None
    finished = Event()
    outcome = []

    def invoke():
        try:
            outcome.append(function())
        except BaseException as exc:  # noqa: BLE001 - convert all worker failures to a safe envelope
            outcome.append(exc)
        finally:
            finished.set()

    Thread(target=invoke, daemon=True).start()
    if not finished.wait(max(timeout, 0)):
        return False, None
    return True, outcome[0] if outcome else None


class OfflineBroker:
    """One-process budget ledger. Provider is trusted; this is not an OS isolation boundary."""

    def __init__(
        self,
        provider: Provider,
        *,
        provider_name: str,
        model: str,
        code_version: str,
        budget: Budget,
        pricing: Pricing,
    ):
        if not all(
            isinstance(x, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", x)
            and not x.lower().startswith(("sk-", "ghp_", "hf_", "bearer"))
            for x in (provider_name, model, code_version)
        ):
            raise ValueError("safe provider, model and code version identifiers are required")
        self.provider, self.provider_name, self.model, self.code_version = (
            provider,
            provider_name,
            model,
            code_version,
        )
        self.budget, self.pricing = budget, pricing
        self._lock = Lock()
        self._slots = BoundedSemaphore(budget.max_concurrency)
        self._run_slots = BoundedSemaphore(budget.max_concurrency)
        self.calls = self.input_tokens = self.output_tokens = self.active = 0
        self.cost = Decimal(0)
        self.reserved_input = self.reserved_output = 0
        self.reserved_cost = Decimal(0)
        self.blocked = False
        self.failure = ""
        self._last_retryable = False

    def _fail(self, reason: str, *, block: bool = False):
        with self._lock:
            self.failure = reason
            if block:
                self.blocked = True
        raise BrokerFailure(reason)

    def request(self, payload: dict) -> str:
        if not isinstance(payload, dict):
            raise BrokerFailure("invalid_payload")
        if not self._slots.acquire(blocking=False):
            self._fail("concurrency_limit")
        try:
            return self._request(payload)
        finally:
            self._slots.release()

    def _request(self, payload: dict) -> str:
        for attempt in range(self.budget.max_retries + 1):
            with self._lock:
                self._last_retryable = False
            deadline = time.monotonic() + self.budget.timeout_seconds
            with self._lock:
                if self.blocked:
                    self.failure = "usage_unavailable"
                    raise BrokerFailure("usage_unavailable")
                if self.calls >= self.budget.max_calls:
                    self.failure = "call_limit"
                    raise BrokerFailure("call_limit")
                if self.active >= self.budget.max_concurrency:
                    self.failure = "concurrency_limit"
                    raise BrokerFailure("concurrency_limit")
            finished, estimated = _bounded_call(
                lambda: self.provider.input_upper_bound(payload), deadline - time.monotonic()
            )
            if (
                not finished
                or isinstance(estimated, BaseException)
                or type(estimated) is not int
                or estimated < 0
            ):
                self._fail("input_estimate_unavailable", block=True)
            output = self.budget.output_tokens_per_call
            reserved_cost = self.pricing.cost(estimated, output)
            with self._lock:
                b = self.budget
                reason = ""
                if self.blocked:
                    reason = "usage_unavailable"
                elif self.active >= b.max_concurrency:
                    reason = "concurrency_limit"
                elif self.calls >= b.max_calls:
                    reason = "call_limit"
                elif (
                    self.input_tokens + self.reserved_input + estimated > b.max_input_tokens
                    or self.output_tokens + self.reserved_output + output > b.max_output_tokens
                    or self.input_tokens
                    + self.output_tokens
                    + self.reserved_input
                    + self.reserved_output
                    + estimated
                    + output
                    > b.max_total_tokens
                ):
                    reason = "token_limit"
                elif self.cost + self.reserved_cost + reserved_cost > b.max_cost_usd:
                    reason = "cost_limit"
                if reason:
                    self.failure = reason
                    raise BrokerFailure(reason)
                self.calls += 1
                self.active += 1
                self.reserved_input += estimated
                self.reserved_output += output
                self.reserved_cost += reserved_cost
            completed, result = _bounded_call(
                lambda output=output: self.provider.complete(payload, output, model=self.model),
                deadline - time.monotonic(),
            )
            if not completed or isinstance(result, BaseException):
                reason = (
                    "timeout_usage_unknown"
                    if not completed or isinstance(result, TimeoutError)
                    else "usage_unavailable"
                )
                with self._lock:
                    self.active -= 1
                    self.reserved_input -= estimated
                    self.reserved_output -= output
                    self.reserved_cost -= reserved_cost
                    self.blocked, self.failure = True, reason
                raise BrokerFailure(reason) from None
            usage = result.usage if isinstance(result, ProviderResult) else None
            valid = (
                isinstance(usage, Usage)
                and type(usage.input_tokens) is int
                and usage.input_tokens >= 0
                and type(usage.output_tokens) is int
                and usage.output_tokens >= 0
                and isinstance(usage.cost_usd, Decimal)
                and usage.cost_usd.is_finite()
                and usage.cost_usd >= 0
            )
            with self._lock:
                self.active -= 1
                self.reserved_input -= estimated
                self.reserved_output -= output
                self.reserved_cost -= reserved_cost
                if not valid:
                    self.blocked, self.failure = True, "usage_unavailable"
                    reason = "usage_unavailable"
                else:
                    self.input_tokens += usage.input_tokens
                    self.output_tokens += usage.output_tokens
                    self.cost += usage.cost_usd
                    reason = "usage_unavailable" if self.blocked else ""
                    if (
                        usage.cost_usd < self.pricing.cost(usage.input_tokens, usage.output_tokens)
                        or usage.input_tokens > estimated
                        or usage.output_tokens > output
                        or usage.cost_usd > reserved_cost
                        or self.input_tokens > b.max_input_tokens
                        or self.output_tokens > b.max_output_tokens
                        or self.input_tokens + self.output_tokens > b.max_total_tokens
                        or self.cost > b.max_cost_usd
                    ):
                        self.blocked, self.failure, reason = (
                            True,
                            "budget_exceeded",
                            "budget_exceeded",
                        )
            if reason:
                raise BrokerFailure(reason)
            if result.error:
                with self._lock:
                    self._last_retryable = result.error == "retryable"
                if result.error == "retryable" and attempt < self.budget.max_retries:
                    continue
                self._fail("provider_error")
            if not isinstance(result.content, str):
                self._fail("invalid_provider_response", block=True)
            return result.content
        raise BrokerFailure("provider_error")

    def begin_run(self) -> bool:
        return self._run_slots.acquire(blocking=False)

    def end_run(self) -> None:
        self._run_slots.release()

    def execution_limits(self) -> tuple[int, int]:
        return self.budget.max_tool_calls, self.budget.max_iterations

    def summary(self) -> dict:
        with self._lock:
            return {
                "provider": self.provider_name,
                "model": self.model,
                "code_version": self.code_version,
                "calls": self.calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
                "cost_usd": str(self.cost),
                "failure": self.failure,
            }


class FakeProvider:
    """Deterministic fixture; not a model provider and performs no I/O."""

    def __init__(self, responses: list[ProviderResult], input_bound: int):
        self.responses = list(responses)
        self.input_bound = input_bound
        self.payloads: list[dict] = []

    def input_upper_bound(self, payload: dict) -> int:
        return self.input_bound

    def complete(self, payload: dict, max_output_tokens: int, *, model: str) -> ProviderResult:
        self.payloads.append(payload)
        return self.responses.pop(0)
