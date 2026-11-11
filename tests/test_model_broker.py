import threading
from decimal import Decimal

import pytest

from rca_agent.model_broker import (
    BrokerFailure,
    Budget,
    FakeProvider,
    OfflineBroker,
    Pricing,
    ProviderResult,
    Usage,
)


def config(**changes):
    values = {
        "max_calls": 4,
        "max_tool_calls": 2,
        "max_iterations": 2,
        "max_input_tokens": 100,
        "max_output_tokens": 80,
        "max_total_tokens": 150,
        "max_cost_usd": Decimal(10),
        "max_concurrency": 1,
        "max_retries": 0,
        "output_tokens_per_call": 20,
        "timeout_seconds": 2.0,
    }
    values.update(changes)
    return Budget(**values)


def broker(provider, **changes):
    return OfflineBroker(
        provider,
        provider_name="fake",
        model="test-model",
        code_version="test",
        budget=config(**changes),
        pricing=Pricing(Decimal("0.01"), Decimal("0.02")),
    )


def result(content="{}", usage=None, error=None):
    return ProviderResult(
        content, usage if usage is not None else Usage(10, 5, Decimal("0.20")), error
    )


def test_budget_accounting_and_safe_summary():
    p = FakeProvider([result()], 10)
    b = broker(p)
    assert b.request({"observation": "secret telemetry"}) == "{}"
    summary = b.summary()
    assert (
        summary["calls"],
        summary["input_tokens"],
        summary["output_tokens"],
        summary["cost_usd"],
    ) == (1, 10, 5, "0.20")
    assert "secret telemetry" not in str(summary)


def test_preflight_cost_and_token_limits_block_before_provider():
    p = FakeProvider([result()], 10)
    with pytest.raises(BrokerFailure, match="cost_limit"):
        broker(p, max_cost_usd=Decimal("0.49")).request({})
    assert not p.payloads
    with pytest.raises(BrokerFailure, match="token_limit"):
        broker(p, max_total_tokens=29).request({})
    assert not p.payloads


def test_usage_unknown_poison_and_timeout():
    class Timeout(FakeProvider):
        def complete(self, payload, max_output_tokens, *, model):
            raise TimeoutError("untrusted details")

    b = broker(Timeout([], 10), max_retries=2)
    with pytest.raises(BrokerFailure, match="timeout_usage_unknown"):
        b.request({})
    with pytest.raises(BrokerFailure, match="usage_unavailable"):
        b.request({})
    assert b.summary()["calls"] == 1


def test_known_usage_error_retry_counts_every_attempt():
    p = FakeProvider(
        [ProviderResult(None, Usage(10, 0, Decimal("0.10")), "retryable"), result()], 10
    )
    b = broker(p, max_retries=1)
    assert b.request({}) == "{}"
    assert b.summary()["calls"] == 2 and b.summary()["input_tokens"] == 20


def test_usage_overrun_fails_closed():
    p = FakeProvider([ProviderResult("{}", Usage(11, 0, Decimal("0.11")))], 10)
    b = broker(p)
    with pytest.raises(BrokerFailure, match="budget_exceeded"):
        b.request({})
    with pytest.raises(BrokerFailure, match="usage_unavailable"):
        b.request({})
    assert b.summary()["input_tokens"] == 11


def test_unknown_usage_response_blocks():
    p = FakeProvider([ProviderResult("{}", None)], 10)
    b = broker(p)
    with pytest.raises(BrokerFailure, match="usage_unavailable"):
        b.request({})
    assert b.blocked


def test_concurrency_reservation_is_atomic():
    entered, release = threading.Event(), threading.Event()

    class Blocking(FakeProvider):
        def complete(self, payload, max_output_tokens, *, model):
            entered.set()
            assert release.wait(3)
            return result()

    b = broker(Blocking([], 10))
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(b.request({})))
    thread.start()
    try:
        assert entered.wait(3)
        with pytest.raises(BrokerFailure, match="concurrency_limit"):
            b.request({})
    finally:
        release.set()
        thread.join(3)
    assert outcomes == ["{}"] and b.summary()["calls"] == 1


def test_unpriced_or_underreported_usage_fails_closed():
    p = FakeProvider([ProviderResult("{}", Usage(10, 5, Decimal(0)))], 10)
    b = broker(p)
    with pytest.raises(BrokerFailure, match="budget_exceeded"):
        b.request({})
    assert b.blocked


def test_invalid_estimator_poison_stops_provider():
    p = FakeProvider([result()], -1)
    b = broker(p)
    with pytest.raises(BrokerFailure, match="input_estimate_unavailable"):
        b.request({})
    assert b.blocked and not p.payloads


def test_poisoned_budget_does_not_call_provider_again():
    class Probe(FakeProvider):
        def __init__(self):
            super().__init__([ProviderResult("{}", None)], 10)
            self.estimates = 0

        def input_upper_bound(self, payload):
            self.estimates += 1
            return super().input_upper_bound(payload)

    p = Probe()
    b = broker(p)
    for _ in range(2):
        with pytest.raises(BrokerFailure, match="usage_unavailable"):
            b.request({})
    assert p.estimates == 1 and len(p.payloads) == 1


def test_total_llm_call_limit_includes_successes():
    p = FakeProvider([result(), result()], 10)
    b = broker(p, max_calls=1)
    assert b.request({}) == "{}"
    with pytest.raises(BrokerFailure, match="call_limit"):
        b.request({})
    assert len(p.payloads) == 1


