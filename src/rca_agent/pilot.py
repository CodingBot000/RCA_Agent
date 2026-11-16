"""Development-only single-case pilot planning. Paid execution is always disabled."""

import argparse
import hashlib
import json
import subprocess
import uuid
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from rca_agent.contracts import RCAInput
from rca_agent.egress_policy import live_client
from rca_agent.llm_reasoner import MODEL_INSTRUCTION
from rca_agent.model_broker import Budget
from rca_agent.phase1 import CASE_ID_PATTERN

VERIFIED_ON = "2026-11-16"
CATALOG = {
    "gpt-6-luna": {"input": Decimal("0.50") / 1000000, "output": Decimal("0.75") / 1000000},
    "gpt-6.1-terra": {"input": Decimal("5.00") / 1000000, "output": Decimal("7.50") / 1000000},
}
PRICE_PROFILE = "standard-global-conservative"
EVALUATION_ROOT = (
    Path(__file__).resolve().parents[2] / "phase1-data/official-blind-private-v2/cases"
)


class PilotError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise PilotError("INVALID_CONFIGURATION")


def development_case(root, case, manifest, evaluation_root=EVALUATION_ROOT):
    root, case, manifest = Path(root), Path(case), Path(manifest)
    if case.is_symlink() or manifest.is_symlink():
        raise PilotError("CASE_SPLIT_BLOCKED")
    root = root.resolve()
    case = case.resolve()
    protected = evaluation_root.resolve()
    if (
        not evaluation_root.is_dir()
        or case.is_relative_to(protected)
        or not case.is_relative_to(root)
        or root == case
    ):
        raise PilotError("CASE_SPLIT_BLOCKED")
    declared = json.loads(manifest.read_text())
    if (
        set(declared) != {"split", "case_id", "directory"}
        or declared["split"] != "development"
        or not isinstance(declared["case_id"], str)
        or not CASE_ID_PATTERN.fullmatch(declared["case_id"])
        or (root / declared["directory"]).resolve() != case
    ):
        raise PilotError("CASE_SPLIT_BLOCKED")
    excluded = {p.name for p in evaluation_root.iterdir() if p.is_dir()}
    if declared["case_id"] in excluded:
        raise PilotError("CASE_SPLIT_BLOCKED")
    task_path = case / "task.json"
    if task_path.is_symlink() or not task_path.is_file() or task_path.stat().st_nlink != 1:
        raise PilotError("CASE_SPLIT_BLOCKED")
    task = RCAInput.parse(json.loads(task_path.read_text()))
    if task.case_id != declared["case_id"]:
        raise PilotError("CASE_SPLIT_BLOCKED")
    metrics = case / "metrics.parquet"
    if not metrics.exists():
        metrics = case / "metrics.csv"
    if metrics.is_symlink() or not metrics.is_file() or metrics.stat().st_nlink != 1:
        raise PilotError("CASE_SPLIT_BLOCKED")
    return task


def plan(model, budget, profile):
    if model not in CATALOG or profile != PRICE_PROFILE:
        raise PilotError("MODEL_OR_PRICE_UNVERIFIED")
    if (
        budget.max_concurrency != 1
        or budget.max_retries != 0
        or budget.output_tokens_per_call > 128000
        or budget.max_input_tokens > 922000 * budget.max_calls
    ):
        raise PilotError("INVALID_CONFIGURATION")
    prices = CATALOG[model]
    # Maximize model token cost within cumulative constraints. Output has the higher rate.
    output = min(
        budget.max_output_tokens,
        budget.output_tokens_per_call * budget.max_calls,
        budget.max_total_tokens,
    )
    inputs = min(budget.max_input_tokens, max(0, budget.max_total_tokens - output))
    cost = prices["input"] * inputs + prices["output"] * output
    if cost > budget.max_cost_usd:
        raise PilotError("BUDGET_REJECTED")
    settings = {
        **asdict(budget),
        "max_cost_usd": str(budget.max_cost_usd),
        "model": model,
        "price_profile": profile,
    }
    return {
        "model": model,
        "settings": settings,
        "settings_version": hashlib.sha256(
            json.dumps(settings, sort_keys=True).encode()
        ).hexdigest(),
        "pricing_verified_on": VERIFIED_ON,
        "estimated_max_model_cost_usd": format(cost, "f"),
        "max_model_requests": budget.max_calls,
        "max_count_requests": budget.max_calls,
        "max_http_requests": 2 * budget.max_calls,
        "other_charges": "UNVERIFIED",
        "cost_estimate_scope": "declared Standard global model token rates; not a billing guarantee",
    }


