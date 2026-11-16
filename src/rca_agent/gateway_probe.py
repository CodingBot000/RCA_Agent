"""Manual isolated-gateway comparison against a live synthetic Engine-host service."""

import json
import secrets
import subprocess
import uuid

from rca_agent.docker_probe import (
    BROKER,
    IMAGE,
    SERVER,
    classify_connection,
    docker,
    validate_inspect,
)

OPTION = "com.docker.network.bridge.gateway_mode_ipv4"


def validate_network(net, isolated):
    if not net["Internal"] or net["Driver"] != "bridge":
        raise ValueError("internal bridge required")
    if isolated and net.get("Options", {}).get(OPTION) != "isolated":
        raise ValueError("isolated option not applied")


def run_probe():
    prefix = "rca-gateway-" + uuid.uuid4().hex
    containers, networks = [], []
    port = 20000 + secrets.randbelow(30000)
    try:
        version = json.loads(docker("version", "--format", "{{json .Server}}"))["Version"]
        image = json.loads(docker("image", "inspect", IMAGE))[0]["Id"]

        def create(name, network, program, extra=(), alias=False):
            containers.append(name)
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
                "--memory",
                "128m",
                "--pids-limit",
                "64",
                "--cpus",
                "1",
                "--log-driver",
                "none",
            ]
            if alias:
                args += ["--network-alias", "mock"]
            if network != "host":
                args += ["--add-host", "host-probe:host-gateway"]
            docker(*args, image, "python", "-B", "-c", program, *extra)
            if network != "host":
                validate_inspect(json.loads(docker("inspect", name))[0], network)

        sentinel = prefix + "-sentinel"
        create(sentinel, "host", SERVER.replace("(8080, 8081)", f"({port},)"))
        docker("start", sentinel)
        # Verify that the receiving service really exists in the Engine host namespace.
        docker(
            "exec",
            sentinel,
            "python",
            "-c",
            f"""
import http.client, time
for i in range(30):
    try:
        c=http.client.HTTPConnection('127.0.0.1',{port},timeout=1)
        c.request('GET','/'); r=c.getresponse()
        assert r.status==200 and r.read()==b'synthetic-mock'
        break
    except OSError: time.sleep(.1)
else: raise SystemExit(1)
""",
        )
        host_gateway = json.loads(docker("network", "inspect", "bridge"))[0]["IPAM"]["Config"][0][
            "Gateway"
        ]
        docker(
            "exec",
            sentinel,
            "python",
            "-c",
            f"""
import http.client
c=http.client.HTTPConnection({host_gateway!r},{port},timeout=2)
c.request('GET','/'); r=c.getresponse()
assert r.status==200 and r.read()==b'synthetic-mock'
""",
        )
        results = {}
        previous_gateway = None
        for isolated in (False, True):
            label = "isolated" if isolated else "baseline"
            network = prefix + "-" + label
            args = ["network", "create", "--internal"]
            if isolated:
                args += ["--opt", OPTION + "=isolated"]
            docker(*args, network)
            networks.append(network)
            net = json.loads(docker("network", "inspect", network))[0]
            validate_network(net, isolated)
            ipv4 = next(c for c in net["IPAM"]["Config"] if ":" not in c["Subnet"])
            gateway = ipv4.get("Gateway") or previous_gateway
            if not gateway:
                raise ValueError("baseline gateway unavailable")
            if not isolated:
                previous_gateway = gateway
            config = {
                "files": {},
                "targets": [
                    ["bridge_gateway", gateway, port],
                    ["previous_gateway", previous_gateway, port],
                    ["host_gateway", "host-probe", port],
                    ["host_gateway_numeric", host_gateway, port],
                    ["host_desktop_dns", "host.docker.internal", port],
                    ["external_ipv4", "1.1.1.1", 443],
                    ["external_ipv6", "2606:4700:4700::1111", 443],
                    ["mapped_gateway", "::ffff:" + gateway, port],
                    ["disallowed_port", "mock", 8081],
                ],
            }
            mock, broker = prefix + "-mock-" + label, prefix + "-broker-" + label
            create(mock, network, SERVER, alias=True)
            create(broker, network, BROKER, [json.dumps(config)])
            docker("start", mock)
            result = json.loads(docker("start", "--attach", broker))
            if json.loads(docker("inspect", broker))[0]["State"]["ExitCode"] != 0:
                raise ValueError("probe failed")
            result["destinations"] = {
                k: classify_connection(v) for k, v in result["connections"].items()
            }
            result["ipv6_enabled"] = net["EnableIPv6"]
            result["gateway_configured"] = ipv4.get("Gateway")
            result["mode"] = net.get("Options", {}).get(OPTION, "default")
            results[label] = result
        baseline_live = results["baseline"]["connections"]["bridge_gateway"] == 0
        return {
            "engine": version,
            "image": image,
            "sentinel_live": True,
            "baseline_gateway_service_reachable": baseline_live,
            "results": results,
            "status": "BLOCKED",
        }
    finally:
        failures = []
        for name in reversed(containers):
            try:
                docker("rm", "--force", name)
            except (OSError, subprocess.SubprocessError):
                failures.append("container cleanup")
        for name in reversed(networks):
            try:
                docker("network", "rm", name)
            except (OSError, subprocess.SubprocessError):
                failures.append("network cleanup")
        if failures:
            raise RuntimeError("; ".join(failures))


def main():
    try:
        result = run_probe()
    except (OSError, ValueError, KeyError, StopIteration, RuntimeError, subprocess.SubprocessError):
        print(json.dumps({"status": "BLOCKED", "reason": "option/probe/cleanup failed"}))
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(2)  # Port restrictions remain explicitly out of scope.


if __name__ == "__main__":
    main()