def test_model_identity_is_not_a_secret():
    p = FakeProvider([], 10)
    with pytest.raises(ValueError):
        OfflineBroker(
            p,
            provider_name="fake",
            model="sk-secret-value",
            code_version="code",
            budget=config(),
            pricing=Pricing(Decimal(0), Decimal(0)),
        )


def test_parallel_reservations_cannot_exceed_cost_budget():
    entered, release = threading.Event(), threading.Event()

    class Blocking(FakeProvider):
        def complete(self, payload, max_output_tokens, *, model):
            entered.set()
            assert release.wait(3)
            return result()

    b = broker(Blocking([], 10), max_concurrency=2, max_cost_usd=Decimal("0.75"))
    completed = []
    worker = threading.Thread(target=lambda: completed.append(b.request({})))
    worker.start()
    try:
        assert entered.wait(3)
        with pytest.raises(BrokerFailure, match="cost_limit"):
            b.request({})
    finally:
        release.set()
        worker.join(3)
    assert completed == ["{}"] and b.summary()["calls"] == 1


def test_non_retryable_error_never_retries():
    p = FakeProvider(
        [ProviderResult(None, Usage(10, 0, Decimal("0.10")), "permission-denied"), result()], 10
    )
    b = broker(p, max_retries=2)
    with pytest.raises(BrokerFailure, match="provider_error"):
        b.request({})
    assert b.summary()["calls"] == 1 and len(p.payloads) == 1


def test_provider_exception_traceback_does_not_expose_details():
    import traceback

    class Leaky(FakeProvider):
        def complete(self, payload, max_output_tokens, *, model):
            raise TimeoutError("SECRET_PROVIDER_DETAILS")

    with pytest.raises(BrokerFailure) as caught:
        broker(Leaky([], 10)).request({})
    assert "SECRET_PROVIDER_DETAILS" not in "".join(traceback.format_exception(caught.value))


def test_estimator_exception_does_not_expose_input():
    import traceback

    class LeakyEstimate(FakeProvider):
        def input_upper_bound(self, payload):
            raise ValueError("SECRET_OBSERVATION")

    with pytest.raises(BrokerFailure) as caught:
        broker(LeakyEstimate([], 10)).request({})
    assert "SECRET_OBSERVATION" not in "".join(traceback.format_exception(caught.value))


def test_hung_provider_times_out_and_prevents_retries():
    entered, release = threading.Event(), threading.Event()

    class Hung(FakeProvider):
        def complete(self, payload, max_output_tokens, *, model):
            entered.set()
            release.wait(3)
            return result()

    b = broker(Hung([], 10), timeout_seconds=0.05, max_retries=2)
    try:
        with pytest.raises(BrokerFailure, match="timeout_usage_unknown"):
            b.request({})
        assert entered.is_set() and b.blocked and b.summary()["calls"] == 1
        with pytest.raises(BrokerFailure, match="usage_unavailable"):
            b.request({})
    finally:
        release.set()


def test_call_limit_blocks_even_input_estimator():
    class Probe(FakeProvider):
        def __init__(self):
            super().__init__([result()], 10)
            self.estimates = 0

        def input_upper_bound(self, payload):
            self.estimates += 1
            return super().input_upper_bound(payload)

    p = Probe()
    b = broker(p, max_calls=1)
    b.request({})
    with pytest.raises(BrokerFailure, match="call_limit"):
        b.request({})
    assert p.estimates == 1


def test_hung_input_estimation_times_out_without_provider_call():
    entered, release = threading.Event(), threading.Event()

    class HungEstimate(FakeProvider):
        def input_upper_bound(self, payload):
            entered.set()
            release.wait(3)
            return 10

    p = HungEstimate([result()], 10)
    b = broker(p, timeout_seconds=0.05)
    try:
        with pytest.raises(BrokerFailure, match="input_estimate_unavailable"):
            b.request({})
        assert entered.is_set() and b.blocked and b.summary()["calls"] == 0
        assert not p.payloads
    finally:
        release.set()


def test_unknown_usage_blocks_other_inflight_result():
    both_started, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    started = []

    class Parallel(FakeProvider):
        def complete(self, payload, max_output_tokens, *, model):
            with lock:
                started.append(payload["id"])
                if len(started) == 2:
                    both_started.set()
            assert both_started.wait(3)
            if payload["id"] == "unknown":
                return ProviderResult("{}", None)
            assert release.wait(3)
            return result()

    b = broker(Parallel([], 10), max_concurrency=2)
    outcomes = []

    def request_one(name):
        try:
            outcomes.append((name, b.request({"id": name})))
        except BrokerFailure as exc:
            outcomes.append((name, exc.reason))

    first = threading.Thread(target=request_one, args=("unknown",))
    second = threading.Thread(target=request_one, args=("other",))
    first.start()
    second.start()
    try:
        first.join(3)
        assert not first.is_alive()
    finally:
        release.set()
        first.join(3)
        second.join(3)
    assert sorted(outcomes) == [("other", "usage_unavailable"), ("unknown", "usage_unavailable")]


def test_expired_deadline_does_not_start_provider_work():
    from rca_agent.model_broker import _bounded_call

    invoked = []
    completed, result_value = _bounded_call(lambda: invoked.append(True), 0)
    assert not completed and result_value is None and not invoked