def report(plan_data, status, failure=None, experiment=None):
    if experiment is None:
        experiment = uuid.uuid4().hex
    if len(experiment) != 32 or any(c not in "0123456789abcdef" for c in experiment):
        raise PilotError("INVALID_CONFIGURATION")
    version = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    if len(version) != 40 or any(c not in "0123456789abcdef" for c in version):
        version = "unversioned"
    from rca_agent.docker_integration import source_hash

    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=False
        ).stdout.strip()
    )
    return {
        "schema_version": "pilot-report-1",
        "run_id": uuid.uuid4().hex,
        "experiment_id": experiment,
        "split": "development",
        "status": status,
        "analysis_success": None,
        "failure_type": failure,
        "harness_version": version,
        "harness_source_version": source_hash(),
        "harness_dirty": dirty,
        "prompt_version": hashlib.sha256(MODEL_INSTRUCTION.encode()).hexdigest(),
        "plan": plan_data,
        "tool_calls": None,
        "iterations": None,
        "input_tokens": None,
        "output_tokens": None,
        "actual_cost_usd": None,
        "evidence_valid": None,
        "official_scoring": {
            "status": "NOT_RUN",
            "top1": None,
            "top3": None,
            "entrypoint": "rca_agent.rcaeval_compat.score_results",
            "evaluator_only": True,
        },
        "live": "LIVE_BLOCKED",
    }


def compare(before, after):
    if (
        before["schema_version"] != "pilot-report-1"
        or after["schema_version"] != "pilot-report-1"
        or before["split"] != "development"
        or after["split"] != "development"
        or before["experiment_id"] != after["experiment_id"]
    ):
        raise PilotError("INCOMPARABLE_REPORTS")
    result = {
        "before_harness": before["harness_version"],
        "after_harness": after["harness_version"],
        "prompt_changed": before["prompt_version"] != after["prompt_version"],
        "source_changed": before["harness_source_version"] != after["harness_source_version"],
        "settings_changed": (before.get("plan") or {}).get("settings_version")
        != (after.get("plan") or {}).get("settings_version"),
        "before_status": before["status"],
        "after_status": after["status"],
        "deltas": {},
    }
    for key in ("tool_calls", "iterations", "input_tokens", "output_tokens", "actual_cost_usd"):
        a, b = before[key], after[key]
        result["deltas"][key] = (
            None if a is None or b is None else str(Decimal(str(b)) - Decimal(str(a)))
        )
    return result


def parser():
    p = SafeParser()
    p.add_argument("--development-root", type=Path, required=True)
    p.add_argument("--case-directory", type=Path, required=True)
    p.add_argument("--development-manifest", type=Path, required=True)
    p.add_argument("--model", choices=sorted(CATALOG), required=True)
    p.add_argument("--price-profile", choices=[PRICE_PROFILE], required=True)
    for name in (
        "max-calls",
        "max-tool-calls",
        "max-iterations",
        "max-input-tokens",
        "max-output-tokens",
        "max-total-tokens",
        "output-tokens-per-call",
        "max-concurrency",
        "max-retries",
    ):
        p.add_argument("--" + name, type=int, required=True)
    p.add_argument("--max-cost-usd", type=Decimal, required=True)
    p.add_argument("--timeout-seconds", type=float, required=True)
    p.add_argument("--experiment-id")
    p.add_argument("--live", action="store_true")
    return p


def main():
    plan_data = None
    args = None
    try:
        args = parser().parse_args()
        values = {key: getattr(args, key) for key in Budget.__dataclass_fields__}
        b = Budget(**values)
        plan_data = plan(args.model, b, args.price_profile)
        development_case(args.development_root, args.case_directory, args.development_manifest)
        if args.live:
            try:
                live_client()
                raise PilotError("LIVE_BLOCKED")
            except RuntimeError:
                raise PilotError("LIVE_BLOCKED") from None
        print(
            json.dumps(
                report(plan_data, "DRY_RUN_VALIDATED", experiment=args.experiment_id),
                sort_keys=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 - no private paths or argument values in public errors
        code = exc.code if isinstance(exc, PilotError) else "INVALID_CONFIGURATION"
        experiment = getattr(args, "experiment_id", None)
        if experiment and (
            len(experiment) != 32 or any(c not in "0123456789abcdef" for c in experiment)
        ):
            experiment = None
        print(
            json.dumps(
                report(plan_data, "FAILED", failure=code, experiment=experiment), sort_keys=True
            )
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
