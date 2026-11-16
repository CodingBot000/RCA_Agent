"""Explicit approved single-case deployment. Never constructed by default or by Mock tests."""

import json
import os
import secrets
import select
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import asdict

from rca_agent.dns_guard import AddressLease, apply_script
from rca_agent.docker_integration import source_hash, validate_ready
from rca_agent.docker_probe import docker
from rca_agent.integrated_worker import MAXIMUM
from rca_agent.ipc_runner import FrameReader, FrameWriter

IMAGE = "rca-egress:mock"


def read_credential_fd(fd):
    if type(fd) is not int or fd < 3:
        raise ValueError("explicit credential FD >=3 required")
    if not stat.S_ISFIFO(os.fstat(fd).st_mode):
        raise ValueError("one-shot credential pipe required")
    raw = bytearray()
    deadline = time.monotonic() + 5
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise ValueError("credential pipe deadline")
        chunk = os.read(fd, 4097 - len(raw))
        if not chunk:
            break
        raw.extend(chunk)
        if len(raw) > 4096:
            raise ValueError("credential too large")
    value = raw.decode("ascii")
    raw[:] = b"\0" * len(raw)
    if not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("invalid credential")
    return value


def system_addresses(timeout):
    code = "import socket,json;print(json.dumps(sorted({x[4][0] for x in socket.getaddrinfo('api.openai.com',443,type=socket.SOCK_STREAM)})))"
    value = subprocess.run(
        [__import__("sys").executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return tuple(json.loads(value.stdout))


class LiveDeployment:
    production = True

    def __init__(
        self,
        profile,
        *,
        credential_fd,
        journal_volume,
        lease_seconds,
        refresh_seconds,
        dns_timeout,
        family_policy,
    ):
        profile.permit.require()
        if (
            type(credential_fd) is not int
            or credential_fd < 3
            or not journal_volume.startswith("rca-pilot-journal-")
            or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in journal_volume)
            or not 0 < refresh_seconds < lease_seconds
            or not 2 * profile.http_timeout < lease_seconds
            or not 0 < dns_timeout < lease_seconds
            or family_policy not in ("ipv4-only", "dual-stack")
        ):
            raise ValueError("invalid approved deployment settings")
        self.profile, self.credential_fd, self.journal_volume = (
            profile,
            credential_fd,
            journal_volume,
        )
        self.lease_seconds, self.refresh_seconds, self.dns_timeout = (
            lease_seconds,
            refresh_seconds,
            dns_timeout,
        )
        self.family_policy = family_policy
        self.prefix = "rca-live-" + uuid.uuid4().hex
        self.volume = self.prefix + "-channel"
        self.lease_volume = self.prefix + "-lease"
        self.network = self.prefix + "-net"
        self.credentials = {"session": secrets.token_hex(16), "key": secrets.token_hex(32)}
        self.names = []
        self.volumes = []
        self.networks = []
        self.procs = []
        self.stop = threading.Event()
        self.guardian = None
        self.lock = threading.Lock()
        self.failed = False

    def __enter__(self):
        return self

    def update(self, expired=False):
        addresses = self.last_addresses if expired else system_addresses(self.dns_timeout)
        if self.family_policy == "ipv4-only":
            addresses = tuple(a for a in addresses if ":" not in a)
        now = int(docker("exec", self.proxy, "python", "-c", "import time;print(int(time.time()))"))
        lease = AddressLease(addresses, now if expired else now + self.lease_seconds, 443)
        with self.lock:
            if self.stop.is_set() and not expired:
                return
            name = self.prefix + "-guard-" + str(len(self.names))
            self.names.append(name)
            script = (
                apply_script(lease)
                + f"""
import os,json,uuid
value={lease.to_dict()!r}
p='/lease/.pending-'+uuid.uuid4().hex
fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o640)
with os.fdopen(fd,'w') as out: json.dump(value,out);out.flush();os.fsync(out.fileno())
os.replace(p,'/lease/current.json')
fd=os.open('/lease',os.O_RDONLY);os.fsync(fd);os.close(fd)
"""
            )
            result = docker(
                "run",
                "--name",
                name,
                "--pull=never",
                "--network",
                "container:" + self.proxy,
                "--read-only",
                "--user",
                "0:10002",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "NET_ADMIN",
                "--security-opt",
                "no-new-privileges",
                "--mount",
                f"type=volume,src={self.lease_volume},dst=/lease",
                "--log-driver",
                "none",
                self.image,
                "python",
                "-c",
                script,
            )
            if result != "lease_applied":
                raise ValueError("kernel refresh failed")
            self.last_addresses = addresses

    def start(self, private_paths, budget):
        info = json.loads(docker("image", "inspect", IMAGE))[0]
        if info["Config"].get("Labels", {}).get("org.rca.source") != source_hash():
            raise ValueError("stale image")
        self.image = info["Id"]
        self.networks.append(self.network)
        docker(
            "network",
            "create",
            *(["--ipv6"] if self.family_policy == "dual-stack" else []),
            self.network,
        )
        for volume, options in (
            (self.volume, "uid=10005,gid=10002,mode=0750,size=1m"),
            (self.lease_volume, "uid=0,gid=10002,mode=0750,size=1m"),
        ):
            self.volumes.append(volume)
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
                "o=" + options,
                volume,
            )
        known = subprocess.run(
            ["docker", "volume", "inspect", self.journal_volume],
            capture_output=True,
            text=True,
            check=False,
        )
        if known.returncode:
            docker("volume", "create", "--label", "rca.pilot.journal=v1", self.journal_volume)
        elif json.loads(known.stdout)[0].get("Labels", {}).get("rca.pilot.journal") != "v1":
            raise ValueError("untrusted persistent volume")
        init = self.prefix + "-journal-init"
        self.names.append(init)
        docker(
            "run",
            "--name",
            init,
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
            self.image,
            "python",
            "-c",
            "import os,stat;s=os.stat('/journal');\nif s.st_uid==0:\n os.chmod('/journal',0o700);os.chown('/journal',10005,10002)\nelse: assert s.st_uid==10005 and stat.S_IMODE(s.st_mode)==0o700",
        )
        inspect_name = self.prefix + "-journal-check"
        self.names.append(inspect_name)
        if (
            docker(
                "run",
                "--name",
                inspect_name,
                "--pull=never",
                "--network",
                "none",
                "--read-only",
                "--user",
                "10005:10002",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--log-driver",
                "none",
                "--mount",
                f"type=volume,src={self.journal_volume},dst=/journal,readonly",
                self.image,
                "python",
                "-c",
                "from rca_agent.request_journal import readonly_summary;assert not any(readonly_summary('/journal/requests.sqlite').values());print('journal_fresh')",
            )
            != "journal_fresh"
        ):
            raise ValueError("existing journal requires manual review before credential use")
        self.proxy = self.prefix + "-proxy"
        self.names.append(self.proxy)
        docker(
            "create",
            "-i",
            "--name",
            self.proxy,
            "--network",
            self.network,
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
            "512m",
            "--cpus",
            "1",
            "--mount",
            f"type=volume,src={self.volume},dst=/proxychannel",
            "--mount",
            f"type=volume,src={self.lease_volume},dst=/lease,readonly",
            "--mount",
            f"type=volume,src={self.journal_volume},dst=/journal",
            "--log-driver",
            "none",
            self.image,
            "python",
            "-B",
            "-m",
            "rca_agent.production_proxy",
        )
        info = json.loads(docker("inspect", self.proxy))[0]
        host = info["HostConfig"]
        if (
            host["NetworkMode"] != self.network
            or host["CapDrop"] != ["ALL"]
            or host.get("CapAdd")
            or host.get("Privileged")
            or host.get("PortBindings")
            or info["Config"]["User"] != "10005:10002"
            or not host["ReadonlyRootfs"]
            or "no-new-privileges" not in host["SecurityOpt"]
            or {m["Destination"] for m in info["Mounts"]} != {"/proxychannel", "/lease", "/journal"}
        ):
            raise ValueError("unsafe production Proxy")
        proc = subprocess.Popen(
            ["docker", "start", "--attach", "--interactive", self.proxy],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self.procs.append(proc)
        for i in range(50):
            if json.loads(docker("inspect", self.proxy))[0]["State"]["Running"]:
                break
            time.sleep(0.1)
        else:
            raise ValueError("Proxy startup failed")
        self.update()
        # DNS/TLS only: no HTTP or credentials in this pre-key readiness probe.
        probe = """
import ssl,socket,json,time
from rca_agent.production_proxy import load_lease
lease=load_lease()
ctx=ssl.create_default_context();ctx.minimum_version=ssl.TLSVersion.TLSv1_2
for address in lease.addresses:
 lease.authorize(address,443,time.time(),5)
 family=socket.AF_INET6 if ':' in address else socket.AF_INET
 with socket.socket(family,socket.SOCK_STREAM) as sock:
  sock.settimeout(5);sock.connect((address,443))
  with ctx.wrap_socket(sock,server_hostname='api.openai.com') as tls: assert tls.version() in ('TLSv1.2','TLSv1.3')
print('system_tls_verified_no_http')
"""
        if docker("exec", self.proxy, "python", "-c", probe) != "system_tls_verified_no_http":
            raise ValueError("actual system TLS not verified")

        def renew():
            while not self.stop.wait(self.refresh_seconds):
                try:
                    self.update()
                except Exception:  # noqa: BLE001 - old kernel entries still expire; no permanent fallback
                    self.failed = True
                    try:
                        self.update(expired=True)
                    except Exception:  # noqa: BLE001 - kernel lease expiry remains the final bound
                        self.revocation_failed = True
                    self.stop.set()

        self.guardian = threading.Thread(target=renew, daemon=True)
        self.guardian.start()
        config = {
            "mode": "production",
            "permit": asdict(self.profile.permit),
            "budget": {**asdict(budget), "max_cost_usd": str(budget.max_cost_usd)},
            "pricing": {
                "input_per_token": str(self.profile.pricing.input_per_token),
                "output_per_token": str(self.profile.pricing.output_per_token),
            },
            "model": self.profile.model,
            "journal_path": "/journal/requests.sqlite",
            "transport_timeout": self.profile.http_timeout,
            "session": self.credentials["session"],
            "key": self.credentials["key"],
            "private_paths": private_paths,
            "api_credential": read_credential_fd(self.credential_fd),
        }
        writer = FrameWriter(proc.stdin, MAXIMUM)
        writer.write(config, 5)
        validate_ready(FrameReader(proc.stdout, MAXIMUM).read(15))

    def failure_code(self):
        try:
            value = json.loads(
                docker(
                    "exec",
                    self.proxy,
                    "python",
                    "-c",
                    "from pathlib import Path;p=Path('/proxychannel/error.json');print(p.read_text() if p.exists() else '{}')",
                )
            )
            allowed = {
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
                "DNS_LEASE_INVALID",
                "RESPONSE_INVALID",
            }
            if not value:
                return None
            if set(value) != {"code"} or value["code"] not in allowed:
                return "TRANSPORT_STATUS_INVALID"
            return value["code"]
        except (OSError, ValueError, subprocess.SubprocessError):
            return "PROXY_UNAVAILABLE"

    def __exit__(self, *args):
        self.stop.set()
        if self.guardian:
            self.guardian.join(timeout=self.dns_timeout + 35)
        errors = []
        with self.lock:
            for name in reversed(self.names):
                try:
                    docker("rm", "--force", name)
                except (OSError, subprocess.SubprocessError):
                    errors.append("container cleanup")
            for proc in self.procs:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    errors.append("attach cleanup")
                for stream in (proc.stdin, proc.stdout):
                    stream.close()
            for name in self.volumes:
                try:
                    docker("volume", "rm", name)
                except (OSError, subprocess.SubprocessError):
                    errors.append("volume cleanup")
            for name in self.networks:
                try:
                    docker("network", "rm", name)
                except (OSError, subprocess.SubprocessError):
                    errors.append("network cleanup")
        # Persistent production journal is intentionally not removed automatically.
        if self.guardian and self.guardian.is_alive():
            errors.append("DNS guardian not stopped")
        if errors:
            raise RuntimeError("; ".join(errors))
