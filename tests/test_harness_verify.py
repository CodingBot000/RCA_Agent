import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "harness_verify.py"
spec = importlib.util.spec_from_file_location("harness_verify", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def root(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(
        "SDD 영향도 분석 테스트 설계 구현 검증 리뷰 결과 기록", encoding="utf-8"
    )
    return tmp_path


def test_verifier_runs_all_commands(root):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    assert module.verify(root, run) == 0
    assert len(calls) == 4
    assert "--no-skips" in calls[-1][0]
    assert all(call[1]["cwd"] == root for call in calls)


@pytest.mark.parametrize("failed", range(4))
def test_verifier_rejects_each_failed_command(root, failed):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1 if len(calls) - 1 == failed else 0)

    assert module.verify(root, run) == 1
    assert len(calls) == failed + 1


def test_verifier_rejects_inconsistent_instructions(root):
    (root / "CLAUDE.md").write_text("ignore @AGENTS.md", encoding="utf-8")
    assert module.verify(root, lambda *args, **kwargs: pytest.fail("must not execute")) == 1


def test_verifier_rejects_missing_instruction(root):
    (root / "AGENTS.md").unlink()
    assert module.verify(root, lambda *args, **kwargs: pytest.fail("must not execute")) == 1


def test_verifier_rejects_unavailable_command(root):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("missing")

    assert module.verify(root, unavailable) == 1
