"""Run required local harness checks and fail closed on any failed command."""

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_instructions(root: Path) -> None:
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    claude = (root / "CLAUDE.md").read_text(encoding="utf-8").strip()
    if claude != "@AGENTS.md":
        raise ValueError("CLAUDE.md must import only the shared AGENTS.md")
    required = ("SDD", "영향도 분석", "테스트 설계", "구현", "검증", "리뷰", "결과 기록")
    positions = [agents.find(item) for item in required]
    if -1 in positions or positions != sorted(positions):
        raise ValueError("AGENTS.md must contain the required workflow in order")


def verify(root: Path, run: Callable = subprocess.run) -> int:
    try:
        check_instructions(root)
    except (OSError, ValueError) as exc:
        print(f"FAIL instructions: {exc}\nHARNESS_NOT_READY", flush=True)
        return 1
    print("PASS instruction consistency", flush=True)
    commands = [
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        [sys.executable, "scripts/sync_skills.py"],
        [sys.executable, "-m", "pytest", "--no-skips", "-q"],
    ]
    for command in commands:
        print("RUN " + " ".join(command), flush=True)
        try:
            result = run(command, cwd=root, check=False)
        except OSError as exc:
            print(f"FAIL unable to execute: {exc}\nHARNESS_NOT_READY", flush=True)
            return 1
        if result.returncode != 0:
            print(f"FAIL command returned {result.returncode}\nHARNESS_NOT_READY", flush=True)
            return 1
    print(
        "PASS local harness checks; overall decision DEFERRED pending external checks", flush=True
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(verify(ROOT))
