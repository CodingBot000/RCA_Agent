"""Manual credential-free Docker Unix-socket isolation experiment."""

import json
import subprocess
import tempfile
import uuid
from pathlib import Path

from rca_agent import uds_mock_worker
from rca_agent.docker_probe import IMAGE, SERVER, docker

CLIENT = r"""
import errno, json, os, socket, stat, sys, time
config=json.loads(sys.argv[1]); role=config['role']
checks={'nonroot':os.getuid()==65534, 'socket_absent': not os.path.exists('/channel/worker.sock')}
status=dict(line.split(':',1) for line in open('/proc/self/status') if ':' in line)
checks['capabilities_removed']=int(status['CapEff'].strip(),16)==0
checks['no_new_privileges']=status['NoNewPrivs'].strip()=='1'
checks['docker_socket_absent']=not os.path.exists('/var/run/docker.sock')
try: open('/root-probe','w'); checks['readonly']=False
except OSError as e: checks['readonly']=e.errno==errno.EROFS
files={}
for name,path in config['files'].items():
    try:
        with open(path,'rb'): pass
        files[name]='READABLE'
    except FileNotFoundError: files[name]='NOT_MOUNTED'
    except PermissionError: files[name]='DENIED'
checks['files_unavailable']=all(v in ('NOT_MOUNTED','DENIED') for v in files.values())
if role=='broker':
    for i in range(30):
        if os.path.exists('/channel/worker.sock'): break
        time.sleep(.1)
    st=os.stat('/channel/worker.sock')
    checks['socket_permission']=stat.S_IMODE(st.st_mode)==0o600 and st.st_uid==65534 and stat.S_ISSOCK(st.st_mode)
    checks['directory_permission']=stat.S_IMODE(os.stat('/channel').st_mode)==0o700
    def send(body, extra=b'', target=b'POST /mock HTTP/1.1'):
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
            s.settimeout(4); s.connect('/channel/worker.sock')
            s.sendall(target+b'\r\nContent-Length: '+str(len(body)).encode()+b'\r\n'+extra+b'\r\n'+body)
            data=b''
            while True:
                try: part=s.recv(4096)
                except ConnectionResetError: break
                if not part: break
                data+=part
            return int(data.split(b' ')[1]),data
    success,data=send(b'{"version":1,"probe":"synthetic"}')
    checks['uds_http']=success==200 and json.loads(data.split(b'\r\n\r\n')[1])=={'ok':True,'probe':'synthetic'}
    checks['invalid_json']=send(b'not-json')[0]==400
    checks['wrong_structure']=send(b'{"version":true,"probe":"synthetic"}')[0]==400
    checks['extra_fields']=send(b'{"version":1,"probe":"synthetic","secret":"x"}')[0]==400
    checks['large_body']=send(b'x'*2049)[0]==413
    checks['duplicate_json']=send(b'{"version":1,"version":1,"probe":"synthetic"}')[0]==400
    checks['wrong_target']=send(b'{}',target=b'POST http://example.invalid/ HTTP/1.1')[0]==400
    checks['transfer_encoding']=send(b'{}',extra=b'Transfer-Encoding: chunked\r\n')[0]==400
    del checks['socket_absent']
elif role=='agent':
    try:
        s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.connect('/channel/worker.sock')
        checks['uds_denied']=False
    except (FileNotFoundError,PermissionError): checks['uds_denied']=True
    finally: s.close()
connections={}
for name,host,port in config.get('targets',[]):
    try:
        with socket.create_connection((host,port),timeout=1): pass
        connections[name]='REACHABLE'
    except OSError as e:
        connections[name]=errno.errorcode.get(e.errno,'UNVERIFIED')
print(json.dumps({'checks':checks,'files':files,'connections':connections}))
"""


def validate_container(value, volume, role):
    host = value["HostConfig"]
    mounts = value["Mounts"]
    if (
        value["Config"]["User"] != "65534:65534"
        or host["NetworkMode"] != "none"
        or set(value["NetworkSettings"]["Networks"]) != {"none"}
        or not host["ReadonlyRootfs"]
        or host["CapDrop"] != ["ALL"]
        or host.get("CapAdd")
        or host.get("Privileged")
        or host.get("PortBindings")
        or "no-new-privileges" not in host["SecurityOpt"]
    ):
        raise ValueError("unsafe container")
    if role == "agent":
        if mounts:
            raise ValueError("agent mount forbidden")
    elif (
        len(mounts) != 1
        or mounts[0]["Type"] != "volume"
        or mounts[0]["Name"] != volume
        or mounts[0]["Destination"] != "/channel"
    ):
        raise ValueError("unexpected worker/broker mounts")


