"""Manual local TLS + netns firewall extension for the offline Docker RCA runner."""

import argparse
import json
import secrets
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from rca_agent.docker_integration import run_session, source_hash, validate_audit, validate_ready
from rca_agent.docker_probe import docker
from rca_agent.integrated_worker import MAXIMUM
from rca_agent.ipc_runner import FrameReader, FrameWriter

IMAGE = "rca-egress:mock"


def certificates(root, fault):
    trust = root / "trust"
    cert = root / "cert"
    trust.mkdir()
    cert.mkdir()

    def openssl(*args):
        subprocess.run(
            ["openssl", *args],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )

    def ca(name):
        openssl(
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(root / (name + ".key")),
            "-out",
            str(root / (name + ".pem")),
            "-days",
            "1",
            "-subj",
            "/CN=rca-local-mock-ca",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
        )

    ca("ca")
    if fault == "tls_chain":
        ca("other")
    hostname = "wrong.example.invalid" if fault == "tls_hostname" else "api.openai.com"
    openssl(
        "req",
        "-new",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(cert / "server.key"),
        "-out",
        str(root / "server.csr"),
        "-subj",
        "/CN=" + hostname,
    )
    (root / "extensions").write_text(
        "subjectAltName=DNS:"
        + hostname
        + "\nbasicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\n"
    )
    signer = "other" if fault == "tls_chain" else "ca"
    openssl(
        "x509",
        "-req",
        "-in",
        str(root / "server.csr"),
        "-CA",
        str(root / (signer + ".pem")),
        "-CAkey",
        str(root / (signer + ".key")),
        "-CAcreateserial",
        "-out",
        str(cert / "server.pem"),
        "-days",
        "1",
        "-extfile",
        str(root / "extensions"),
    )
    (trust / "ca.pem").write_bytes((root / "ca.pem").read_bytes())
    # Parent temporary directory remains 0700 on the host; only specific child directories are mounted.
    for directory in (trust, cert):
        directory.chmod(0o755)
        for path in directory.iterdir():
            path.chmod(0o444)
    return trust, cert


def firewall_rules(ip):
    # Runs in the Proxy's own network namespace, never the Engine-host namespace.
    return f"""
import subprocess
ip={ip!r}
def run(*args): subprocess.run(args,check=True,stdout=subprocess.DEVNULL)
for binary in ('iptables','ip6tables'):
 run(binary,'-P','OUTPUT','DROP');run(binary,'-P','INPUT','DROP')
 run(binary,'-F','OUTPUT');run(binary,'-F','INPUT')
 run(binary,'-A','INPUT','-m','conntrack','--ctstate','ESTABLISHED,RELATED','-j','ACCEPT')
 run(binary,'-A','OUTPUT','-m','conntrack','--ctstate','ESTABLISHED,RELATED','-j','ACCEPT')
 if binary=='iptables': run(binary,'-A','OUTPUT','-p','tcp','-d',ip,'--dport','8443','-j','ACCEPT')
 run(binary,'-A','OUTPUT','-j','REJECT','--reject-with','icmp-admin-prohibited' if binary=='iptables' else 'icmp6-adm-prohibited')
print('firewall_applied')
"""


