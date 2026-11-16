import json
import os
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rca_agent import local_pilot as local
from rca_agent.phase1 import REVISION


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    root = tmp_path
    cfg = json.loads((local.ROOT / "configs/pilot-preflight-v0.8.json").read_text())
    (root / "configs").mkdir()
    (root / "configs/pilot-preflight-v0.8.json").write_text(json.dumps(cfg))
    (root / "uv.lock").write_text("locked-fixture")
    case_id = "re2-ob-v2-" + "f" * 32
    case = root / "development-data/pilot-v1/cases" / case_id
    case.mkdir(parents=True, mode=0o700)
    task = {
        "case_id": case_id,
        "candidates": ["api_cpu"],
        "evidence": [{"id": "ctx", "content": "development only"}],
    }
    (case / "task.json").write_text(json.dumps(task))
    for signal in ("metrics", "logs", "traces"):
        pq.write_table(pa.table({"time": [1], "api_cpu": [1]}), case / (signal + ".parquet"))
    for path in case.iterdir():
        path.chmod(0o600)
    (case.parent / "manifest.json").write_text(
        json.dumps({"split": "development", "case_id": case_id, "directory": case_id})
    )
    evaluator = root / "phase1-data/official-evaluator-private-v2"
    evaluator.mkdir(parents=True)
    blind = root / "phase1-data/official-blind-private-v2/cases"
    blind.mkdir(parents=True)
    rows, pairs = [], []
    for n in range(20):
        phase, official = "re2-ob-v2-" + f"{n:032x}", "re2-ob-v2-" + f"{n + 100:032x}"
        rows.append(
            {
                "case_id": phase,
                "source_case": f"re2ob_api_cpu_{n}",
                "root_cause_service": "GT_DO_NOT_SEND",
            }
        )
        pairs.append({"phase_id": phase, "official_id": official})
        (blind / official).mkdir()
    mapping = evaluator / "selection.json"
    mapping.write_text(json.dumps(rows))
    (evaluator / "id-map.json").write_text(json.dumps(pairs))
    private = root / "development-data/pilot-v1/private"
    private.mkdir(mode=0o700)
    proof = {
        "version": 1,
        "revision": REVISION,
        "case_id": case_id,
        "source_case": "re2ob_api_cpu_999",
        "identity_disjoint": True,
        "evaluation_mapping_digest": local.digest(mapping),
        "files": {p.name: local.digest(p) for p in case.iterdir()},
    }
    (private / "provenance.json").write_text(json.dumps(proof))
    (private / "provenance.json").chmod(0o600)
    monkeypatch.setattr(local, "git_binding", lambda root: "a" * 40)
    monkeypatch.setattr(local, "image_binding", lambda: ["synthetic-image"])
    monkeypatch.setattr(local, "source_hash", lambda: "a" * 64)
    return local.prepare(root)


def interactive(monkeypatch, prepared):
    monkeypatch.setattr(local, "require_terminal", lambda: None)
    monkeypatch.setattr(local, "run_probe", lambda *a, **kw: {"status": "PASS"})
    monkeypatch.setattr("builtins.input", lambda prompt: local.CONFIRM)
    monkeypatch.setattr(local, "read_secret", lambda: bytearray(b"SYNTHETIC_CREDENTIAL_ONLY"))


def test_prepare_is_no_key_no_approval_no_writes_and_binds_source_data(prepared):
    assert prepared.budget.max_cost_usd == Decimal("0.05")
    assert prepared.plan["estimated_max_model_cost_usd"] == "0.03800000"
    assert not (prepared.root / "pilot-private-results").exists()
    (prepared.case / "logs.parquet").write_bytes(b"changed telemetry")
    with pytest.raises(ValueError):
        local.prepare(prepared.root)


@pytest.mark.parametrize("change", ["overlap", "permissions", "runtime", "retention"])
def test_preparation_rejects_unsafe_configuration_and_origin(prepared, change):
    if change == "permissions":
        (prepared.case / "task.json").chmod(0o644)
    elif change == "overlap":
        proof = prepared.root / "development-data/pilot-v1/private/provenance.json"
        value = json.loads(proof.read_text())
        value["source_case"] = "re2ob_api_cpu_0"
        proof.write_text(json.dumps(value))
    else:
        path = prepared.root / "configs/pilot-preflight-v0.8.json"
        cfg = json.loads(path.read_text())
        if change == "runtime":
            cfg["runtime"]["http_timeout"] = 40
        else:
            cfg["retention"]["delete_mode"] = "automatic"
        path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError):
        local.prepare(prepared.root)


