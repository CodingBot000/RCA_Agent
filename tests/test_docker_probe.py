"""Offline regressions; live Docker checks are an explicit manual command."""

import copy
import json
import subprocess

import pytest

from rca_agent import docker_probe as probe


def inspected():
    return {
        "Config": {"User": "65534:65534"},
        "HostConfig": {
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Privileged": False,
            "PortBindings": {},
        },
        "Mounts": [],
        "NetworkSettings": {"Networks": {"test-net": {}}},
    }


@pytest.mark.parametrize(
    "code,expected",
    [
        ("ENETUNREACH", "DENIED"),
        ("ECONNREFUSED", "REACHABLE"),
        (0, "REACHABLE"),
        ("DNS_UNVERIFIED", "UNVERIFIED"),
        ("UNVERIFIED", "UNVERIFIED"),
        ("ETIMEDOUT", "UNVERIFIED"),
        (101, "UNVERIFIED"),  # Never interpret Linux errno using the macOS errno table.
    ],
)
def test_connection_evidence(code, expected):
    assert probe.classify_connection(code) == expected


@pytest.mark.parametrize(
    "change", ["user", "write", "caps", "privilege", "mount", "network", "ports", "root", "cap_add"]
)
def test_inspection_rejects_unsafe_configuration(change):
    value = copy.deepcopy(inspected())
    if change == "user":
        value["Config"]["User"] = "0"
    elif change == "write":
        value["HostConfig"]["ReadonlyRootfs"] = False
    elif change == "caps":
        value["HostConfig"]["CapDrop"] = []
    elif change == "cap_add":
        value["HostConfig"]["CapAdd"] = ["NET_ADMIN"]
    elif change == "privilege":
        value["HostConfig"]["SecurityOpt"] = []
    elif change == "mount":
        value["Mounts"] = [{"Destination": "/blind"}]
    elif change == "network":
        value["NetworkSettings"]["Networks"]["external"] = {}
    elif change == "ports":
        value["HostConfig"]["PortBindings"] = {"8080/tcp": [{}]}
    else:
        value["HostConfig"]["Privileged"] = True
    with pytest.raises(ValueError):
        probe.validate_inspect(value, "test-net")


def test_safe_configuration():
    probe.validate_inspect(inspected(), "test-net")


def test_timeout_cleans_up_owned_resources(monkeypatch):
    calls = []

    def docker(*args):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Id": "synthetic-image"}])
        if args[:2] == ("network", "inspect"):
            return json.dumps([{"Internal": True, "IPAM": {"Config": [{"Gateway": "192.0.2.1"}]}}])
        if args[0] == "inspect":
            value = inspected()
            value["NetworkSettings"]["Networks"] = {calls[1][-1]: {}}
            return json.dumps([value])
        if args[0] == "start":
            raise subprocess.TimeoutExpired("docker", 30)
        return ""

    monkeypatch.setattr(probe, "docker", docker)
    with pytest.raises(subprocess.TimeoutExpired):
        probe.run_probe()
    removals = [args for args in calls if args[:2] == ("rm", "--force")]
    assert len(removals) == 2
    assert calls[-1][:2] == ("network", "rm")
    assert all(args[-1].startswith("rca-broker-probe-") for args in removals)
