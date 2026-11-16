"""Linux-only authenticated bounded UDS channel for one short-lived offline session."""

import hashlib
import hmac
import json
import os
import socket
import stat
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field

from rca_agent.ipc_protocol import IPCError, encode_frame, parse_frame

MAXIMUM = 262144


def peer_identity(conn):
    if not hasattr(socket, "SO_PEERCRED"):
        raise IPCError("peer credentials unavailable")
    _, uid, gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return uid, gid


def check_socket(path, uid, gid):
    directory = os.lstat(os.path.dirname(path))
    node = os.lstat(path)
    if (
        not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != uid
        or directory.st_gid != gid
        or stat.S_IMODE(directory.st_mode) != 0o750
        or not stat.S_ISSOCK(node.st_mode)
        or node.st_uid != uid
        or node.st_gid != gid
        or stat.S_IMODE(node.st_mode) != 0o660
        or node.st_nlink != 1
    ):
        raise IPCError("unsafe socket path")
    return node.st_dev, node.st_ino


def read_frame(conn):
    data = bytearray()
    deadline = time.monotonic() + (conn.gettimeout() or 3)
    while not data.endswith(b"\n"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IPCError("channel frame deadline exceeded")
        conn.settimeout(remaining)
        chunk = conn.recv(1)
        if not chunk or len(data) >= MAXIMUM:
            raise IPCError("incomplete or oversized channel frame")
        data.extend(chunk)
    return parse_frame(bytes(data), MAXIMUM)


@dataclass(frozen=True)
class Credentials:
    session: str
    key: bytes = field(repr=False)

    def __post_init__(self):
        if len(self.session) != 32 or len(self.key) != 32:
            raise ValueError("invalid session credentials")

    def sign(self, body, direction):
        data = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        mac = hmac.new(self.key, direction.encode() + b"\0" + data, hashlib.sha256).hexdigest()
        return {**body, "mac": mac}

    def verify(self, envelope, direction):
        if not isinstance(envelope, dict) or "mac" not in envelope:
            raise IPCError("authentication failed")
        body = {k: v for k, v in envelope.items() if k != "mac"}
        mac = self.sign(body, direction)["mac"]
        if (
            body.get("session") != self.session
            or not isinstance(envelope["mac"], str)
            or not hmac.compare_digest(mac, envelope["mac"])
        ):
            raise IPCError("authentication failed")
        return body


class AuthenticatedSession:
    """In-memory replay state. No restart/recovery or ambiguous automatic resend."""

    def __init__(self, credentials, handler, capacity=32):
        self.credentials, self.handler, self.capacity = credentials, handler, capacity
        self.entries = {}
        self.lock = threading.Lock()

    def accept(self, envelope):
        body = self.credentials.verify(envelope, "request")
        if (
            set(body) != {"session", "id", "sequence", "operation", "data"}
            or not isinstance(body["id"], str)
            or len(body["id"]) != 32
            or type(body["sequence"]) is not int
            or body["sequence"] < 0
            or body["operation"] not in {"count", "complete"}
            or not isinstance(body["data"], dict)
        ):
            raise IPCError("invalid authenticated request")
        canonical = json.dumps(body, sort_keys=True, allow_nan=False)
        with self.lock:
            if body["id"] in self.entries:
                previous, response = self.entries[body["id"]]
                if previous != canonical:
                    raise IPCError("request ID collision")
                return response
            if body["sequence"] != len(self.entries) or len(self.entries) >= self.capacity:
                raise IPCError("invalid sequence or capacity")
            # Record failures too; no re-execution of uncertain outcomes.
            try:
                result = self.handler(body["operation"], body["data"])
            except Exception:  # noqa: BLE001 - never disclose authenticated payloads
                result = {"error": "worker_failure"}
            response = self.credentials.sign(
                {
                    "session": self.credentials.session,
                    "id": body["id"],
                    "sequence": body["sequence"],
                    "result": result,
                },
                "response",
            )
            self.entries[body["id"]] = (canonical, response)
            return response


class ChannelClient:
    def __init__(self, path, credentials, timeout=3):
        self.path, self.credentials, self.timeout = path, credentials, timeout
        self.sequence = 0
        self.lock = threading.Lock()
        self.blocked = False
        self.failure = ""

    def request(self, operation, data):
        with self.lock:
            if self.blocked:
                raise IPCError("channel blocked")
            body = {
                "session": self.credentials.session,
                "id": uuid.uuid4().hex,
                "sequence": self.sequence,
                "operation": operation,
                "data": data,
            }
            try:
                response = self._exchange(body)
                if (
                    set(response) != {"session", "id", "sequence", "result"}
                    or response["id"] != body["id"]
                    or response["sequence"] != body["sequence"]
                ):
                    raise IPCError("response identity mismatch")
                self.sequence += 1
                self.failure = "worker_result"
                if response["result"].get("error"):
                    raise IPCError("worker failed")
                self.failure = ""
                return response["result"]
            except Exception:  # noqa: BLE001 - no authenticated payload disclosure
                self.blocked = True
                raise IPCError("authenticated channel failed") from None

    def _exchange(self, body):
        self.failure = "socket_path"
        before = check_socket(self.path, 10001, 10002)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(self.timeout)
            conn.connect(self.path)
            self.failure = "peer_identity"
            if (
                peer_identity(conn) != (10001, 10002)
                or check_socket(self.path, 10001, 10002) != before
            ):
                raise IPCError("worker identity changed")
            conn.sendall(encode_frame(self.credentials.sign(body, "request"), MAXIMUM))
            self.failure = "response_auth"
            return self.credentials.verify(read_frame(conn), "response")


def serve(path, credentials, handler):
    session = AuthenticatedSession(credentials, handler)
    os.umask(0o177)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        # Refuse stale files and symlinks; never unlink an existing path.
        server.bind(path)
        os.chmod(path, 0o660)
        server.listen(8)
        while True:
            conn, _ = server.accept()
            with conn:
                conn.settimeout(3)
                try:
                    if peer_identity(conn) != (10002, 10002):
                        raise IPCError("broker identity failed")
                    response = session.accept(read_frame(conn))
                    conn.sendall(encode_frame(response, MAXIMUM))
                except Exception:  # noqa: BLE001, S110 - no telemetry logging
                    # Authentication/protocol errors close the connection; no payload logging.
                    pass
