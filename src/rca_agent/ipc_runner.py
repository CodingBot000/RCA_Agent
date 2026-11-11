"""Trusted stdin/stdout relay for a local fake-only sandboxed IPC exercise.

This launcher intentionally supports macOS sandbox-exec only; it fails closed
on unsupported systems and is not a production or external-model broker.
"""

import json
import os
import select
import signal
import site
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from rca_agent.graph import CaseContext, ObservationLimits
from rca_agent.ipc_protocol import (
    IPCError,
    ReplayCache,
    encode_frame,
    parse_frame,
    validate_agent_ready,
    validate_model_payload,
    validate_request,
    validate_response,
    validate_status,
)
from rca_agent.model_broker import Budget, Pricing
from rca_agent.phase1 import CASE_ID_PATTERN
from rca_agent.telemetry import EvidenceRegistry, TelemetryTools


@dataclass(frozen=True)
class IPCConfig:
    max_frame_bytes: int
    max_messages: int
    max_requests: int
    max_cases: int
    timeout_seconds: float
    case_budget: Budget
    evaluation_budget: Budget
    observation_limits: ObservationLimits
    pricing: Pricing

    def __post_init__(self):
        if any(
            type(x) is not int or x < 1
            for x in (
                self.max_frame_bytes,
                self.max_messages,
                self.max_requests,
                self.max_cases,
            )
        ):
            raise ValueError("explicit positive IPC limits required")
        if type(self.timeout_seconds) not in (int, float) or not 0 < self.timeout_seconds < float(
            "inf"
        ):
            raise ValueError("explicit positive timeout required")
        if not isinstance(self.observation_limits, ObservationLimits):
            raise TypeError("explicit observation limits required")
        if not isinstance(self.pricing, Pricing):
            raise TypeError("explicit pricing required")
        if not isinstance(self.case_budget, Budget) or not isinstance(
            self.evaluation_budget, Budget
        ):
            raise TypeError("explicit case and evaluation budgets required")
        if (
            self.evaluation_budget.max_retries > self.case_budget.max_retries
            or self.evaluation_budget.output_tokens_per_call
            != self.case_budget.output_tokens_per_call
        ):
            raise ValueError("case and evaluation budgets conflict")


class ExecutionCounters:
    """Conservative planned-call accounting, shared across all cases in one session."""

    def __init__(self, config: IPCConfig):
        self.config = config
        self.iterations = Counter()
        self.tool_calls = Counter()

    def begin(self, request: dict) -> None:
        if request["payload"]["phase"] != "investigate":
            return
        case_id = request["payload"]["context"]["case_id"]
        if (
            self.iterations[case_id] >= self.config.case_budget.max_iterations
            or sum(self.iterations.values()) >= self.config.evaluation_budget.max_iterations
        ):
            raise IPCError("iteration budget exhausted")
        self.iterations[case_id] += 1

    def response(self, request: dict, response: dict) -> None:
        if request["payload"]["phase"] != "investigate":
            return
        if json.loads(response["content"])["kind"] == "finish":
            return
        case_id = request["payload"]["context"]["case_id"]
        if (
            self.tool_calls[case_id] >= self.config.case_budget.max_tool_calls
            or sum(self.tool_calls.values()) >= self.config.evaluation_budget.max_tool_calls
        ):
            raise IPCError("tool budget exhausted")
        self.tool_calls[case_id] += 1


class FrameReader:
    def __init__(self, pipe, maximum):
        self.pipe, self.maximum, self.buffer = pipe, maximum, b""

    def read(self, timeout):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.pipe], [], [], left)[0]:
                raise IPCError("IPC timeout")
            data = os.read(self.pipe.fileno(), min(4096, self.maximum + 1 - len(self.buffer)))
            if not data:
                raise IPCError("IPC closed")
            self.buffer += data
            if len(self.buffer) > self.maximum:
                raise IPCError("frame too large")
        line, self.buffer = self.buffer.split(b"\n", 1)
        return parse_frame(line + b"\n", self.maximum)


class FrameWriter:
    def __init__(self, pipe, maximum):
        self.pipe, self.maximum = pipe, maximum
        os.set_blocking(pipe.fileno(), False)

    def write(self, value, timeout):
        data = memoryview(encode_frame(value, self.maximum))
        deadline = time.monotonic() + timeout
        while data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.pipe], [], remaining)[1]:
                raise IPCError("IPC write timeout")
            try:
                sent = os.write(self.pipe.fileno(), data)
            except (BrokenPipeError, OSError) as exc:
                raise IPCError("IPC write failed") from exc
            if sent <= 0:
                raise IPCError("IPC write failed")
            data = data[sent:]


