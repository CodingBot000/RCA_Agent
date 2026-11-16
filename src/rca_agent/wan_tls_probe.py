"""Manual real DNS/TLS handshake-only test. No HTTP bytes, API keys, or model requests."""

import argparse
import json
import subprocess
import uuid

from rca_agent.dns_guard import AddressLease, apply_script
from rca_agent.docker_probe import docker
from rca_agent.live_deployment import system_addresses

CLIENT = r"""
import sys,json,socket,ssl,time,errno
config=json.loads(sys.stdin.readline())
ctx=ssl.create_default_context();ctx.minimum_version=ssl.TLSVersion.TLSv1_2
assert ctx.check_hostname and ctx.verify_mode==ssl.CERT_REQUIRED
assert not any('rca-local-mock-ca' in str(cert) for cert in ctx.get_ca_certs())
results=[]
for address in config['addresses']:
 if time.time()+config['timeout']>=config['expires']: raise SystemExit(2)
 family=socket.AF_INET6 if ':' in address else socket.AF_INET
 try:
  with socket.socket(family,socket.SOCK_STREAM) as conn:
   conn.settimeout(config['timeout']);conn.connect((address,443))
   with ctx.wrap_socket(conn,server_hostname='api.openai.com') as tls:
    results.append({'family':6 if ':' in address else 4,'tls':tls.version(),'verified':True})
 except OSError: results.append({'family':6 if ':' in address else 4,'verified':False})
checks={}
for name,host,port in [('other_ip','1.1.1.1',443),('wrong_port',config['addresses'][0],80)]:
 try:
  with socket.create_connection((host,port),timeout=1): pass
  checks[name]='REACHABLE'
 except OSError as e: checks[name]=errno.errorcode.get(e.errno,'UNVERIFIED')
print(json.dumps({'tls':results,'denials':checks,'http_requests':0,'keys_used':False}))
"""


def run_probe(family_policy, lease_seconds, timeout):
    if family_policy not in ("ipv4-only", "dual-stack") or not 2 * timeout < lease_seconds:
        raise ValueError("explicit safe probe limits required")
    addresses = system_addresses(timeout)
    if family_policy == "ipv4-only":
        addresses = tuple(a for a in addresses if ":" not in a)
    prefix = "rca-wan-" + uuid.uuid4().hex
    net = prefix + "-net"
    proxy = prefix + "-proxy"
    helper = prefix + "-guard"
    names = []
    created = False
    proc = None
    try:
        image = json.loads(docker("image", "inspect", "rca-egress:mock"))[0]["Id"]
        docker("network", "create", *(["--ipv6"] if family_policy == "dual-stack" else []), net)
        created = True
        names.append(proxy)
        docker(
            "create",
            "-i",
            "--name",
            proxy,
            "--network",
            net,
            "--read-only",
            "--user",
            "10005:10002",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "128m",
            "--pids-limit",
            "32",
            "--log-driver",
            "none",
            image,
            "python",
            "-c",
            CLIENT,
        )
        proc = subprocess.Popen(
            ["docker", "start", "--attach", "--interactive", proxy],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        import time

        for i in range(50):
            if json.loads(docker("inspect", proxy))[0]["State"]["Running"]:
                break
            time.sleep(0.1)
        else:
            raise ValueError("handshake role not running")
        now = int(docker("exec", proxy, "python", "-c", "import time;print(int(time.time()))"))
        lease = AddressLease(addresses, now + lease_seconds, 443)
        names.append(helper)
        result = docker(
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
        if result != "lease_applied":
            raise ValueError("lease failed")
        raw, _ = proc.communicate(
            json.dumps(
                {"addresses": addresses, "expires": lease.expires, "timeout": timeout}
            ).encode()
            + b"\n",
            timeout=lease_seconds + 5,
        )
        if proc.returncode != 0:
            raise ValueError("TLS-only probe failed")
        output = json.loads(raw)
        verified = all(item["verified"] for item in output["tls"])
        denied = all(
            code in ("EHOSTUNREACH", "ENETUNREACH", "EACCES", "EPERM")
            for code in output["denials"].values()
        )
        return {
            "status": "PASS" if verified and denied else "BLOCKED",
            "family_policy": family_policy,
            "evidence": output,
            "live": "LIVE_BLOCKED",
        }
    finally:
        errors = []
        for name in reversed(names):
            try:
                docker("rm", "--force", name)
            except (OSError, subprocess.SubprocessError):
                errors.append("container cleanup")
        if proc:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout):
                if stream and not stream.closed:
                    stream.close()
        if created:
            try:
                docker("network", "rm", net)
            except (OSError, subprocess.SubprocessError):
                errors.append("network cleanup")
        if errors:
            raise RuntimeError("; ".join(errors))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--family-policy", choices=["ipv4-only", "dual-stack"], required=True)
    p.add_argument("--lease-seconds", type=int, required=True)
    p.add_argument("--timeout-seconds", type=float, required=True)
    args = p.parse_args()
    try:
        result = run_probe(args.family_policy, args.lease_seconds, args.timeout_seconds)
    except Exception:  # noqa: BLE001 - no resolver or TLS exception detail disclosure
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "scope": "DNS/TLS only",
                    "http_requests": 0,
                    "keys_used": False,
                    "live": "LIVE_BLOCKED",
                }
            )
        )
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True))
    if result["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
