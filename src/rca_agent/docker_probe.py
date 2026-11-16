"""Manual synthetic Docker network probe. Never uses credentials or provider APIs."""

import http.server
import json
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

IMAGE = "python:3.12.13-slim"
SERVER = """
import http.server, threading
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'synthetic-mock')
    def log_message(self, *args): pass
for port in (8080, 8081):
    server = http.server.ThreadingHTTPServer(('0.0.0.0', port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
threading.Event().wait()
"""
BROKER = r"""
import errno, http.client, json, os, socket, sys, time
config = json.loads(sys.argv[1])
status = dict(line.split(':', 1) for line in open('/proc/self/status') if ':' in line)
checks = {'nonroot': os.getuid() == 65534, 'capabilities_removed': int(status['CapEff'].strip(),16)==0,
          'no_new_privileges': status['NoNewPrivs'].strip()=='1'}
try:
    open('/root-write-probe','w')
    checks['readonly'] = False
except OSError as exc:
    checks['readonly'] = exc.errno == errno.EROFS
checks['docker_socket_absent'] = not os.path.exists('/var/run/docker.sock')
files = {}
for name, path in config['files'].items():
    try:
        with open(path, 'rb'): pass
        files[name] = 'readable'
    except FileNotFoundError:
        files[name] = 'not_mounted'
    except PermissionError:
        files[name] = 'permission_denied'
checks['files_unavailable'] = all(v in ('not_mounted','permission_denied') for v in files.values())
mock = False
for attempt in range(30):
    try:
        c = http.client.HTTPConnection('mock',8080,timeout=1)
        c.request('GET','/fixture')
        r = c.getresponse()
        mock = r.status==200 and r.read()==b'synthetic-mock'
        c.close()
        break
    except OSError:
        time.sleep(0.1)
checks['mock_http'] = mock
probes = {}
for name, host, port in config['targets']:
    try:
        with socket.create_connection((host,port),timeout=1): pass
        probes[name] = 0
    except OSError as exc:
        probes[name] = errno.errorcode.get(exc.errno, 'UNVERIFIED') if not isinstance(exc, socket.gaierror) else 'DNS_UNVERIFIED'
print(json.dumps({'checks':checks,'files':files,'connections':probes}))
"""


def classify_connection(code):
    if code in ("ENETUNREACH", "EHOSTUNREACH", "EACCES", "EPERM"):
        return "DENIED"
    if code in (0, "ECONNREFUSED"):
        return "REACHABLE"
    return "UNVERIFIED"


def validate_inspect(value, network):
    host = value["HostConfig"]
    if (
        value["Config"]["User"] != "65534:65534"
        or not host["ReadonlyRootfs"]
        or host["CapDrop"] != ["ALL"]
        or host.get("CapAdd")
        or "no-new-privileges" not in host["SecurityOpt"]
        or value["Mounts"]
        or set(value["NetworkSettings"]["Networks"]) != {network}
        or host.get("Privileged")
        or host.get("PortBindings")
    ):
        raise ValueError("unsafe container configuration")


def docker(*args):
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


def run_probe():
    prefix = "rca-broker-probe-" + uuid.uuid4().hex
    network, server, broker = prefix + "-net", prefix + "-mock", prefix + "-broker"
    resources = []
    created_network = False
    listener = http.server.ThreadingHTTPServer(("0.0.0.0", 0), QuietHandler)
    threading.Thread(target=listener.serve_forever, daemon=True).start()
    try:
        # Resolve an existing local image to an immutable ID; never pull or build.
        image = json.loads(docker("image", "inspect", IMAGE))[0]["Id"]
        with tempfile.TemporaryDirectory(prefix="rca-evaluator-synthetic-") as tmp:
            files = {}
            for name in ("blind", "answer", "mapping", "key"):
                path = Path(tmp) / name
                path.write_text("synthetic-only")
                files[name] = str(path)
            docker("network", "create", "--internal", network)
            created_network = True
            net = json.loads(docker("network", "inspect", network))[0]
            if not net["Internal"]:
                raise ValueError("internal network required")
            gateway = net["IPAM"]["Config"][0]["Gateway"]
            config = {
                "files": files,
                "targets": [
                    ["external_ip", "1.1.1.1", 443],
                    ["bridge_gateway", gateway, 80],
                    ["host_gateway", "host.docker.internal", listener.server_port],
                    ["disallowed_port", "mock", 8081],
                ],
            }
            for name, program, extra in (
                (server, SERVER, []),
                (broker, BROKER, [json.dumps(config)]),
            ):
                args = [
                    "create",
                    "--name",
                    name,
                    "--network",
                    network,
                    "--read-only",
                    "--user",
                    "65534:65534",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--pids-limit",
                    "64",
                    "--memory",
                    "128m",
                    "--cpus",
                    "1",
                    "--log-driver",
                    "none",
                ]
                if name == server:
                    args += ["--network-alias", "mock"]
                resources.append(name)
                docker(*args, image, "python", "-B", "-c", program, *extra)
                validate_inspect(json.loads(docker("inspect", name))[0], network)
            docker("start", server)
            result = json.loads(docker("start", "--attach", broker))
            exit_code = json.loads(docker("inspect", broker))[0]["State"]["ExitCode"]
            if exit_code != 0:
                raise ValueError("broker probe failed")
            result["destinations"] = {
                name: classify_connection(code) for name, code in result["connections"].items()
            }
            result["status"] = (
                "PASS"
                if (
                    all(result["checks"].values())
                    and all(v == "DENIED" for v in result["destinations"].values())
                )
                else "BLOCKED"
            )
            return result
    finally:
        listener.shutdown()
        listener.server_close()
        failures = []
        for name in reversed(resources):
            try:
                docker("rm", "--force", name)
            except (subprocess.SubprocessError, OSError):
                failures.append("container cleanup failed")
        if created_network:
            try:
                docker("network", "rm", network)
            except (subprocess.SubprocessError, OSError):
                failures.append("network cleanup failed")
        if failures:
            raise RuntimeError("; ".join(failures))


class QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def main():
    try:
        result = run_probe()
    except (ValueError, OSError, subprocess.SubprocessError, RuntimeError, KeyError):
        print(json.dumps({"status": "BLOCKED", "reason": "probe or cleanup failed"}))
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True))
    if result["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
