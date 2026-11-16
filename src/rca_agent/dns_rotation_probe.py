"""Manual kernel address-set rotation/expiry experiment, no OpenAI traffic or keys."""

import argparse
import json
import subprocess
import uuid

from rca_agent.dns_guard import AddressLease, apply_script
from rca_agent.docker_probe import docker

SERVER = r"""
import http.server,socket,socketserver,threading
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self): self.send_response(200);self.end_headers();self.wfile.write(b'lease-mock')
 def log_message(self,*args): pass
class Server(http.server.ThreadingHTTPServer):
 address_family=socket.AF_INET6
 def server_bind(self):
  self.socket.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,0)
  socketserver.TCPServer.server_bind(self);self.server_name='mock';self.server_port=self.server_address[1]
for port in (8443,8444):
 s=Server(('::',port),Handler);threading.Thread(target=s.serve_forever,daemon=True).start()
threading.Event().wait()
"""
CLIENT = r"""
import socket,errno,json,sys
out={}
for item in json.loads(sys.argv[1]):
 name,host,port=item
 try:
  with socket.create_connection((host,port),timeout=1) as c:
   c.sendall(b'GET / HTTP/1.0\r\n\r\n'); data=c.recv(1024)
   out[name]='REACHABLE' if b'200' in data else 'UNVERIFIED'
 except OSError as e: out[name]=errno.errorcode.get(e.errno,'UNVERIFIED')
print(json.dumps(out))
"""


def run_probe():
    prefix = "rca-dns-" + uuid.uuid4().hex
    network = prefix + "-net"
    names = []
    networks = []
    try:
        image = json.loads(docker("image", "inspect", "rca-egress:mock"))[0]["Id"]
        docker(
            "network",
            "create",
            "--internal",
            "--ipv6",
            "--opt",
            "com.docker.network.bridge.gateway_mode_ipv4=isolated",
            "--opt",
            "com.docker.network.bridge.gateway_mode_ipv6=isolated",
            network,
        )
        networks.append(network)
        addresses = []
        for role in ("first", "second", "proxy"):
            name = prefix + "-" + role
            names.append(name)
            docker(
                "run",
                "--detach",
                "--name",
                name,
                "--pull=never",
                "--network",
                network,
                "--read-only",
                "--user",
                "10005:10002",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "64",
                "--memory",
                "128m",
                "--log-driver",
                "none",
                image,
                "python",
                "-c",
                SERVER if role != "proxy" else "import threading;threading.Event().wait()",
            )
            if role != "proxy":
                info = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Networks"][
                    network
                ]
                addresses.append((info["IPAddress"], info["GlobalIPv6Address"]))
                docker(
                    "exec",
                    name,
                    "python",
                    "-c",
                    "import socket,time;\nfor i in range(50):\n try:\n  c=socket.create_connection(('127.0.0.1',8444),timeout=1);c.close();break\n except OSError: time.sleep(.1)\nelse: raise SystemExit(1)",
                )
        proxy = names[-1]

        def apply(selected, seconds):
            now = int(docker("exec", proxy, "python", "-c", "import time;print(int(time.time()))"))
            lease = AddressLease(tuple(selected), now + seconds, 8443, True)
            helper = prefix + "-helper-" + str(len(names))
            names.append(helper)
            value = docker(
                "run",
                "--name",
                helper,
                "--pull=never",
                "--network",
                "container:" + proxy,
                "--read-only",
                "--user",
                "0",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "NET_ADMIN",
                "--security-opt",
                "no-new-privileges",
                "--log-driver",
                "none",
                image,
                "python",
                "-c",
                apply_script(lease),
            )
            if value != "lease_applied":
                raise ValueError("lease update failed")
            return lease

        raw_errors = {}

        def counters():
            helper = prefix + "-counter-" + str(len(names))
            names.append(helper)
            code = "import subprocess,json;d={};\nfor fam,binary in ((4,'iptables'),(6,'ip6tables')):\n out=subprocess.check_output([binary,'-n','-v','-x','-L','OUTPUT'],text=True)\n d[fam]=sum(int(line.split()[0]) for line in out.splitlines() if 'REJECT' in line)\nprint(json.dumps(d))"
            return json.loads(
                docker(
                    "run",
                    "--name",
                    helper,
                    "--pull=never",
                    "--network",
                    "container:" + proxy,
                    "--read-only",
                    "--user",
                    "0",
                    "--cap-drop",
                    "ALL",
                    "--cap-add",
                    "NET_ADMIN",
                    "--log-driver",
                    "none",
                    image,
                    "python",
                    "-c",
                    code,
                )
            )

        def connect(targets):
            import ipaddress

            result = {}
            for target in targets:
                before_count = counters()
                raw = json.loads(
                    docker("exec", proxy, "python", "-c", CLIENT, json.dumps([target]))
                )
                after_count = counters()
                name, host, _ = target
                value = raw[name]
                raw_errors[name] = value
                family = str(ipaddress.ip_address(host).version)
                if value == "UNVERIFIED" and after_count[family] > before_count[family]:
                    value = "DENIED_BY_KERNEL_COUNTER"
                result[name] = value
            return result

        first, second = addresses
        apply(first, 40)
        before = connect(
            [
                ["v4", first[0], 8443],
                ["v6", first[1], 8443],
                ["wrong_ip", second[0], 8443],
                ["wrong_port", first[0], 8444],
            ]
        )
        lease = apply(second, 20)
        after = connect(
            [
                ["old_v4", first[0], 8443],
                ["old_v6", first[1], 8443],
                ["new_v4", second[0], 8443],
                ["new_v6", second[1], 8443],
            ]
        )
        # No guardian remains running. Kernel time rules must expire independently.
        docker(
            "exec",
            proxy,
            "python",
            "-c",
            f"import time;time.sleep(max(0,{lease.expires}+1-time.time()))",
        )
        expired = connect([["expired_v4", second[0], 8443], ["expired_v6", second[1], 8443]])
        denied = {"EHOSTUNREACH", "ENETUNREACH", "EACCES", "EPERM", "DENIED_BY_KERNEL_COUNTER"}
        passed = (
            before["v4"] == before["v6"] == "REACHABLE"
            and before["wrong_ip"] in denied
            and before["wrong_port"] in denied
            and after["new_v4"] == after["new_v6"] == "REACHABLE"
            and after["old_v4"] in denied
            and after["old_v6"] in denied
            and all(v in denied for v in expired.values())
        )
        return {
            "status": "PASS" if passed else "BLOCKED",
            "before": before,
            "rotated": after,
            "expired": expired,
            "raw_errors": raw_errors,
            "live": "LIVE_BLOCKED",
        }
    finally:
        errors = []
        for name in reversed(names):
            try:
                docker("rm", "--force", name)
            except (OSError, subprocess.SubprocessError):
                errors.append("container cleanup")
        for name in networks:
            try:
                docker("network", "rm", name)
            except (OSError, subprocess.SubprocessError):
                errors.append("network cleanup")
        if errors:
            raise RuntimeError("; ".join(errors))


def main():
    argparse.ArgumentParser().parse_args()
    try:
        result = run_probe()
    except Exception:  # noqa: BLE001 - no namespace command details in public errors
        print(
            json.dumps(
                {"status": "BLOCKED", "live": "LIVE_BLOCKED", "reason": "kernel lease probe failed"}
            )
        )
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True))
    if result["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
