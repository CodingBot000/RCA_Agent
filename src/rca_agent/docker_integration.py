"""Authenticated offline Docker RCA pipeline; no live provider entry point."""

import argparse
import hashlib
import json
import secrets
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from rca_agent.docker_probe import docker
from rca_agent.graph import CaseContext, ObservationLimits
from rca_agent.integrated_worker import IDENTITY, MAXIMUM
from rca_agent.ipc_protocol import IPCError, ReplayCache, validate_request, validate_response
from rca_agent.ipc_runner import ExecutionCounters, FrameReader, FrameWriter, IPCConfig
from rca_agent.model_broker import Budget, Pricing
from rca_agent.telemetry import EvidenceRegistry, TelemetryTools

IMAGE = "rca-offline:phase4b3b"


def source_hash():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def fixture_budget(fault):
    # Synthetic test settings only. Never an approved live budget.
    return Budget(
        3,
        1,
        1,
        9 if fault == "token_limit" else 100,
        3000,
        4000,
        Decimal(".001") if fault == "cost_limit" else Decimal(10),
        1,
        0,
        1000,
        3,
    )


def stage_blind(destination, source=None, case_id=None):
    if source is None:
        case_id = "re2-ob-v2-" + "a" * 32
        case = destination / case_id
        case.mkdir(parents=True)
        (case / "task.json").write_text(
            json.dumps(
                {
                    "case_id": case_id,
                    "candidates": ["api_cpu"],
                    "evidence": [{"id": "base", "content": "synthetic"}],
                }
            )
        )
        pq.write_table(
            pa.table({"time": [100, 101], "api_cpu": [1.0, 2.0]}), case / "metrics.parquet"
        )
    else:
        from rca_agent.phase1 import CASE_ID_PATTERN

        if not case_id or not CASE_ID_PATTERN.fullmatch(case_id):
            raise ValueError("invalid case")
        root = Path(source).resolve()
        original = root / case_id
        if original.is_symlink():
            raise ValueError("case symlink forbidden")
        case = destination / case_id
        case.mkdir(parents=True)
        for name in ("task.json", "metrics.parquet"):
            path = original / name
            if name == "metrics.parquet" and not path.exists():
                path = original / "metrics.csv"
                if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                    raise ValueError("independent metrics source required")
                from pyarrow import csv

                pq.write_table(csv.read_csv(path), case / name)
            else:
                if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                    raise ValueError("independent blind files required")
                shutil.copyfile(path, case / name)
    task_path = case / "task.json"
    raw_task = json.loads(task_path.read_text())
    if "candidate_indicators" in raw_task and "candidates" not in raw_task:
        # Adapt only approved opaque candidates and case ID. Do not pass dataset metadata,
        # sampling details, injection time, paths, or any evaluator mapping to the Agent.
        task_path.write_text(
            json.dumps(
                {
                    "case_id": raw_task["case_id"],
                    "candidates": raw_task["candidate_indicators"],
                    "evidence": [{"id": "blind-task", "content": "offline blind task context"}],
                }
            )
        )
    # Docker Desktop needs directories traversable by the non-root Agent only in this staging tree.
    destination.chmod(0o755)
    case.chmod(0o755)
    for path in case.iterdir():
        path.chmod(0o444)
    tools = TelemetryTools(destination, case_id, EvidenceRegistry(case_id))
    context = json.loads(json.dumps(asdict(CaseContext.from_tools(tools))))
    return case_id, context


def validate_role(info, role, volume, blind):
    uid = {"worker": "10001:10002", "broker": "10002:10002", "agent": "10003:10003"}[role]
    host = info["HostConfig"]
    mounts = info["Mounts"]
    if (
        info["Config"]["User"] != uid
        or host["NetworkMode"] != "none"
        or set(info["NetworkSettings"]["Networks"]) != {"none"}
        or not host["ReadonlyRootfs"]
        or host["CapDrop"] != ["ALL"]
        or host.get("CapAdd")
        or host.get("Privileged")
        or host.get("PortBindings")
        or "no-new-privileges" not in host["SecurityOpt"]
    ):
        raise ValueError("unsafe role configuration")
    if len(mounts) != 1:
        raise ValueError("unexpected mount")
    mount = mounts[0]
    if role == "agent":
        if (
            mount["Type"] != "bind"
            or mount["Source"] != str(blind)
            or mount["Destination"] != "/blind"
            or mount["RW"]
        ):
            raise ValueError("unsafe Agent mount")
    elif (
        mount["Type"] != "volume"
        or mount["Name"] != volume
        or mount["Destination"] != "/channel"
        or mount["RW"] != (role == "worker")
    ):
        raise ValueError("unsafe channel mount")


