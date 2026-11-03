"""Limited preflight checks, not an OS security boundary."""

from pathlib import Path
from typing import Any

from rca_agent.contracts import ContractError

FORBIDDEN_KEYS = frozenset({"answer", "answers", "ground_truth", "root_cause", "label"})


def reject_leakage(value: Any, secrets: tuple[str, ...] = ()) -> None:
    if any(not isinstance(secret, str) or not secret for secret in secrets):
        raise ValueError("secrets must be nonempty strings")

    def scan(item: Any) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise ContractError("non-string key")
                if key.casefold() in FORBIDDEN_KEYS:
                    raise ContractError("answer field in agent input")
                scan(key)
                scan(nested)
        elif isinstance(item, list):
            for nested in item:
                scan(nested)
        elif isinstance(item, str) and any(secret in item for secret in secrets):
            raise ContractError("answer marker in agent input")

    scan(value)


def check_task_tree(task_dir: Path) -> None:
    if task_dir.is_symlink() or not task_dir.is_dir():
        raise ContractError("task directory must be a real directory")
    for path in task_dir.rglob("*"):
        if path.is_symlink():
            raise ContractError("symlink in task directory")
        if path.name.casefold().endswith(".answers.jsonl"):
            raise ContractError("answers file in task directory")