def test_refused_consent_cannot_create_approval_or_read_key(prepared, monkeypatch):
    interactive(monkeypatch, prepared)
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    monkeypatch.setattr(local, "read_secret", lambda: pytest.fail("key reader called"))
    with pytest.raises(local.Blocked, match="LIVE_BLOCKED"):
        local.run(prepared)
    directory = prepared.root / local.STORAGE
    assert not (directory / "approval.json").exists() and not (directory / "attempt.json").exists()


def test_synthetic_terminal_approval_uses_existing_contract_and_claim_precedes_send(
    prepared, monkeypatch
):
    interactive(monkeypatch, prepared)
    seen = []

    def fake_child(p, approval, controls, raw):
        value = local.private_read(approval)
        assert value["approved"] is True and value["runtime"] == controls
        assert 3500 < value["expires"] - local.time.time() <= 3600
        assert approval.stat().st_mode & 0o777 == 0o600
        assert local.private_read(approval.parent / "attempt.json")["runtime"] == controls
        args = local.command(p, approval, controls, 7)
        assert "SYNTHETIC_CREDENTIAL_ONLY" not in " ".join(args)
        assert "--credential-fd" in args and args[-1] == "--live"
        assert bytes(raw) == b"SYNTHETIC_CREDENTIAL_ONLY"
        seen.append(controls["journal_volume"])
        return {"status": "ANALYSIS_SUCCEEDED", "billing_status": "UNRECONCILED"}

    monkeypatch.setattr(local, "invoke", fake_child)
    result = local.run(prepared)
    assert result["status"] == "ANALYSIS_SUCCEEDED" and len(seen) == 1
    directory = prepared.root / local.STORAGE
    assert not (directory / "approval.json").exists()
    with pytest.raises(local.Blocked, match="EXISTING_ATTEMPT"):
        local.run(prepared)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "failure", ["changed_before_approval", "changed_after_key", "key_error", "child_error"]
)
def test_failure_has_no_automatic_new_journal_or_second_invocation(prepared, monkeypatch, failure):
    interactive(monkeypatch, prepared)
    sends = []
    original_prepare = local.prepare

    def changed():
        path = prepared.root / "configs/pilot-preflight-v0.8.json"
        cfg = json.loads(path.read_text())
        cfg["retention"]["artifact_days"] += 1
        path.write_text(json.dumps(cfg))

    if failure == "changed_before_approval":
        monkeypatch.setattr("builtins.input", lambda prompt: changed() or local.CONFIRM)
    elif failure == "changed_after_key":
        monkeypatch.setattr(
            local, "read_secret", lambda: changed() or bytearray(b"SYNTHETIC_CREDENTIAL_ONLY")
        )
    elif failure == "key_error":
        monkeypatch.setattr(
            local,
            "read_secret",
            lambda: (_ for _ in ()).throw(RuntimeError("SYNTHETIC_SECRET_NEVER_PRINT")),
        )

    def child(*args):
        sends.append(1)
        raise TimeoutError("SYNTHETIC_SECRET_NEVER_PRINT")

    monkeypatch.setattr(local, "invoke", child)
    with pytest.raises((ValueError, RuntimeError, TimeoutError)):
        local.run(prepared)
    assert not (prepared.root / local.STORAGE / "approval.json").exists()
    assert len(sends) == (1 if failure == "child_error" else 0)
    if failure != "changed_before_approval":
        monkeypatch.setattr(local, "prepare", lambda root: prepared)
        with pytest.raises(local.Blocked, match="EXISTING_ATTEMPT"):
            local.run(prepared)
    monkeypatch.setattr(local, "prepare", original_prepare)


def test_no_run_from_captured_or_codex_terminal(monkeypatch, capsys):
    monkeypatch.setattr(local.sys, "argv", ["local_pilot", "--run"])
    monkeypatch.setattr(local, "prepare", lambda: pytest.fail("preparation before tty gate"))
    with pytest.raises(SystemExit):
        local.main()
    assert "LIVE_BLOCKED" in capsys.readouterr().out


