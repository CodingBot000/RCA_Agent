import json
from decimal import Decimal

import pytest

from rca_agent import pilot
from rca_agent.docker_integration import stage_blind
from rca_agent.model_broker import Budget


def budget(**changes):
    data = {
        "max_calls": 3,
        "max_tool_calls": 1,
        "max_iterations": 1,
        "max_input_tokens": 100,
        "max_output_tokens": 3000,
        "max_total_tokens": 4000,
        "max_cost_usd": Decimal(1),
        "max_concurrency": 1,
        "max_retries": 0,
        "output_tokens_per_call": 1000,
        "timeout_seconds": 3,
    }
    return Budget(**{**data, **changes})


def development(tmp_path):
    root = tmp_path / "development"
    case_id, _ = stage_blind(root)
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps({"split": "development", "case_id": case_id, "directory": case_id})
    )
    evaluation = tmp_path / "evaluation"
    evaluation.mkdir()
    (evaluation / ("re2-ob-v2-" + "0" * 32)).mkdir()
    return root, root / case_id, manifest, evaluation


def test_plan_costs_and_requests_are_explicit():
    result = pilot.plan("gpt-6-luna", budget(), pilot.PRICE_PROFILE)
    assert Decimal(result["estimated_max_model_cost_usd"]) == Decimal(".00230")
    assert result["max_http_requests"] == 6 and result["other_charges"] == "UNVERIFIED"
    with pytest.raises(pilot.PilotError, match="BUDGET_REJECTED"):
        pilot.plan("gpt-6-luna", budget(max_cost_usd=Decimal(".00001")), pilot.PRICE_PROFILE)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_concurrency": 2},
        {"max_retries": 1},
        {"output_tokens_per_call": 128001, "max_output_tokens": 128001},
    ],
)
def test_unapproved_execution_profiles_rejected(changes):
    with pytest.raises(pilot.PilotError):
        pilot.plan("gpt-6-luna", budget(**changes), pilot.PRICE_PROFILE)


def test_development_and_evaluation_split(tmp_path):
    root, case, manifest, evaluation = development(tmp_path)
    assert pilot.development_case(root, case, manifest, evaluation).case_id == case.name
    (evaluation / case.name).mkdir()
    with pytest.raises(pilot.PilotError, match="CASE_SPLIT_BLOCKED"):
        pilot.development_case(root, case, manifest, evaluation)


def test_missing_evaluation_registry_fails_closed(tmp_path):
    root, case, manifest, _ = development(tmp_path)
    with pytest.raises(pilot.PilotError):
        pilot.development_case(root, case, manifest, tmp_path / "missing")


def test_cli_dry_run_and_live_blocked(tmp_path, monkeypatch, capsys):
    import sys

    root, case, manifest, evaluation = development(tmp_path)
    validate = pilot.development_case
    monkeypatch.setattr(
        pilot, "development_case", lambda *args: validate(*args, evaluation_root=evaluation)
    )
    argv = [
        "pilot",
        "--development-root",
        str(root),
        "--case-directory",
        str(case),
        "--development-manifest",
        str(manifest),
        "--model",
        "gpt-6-luna",
        "--price-profile",
        pilot.PRICE_PROFILE,
    ]
    for key, value in budget().__dict__.items():
        argv += ["--" + key.replace("_", "-"), str(value)]
    monkeypatch.setattr(sys, "argv", argv)
    pilot.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "DRY_RUN_VALIDATED" and result["analysis_success"] is None
    assert result["actual_cost_usd"] is None and result["official_scoring"]["top1"] is None
    assert case.name not in json.dumps(result) and str(case) not in json.dumps(result)
    monkeypatch.setattr(sys, "argv", argv + ["--live"])
    with pytest.raises(SystemExit):
        pilot.main()
    result = json.loads(capsys.readouterr().out)
    assert result["failure_type"] == "LIVE_BLOCKED" and result["analysis_success"] is None


def test_argument_errors_never_echo_secrets(monkeypatch, capsys):
    import sys

    monkeypatch.setattr(sys, "argv", ["pilot", "--api-key", "secret-canary"])
    with pytest.raises(SystemExit):
        pilot.main()
    assert "secret-canary" not in capsys.readouterr().out


def test_report_regression_comparison_is_null_without_measurements():
    plan = pilot.plan("gpt-6-luna", budget(), pilot.PRICE_PROFILE)
    before = pilot.report(plan, "DRY_RUN_VALIDATED", experiment="a" * 32)
    after = pilot.report(plan, "DRY_RUN_VALIDATED", experiment="a" * 32)
    result = pilot.compare(before, after)
    assert all(value is None for value in result["deltas"].values())
    after["experiment_id"] = "b" * 32
    with pytest.raises(pilot.PilotError):
        pilot.compare(before, after)


def test_report_records_dirty_source_and_compares_preflight_failures():
    before = pilot.report(None, "FAILED", failure="INVALID_CONFIGURATION", experiment="a" * 32)
    after = pilot.report(None, "FAILED", failure="CASE_SPLIT_BLOCKED", experiment="a" * 32)
    assert len(before["harness_source_version"]) == 64
    assert type(before["harness_dirty"]) is bool
    assert pilot.compare(before, after)["settings_changed"] is False
    after["harness_source_version"] = "c" * 64
    assert pilot.compare(before, after)["source_changed"] is True