def run_probe():
    prefix = "rca-uds-" + uuid.uuid4().hex
    volume, network = prefix + "-volume", prefix + "-net"
    containers = []
    volumes = []
    networks = []
    try:
        image = json.loads(docker("image", "inspect", IMAGE))[0]["Id"]
        # tmpfs ownership is assigned by the local Docker volume driver, without a root helper.
        volumes.append(volume)
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
            "o=size=1m,uid=65534,gid=65534,mode=0700",
            volume,
        )
        networks.append(network)
        docker("network", "create", "--internal", network)
        net = json.loads(docker("network", "inspect", network))[0]
        if not net["Internal"]:
            raise ValueError("internal control network required")
        gateway = net["IPAM"]["Config"][0]["Gateway"]

        def create(name, program, args=(), role=None):
            containers.append(name)
            opts = [
                "create",
                "--name",
                name,
                "--network",
                "none" if role else network,
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
            if role in ("worker", "broker"):
                opts += ["--mount", f"type=volume,src={volume},dst=/channel"]
            docker(*opts, image, "python", "-B", "-c", program, *args)
            info = json.loads(docker("inspect", name))[0]
            if role:
                validate_container(info, volume, role)
            return info

        control = prefix + "-control"
        create(control, SERVER)
        docker("start", control)
        control_ip = json.loads(docker("inspect", control))[0]["NetworkSettings"]["Networks"][
            network
        ]["IPAddress"]
        docker(
            "exec",
            control,
            "python",
            "-c",
            """
import http.client,time
for port in (8080,8081):
    for attempt in range(30):
        try:
            c=http.client.HTTPConnection('127.0.0.1',port,timeout=1)
            c.request('GET','/'); r=c.getresponse()
            assert r.status==200 and r.read()==b'synthetic-mock'
            break
        except OSError: time.sleep(.1)
    else: raise SystemExit(1)
""",
        )
        worker = prefix + "-worker"
        create(worker, Path(uds_mock_worker.__file__).read_text(), role="worker")
        docker("start", worker)
        with tempfile.TemporaryDirectory(prefix="rca-uds-private-") as tmp:
            files = {}
            for name in ("blind", "answer", "mapping", "key"):
                path = Path(tmp) / name
                path.write_text("synthetic-only")
                files[name] = str(path)
            results = {}
            for role in ("broker", "agent"):
                name = prefix + "-" + role
                config = {
                    "role": role,
                    "files": files,
                    "targets": [
                        ["external_ipv4", "1.1.1.1", 443],
                        ["previous_gateway", "192.168.96.1", 8080],
                        ["internal_gateway", gateway, 8080],
                        ["other_container", control_ip, 8080],
                        ["disallowed_port", control_ip, 8081],
                        ["external_ipv6", "2606:4700:4700::1111", 443],
                    ],
                }
                create(name, CLIENT, [json.dumps(config)], role)
                results[role] = json.loads(docker("start", "--attach", name))
                if json.loads(docker("inspect", name))[0]["State"]["ExitCode"] != 0:
                    raise ValueError("client failed")
            # Inspect worker files/settings without mounting any project source or private data.
            config = {"role": "worker", "files": files, "targets": []}
            results["worker"] = json.loads(
                docker("exec", worker, "python", "-B", "-c", CLIENT, json.dumps(config))
            )
            results["worker"]["checks"].pop("socket_absent")
            passed = all(
                all(r["checks"].values())
                and all(
                    code in ("ENETUNREACH", "EHOSTUNREACH", "EACCES", "EPERM")
                    for code in r["connections"].values()
                )
                for r in results.values()
            )
            return {
                "status": "PASS" if passed else "BLOCKED",
                "scope": "synthetic UDS probe only",
                "results": results,
            }
    finally:
        failures = []
        for name in reversed(containers):
            try:
                docker("rm", "--force", name)
            except (OSError, subprocess.SubprocessError):
                failures.append("container cleanup")
        for name in volumes:
            try:
                docker("volume", "rm", name)
            except (OSError, subprocess.SubprocessError):
                failures.append("volume cleanup")
        for name in networks:
            try:
                docker("network", "rm", name)
            except (OSError, subprocess.SubprocessError):
                failures.append("network cleanup")
        if failures:
            raise RuntimeError("; ".join(failures))


def main():
    try:
        result = run_probe()
    except (ValueError, OSError, KeyError, IndexError, RuntimeError, subprocess.SubprocessError):
        print(json.dumps({"status": "BLOCKED", "reason": "probe or cleanup failed"}))
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True))
    if result["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