def validate_ready(message):
    expected = {"private_unavailable", "tcp_denied", "agent_channel_absent", "agent_auth_absent"}
    if (
        set(message) != {"type", "checks"}
        or message["type"] != "ready"
        or not isinstance(message["checks"], dict)
        or set(message["checks"]) != expected
        or any(type(v) is not bool or not v for v in message["checks"].values())
    ):
        raise IPCError("invalid isolation status")


def safe_failure(message, fault):
    from rca_agent.public_report import SAFE_REASONS

    if (
        set(message) != {"type", "reason", "calls", "channel_failure", "ledger_failure"}
        or message["type"] != "error"
        or message["reason"] != "broker_failed"
        or type(message["calls"]) is not int
        or not 0 <= message["calls"] <= 3
        or message["channel_failure"]
        not in {"", "socket_path", "peer_identity", "response_auth", "worker_result"}
        or message["ledger_failure"] not in SAFE_REASONS
    ):
        raise IPCError("invalid failure status")
    return {
        "status": "EXPECTED_FAILURE" if fault != "none" else "BLOCKED",
        "fault": fault,
        "live": "LIVE_BLOCKED",
        "calls": message["calls"],
        "channel_failure": message["channel_failure"],
        "ledger_failure": message["ledger_failure"],
    }


def validate_audit(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"http_calls", "model_calls"}
        or any(type(n) is not int or not 0 <= n <= 32 for n in value.values())
    ):
        raise IPCError("invalid mock audit")


