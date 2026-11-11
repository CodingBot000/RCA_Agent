import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rca_agent.graph import ObservationLimits
from rca_agent.ipc_protocol import IPCError
from rca_agent.ipc_runner import FrameReader, IPCConfig, run_fixture, stop_process
from rca_agent.ipc_worker import SessionProvider
from rca_agent.model_broker import BrokerFailure, Budget, OfflineBroker, Pricing


def budget(**changes):
    data = {
        "max_calls": 3,
        "max_tool_calls": 2,
        "max_iterations": 2,
        "max_input_tokens": 10,
        "max_output_tokens": 10,
        "max_total_tokens": 20,
        "max_cost_usd": Decimal(0),
        "max_concurrency": 1,
        "max_retries": 0,
        "output_tokens_per_call": 1,
        "timeout_seconds": 10.0,
    }
    data.update(changes)
    return Budget(**data)


def fixture_paths(tmp_path):
    blind = tmp_path / "blind"
    blind.mkdir()
    (blind / "fixture.txt").write_text("synthetic-blind")
    case_id = "re2-ob-v2-" + "a" * 32
    case = blind / case_id
    case.mkdir()
    (case / "task.json").write_text(
        json.dumps(
            {
                "case_id": case_id,
                "candidates": ["api_cpu"],
                "evidence": [{"id": "base", "content": "fixture"}],
            }
        )
    )
    pq.write_table(pa.table({"time": [100, 101], "api_cpu": [1.0, 2.0]}), case / "metrics.parquet")
    private = tmp_path / "private"
    private.mkdir()
    (private / "mapping").write_text("SYNTHETIC_PRIVATE_DATA")
    (private / "key").write_text("SYNTHETIC_PRIVATE_KEY")
    return blind, private / "mapping", private / "key", case_id


def config(**changes):
    values = {
        "max_frame_bytes": 65536,
        "max_messages": 5,
        "max_requests": 3,
        "max_cases": 2,
        "timeout_seconds": 15.0,
        "case_budget": budget(),
        "evaluation_budget": budget(),
        "observation_limits": ObservationLimits(1, 1000),
        "pricing": Pricing(Decimal(0), Decimal(0)),
    }
    values.update(changes)
    return IPCConfig(**values)


def test_two_process_sandbox_and_replay(tmp_path, capsys):
    blind, private, key, case_id = fixture_paths(tmp_path)
    if sys.platform != "darwin":
        with pytest.raises(IPCError, match="OS sandbox unavailable"):
            run_fixture(
                config(), blind_dir=blind, private_file=private, key_file=key, case_id=case_id
            )
        return
    result = run_fixture(
        config(), blind_dir=blind, private_file=private, key_file=key, case_id=case_id
    )
    assert result["status"] == "ok"
    assert result["transmitted_requests"] == 3 and result["provider_calls"] == 2
    assert (
        result["agent_private_denied"]
        and result["agent_key_denied"]
        and result["agent_network_denied"]
    )
    assert result["broker_private_denied"] and result["broker_blind_denied"]
    output = capsys.readouterr()
    assert "SYNTHETIC_PRIVATE" not in output.out + output.err


def test_timeout_and_oversized_frames():
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd, "rb", buffering=0) as stream:
            reader = FrameReader(stream, 8)
            with pytest.raises(IPCError, match="timeout"):
                reader.read(0.01)
            os.write(write_fd, b"x" * 9)
            with pytest.raises(IPCError, match="too large"):
                reader.read(1)
    finally:
        os.close(write_fd)


def test_total_and_per_case_budget_are_distinct():
    case = budget(max_calls=2)
    session = SessionProvider(case, 2, Pricing(Decimal(0), Decimal(0)))
    overall = OfflineBroker(
        session,
        provider_name="fake",
        model="ipc-fixture",
        code_version="ipc-test",
        budget=budget(max_calls=2),
        pricing=Pricing(Decimal(0), Decimal(0)),
    )
    for case_id in ("case-one", "case-two"):
        payload = {
            "phase": "investigate",
            "context": {"case_id": case_id, "start": 100, "end": 101},
        }
        assert json.loads(overall.request(payload))["kind"] == "metrics"
    with pytest.raises(BrokerFailure, match="call_limit"):
        overall.request(
            {"phase": "investigate", "context": {"case_id": "case-one", "start": 100, "end": 101}}
        )
    assert overall.summary()["calls"] == 2
    assert [session.cases[k].summary()["calls"] for k in ("case-one", "case-two")] == [1, 1]