def test_launcher_argument_errors_never_echo_accidental_secrets(monkeypatch, capsys):
    monkeypatch.setattr(local.sys, "argv", ["local_pilot", "--api-key", "SYNTHETIC_SECRET_CANARY"])
    with pytest.raises(SystemExit):
        local.main()
    captured = capsys.readouterr()
    assert "SYNTHETIC_SECRET_CANARY" not in captured.out + captured.err


def test_codex_marker_blocks_even_when_all_streams_are_ttys(monkeypatch):
    for stream in (local.sys.stdin, local.sys.stdout, local.sys.stderr):
        monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setenv("CODEX_THREAD_ID", "synthetic-session")
    with pytest.raises(local.Blocked, match="DIRECT_LOCAL_TERMINAL"):
        local.require_terminal()


def test_child_timeout_interrupts_once_and_never_retries(prepared, monkeypatch):
    events = []

    class Child:
        stdout = None
        stopped = False

        def __init__(self, *args, **kwargs):
            events.append("start")

        def communicate(self, **kwargs):
            raise local.subprocess.TimeoutExpired("synthetic", 390)

        def poll(self):
            return 2 if self.stopped else None

        def send_signal(self, sig):
            events.append("interrupt")
            self.stopped = True

        def wait(self, **kwargs):
            return 2

    monkeypatch.setattr(local.subprocess, "Popen", Child)
    controls = {
        **prepared.config["runtime"],
        "journal_volume": "rca-pilot-journal-unit",
        "private_output": str(prepared.root / "private"),
    }
    raw = bytearray(b"SYNTHETIC_ONLY")
    with pytest.raises(local.subprocess.TimeoutExpired):
        local.invoke(prepared, prepared.root / "approval", controls, raw)
    assert events == ["start", "interrupt"] and not any(raw)


def test_owner_lock_blocks_concurrent_launch_and_symlinks(tmp_path):
    tmp_path.chmod(0o700)
    with (
        local.owner_lock(tmp_path),
        pytest.raises(local.Blocked, match="ALREADY_RUNNING"),
        local.owner_lock(tmp_path),
    ):
        pytest.fail("second owner")
    target = tmp_path / "target"
    target.write_text("synthetic")
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        local.private_read(link)


def test_getpass_echo_fallback_never_reads_key(monkeypatch):
    def fallback(*args):
        local.warnings.warn("echo would be enabled", local.getpass.GetPassWarning)
        pytest.fail("echo fallback reached")

    monkeypatch.setattr(local.getpass, "getpass", fallback)
    with pytest.raises(local.getpass.GetPassWarning):
        local.read_secret()


@pytest.mark.parametrize("raw", [b"SYNTHETIC_CREDENTIAL_ONLY", b"", b"bad\nvalue", b"x" * 4097])
def test_credential_fd_accepts_only_bounded_raw_pipe(raw):
    from rca_agent.live_deployment import read_credential_fd

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, raw)
        os.close(write_fd)
        write_fd = None
        if raw == b"SYNTHETIC_CREDENTIAL_ONLY":
            assert read_credential_fd(read_fd) == raw.decode()
        else:
            with pytest.raises(ValueError):
                read_credential_fd(read_fd)
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def test_file_and_stalled_fd_are_rejected_without_credentials(tmp_path, monkeypatch):
    from rca_agent import live_deployment as module

    path = tmp_path / "synthetic"
    path.write_text("SYNTHETIC_ONLY")
    with path.open("rb") as stream, pytest.raises(ValueError, match="pipe"):
        module.read_credential_fd(stream.fileno())
    r, w = os.pipe()
    try:
        monkeypatch.setattr(module.select, "select", lambda *args: ([], [], []))
        with pytest.raises(ValueError, match="deadline"):
            module.read_credential_fd(r)
    finally:
        os.close(r)
        os.close(w)


def test_dns_timeout_is_independent_of_http_timeout(monkeypatch):
    from rca_agent import wan_tls_probe as probe

    seen = []

    def resolve(seconds):
        seen.append(seconds)
        raise RuntimeError("stop before Docker or WAN")

    monkeypatch.setattr(probe, "system_addresses", resolve)
    with pytest.raises(RuntimeError):
        probe.run_probe("ipv4-only", 90, 20, dns_timeout=5)
    assert seen == [5]


