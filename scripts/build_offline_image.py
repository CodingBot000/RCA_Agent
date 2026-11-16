"""Explicit manual dependency build; copies only locked requirements and Python source."""

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_hash():
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src/rca_agent").glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main():
    with tempfile.TemporaryDirectory(prefix="rca-build-") as tmp:
        context = Path(tmp)
        subprocess.run(
            [
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--extra",
                "phase1",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "--output-file",
                str(context / "requirements.txt"),
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        shutil.copy(ROOT / "docker/Dockerfile.offline", context / "Dockerfile")
        shutil.copytree(
            ROOT / "src/rca_agent",
            context / "rca_agent",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        subprocess.run(
            [
                "docker",
                "build",
                "--pull=false",
                "--label",
                "org.rca.source=" + source_hash(),
                "-t",
                "rca-offline:phase4b3b",
                str(context),
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