def test_process_group_kills_child_and_parent():
    if sys.platform != "darwin":
        # This launcher explicitly refuses other OSes; POSIX group behavior is verified on macOS.
        with pytest.raises(IPCError, match="OS sandbox unavailable"):
            from rca_agent.ipc_runner import _start

            _start("agent", config(), Path("/x"), Path("/y"), Path("/z"), Path("/z"), "x")
        return
    script = (
        "import os,time; p=os.fork(); print(p if p else os.getpid(),flush=True); time.sleep(60)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        assert proc.stdout.readline().strip()
        assert proc.stdout.readline().strip()
        stop_process(proc)
        assert proc.returncode == -9
    finally:
        if proc.poll() is None:
            stop_process(proc)
        proc.stdout.close()


def test_per_case_budget_remains_independent_of_evaluation_budget():
    session = SessionProvider(budget(max_calls=1), 2, Pricing(Decimal(0), Decimal(0)))
    overall = OfflineBroker(
        session,
        provider_name="fake",
        model="ipc-fixture",
        code_version="ipc-test",
        budget=budget(max_calls=3),
        pricing=Pricing(Decimal(0), Decimal(0)),
    )
    request = {"phase": "investigate", "context": {"case_id": "case-one", "start": 100, "end": 101}}
    assert json.loads(overall.request(request))["kind"] == "metrics"
    with pytest.raises(BrokerFailure):
        overall.request(request)
    assert session.cases["case-one"].summary()["calls"] == 1
    assert not overall.blocked
    assert (
        json.loads(
            overall.request(
                {
                    "phase": "investigate",
                    "context": {"case_id": "case-two", "start": 100, "end": 101},
                }
            )
        )["kind"]
        == "metrics"
    )


def test_timeout_kills_and_reaps_started_worker(tmp_path, monkeypatch):
    from rca_agent import ipc_runner

    blind, private, key, case_id = fixture_paths(tmp_path)
    started = []

    def hang(*args, **kwargs):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            bufsize=0,
        )
        started.append(proc)
        return proc

    monkeypatch.setattr(ipc_runner, "_start", hang)
    with pytest.raises(IPCError, match="timeout"):
        run_fixture(
            config(timeout_seconds=0.1),
            blind_dir=blind,
            private_file=private,
            key_file=key,
            case_id=case_id,
        )
    assert len(started) == 1 and started[0].returncode == -9


def test_oversized_ipc_fails_without_logging_secrets(tmp_path, capsys):
    blind, private, key, case_id = fixture_paths(tmp_path)
    if sys.platform != "darwin":
        with pytest.raises(IPCError, match="OS sandbox unavailable"):
            run_fixture(
                config(max_frame_bytes=128),
                blind_dir=blind,
                private_file=private,
                key_file=key,
                case_id=case_id,
            )
        return
    with pytest.raises(IPCError):
        run_fixture(
            config(max_frame_bytes=128),
            blind_dir=blind,
            private_file=private,
            key_file=key,
            case_id=case_id,
        )
    captured = capsys.readouterr()
    assert "SYNTHETIC_PRIVATE" not in captured.out + captured.err


def test_case_and_evaluation_tool_and_iteration_budgets():
    from rca_agent.ipc_runner import ExecutionCounters

    c = config(
        case_budget=budget(max_tool_calls=2, max_iterations=2),
        evaluation_budget=budget(max_tool_calls=1, max_iterations=2),
    )
    counters = ExecutionCounters(c)

    def req(case_id):
        return {"payload": {"phase": "investigate", "context": {"case_id": case_id}}}

    first = req("case-one")
    counters.begin(first)
    counters.response(first, {"content": '{"kind":"metrics"}'})
    second = req("case-two")
    counters.begin(second)
    with pytest.raises(IPCError, match="tool budget"):
        counters.response(second, {"content": '{"kind":"logs"}'})
    with pytest.raises(IPCError, match="iteration budget"):
        counters.begin(req("case-three"))