def _profile(src: Path, venv: Path, base: Path, blind: Path | None) -> str:
    allowed = [src, venv, base]
    if blind is not None:
        allowed.append(blind)
    parts = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        "(deny file-write*)",
        "(deny process-fork)",
        '(deny file-read* (subpath "/Users") (subpath "/private") (subpath "/Volumes") (subpath "/opt") (subpath "/usr/local") (subpath "/Library/Keychains"))',
    ]
    parts.extend(f"(allow file-read* (subpath {json.dumps(str(p.resolve()))}))" for p in allowed)
    return "\n".join(parts)


def stop_process(proc: subprocess.Popen) -> None:
    """Kill the live process group; callers must prohibit fork in the worker sandbox."""
    # A leader may have exited while an inherited child remains in its group.
    proc.poll()
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise IPCError("cannot terminate process group") from exc
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise IPCError("process did not terminate") from exc
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        return
    raise IPCError("process group still exists")


def _start(role, config, private_file, key_file, blind_file, blind_dir, case_id):
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise IPCError("OS sandbox unavailable")
    source = Path(__file__).resolve().parents[1]
    venv = Path(sys.prefix).resolve()
    base = Path(sys.base_prefix).resolve()
    # A non-isolated base interpreter would make the read allowlist overly broad.
    if venv == base or not str(venv).startswith(str(Path.home()) + os.sep):
        raise IPCError("private virtual environment required")
    python = Path(sys.executable).resolve()
    env = {
        "HOME": "/nonexistent",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "PYTHONPATH": os.pathsep.join([str(source), *site.getsitepackages()]),
    }
    cmd = [
        "/usr/bin/sandbox-exec",
        "-p",
        _profile(source, venv, base, blind_dir if role == "agent" else None),
        str(python),
        "-m",
        "rca_agent.ipc_worker",
        role,
        "--max-frame",
        str(config.max_frame_bytes),
        "--capacity",
        str(config.max_requests),
        "--max-cases",
        str(config.max_cases),
        "--case-budget",
        json.dumps(
            {**asdict(config.case_budget), "max_cost_usd": str(config.case_budget.max_cost_usd)}
        ),
        "--evaluation-budget",
        json.dumps(
            {
                **asdict(config.evaluation_budget),
                "max_cost_usd": str(config.evaluation_budget.max_cost_usd),
            }
        ),
        "--pricing",
        json.dumps(
            {
                "input_per_token": str(config.pricing.input_per_token),
                "output_per_token": str(config.pricing.output_per_token),
            }
        ),
        "--probe-path",
        str(private_file),
        "--key-path",
        str(key_file),
        "--blind-file",
        str(blind_file),
        "--blind-dir",
        str(blind_dir) if role == "agent" else "",
        "--case-id",
        case_id if role == "agent" else "",
        "--obs-records",
        str(config.observation_limits.max_records),
        "--obs-chars",
        str(config.observation_limits.max_chars),
    ]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd="/",
        env=env,
        start_new_session=True,
        close_fds=True,
        bufsize=0,
    )


