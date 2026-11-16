"""Offline Unix-socket protocol and Docker policy regressions."""

import copy
import errno
import socket
import threading

import pytest

from rca_agent import uds_mock_worker as worker
from rca_agent.uds_probe import validate_container


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"null",
        b'{"version":true,"probe":"synthetic"}',
        b'{"version":1,"probe":"synthetic","extra":1}',
        b'{"version":1,"version":1,"probe":"synthetic"}',
        b'{"version":NaN,"probe":"synthetic"}',
    ],
)
def test_bad_body_rejected(body):
    with pytest.raises(ValueError):
        worker.validate_body(body)


def request(raw):
    client, server = socket.socketpair()
    statuses = []

    def serve():
        with server:
            statuses.append(worker.exchange(server))

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        client.settimeout(4)
        client.sendall(raw)
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError as exc:
            if exc.errno != errno.ENOTCONN:
                raise
        output = b""
        while True:
            try:
                chunk = client.recv(4096)
            except ConnectionResetError:
                break
            if not chunk:
                break
            output += chunk
        thread.join(4)
        assert not thread.is_alive()
        return statuses[0], output
    finally:
        client.close()
        thread.join(4)


def test_valid_http_over_unix_socket():
    body = b'{"version":1,"probe":"synthetic"}'
    status, output = request(
        b"POST /mock HTTP/1.1\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    assert status == 200 and b'"ok": true' in output


@pytest.mark.parametrize(
    "raw,status",
    [
        (b"POST /mock HTTP/1.1\r\nContent-Length: 2049\r\n\r\n", 413),
        (b"POST /mock HTTP/1.1\r\nContent-Length: -1\r\n\r\n", 400),
        (b"POST /mock HTTP/1.1\r\nContent-Length: 2\r\ncontent-length: 2\r\n\r\n{}", 400),
        (b"POST /mock HTTP/1.1\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}", 400),
        (b"GET http://example.invalid HTTP/1.1\r\n\r\n", 400),
        (b"POST /mock HTTP/1.1\r\nX: " + b"x" * 4096 + b"\r\n\r\n", 400),
        (b"POST /mock HTTP/1.1\r\nContent-Length: 10\r\n\r\n{}", 400),
    ],
)
def test_framing_and_size_rejections(raw, status):
    assert request(raw)[0] == status


def configuration(role):
    return {
        "Config": {"User": "65534:65534"},
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
        },
        "NetworkSettings": {"Networks": {"none": {}}},
        "Mounts": []
        if role == "agent"
        else [{"Type": "volume", "Name": "test-volume", "Destination": "/channel"}],
    }


@pytest.mark.parametrize("role", ["agent", "broker", "worker"])
def test_safe_container_policy(role):
    validate_container(configuration(role), "test-volume", role)


@pytest.mark.parametrize("change", ["network", "extra_mount", "wrong_volume", "agent_socket"])
def test_container_policy_rejects_exposure(change):
    role = "agent" if change == "agent_socket" else "broker"
    value = copy.deepcopy(configuration(role))
    if change == "network":
        value["HostConfig"]["NetworkMode"] = "host"
    elif change == "extra_mount":
        value["Mounts"].append({"Type": "bind", "Destination": "/private"})
    elif change == "wrong_volume":
        value["Mounts"][0]["Name"] = "other"
    else:
        value["Mounts"] = [{"Type": "volume", "Name": "test-volume", "Destination": "/channel"}]
    with pytest.raises(ValueError):
        validate_container(value, "test-volume", role)


def test_creation_timeout_removes_all_owned_resources(monkeypatch):
    import json
    import subprocess

    from rca_agent import uds_probe

    calls = []
    network = None

    def docker(*args):
        nonlocal network
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Id": "synthetic"}])
        if args[:2] == ("network", "create"):
            network = args[-1]
        if args[:2] == ("network", "inspect"):
            return json.dumps([{"Internal": True, "IPAM": {"Config": [{"Gateway": "192.0.2.1"}]}}])
        if args[0] == "inspect":
            return json.dumps(
                [{"NetworkSettings": {"Networks": {network: {"IPAddress": "192.0.2.2"}}}}]
            )
        if args[0] == "create" and args[2].endswith("-worker"):
            raise subprocess.TimeoutExpired("docker", 30)
        return ""

    monkeypatch.setattr(uds_probe, "docker", docker)
    with pytest.raises(subprocess.TimeoutExpired):
        uds_probe.run_probe()
    assert len([c for c in calls if c[:2] == ("rm", "--force")]) == 2
    assert calls[-2][:2] == ("volume", "rm")
    assert calls[-1][:2] == ("network", "rm")