class MockEgress:
    def __init__(self, fault="none", *, journal_volume=None):
        self.fault = fault
        self.prefix = "rca-proxy-" + uuid.uuid4().hex
        self.volume = self.prefix + "-channel"
        self.network = self.prefix + "-net"
        self.journal_volume = journal_volume or self.prefix + "-journal"
        self.own_journal = journal_volume is None
        self.names = []
        self.volumes = []
        self.networks = []
        self.procs = []
        self.temp = None
        self.credentials = {
            "session": secrets.token_hex(16),
            "key": secrets.token_hex(32),
            "replay": fault == "proxy_replay",
        }
        self.evidence = {}

    def __enter__(self):
        return self

    def start(self, private_paths, budget):
        image_info = json.loads(docker("image", "inspect", IMAGE))[0]
        if image_info["Config"].get("Labels", {}).get("org.rca.source") != source_hash():
            raise ValueError("rebuild egress image after source changes")
        image = image_info["Id"]
        self.temp = tempfile.TemporaryDirectory(prefix="rca-tls-")
        trust, cert = certificates(Path(self.temp.name), self.fault)
        self.networks.append(self.network)
        docker(
            "network",
            "create",
            "--internal",
            "--opt",
            "com.docker.network.bridge.gateway_mode_ipv4=isolated",
            self.network,
        )
        net = json.loads(docker("network", "inspect", self.network))[0]
        if (
            not net["Internal"]
            or net["Options"].get("com.docker.network.bridge.gateway_mode_ipv4") != "isolated"
        ):
            raise ValueError("isolated internal network required")
        self.volumes.append(self.volume)
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
            "o=size=1m,uid=10005,gid=10002,mode=0750",
            self.volume,
        )
        if self.own_journal:
            self.volumes.append(self.journal_volume)
            docker("volume", "create", self.journal_volume)
        initializer = self.prefix + "-journal-init"
        self.names.append(initializer)
        docker(
            "run",
            "--name",
            initializer,
            "--pull=never",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:10002",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=volume,src={self.journal_volume},dst=/journal",
            "--log-driver",
            "none",
            image,
            "python",
            "-c",
            "import os,stat;s=os.stat('/journal');\nif s.st_uid==0:\n os.chmod('/journal',0o700);os.chown('/journal',10005,10002)\nelse: assert s.st_uid==10005 and stat.S_IMODE(s.st_mode)==0o700",
        )
        api_ips = []
        for role in ("api", "deny-api", "proxy"):
            name = self.prefix + "-" + role
            self.names.append(name)
            args = [
                "create",
                "-i",
                "--name",
                name,
                "--network",
                self.network,
                "--read-only",
                "--user",
                "10005:10002" if role == "proxy" else "10006:10006",
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
            if role == "proxy":
                args += [
                    "--env",
                    f"HTTPS_PROXY=http://{api_ips[1]}:8444",
                    "--env",
                    f"ALL_PROXY=http://{api_ips[1]}:8444",
                ]
                args += [
                    "--mount",
                    f"type=volume,src={self.journal_volume},dst=/journal",
                    "--mount",
                    f"type=volume,src={self.volume},dst=/proxychannel",
                    "--mount",
                    f"type=bind,src={trust},dst=/trust,readonly",
                ]
            else:
                args += [
                    "--mount",
                    f"type=bind,src={cert},dst=/cert,readonly",
                    "--tmpfs",
                    "/audit:uid=10006,gid=10006,mode=0700,size=1m",
                ]
            module = "rca_agent.egress_proxy" if role == "proxy" else "rca_agent.tls_mock_api"
            docker(*args, image, "python", "-B", "-m", module)
            info = json.loads(docker("inspect", name))[0]
            host = info["HostConfig"]
            if (
                host.get("Privileged")
                or host.get("CapAdd")
                or host["CapDrop"] != ["ALL"]
                or not host["ReadonlyRootfs"]
                or host.get("PortBindings")
                or set(info["NetworkSettings"]["Networks"]) != {self.network}
            ):
                raise ValueError("unsafe egress container")
            mounts = {m["Destination"]: m for m in info["Mounts"]}
            if role == "proxy":
                if (
                    set(mounts) != {"/proxychannel", "/trust", "/journal"}
                    or mounts["/trust"]["RW"]
                    or mounts["/proxychannel"]["Name"] != self.volume
                ):
                    raise ValueError("unsafe Proxy mounts")
            else:
                if set(mounts) != {"/cert"} or mounts["/cert"]["RW"]:
                    raise ValueError("unsafe API mounts")
            proc = subprocess.Popen(
                ["docker", "start", "--attach", "--interactive", name],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            self.procs.append(proc)
            for attempt in range(50):
                state = json.loads(docker("inspect", name))[0]["State"]
                if state["Running"]:
                    break
                if proc.poll() is not None:
                    raise ValueError("Mock role did not start")
                time.sleep(0.1)
            else:
                raise ValueError("container start deadline exceeded")
            writer = FrameWriter(proc.stdin, MAXIMUM)
            if role != "proxy":
                writer.write(
                    {"fault": self.fault, "signals": getattr(self, "signals", ["metrics"])}, 5
                )
                docker(
                    "exec",
                    name,
                    "python",
                    "-c",
                    "from pathlib import Path;import time;\nfor i in range(150):\n if Path('/audit/ready').exists(): break\n time.sleep(.1)\nelse: raise SystemExit(1)",
                )
                api_ips.append(
                    json.loads(docker("inspect", name))[0]["NetworkSettings"]["Networks"][
                        self.network
                    ]["IPAddress"]
                )
                # Known receiving service for forbidden destination/port checks.
                docker(
                    "exec",
                    name,
                    "python",
                    "-c",
                    "import http.client;c=http.client.HTTPConnection('127.0.0.1',8444,timeout=1);c.request('GET','/');r=c.getresponse();assert r.status==200 and r.read()==b'mock-listener'",
                )
            else:
                self.proxy = name
                helper = self.prefix + "-firewall"
                self.names.append(helper)
                response = docker(
                    "run",
                    "--name",
                    helper,
                    "--pull=never",
                    "--network",
                    "container:" + name,
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
                    firewall_rules(api_ips[0]),
                )
                if response != "firewall_applied":
                    raise ValueError("firewall not verified")
                config = {
                    "mode": "offline-local-tls",
                    "ip": api_ips[0],
                    "port": 8443,
                    "fault": self.fault,
                    "session": self.credentials["session"],
                    "key": self.credentials["key"],
                    "private_paths": private_paths,
                    "budget": {
                        **asdict(budget),
                        "max_cost_usd": str(budget.max_cost_usd),
                    },
                    "pricing": {"input_per_token": "0.001", "output_per_token": "0.002"},
                    "model": "gpt-6-luna",
                    "journal_path": "/journal/requests.sqlite",
                }
                writer.write(config, 5)
                validate_ready(FrameReader(proc.stdout, MAXIMUM).read(10))
                docker(
                    "exec",
                    name,
                    "python",
                    "-c",
                    "from pathlib import Path;import time;\nfor i in range(150):\n if Path('/proxychannel/proxy.sock').exists(): break\n time.sleep(.1)\nelse: raise SystemExit(1)",
                )
        probes = r"""
import errno,json,socket,sys
ip,other=sys.argv[1:]
result={}
for name,host,port in [('wrong_port',ip,8444),('wrong_ip',other,8443),('external','1.1.1.1',443),('ipv6','2606:4700:4700::1111',443)]:
 try:
  with socket.create_connection((host,port),timeout=1): pass
  result[name]='REACHABLE'
 except OSError as e: result[name]=errno.errorcode.get(e.errno,'UNVERIFIED')
for host in ('127.0.0.11','8.8.8.8'):
 with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
  s.settimeout(1)
  try: s.sendto(b'\x00'*12,(host,53)); result['dns_'+host]='SENT'
  except OSError as e: result['dns_'+host]=errno.errorcode.get(e.errno,'UNVERIFIED')
print(json.dumps(result))
"""
        self.evidence = json.loads(docker("exec", self.proxy, "python", "-c", probes, *api_ips))
        if any(
            v not in ("EHOSTUNREACH", "ENETUNREACH", "EACCES", "EPERM")
            for v in self.evidence.values()
        ):
            raise ValueError("OS egress policy not proven")

    def audit(self):
        result = json.loads(
            docker(
                "exec",
                self.proxy,
                "python",
                "-c",
                "from pathlib import Path;print(Path('/proxychannel/audit.json').read_text())",
            )
        )
        validate_audit(result)
        return result

    def __exit__(self, *exc):
        failures = []
        for name in reversed(self.names):
            try:
                docker("rm", "--force", name)
            except (OSError, subprocess.SubprocessError):
                failures.append("container cleanup")
        for proc in self.procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                failures.append("attach cleanup")
            for stream in (proc.stdin, proc.stdout):
                stream.close()
        for name in self.volumes:
            try:
                docker("volume", "rm", name)
            except (OSError, subprocess.SubprocessError):
                failures.append("volume cleanup")
        for name in self.networks:
            try:
                docker("network", "rm", name)
            except (OSError, subprocess.SubprocessError):
                failures.append("network cleanup")
        if self.temp:
            self.temp.cleanup()
        if failures:
            raise RuntimeError("; ".join(failures))


def scenario(fault):
    mapped = (
        fault
        if fault
        in ("count_failure", "cost_limit", "unknown_usage", "delay", "worker_stop", "uds_replay")
        else "none"
    )
    with MockEgress(fault) as extension:
        result = run_session(mapped, extension=extension)
        result["scenario"] = fault
        result["os_egress"] = extension.evidence
        result["proxy_audit"] = extension.audit()
        error = json.loads(
            docker(
                "exec",
                extension.proxy,
                "python",
                "-c",
                "from pathlib import Path;p=Path('/proxychannel/error.json');print(p.read_text() if p.exists() else '{}')",
            )
        )
        allowed_errors = {
            "TLS_CERTIFICATE",
            "TLS_HOSTNAME",
            "TLS_PROTOCOL",
            "REDIRECT_DENIED",
            "HTTP_429",
            "HTTP_5XX",
            "HTTP_ERROR",
            "REFUSAL",
            "USAGE_UNAVAILABLE",
            "TIMEOUT",
            "RESPONSE_INVALID",
        }
        if error and (set(error) != {"code"} or error["code"] not in allowed_errors):
            raise ValueError("unsafe error audit")
        result["proxy_error"] = error.get("code")
        positive = fault in ("none", "uds_replay", "proxy_replay")
        if positive:
            if result["status"] != "PASS" or result["proxy_audit"] != {
                "http_calls": 4,
                "model_calls": 2,
            }:
                raise ValueError("positive scenario or replay accounting failed")
        else:
            reasons = {
                "tls_chain": {"input_estimate_unavailable"},
                "tls_hostname": {"input_estimate_unavailable"},
                "tls_version": {"input_estimate_unavailable"},
                "redirect": {"input_estimate_unavailable"},
                "count_failure": {"input_estimate_unavailable"},
                "cost_limit": {"cost_limit"},
                "429": {"usage_unavailable"},
                "5xx": {"usage_unavailable"},
                "refusal": {"provider_error"},
                "unknown_usage": {"usage_unavailable"},
                "delay": {"usage_unavailable", "timeout_usage_unknown"},
                "slow_response": {"usage_unavailable", "timeout_usage_unknown"},
                "worker_stop": {"input_estimate_unavailable"},
            }
            if result.get("ledger_failure") not in reasons[fault]:
                raise ValueError("negative scenario failed for an unexpected reason")
            if (
                fault
                in (
                    "tls_chain",
                    "tls_hostname",
                    "tls_version",
                    "redirect",
                    "count_failure",
                    "cost_limit",
                    "worker_stop",
                )
                and result["proxy_audit"]["model_calls"] != 0
            ):
                raise ValueError("model attempted before preflight")
            if fault == "refusal" and (
                result["usage_status"] != "CONFIRMED"
                or result["settled_usage"]["output_tokens"] != 50
            ):
                raise ValueError("known refusal usage not settled")
            expected_errors = {
                "tls_chain": "TLS_CERTIFICATE",
                "tls_hostname": "TLS_HOSTNAME",
                "tls_version": "TLS_PROTOCOL",
                "redirect": "REDIRECT_DENIED",
                "count_failure": "HTTP_5XX",
                "429": "HTTP_429",
                "5xx": "HTTP_5XX",
                "refusal": "REFUSAL",
                "unknown_usage": "USAGE_UNAVAILABLE",
                "delay": "TIMEOUT",
                "slow_response": "TIMEOUT",
            }
            if fault in expected_errors and result["proxy_error"] != expected_errors[fault]:
                raise ValueError("wrong failure category")
            result["status"] = "EXPECTED_FAILURE"
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="none")
    parser.add_argument("--suite", action="store_true")
    args = parser.parse_args()
    try:
        faults = (
            (
                "none",
                "uds_replay",
                "proxy_replay",
                "tls_chain",
                "tls_hostname",
                "tls_version",
                "redirect",
                "count_failure",
                "cost_limit",
                "429",
                "5xx",
                "refusal",
                "unknown_usage",
                "delay",
                "slow_response",
                "worker_stop",
            )
            if args.suite
            else (args.scenario,)
        )
        for fault in faults:
            if fault not in (
                "none",
                "uds_replay",
                "proxy_replay",
                "tls_chain",
                "tls_hostname",
                "tls_version",
                "redirect",
                "count_failure",
                "cost_limit",
                "429",
                "5xx",
                "refusal",
                "unknown_usage",
                "delay",
                "slow_response",
                "worker_stop",
            ):
                raise ValueError("unknown scenario")
            print(json.dumps(scenario(fault), sort_keys=True), flush=True)
        print(
            json.dumps({"offline_egress": "PASS", "scenarios": len(faults), "live": "LIVE_BLOCKED"})
        )
    except Exception:  # noqa: BLE001 - no credentials, TLS private keys or telemetry in public errors
        print(json.dumps({"offline_egress": "BLOCKED", "live": "LIVE_BLOCKED"}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
