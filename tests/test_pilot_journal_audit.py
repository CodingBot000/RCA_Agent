import importlib.util
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "pilot_attempt_audit", ROOT / "scripts/audit_pilot_attempt.py"
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def absent_row():
    return {
        "local_unix": True,
        "reachable": True,
        "recorded_volume_present": False,
        "managed_journal_name_count": 0,
        "journal": None,
    }


@pytest.fixture
def records(tmp_path):
    base = tmp_path / "pilot-private-results"
    base.mkdir(mode=0o700)
    directory = tmp_path / audit.STORAGE
    directory.mkdir(mode=0o700)
    attempt = {
        "state": "CLAIMED_MANUAL_REVIEW_UNTIL_SETTLED",
        "binding": "a" * 64,
        "created": 1000,
        "runtime": {
            "journal_volume": "rca-pilot-journal-synthetic-private",
            "private_output": "PRIVATE_PATH_DO_NOT_PRINT",
            "dns_lease": 90,
            "dns_refresh": 20,
            "dns_timeout": 5,
            "family_policy": "ipv4-only",
            "http_timeout": 20,
            "session_timeout": 300,
        },
    }
    outcome = {
        "status": "FAILED",
        "failure_type": "INVALID_CONFIGURATION",
        "billing_status": "UNRECONCILED",
        "actual_cost_usd": None,
    }
    for name, value in (("attempt.json", attempt), ("outcome.json", outcome)):
        path = directory / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    for name in (
        "configs/pilot-preflight-v0.8.json",
        "development-data/pilot-v1/cases/manifest.json",
        "development-data/pilot-v1/private/provenance.json",
        "uv.lock",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic metadata")
    return tmp_path, attempt, outcome


def test_binding_matches_only_original_snapshot_and_image_order(records):
    root, attempt, _ = records
    images = ["sha256:" + "1" * 64, "sha256:" + "2" * 64]
    snapshot = {
        "commit": audit.HISTORICAL_COMMIT,
        "source": audit.HISTORICAL_SOURCE,
        "config": audit.sha((root / "configs/pilot-preflight-v0.8.json").read_bytes()),
        "manifest": audit.sha(
            (root / "development-data/pilot-v1/cases/manifest.json").read_bytes()
        ),
        "provenance": audit.sha(
            (root / "development-data/pilot-v1/private/provenance.json").read_bytes()
        ),
        "lock": audit.sha((root / "uv.lock").read_bytes()),
        "images": images,
    }
    attempt["binding"] = audit.sha(json.dumps(snapshot, sort_keys=True).encode())
    assert audit.binding_matches(root, attempt, images, audit.HISTORICAL_SOURCE) == [tuple(images)]
    assert not audit.binding_matches(root, attempt, images[:1], audit.HISTORICAL_SOURCE)
    (root / "configs/pilot-preflight-v0.8.json").write_text("changed settings")
    assert not audit.binding_matches(root, attempt, images, audit.HISTORICAL_SOURCE)


def test_exception_boundary_is_checked_in_code_not_assumed():
    source = (ROOT / "src/rca_agent/pilot.py").read_text()
    assert audit.predispatch_barrier(source)
    changed = source.replace(
        'raise PilotError("MANUAL_REVIEW_REQUIRED") from None',
        'raise PilotError("INVALID_CONFIGURATION") from None',
    )
    assert not audit.predispatch_barrier(changed)


@pytest.mark.parametrize(
    "missing",
    [
        "binding",
        "barrier",
        "outcome",
        "context",
        "remote",
        "other_journal",
        "SENT",
        "COMPLETED",
        "MANUAL_REVIEW",
    ],
)
def test_incomplete_or_conflicting_evidence_never_authorizes_recovery(records, missing):
    _, _, outcome = records
    row = absent_row()
    if missing == "outcome":
        outcome = {**outcome, "failure_type": "MANUAL_REVIEW_REQUIRED"}
    elif missing == "context":
        row["reachable"] = False
    elif missing == "remote":
        row["local_unix"] = False
    elif missing == "other_journal":
        row["managed_journal_name_count"] = 1
    elif missing in {"SENT", "COMPLETED", "MANUAL_REVIEW"}:
        row["recorded_volume_present"] = True
        row["journal"] = {"status": "READ_ONLY", "counts": {missing: 1}}
    result = audit.classify(outcome, missing != "binding", missing != "barrier", [row])
    assert result["manual_recovery_plan_eligible"] is False
    assert result["execution_state"] == "LIVE_BLOCKED" and result["recovery_authorized"] is False
    assert result["billing_status"] == "UNRECONCILED"
    assert result["journal_counts"] is None


def test_sufficient_local_evidence_proposes_only_manual_plan_not_billing_settlement(records):
    result = audit.classify(records[2], True, True, [absent_row()])
    assert result["manual_recovery_plan_eligible"] is True
    assert result["api_http_dispatch_on_bound_codepath"] == "NOT_REACHED"
    assert result["parent_key_acquisition"].startswith("REACHED")
    assert result["billing_status"] == "UNRECONCILED" and not result["provider_usage_queried"]
    assert not result["recovery_authorized"] and result["execution_state"] == "LIVE_BLOCKED"


def test_usage_fields_conflict_with_predispatch_outcome(records):
    value = {**records[2], "input_tokens": 10}
    report = audit.classify(value, True, True, [absent_row()])
    assert (
        report["recorded_codepath"] == "NOT_PROVEN" and not report["manual_recovery_plan_eligible"]
    )


@pytest.mark.parametrize("change", ["mode", "symlink"])
def test_private_record_safety_is_required_without_repairs(records, change):
    root, _, _ = records
    path = root / audit.STORAGE / "attempt.json"
    if change == "mode":
        path.chmod(0o644)
    else:
        target = path.with_name("original.json")
        path.rename(target)
        path.symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        audit.protected_records(root)


def test_cli_errors_do_not_disclose_private_identifiers(monkeypatch, capsys):
    monkeypatch.setattr(__import__("sys"), "argv", ["audit"])
    monkeypatch.setattr(
        audit,
        "investigate",
        lambda: (_ for _ in ()).throw(ValueError("PRIVATE_IDENTIFIER_MUST_NOT_PRINT")),
    )
    with pytest.raises(SystemExit):
        audit.main()
    captured = capsys.readouterr()
    assert "PRIVATE_IDENTIFIER_MUST_NOT_PRINT" not in captured.out + captured.err
    assert json.loads(captured.out)["execution_state"] == "LIVE_BLOCKED"


def test_readonly_investigation_receipt_preserves_original_records_and_hides_private_fields(
    records, monkeypatch
):
    root, attempt, _ = records
    before = {p.name: p.read_bytes() for p in (root / audit.STORAGE).iterdir()}
    monkeypatch.setattr(
        audit, "historical_source", lambda root: (audit.HISTORICAL_SOURCE, "synthetic")
    )
    monkeypatch.setattr(audit, "predispatch_barrier", lambda source: True)
    monkeypatch.setattr(audit, "docker_inventory", lambda volume: ([absent_row()], 1, set()))
    monkeypatch.setattr(audit, "image_candidates", lambda: set())
    monkeypatch.setattr(audit, "binding_matches", lambda *args: [("image-a", "image-b")])
    monkeypatch.setattr(
        audit, "command", lambda args, root: b"a" * 40 if "rev-parse" in args else b""
    )
    report, receipt = audit.investigate(root)
    audit.write_receipt(root, receipt)
    assert before == {p.name: p.read_bytes() for p in (root / audit.STORAGE).iterdir()}
    assert attempt["runtime"]["journal_volume"] not in json.dumps(report)
    assert "private_record_sha256" not in report and report["original_records_unchanged"]
    files = list((root / "pilot-private-results/journal-audits").glob("*.json"))
    assert len(files) == 1 and files[0].stat().st_mode & 0o777 == 0o600
    assert json.loads(files[0].read_text())["recovery_approval"] is False


def test_missing_volume_never_causes_implicit_docker_volume_creation(monkeypatch):
    calls = []

    def fake(args, root=audit.ROOT):
        calls.append(args)
        if args[1:3] == ["context", "ls"]:
            return b"desktop-linux\n"
        if args[1:3] == ["context", "inspect"]:
            return b'[{"Name":"desktop-linux","Endpoints":{"docker":{"Host":"unix:///local"}}}]'
        if "info" in args:
            return b"synthetic-engine"
        return b""

    monkeypatch.setattr(audit, "command", fake)
    monkeypatch.setattr(
        audit, "inspect_journal", lambda *args: pytest.fail("must not mount missing volume")
    )
    rows, engines, _ = audit.docker_inventory("rca-pilot-journal-missing")
    assert rows[0]["recorded_volume_present"] is False and engines == 1
    assert not any("run" in args or "create" in args or "rm" in args for args in calls)


@pytest.mark.parametrize("stage", ["pre_dispatch", "deployment", "simulated_dispatch"])
def test_cli_timeout_failure_boundary_without_any_api_or_key_read(
    tmp_path, monkeypatch, capsys, stage
):
    from rca_agent import approval as approvals
    from rca_agent import docker_integration as integration
    from rca_agent import live_deployment as deployment
    from rca_agent import pilot
    from rca_agent.model_broker import Budget

    tmp_path.chmod(0o700)
    cfg = json.loads((ROOT / "configs/pilot-preflight-v0.8.json").read_text())
    b = Budget(
        **{
            **cfg["case_and_total_budget"],
            "max_cost_usd": Decimal("0.05"),
            "timeout_seconds": 30 if stage == "pre_dispatch" else 30.0,
        }
    )
    real_plan = pilot.plan

    def old_plan(model, budget, profile):
        p = real_plan(model, budget, profile)
        settings = {
            **asdict(budget),
            "max_cost_usd": str(budget.max_cost_usd),
            "model": model,
            "price_profile": profile,
        }
        return {
            **p,
            "settings": settings,
            "settings_version": audit.sha(json.dumps(settings, sort_keys=True).encode()),
        }

    parent = old_plan(cfg["model"], b, cfg["price_profile"])
    proof = tmp_path / "proof.json"
    proof.write_text("synthetic-proof")
    proof.chmod(0o600)
    controls = {
        **cfg["runtime"],
        "journal_volume": "rca-pilot-journal-fixture",
        "private_output": str(tmp_path / "private"),
    }
    value = {
        **dict.fromkeys(
            (
                "approved",
                "allow_api_key_use",
                "allow_provider_data_processing",
                "allow_unverified_api_contract",
                "retention_approved",
            ),
            True,
        ),
        "retention": cfg["retention"],
        "settings_version": parent["settings_version"],
        "source_version": "a" * 64,
        "provenance_digest": audit.sha(proof.read_bytes()),
        "expires": __import__("time").time() + 3600,
        "runtime": controls,
        "total_budget": {**asdict(b), "max_cost_usd": str(b.max_cost_usd)},
        "observation_limits": cfg["observation_limits"],
    }
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(value))
    approval.chmod(0o600)
    argv = [
        "pilot",
        "--development-root",
        str(tmp_path),
        "--case-directory",
        str(tmp_path / "case"),
        "--development-manifest",
        str(tmp_path / "manifest"),
        "--provenance-file",
        str(proof),
        "--evaluation-mapping",
        str(tmp_path / "mapping"),
        "--model",
        cfg["model"],
        "--price-profile",
        cfg["price_profile"],
        "--approval-file",
        str(approval),
        "--credential-fd",
        "7",
        "--journal-volume",
        controls["journal_volume"],
        "--private-output",
        controls["private_output"],
        "--dns-lease-seconds",
        "90",
        "--dns-refresh-seconds",
        "20",
        "--dns-timeout-seconds",
        "5",
        "--http-timeout-seconds",
        "20",
        "--session-timeout-seconds",
        "300",
        "--address-family-policy",
        "ipv4-only",
        "--live",
    ]
    for k, v in asdict(b).items():
        argv.extend(["--" + k.replace("_", "-"), str(v)])
    events = []
    real_approval = approvals.approved_profile

    def validate(*args, **kwargs):
        events.append("approval")
        return real_approval(*args, **kwargs)

    class FakeDeployment:
        def __init__(self, *args, **kwargs):
            events.append("deployment")
            if stage == "deployment":
                raise RuntimeError("SYNTHETIC_ONLY")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def simulated(*args, **kwargs):
        events.append("simulated_dispatch")
        raise TimeoutError("SYNTHETIC_ONLY")

    monkeypatch.setattr(pilot, "plan", old_plan)
    monkeypatch.setattr(
        pilot, "development_case", lambda *a, **kw: SimpleNamespace(case_id="synthetic")
    )
    monkeypatch.setattr(pilot, "__name__", "pilot_test")
    monkeypatch.setattr(integration, "source_hash", lambda: "a" * 64)
    monkeypatch.setattr(approvals, "approved_profile", validate)
    monkeypatch.setattr(deployment, "LiveDeployment", FakeDeployment)
    monkeypatch.setattr(deployment, "read_credential_fd", lambda *args: pytest.fail("key FD read"))
    monkeypatch.setattr(integration, "run_session", simulated)
    monkeypatch.setattr(__import__("sys"), "argv", argv)
    with pytest.raises(SystemExit):
        pilot.main()
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "FAILED"
    assert output["failure_type"] == (
        "INVALID_CONFIGURATION" if stage == "pre_dispatch" else "MANUAL_REVIEW_REQUIRED"
    )
    assert (
        events
        == {
            "pre_dispatch": ["approval"],
            "deployment": ["approval", "deployment"],
            "simulated_dispatch": ["approval", "deployment", "simulated_dispatch"],
        }[stage]
    )