def run_fixture(
    config: IPCConfig, *, blind_dir: Path, private_file: Path, key_file: Path, case_id: str
) -> dict:
    """Run synthetic-only fixture. No host path or payload is printed or logged."""
    blind_dir = blind_dir.resolve()
    private_file = private_file.resolve()
    key_file = key_file.resolve()
    blind_file = blind_dir / "fixture.txt"
    if not isinstance(case_id, str) or not CASE_ID_PATTERN.fullmatch(case_id):
        raise IPCError("invalid fixture case ID")
    if (
        not blind_dir.is_dir()
        or not blind_file.is_file()
        or not private_file.is_file()
        or not key_file.is_file()
    ):
        raise IPCError("synthetic fixtures unavailable")
    if any(path == blind_dir or blind_dir in path.parents for path in (private_file, key_file)):
        raise IPCError("private fixture overlaps blind directory")
    case = blind_dir / case_id
    if (
        {p.name for p in blind_dir.iterdir()} != {"fixture.txt", case_id}
        or case.is_symlink()
        or not case.is_dir()
    ):
        raise IPCError("unexpected blind fixture contents")
    if {p.name for p in case.iterdir()} != {"task.json", "metrics.parquet"}:
        raise IPCError("unexpected blind case contents")
    for path in (blind_file, case / "task.json", case / "metrics.parquet"):
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise IPCError("blind fixture must contain independent regular files")
    try:
        context = CaseContext.from_tools(
            TelemetryTools(blind_dir, case_id, EvidenceRegistry(case_id))
        )
        expected_context = json.loads(json.dumps(asdict(context)))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise IPCError("invalid synthetic case") from exc
    broker = agent = None
    try:
        broker = _start("broker", config, private_file, key_file, blind_file, blind_dir, case_id)
        broker_reader = FrameReader(broker.stdout, config.max_frame_bytes)
        broker_writer = FrameWriter(broker.stdin, config.max_frame_bytes)
        ready = broker_reader.read(config.timeout_seconds)
        validate_status(ready, ready=True)
        if ready != {
            "version": 1,
            "type": "ready",
            "private_denied": True,
            "key_denied": True,
            "blind_denied": True,
            "network_denied": True,
            "fork_denied": True,
        }:
            raise IPCError("broker isolation verification failed")
        agent = _start("agent", config, private_file, key_file, blind_file, blind_dir, case_id)
        agent_reader = FrameReader(agent.stdout, config.max_frame_bytes)
        validate_agent_ready(agent_reader.read(config.timeout_seconds))
        agent_writer = FrameWriter(agent.stdin, config.max_frame_bytes)
        cache = ReplayCache(config.max_requests)
        execution = ExecutionCounters(config)
        transmissions = unique = 0
        last_calls = 0
        for _ in range(config.max_messages):
            message = agent_reader.read(config.timeout_seconds)
            if message.get("type") == "done":
                validate_status(message, ready=False)
                expected = {
                    "version": 1,
                    "type": "done",
                    "file_denied": True,
                    "key_denied": True,
                    "network_denied": True,
                    "fork_denied": True,
                    "blind_readable": True,
                    "graph_success": True,
                    "replay_equal": True,
                    "calls_before_replay": 2,
                    "calls_after_replay": 2,
                    "calls_total": 2,
                }
                if message != expected or transmissions != 3 or unique != 2 or last_calls != 2:
                    raise IPCError("agent isolation or replay verification failed")
                return {
                    "status": "ok",
                    "unique_requests": 2,
                    "transmitted_requests": 3,
                    "provider_calls": 2,
                    "agent_private_denied": True,
                    "agent_key_denied": True,
                    "agent_network_denied": True,
                    "broker_private_denied": True,
                    "broker_key_denied": True,
                    "broker_network_denied": True,
                    "broker_fork_denied": True,
                    "agent_fork_denied": True,
                    "broker_blind_denied": True,
                }
            validate_request(message)
            if message["payload"]["context"] != expected_context:
                raise IPCError("case context differs from approved input")
            validate_model_payload(message["payload"])
            transmissions += 1  # noqa: SIM113 - done frames are not requests
            response = cache.lookup(message)
            if response is None:
                execution.begin(message)
                broker_writer.write(message, config.timeout_seconds)
                response = broker_reader.read(config.timeout_seconds)
                validate_response(message, response)
                execution.response(message, response)
                metadata = response["metadata"]
                case_metadata = response["case_metadata"]
                if (
                    set(case_metadata)
                    != {
                        "provider",
                        "model",
                        "code_version",
                        "calls",
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                        "cost_usd",
                        "failure",
                    }
                    or type(case_metadata["calls"]) is not int
                    or case_metadata["calls"] != unique + 1
                    or set(metadata)
                    != {
                        "provider",
                        "model",
                        "code_version",
                        "calls",
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                        "cost_usd",
                        "failure",
                    }
                    or metadata["provider"] != "fake"
                    or metadata["model"] != "ipc-fixture"
                    or metadata["code_version"] != "ipc-test"
                    or type(metadata["calls"]) is not int
                    or metadata["calls"] != unique + 1
                ):
                    raise IPCError("invalid usage metadata")
                unique += 1
                last_calls = metadata["calls"]
                cache.store(message, response)
            agent_writer.write(response, config.timeout_seconds)
        raise IPCError("message limit exceeded")
    finally:
        errors = []
        for proc in (agent, broker):
            if proc is not None:
                try:
                    stop_process(proc)
                except IPCError as exc:
                    errors.append(exc)
                finally:
                    for stream in (proc.stdin, proc.stdout):
                        if stream is not None:
                            stream.close()
        if errors:
            raise IPCError("worker termination failed") from errors[0]
