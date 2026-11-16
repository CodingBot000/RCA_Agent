"""Synthetic Docker Agent baseline probe; this is not the complete 4B-3B-1 harness."""

import subprocess
import tempfile
import uuid
from pathlib import Path

CONTAINER_PROBE = r"""
import os, socket
assert os.getuid() == 65534
assert open('/blind/fixture.txt', 'rb').read() == b'synthetic-blind'
assert not os.path.exists('/var/run/docker.sock')
assert not os.path.exists('/private/answer.json')
assert not os.path.exists('/private/api-key')
try:
    open('/blind/fixture.txt', 'wb')
except OSError:
    pass
else:
    raise SystemExit('blind write allowed')
try:
    open('/root-probe', 'wb')
except OSError:
    pass
else:
    raise SystemExit('root write allowed')
s = socket.socket()
s.settimeout(2)
assert s.connect_ex(('192.0.2.1', 443)) != 0
s.close()
print('PASS synthetic agent baseline')
"""


def main():
    with tempfile.TemporaryDirectory(prefix="rca-agent-probe-") as tmp:
        blind = Path(tmp) / "blind"
        blind.mkdir(mode=0o755)
        (blind / "fixture.txt").write_bytes(b"synthetic-blind")
        (blind / "fixture.txt").chmod(0o644)
        name = "rca-agent-probe-" + uuid.uuid4().hex
        try:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--name",
                    name,
                    "--pull=never",
                    "--network=none",
                    "--read-only",
                    "--user=65534:65534",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--mount",
                    f"type=bind,src={blind},dst=/blind,readonly",
                    "python:3.12.13-slim",
                    "python",
                    "-c",
                    CONTAINER_PROBE,
                ],
                check=True,
                timeout=30,
            )
        finally:
            subprocess.run(
                ["docker", "rm", "--force", name], check=True, timeout=30, stdout=subprocess.DEVNULL
            )


if __name__ == "__main__":
    main()
