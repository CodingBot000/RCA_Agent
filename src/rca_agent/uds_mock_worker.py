"""Bounded synthetic HTTP over Unix sockets. No forwarding or provider integration."""

import json
import os
import socket

SOCKET = "/channel/worker.sock"
MAX_BODY = 2048
MAX_HEADER = 4096


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def validate_body(body):
    value = json.loads(
        body,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")),
    )
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "probe"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["probe"] != "synthetic"
    ):
        raise ValueError("invalid request")
    return {"ok": True, "probe": "synthetic"}


def exchange(conn):
    conn.settimeout(2)
    status, output = 400, b"{}"
    try:
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = conn.recv(1)
            if not chunk:
                raise ValueError("incomplete header")
            header += chunk
            if len(header) > MAX_HEADER:
                raise ValueError("large header")
        lines = header.decode("ascii").split("\r\n")
        if lines[0] != "POST /mock HTTP/1.1":
            raise ValueError("unsupported request target")
        headers = pairs(tuple(line.split(":", 1)) for line in lines[1:-2])
        # Normalize names before duplicate checking to reject ambiguous framing.
        headers = pairs((key.lower(), value.strip()) for key, value in headers.items())
        if set(headers) - {"host", "content-length", "content-type"}:
            raise ValueError("unsupported header")
        length = headers.get("content-length", "")
        if not length.isascii() or not length.isdecimal():
            raise ValueError("invalid length")
        length = int(length)
        if length > MAX_BODY:
            status = 413
        else:
            body = b""
            while len(body) < length:
                chunk = conn.recv(length - len(body))
                if not chunk:
                    raise ValueError("incomplete body")
                body += chunk
            output = json.dumps(validate_body(body)).encode()
            status = 200
    except (ValueError, OSError, UnicodeError, RecursionError):
        status = 400
    try:
        conn.sendall(
            f"HTTP/1.1 {status} Result\r\nContent-Length: {len(output)}\r\nConnection: close\r\n\r\n".encode()
            + output
        )
    except OSError:
        pass
    return status


def main():
    os.umask(0o177)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(SOCKET)
        os.chmod(SOCKET, 0o600)
        server.listen(8)
        try:
            while True:
                conn, _ = server.accept()
                with conn:
                    exchange(conn)
        finally:
            os.unlink(SOCKET)


if __name__ == "__main__":
    main()
