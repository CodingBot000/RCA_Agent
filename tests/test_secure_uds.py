import copy
import os
import socket

import pytest

from rca_agent.egress_policy import live_client, tls_context, validate_destination
from rca_agent.ipc_protocol import IPCError
from rca_agent.secure_uds import AuthenticatedSession, Credentials, check_socket


def envelope(credentials, sequence=0, ident="a" * 32, data=None):
    return credentials.sign(
        {
            "session": credentials.session,
            "id": ident,
            "sequence": sequence,
            "operation": "count",
            "data": data or {"test": 1},
        },
        "request",
    )


def test_authenticated_replay_is_idempotent():
    credentials = Credentials("b" * 32, b"x" * 32)
    calls = []
    session = AuthenticatedSession(
        credentials, lambda op, data: calls.append(data) or {"input_tokens": 10}
    )
    request = envelope(credentials)
    first = session.accept(request)
    assert session.accept(copy.deepcopy(request)) == first and len(calls) == 1
    assert credentials.verify(first, "response")["result"] == {"input_tokens": 10}
    with pytest.raises(IPCError):
        session.accept(envelope(credentials, data={"test": 2}))
    with pytest.raises(IPCError):
        session.accept(envelope(credentials, sequence=3, ident="c" * 32))


@pytest.mark.parametrize("attack", ["key", "session", "direction", "body", "mac"])
def test_authentication_failure_before_dispatch(attack):
    credentials = Credentials("b" * 32, b"x" * 32)
    called = []
    session = AuthenticatedSession(credentials, lambda *args: called.append(args))
    request = envelope(credentials)
    if attack == "key":
        request = envelope(Credentials("b" * 32, b"y" * 32))
    elif attack == "session":
        request = envelope(Credentials("c" * 32, b"x" * 32))
    elif attack == "direction":
        request = credentials.sign({k: v for k, v in request.items() if k != "mac"}, "response")
    elif attack == "body":
        request["operation"] = "complete"
    else:
        request["mac"] = "00" * 32
    with pytest.raises(IPCError):
        session.accept(request)
    assert not called


def test_socket_symlink_permissions_and_inode():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="rca-auth-") as tmp:
        socket_path_case(Path(tmp))


def socket_path_case(tmp_path):
    tmp_path.chmod(0o750)
    gid = tmp_path.stat().st_gid
    path = tmp_path / "worker.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        path.chmod(0o660)
        identity = check_socket(str(path), os.getuid(), gid)
        assert identity[1] == path.stat().st_ino
        path.chmod(0o666)
        with pytest.raises(IPCError):
            check_socket(str(path), os.getuid(), gid)
        path.chmod(0o660)
        link = tmp_path / "link.sock"
        link.symlink_to(path)
        with pytest.raises(IPCError):
            check_socket(str(link), os.getuid(), gid)
        with pytest.raises(IPCError):
            check_socket(str(path), os.getuid() + 1, gid)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com/v1/responses",
        "https://example.invalid/v1/responses",
        "https://api.openai.com:444/v1/responses",
        "https://user@api.openai.com/v1/responses",
        "https://api.openai.com/v1/responses?redirect=x",
        "https://api.openai.com/v1/files",
        "https://127.0.0.1/v1/responses",
    ],
)
def test_mock_egress_policy_rejects_unapproved(url):
    with pytest.raises(ValueError):
        validate_destination(url)


def test_live_disabled_and_tls_defaults():
    import ssl

    validate_destination("https://api.openai.com:443/v1/responses")
    ctx = tls_context()
    assert ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED
    with pytest.raises(RuntimeError, match="LIVE_BLOCKED"):
        live_client()


def test_socket_replacement_between_stat_and_connect_is_rejected(monkeypatch):
    from rca_agent import secure_uds

    calls = []
    identities = iter([(1, 2), (1, 3)])
    monkeypatch.setattr(secure_uds, "check_socket", lambda *args: next(identities))
    monkeypatch.setattr(secure_uds, "peer_identity", lambda conn: (10001, 10002))

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            pass

        def connect(self, path):
            pass

        def sendall(self, data):
            calls.append(data)

    monkeypatch.setattr(secure_uds.socket, "socket", lambda *args: Connection())
    client = secure_uds.ChannelClient("/channel/worker.sock", Credentials("b" * 32, b"x" * 32))
    with pytest.raises(IPCError):
        client.request("count", {})
    assert client.blocked and not calls


def test_normal_null_error_response_is_not_failure(monkeypatch):
    from rca_agent import secure_uds
    from rca_agent.ipc_protocol import encode_frame, parse_frame

    credentials = Credentials("b" * 32, b"x" * 32)
    monkeypatch.setattr(secure_uds, "check_socket", lambda *args: (1, 2))
    monkeypatch.setattr(secure_uds, "peer_identity", lambda conn: (10001, 10002))

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            pass

        def connect(self, path):
            pass

        def sendall(self, data):
            request = credentials.verify(parse_frame(data, 262144), "request")
            self.buffer = encode_frame(
                credentials.sign(
                    {
                        "session": credentials.session,
                        "id": request["id"],
                        "sequence": request["sequence"],
                        "result": {"content": "{}", "error": None},
                    },
                    "response",
                ),
                262144,
            )

        def gettimeout(self):
            return 3

        def recv(self, n):
            out, self.buffer = self.buffer[:n], self.buffer[n:]
            return out

    monkeypatch.setattr(secure_uds.socket, "socket", lambda *args: Connection())
    client = secure_uds.ChannelClient("/channel/worker.sock", credentials)
    assert client.request("complete", {})["error"] is None and not client.blocked


def test_slow_frame_uses_absolute_deadline(monkeypatch):
    from rca_agent import secure_uds

    moments = iter([0, 1, 2, 4])
    monkeypatch.setattr(secure_uds.time, "monotonic", lambda: next(moments))

    class Slow:
        def gettimeout(self):
            return 3

        def settimeout(self, n):
            assert n > 0

        def recv(self, n):
            return b" "

    with pytest.raises(IPCError, match="deadline"):
        secure_uds.read_frame(Slow())