def test_evaluation_wide_cost_reservation_across_cases():
    price = Pricing(Decimal("0.01"), Decimal("0.01"))
    case_budget = budget(max_calls=3, max_cost_usd=Decimal(1))
    session = SessionProvider(case_budget, 2, price)
    overall = OfflineBroker(
        session,
        provider_name="fake",
        model="ipc-fixture",
        code_version="ipc-test",
        budget=budget(max_calls=3, max_cost_usd=Decimal("0.03")),
        pricing=price,
    )

    def request(case_id):
        return {"phase": "investigate", "context": {"case_id": case_id, "start": 100, "end": 101}}

    overall.request(request("case-one"))
    with pytest.raises(BrokerFailure, match="cost_limit"):
        overall.request(request("case-two"))
    assert overall.summary()["calls"] == 1 and overall.summary()["cost_usd"] == "0.02"
    assert session.cases["case-one"].summary()["cost_usd"] == "0.02"


def test_retry_attempts_are_charged_once_to_each_budget(monkeypatch):
    from rca_agent import ipc_worker
    from rca_agent.model_broker import ProviderResult, Usage

    class RetryFixture(ipc_worker.FixtureProvider):
        def __init__(self, pricing):
            super().__init__(pricing)
            self.calls = 0

        def complete(self, request):
            self.calls += 1
            if self.calls == 1:
                return ProviderResult(None, Usage(1, 0, self.pricing.cost(1, 0)), "retryable")
            return super().complete(request)

    monkeypatch.setattr(ipc_worker, "FixtureProvider", RetryFixture)
    price = Pricing(Decimal("0.01"), Decimal("0.01"))
    per_case = budget(max_calls=3, max_retries=1, max_cost_usd=Decimal(1))
    session = SessionProvider(per_case, 1, price)
    overall = OfflineBroker(
        session,
        provider_name="fake",
        model="ipc-fixture",
        code_version="ipc-test",
        budget=budget(max_calls=3, max_retries=1, max_cost_usd=Decimal(1)),
        pricing=price,
    )
    payload = {"phase": "investigate", "context": {"case_id": "case-one", "start": 100, "end": 101}}
    assert json.loads(overall.request(payload))["kind"] == "metrics"
    assert overall.summary()["calls"] == 2 and session.cases["case-one"].summary()["calls"] == 2
    assert Decimal(overall.summary()["cost_usd"]) == Decimal("0.03")
    assert Decimal(session.cases["case-one"].summary()["cost_usd"]) == Decimal("0.03")


def test_process_group_kills_child_after_leader_exits():
    if sys.platform != "darwin":
        with pytest.raises(IPCError, match="OS sandbox unavailable"):
            from rca_agent.ipc_runner import _start

            _start("agent", config(), Path("/x"), Path("/y"), Path("/z"), Path("/z"), "x")
        return
    script = (
        "import os,time; p=os.fork(); (print(p,flush=True),os._exit(0)) if p else time.sleep(60)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        child_id = int(proc.stdout.readline().strip())
        assert proc.wait(timeout=3) == 0
        try:
            stop_process(proc)
        except IPCError as exc:
            assert "process group still exists" in str(exc)
            # An orphaned zombie has exited but cannot be reaped by this process;
            # the watchdog must report uncertainty rather than claim cleanup.
            status = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(child_id)],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            assert not status or status.startswith("Z")
    finally:
        if proc.poll() is None:
            stop_process(proc)
        proc.stdout.close()


def test_broken_ipc_write_is_fail_closed():
    from rca_agent.ipc_runner import FrameWriter

    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    with (
        os.fdopen(write_fd, "wb", buffering=0) as stream,
        pytest.raises(IPCError, match="write failed"),
    ):
        FrameWriter(stream, 100).write({"x": 1}, 1)


def test_blind_fixture_rejects_extra_files_and_hardlinks(tmp_path):
    blind, private, key, case_id = fixture_paths(tmp_path)
    extra = blind / "answers.jsonl"
    extra.write_text("synthetic answer")
    with pytest.raises(IPCError, match="unexpected blind fixture"):
        run_fixture(config(), blind_dir=blind, private_file=private, key_file=key, case_id=case_id)
    extra.unlink()
    (blind / "fixture.txt").unlink()
    os.link(private, blind / "fixture.txt")
    with pytest.raises(IPCError, match="independent regular files"):
        run_fixture(config(), blind_dir=blind, private_file=private, key_file=key, case_id=case_id)