def test_subprocess_inherits_only_pipe_and_filters_public_output(prepared, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_ENV_MUST_NOT_PASS")
    seen = []

    class Child:
        stdout = None

        def __init__(self, args, **kwargs):
            assert kwargs["start_new_session"] is True
            assert kwargs["pass_fds"] and kwargs["pass_fds"][0] >= 3
            assert os.read(kwargs["pass_fds"][0], 5000) == b"SYNTHETIC_PIPE_ONLY"
            assert "OPENAI_API_KEY" not in kwargs["env"]
            assert "SYNTHETIC_PIPE_ONLY" not in " ".join(args)
            seen.append(1)

        def communicate(self, **kwargs):
            return json.dumps(
                {
                    "status": "ANALYSIS_FAILED",
                    "input_tokens": 12,
                    "error": "SYNTHETIC_PRIVATE_DIAGNOSTIC",
                    "model_output": "SYNTHETIC_RAW_TELEMETRY",
                }
            ).encode(), None

        def poll(self):
            return 2

    monkeypatch.setattr(local.subprocess, "Popen", Child)
    controls = {
        **prepared.config["runtime"],
        "journal_volume": "rca-pilot-journal-synthetic",
        "private_output": str(prepared.root / "private"),
    }
    raw = bytearray(b"SYNTHETIC_PIPE_ONLY")
    result = local.invoke(prepared, prepared.root / "approval", controls, raw)
    assert seen == [1] and not any(raw)
    assert result["input_tokens"] == 12 and "SYNTHETIC" not in json.dumps(result)


@pytest.mark.parametrize("state", ["SENT", "MANUAL_REVIEW", "COMPLETED"])
def test_readonly_journal_preserves_state_and_production_cannot_restart(
    tmp_path, monkeypatch, state
):
    from rca_agent.egress_proxy import EgressProxy
    from rca_agent.model_broker import PreparedRequest
    from rca_agent.production_proxy import ProductionProxy
    from rca_agent.request_journal import RequestJournal, readonly_summary

    tmp_path.chmod(0o700)
    db = tmp_path / "requests.sqlite"
    assert readonly_summary(db) == {"completed": 0, "manual_review": 0, "sent": 0}
    assert not db.exists()
    with RequestJournal(db) as journal:
        prepared = PreparedRequest.from_payload(
            {"synthetic": True}, model="gpt-6-luna", max_output_tokens=10
        )
        fingerprint = journal.sent(prepared, "0" * 32, 1)
        if state == "COMPLETED":
            journal.completed(
                fingerprint, {"input_tokens": 1, "output_tokens": 1, "cost_usd": "0.01"}
            )
        elif state == "MANUAL_REVIEW":
            journal.uncertain(fingerprint)
        counts = readonly_summary(db)
        assert (
            counts[
                {"SENT": "sent", "MANUAL_REVIEW": "manual_review", "COMPLETED": "completed"}[state]
            ]
            == 1
        )
        assert journal.summary() == counts

    monkeypatch.setattr(
        EgressProxy, "__init__", lambda self, config: setattr(self, "journal", RequestJournal(db))
    )
    with pytest.raises(ValueError, match="manual reconciliation"):
        ProductionProxy({})


def test_status_reads_same_volume_readonly_and_never_creates_one(prepared, monkeypatch):
    base = prepared.root / "pilot-private-results"
    local.private_directory(base)
    directory = prepared.root / local.STORAGE
    local.private_directory(directory)
    local.private_create(
        directory / "attempt.json", {"runtime": {"journal_volume": "rca-pilot-journal-unit"}}
    )
    calls = []

    def docker(args, **kwargs):
        calls.append(args)
        if args[1:3] == ["volume", "inspect"]:
            return b'[{"Labels":{"rca.pilot.journal":"v1"}}]'
        assert "none" in args and "--read-only" in args
        assert any("rca-pilot-journal-unit" in a and "readonly" in a for a in args)
        return b'{"sent":1,"manual_review":0,"completed":0}'

    monkeypatch.setattr(local.subprocess, "check_output", docker)
    result = local.status(prepared.root)
    assert result["journal"]["sent"] == 1 and len(calls) == 2
    assert "LIVE_BLOCKED" in json.dumps(result) and "rca-pilot-journal-unit" not in json.dumps(
        result
    )
