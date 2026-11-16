"""Local synthetic TLS API. Test keys are never copied into an image."""

import http.server
import json
import socketserver
import ssl
import sys
import threading
import time
from pathlib import Path

import httpx

from rca_agent.integrated_worker import MockAPI


def main():
    config = json.loads(sys.stdin.readline())
    api = MockAPI(config["fault"], "/audit/api.json")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= size <= 262144:
                    raise ValueError("body limit")
                body = self.rfile.read(size)
                if config["fault"] == "redirect":
                    self.send_response(307)
                    self.send_header("Location", "https://example.invalid/")
                    self.end_headers()
                    return
                model = not self.path.endswith("/input_tokens")
                if model and config["fault"] in ("429", "5xx"):
                    self.send_response(429 if config["fault"] == "429" else 503)
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"mock"}}')
                    return
                if model and config["fault"] == "slow_response":
                    self.send_response(200)
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    for i in range(20):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.2)
                    return
                result = api(
                    httpx.Request("POST", "https://api.openai.com" + self.path, content=body)
                )
                data = result.content
                if model and config["fault"] == "refusal":
                    raw = result.json()
                    raw["output"][0]["content"] = [{"type": "refusal", "refusal": "mock refusal"}]
                    data = json.dumps(raw).encode()
                self.send_response(result.status_code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:  # noqa: BLE001 - never echo request bodies
                try:
                    self.send_error(400)
                except OSError:
                    pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"mock-listener")

        def log_message(self, *args):
            pass

    class Server(http.server.ThreadingHTTPServer):
        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
            self.server_name = "local-mock"
            self.server_port = self.server_address[1]

    server = Server(("0.0.0.0", 8443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if config["fault"] == "tls_version":
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain("/cert/server.pem", "/cert/server.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    control = Server(("0.0.0.0", 8444), Handler)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    Path("/audit/ready").write_text("ready")
    server.serve_forever()


if __name__ == "__main__":
    main()
