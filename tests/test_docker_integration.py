import copy
from pathlib import Path

import pytest

from rca_agent.docker_integration import stage_blind, validate_role


def info(role):
    uid = {"worker": "10001:10002", "broker": "10002:10002", "agent": "10003:10003"}[role]
    mount = {"Type": "volume", "Name": "session", "Destination": "/channel", "RW": role == "worker"}
    if role == "agent":
        mount = {"Type": "bind", "Source": "/synthetic-blind", "Destination": "/blind", "RW": False}
    return {
        "Config": {"User": uid},
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
        },
        "NetworkSettings": {"Networks": {"none": {}}},
        "Mounts": [mount],
    }


@pytest.mark.parametrize("role", ["worker", "broker", "agent"])
def test_role_policies(role):
    validate_role(info(role), role, "session", Path("/synthetic-blind"))


@pytest.mark.parametrize(
    "role,change",
    [
        ("broker", "rw"),
        ("agent", "channel"),
        ("worker", "tcp"),
        ("broker", "uid"),
        ("worker", "private"),
    ],
)
def test_role_exposure_rejected(role, change):
    value = copy.deepcopy(info(role))
    if change == "rw":
        value["Mounts"][0]["RW"] = True
    elif change == "channel":
        value["Mounts"][0] = {
            "Type": "volume",
            "Name": "session",
            "Destination": "/channel",
            "RW": False,
        }
    elif change == "tcp":
        value["HostConfig"]["NetworkMode"] = "bridge"
    elif change == "uid":
        value["Config"]["User"] = "10001:10002"
    else:
        value["Mounts"].append({"Destination": "/private"})
    with pytest.raises(ValueError):
        validate_role(value, role, "session", Path("/synthetic-blind"))


def test_staging_copies_only_approved_case_files(tmp_path):
    original = tmp_path / "source"
    staged = tmp_path / "staged"
    case_id, _ = stage_blind(original)
    (original / case_id / "answer.txt").write_text("synthetic-answer-not-for-agent")
    result, context = stage_blind(staged, original, case_id)
    assert result == case_id and context["candidates"] == ["api_cpu"]
    assert {p.name for p in (staged / case_id).iterdir()} == {"task.json", "metrics.parquet"}
    assert (staged / case_id / "task.json").stat().st_ino != (
        original / case_id / "task.json"
    ).stat().st_ino


def test_staging_rejects_symlink(tmp_path):
    original = tmp_path / "source"
    case_id, _ = stage_blind(original)
    task = original / case_id / "task.json"
    task.chmod(0o644)
    task.rename(original / "outside-task")
    task.symlink_to(original / "outside-task")
    with pytest.raises(ValueError):
        stage_blind(tmp_path / "staged", original, case_id)


def test_private_csv_adapter_drops_extra_task_metadata(tmp_path):
    import json

    import pyarrow.parquet as pq
    from pyarrow import csv

    source = tmp_path / "source"
    case_id, _ = stage_blind(source)
    case = source / case_id
    task = case / "task.json"
    task.chmod(0o644)
    task.write_text(
        json.dumps(
            {
                "case_id": case_id,
                "candidate_indicators": ["api_cpu"],
                "inject_time": "private-canary",
                "files": {"mapping": "private-canary"},
            }
        )
    )
    metrics = case / "metrics.parquet"
    csv.write_csv(pq.read_table(metrics), case / "metrics.csv")
    metrics.unlink()
    target = tmp_path / "staged"
    _, context = stage_blind(target, source, case_id)
    assert context["candidates"] == ["api_cpu"]
    assert "private-canary" not in (target / case_id / "task.json").read_text()
    assert set(json.loads((target / case_id / "task.json").read_text())) == {
        "case_id",
        "candidates",
        "evidence",
    }


def test_cli_redacts_unexpected_failure(monkeypatch, capsys):
    import sys

    from rca_agent import docker_integration as runner

    monkeypatch.setattr(sys, "argv", ["docker_integration"])

    def fail(*args):
        raise TypeError("private-telemetry-canary")

    monkeypatch.setattr(runner, "run_session", fail)
    with pytest.raises(SystemExit):
        runner.main()
    output = capsys.readouterr()
    assert "BLOCKED" in output.out and "private-telemetry-canary" not in output.out + output.err


def test_partial_creation_timeout_cleans_owned_resources(monkeypatch):
    import json
    import subprocess

    from rca_agent import docker_integration as runner

    calls = []

    def docker(*args):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return json.dumps(
                [
                    {
                        "Id": "synthetic",
                        "Config": {"Labels": {"org.rca.source": runner.source_hash()}},
                    }
                ]
            )
        if args[0] == "create":
            raise subprocess.TimeoutExpired("docker", 30)
        return ""

    monkeypatch.setattr(runner, "docker", docker)
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run_session()
    assert len([c for c in calls if c[:2] == ("rm", "--force")]) == 1
    assert calls[-1][:2] == ("volume", "rm")


@pytest.mark.parametrize("key", ["calls", "channel_failure", "ledger_failure"])
def test_untrusted_failure_summary_cannot_leak_telemetry(key):
    from rca_agent.docker_integration import safe_failure
    from rca_agent.ipc_protocol import IPCError

    message = {
        "type": "error",
        "reason": "broker_failed",
        "calls": 0,
        "channel_failure": "",
        "ledger_failure": "",
        "settled_usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": "0",
        },
    }
    message[key] = "private-telemetry-canary"
    with pytest.raises(IPCError):
        safe_failure(message, "bad_ipc")


def test_untrusted_audit_and_ready_are_rejected():
    from rca_agent.docker_integration import validate_audit, validate_ready
    from rca_agent.ipc_protocol import IPCError

    with pytest.raises(IPCError):
        validate_audit({"http_calls": "private-canary", "model_calls": 0})
    with pytest.raises(IPCError):
        validate_ready({"type": "ready", "checks": {}})
    with pytest.raises(IPCError):
        validate_ready({"type": "ready", "checks": {"private_unavailable": "yes"}})
    validate_audit({"http_calls": 4, "model_calls": 2})


def test_valid_failure_preserves_known_usage_and_marks_unknown():
    from rca_agent.docker_integration import safe_failure

    message = {
        "type": "error",
        "reason": "broker_failed",
        "calls": 1,
        "channel_failure": "",
        "ledger_failure": "provider_error",
        "settled_usage": {
            "input_tokens": 10,
            "output_tokens": 50,
            "total_tokens": 60,
            "cost_usd": "0.110",
        },
    }
    result = safe_failure(message, "none")
    assert result["usage_status"] == "CONFIRMED" and result["settled_usage"]["cost_usd"] == "0.110"
    message["ledger_failure"] = "usage_unavailable"
    result = safe_failure(message, "unknown_usage")
    assert result["usage_status"] == "UNAVAILABLE"