def run_session(fault="none", blind_source=None, case_id=None):
    if fault not in {
        "none",
        "count_failure",
        "token_limit",
        "cost_limit",
        "delay",
        "unknown_usage",
        "worker_stop",
        "bad_auth",
        "bad_ipc",
        "symlink",
        "wrong_permissions",
        "imposter",
        "uds_replay",
    }:
        raise ValueError("unknown fault")
    prefix = "rca-e2e-" + uuid.uuid4().hex
    volume = prefix + "-channel"
    names = []
    processes = []
    volume_created = False
    result = None
    try:
        image_info = json.loads(docker("image", "inspect", IMAGE))[0]
        if image_info["Config"].get("Labels", {}).get("org.rca.source") != source_hash():
            raise ValueError("rebuild offline image after source changes")
        image = image_info["Id"]
        volume_created = True
        docker(
            "volume",
            "create",
            "--driver",
            "local",
            "--opt",
            "type=tmpfs",
            "--opt",
            "device=tmpfs",
            "--opt",
            "o=size=1m,uid=10001,gid=10002,mode=0750",
            volume,
        )
        with tempfile.TemporaryDirectory(prefix="rca-e2e-") as tmp:
            root = Path(tmp)
            blind = root / "blind"
            case_id, context = stage_blind(blind, blind_source, case_id)
            private = []
            for label in ("answer", "mapping", "key"):
                path = root / label
                path.write_text("synthetic-only")
                private.append(str(path))
            b = fixture_budget(fault)
            common = {
                "case_id": case_id,
                "case_budget": {**asdict(b), "max_cost_usd": str(b.max_cost_usd)},
                "total_budget": {**asdict(b), "max_cost_usd": str(b.max_cost_usd)},
                "private_paths": private,
                "fault": fault,
            }
            auth = {"session": secrets.token_hex(16), "key": secrets.token_hex(32)}
            endpoints = {}
            for role in ("worker", "broker", "agent"):
                name = prefix + "-" + role
                names.append(name)
                uid = {"worker": "10001:10002", "broker": "10002:10002", "agent": "10003:10003"}[
                    role
                ]
                args = [
                    "create",
                    "-i",
                    "--name",
                    name,
                    "--network",
                    "none",
                    "--read-only",
                    "--user",
                    uid,
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--pids-limit",
                    "64",
                    "--memory",
                    "512m",
                    "--cpus",
                    "1",
                    "--log-driver",
                    "none",
                ]
                if role == "agent":
                    args += ["--mount", f"type=bind,src={blind},dst=/blind,readonly"]
                else:
                    args += [
                        "--mount",
                        f"type=volume,src={volume},dst=/channel"
                        + (",readonly" if role == "broker" else ""),
                    ]
                docker(*args, image, "python", "-B", "-m", "rca_agent.integrated_worker", role)
                validate_role(json.loads(docker("inspect", name))[0], role, volume, blind)
                proc = subprocess.Popen(
                    ["docker", "start", "--attach", "--interactive", name],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
                processes.append(proc)
                reader, writer = FrameReader(proc.stdout, MAXIMUM), FrameWriter(proc.stdin, MAXIMUM)
                config = {**common, **(auth if role != "agent" else {})}
                if role == "worker":
                    config["audit_path"] = "/channel/audit.json"
                if fault == "bad_auth" and role == "broker":
                    config["key"] = "00" * 32
                writer.write(config, 5)
                ready = reader.read(15)
                validate_ready(ready)
                endpoints[role] = (reader, writer)
                if role == "broker" and fault == "none":
                    docker(
                        "exec",
                        name,
                        "python",
                        "-c",
                        "import os,errno;\ntry: os.rename('/channel/worker.sock','/channel/stolen.sock')\nexcept OSError as e: assert e.errno in (errno.EROFS,errno.EACCES,errno.EPERM)\nelse: raise SystemExit(1)",
                    )
                    code = """
import sys,socket,json
from rca_agent.secure_uds import Credentials
from rca_agent.ipc_protocol import encode_frame
config=json.loads(sys.stdin.readline()); c=Credentials(config['session'],bytes.fromhex(config['key']))
body={'session':c.session,'id':'d'*32,'sequence':0,'operation':'count','data':{}}
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as conn:
 conn.settimeout(2);conn.connect('/channel/worker.sock')
 try:
  conn.sendall(encode_frame(c.sign(body,'request'),262144)); response=conn.recv(4096)
 except (ConnectionResetError,BrokenPipeError): response=b''
 assert response==b''
print('wrong_uid_denied')
"""
                    check = subprocess.run(
                        [
                            "docker",
                            "exec",
                            "--interactive",
                            "--user",
                            "10004:10002",
                            name,
                            "python",
                            "-c",
                            code,
                        ],
                        input=json.dumps(auth) + "\n",
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=True,
                    )
                    if check.stdout.strip() != "wrong_uid_denied":
                        raise IPCError("peer UID test failed")

                if role == "worker":
                    docker(
                        "exec",
                        name,
                        "python",
                        "-c",
                        "import os,time;\nfor i in range(30):\n if os.path.exists('/channel/worker.sock'): break\n time.sleep(.1)\nelse: raise SystemExit(1)",
                    )
                    if fault == "symlink":
                        docker(
                            "exec",
                            name,
                            "python",
                            "-c",
                            "import os;os.rename('/channel/worker.sock','/channel/real.sock');os.symlink('real.sock','/channel/worker.sock')",
                        )
                    if fault == "wrong_permissions":
                        docker(
                            "exec",
                            name,
                            "python",
                            "-c",
                            "import os;os.chmod('/channel/worker.sock',0o666)",
                        )
                    if fault == "imposter":
                        docker(
                            "exec",
                            name,
                            "python",
                            "-c",
                            "import os;os.rename('/channel/worker.sock','/channel/real.sock')",
                        )
                        docker(
                            "exec",
                            "--detach",
                            name,
                            "python",
                            "-c",
                            "import socket,os;s=socket.socket(socket.AF_UNIX);s.bind('/channel/worker.sock');os.chmod('/channel/worker.sock',0o660);s.listen();c,_=s.accept();c.recv(262144);c.sendall(b'{\"mac\":\"wrong\"}\\n');c.close()",
                        )
                        docker(
                            "exec",
                            name,
                            "python",
                            "-c",
                            "import os,time;\nfor i in range(30):\n if os.path.exists('/channel/worker.sock'): break\n time.sleep(.1)\nelse: raise SystemExit(1)",
                        )
                    if fault == "worker_stop":
                        docker("kill", name)
            ar, aw = endpoints["agent"]
            br, bw = endpoints["broker"]
            cache = ReplayCache(8)
            cfg = IPCConfig(
                MAXIMUM,
                8,
                8,
                1,
                5,
                b,
                b,
                ObservationLimits(1, 4000),
                Pricing(Decimal(".001"), Decimal(".002")),
            )
            counters = ExecutionCounters(cfg)
            unique = transmitted = 0
            for _ in range(8):
                message = ar.read(10)
                if message.get("type") == "done":
                    if (
                        set(message) != {"type", "success", "replay_equal", "calls"}
                        or message["success"] is not True
                        or message["replay_equal"] is not True
                        or type(message["calls"]) is not int
                        or message["calls"] != 2
                        or unique != 2
                        or transmitted != 3
                    ):
                        raise IPCError("graph or replay failed")
                    result = {
                        "status": "PASS",
                        "live": "LIVE_BLOCKED",
                        "calls": message["calls"],
                        "unique_requests": unique,
                        "transmissions": transmitted,
                        "roles_isolated": True,
                    }
                    break
                validate_request(message)
                if message["payload"]["context"] != context:
                    raise IPCError("unapproved context")
                previous = cache.lookup(message)
                if previous is None:
                    counters.begin(message)
                bw.write({"invalid": "frame"} if fault == "bad_ipc" else message, 5)
                response = br.read(6)
                if response.get("type") == "error":
                    result = safe_failure(response, fault)
                    break
                validate_response(message, response, identity=IDENTITY)
                if previous is not None and previous != response:
                    raise IPCError("replay mismatch")
                if previous is None:
                    counters.response(message, response)
                    cache.store(message, response)
                    unique += 1
                transmitted += 1  # noqa: SIM113 - terminal frames are not requests
                aw.write(response, 5)
            if result is None:
                raise IPCError("message limit")
            if fault != "worker_stop":
                audit = json.loads(
                    docker(
                        "exec",
                        prefix + "-worker",
                        "python",
                        "-c",
                        "from pathlib import Path;print(Path('/channel/audit.json').read_text())",
                    )
                )
                validate_audit(audit)
                result["mock_http_calls"] = audit["http_calls"]
                result["mock_model_calls"] = audit["model_calls"]
                if fault in ("none", "uds_replay") and (
                    audit["model_calls"] != 2 or audit["http_calls"] != 4
                ):
                    raise IPCError("mock/replay accounting failed")
                if (
                    fault
                    in {
                        "count_failure",
                        "token_limit",
                        "cost_limit",
                        "bad_auth",
                        "bad_ipc",
                        "symlink",
                        "wrong_permissions",
                        "imposter",
                    }
                    and audit["model_calls"] != 0
                ):
                    raise IPCError("model called despite preflight failure")
    finally:
        failures = []
        for name in reversed(names):
            try:
                docker("rm", "--force", name)
            except (OSError, subprocess.SubprocessError):
                failures.append("container cleanup")
        for proc in processes:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                failures.append("attach cleanup")
            for stream in (proc.stdin, proc.stdout):
                stream.close()
        if volume_created:
            try:
                docker("volume", "rm", volume)
            except (OSError, subprocess.SubprocessError):
                failures.append("volume cleanup")
        if failures:
            raise RuntimeError("; ".join(failures))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fault", default="none")
    parser.add_argument("--suite", action="store_true")
    parser.add_argument("--blind-source", type=Path)
    parser.add_argument("--case-id")
    args = parser.parse_args()
    try:
        if args.suite:
            results = []
            for fault in (
                "none",
                "uds_replay",
                "count_failure",
                "token_limit",
                "cost_limit",
                "unknown_usage",
                "delay",
                "worker_stop",
                "bad_auth",
                "bad_ipc",
                "symlink",
                "wrong_permissions",
                "imposter",
            ):
                item = run_session(fault)
                expected = "PASS" if fault in ("none", "uds_replay") else "EXPECTED_FAILURE"
                if item["status"] != expected:
                    raise IPCError("suite scenario failed")
                results.append(item)
                print(json.dumps(item, sort_keys=True), flush=True)
            print(
                json.dumps(
                    {"offline_suite": "PASS", "scenarios": len(results), "live": "LIVE_BLOCKED"}
                )
            )
            return
        result = run_session(args.fault, args.blind_source, args.case_id)
    except Exception:  # noqa: BLE001 - CLI must not print telemetry or credential exception bodies
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": "integration or cleanup failed",
                    "live": "LIVE_BLOCKED",
                }
            )
        )
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True))
    if result["status"] == "BLOCKED":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
