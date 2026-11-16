"""Human-terminal-only single pilot launcher. Default check never creates approval or reads keys."""

import fcntl
import hashlib
import json
import os
import re
import resource
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq

from rca_agent.approval import approved_profile
from rca_agent.development import verify_mapping
from rca_agent.docker_integration import source_hash
from rca_agent.graph import ObservationLimits
from rca_agent.model_broker import Budget
from rca_agent.pilot import CATALOG, SafeParser, development_case, plan
from rca_agent.wan_tls_probe import run_probe

ROOT = Path(__file__).resolve().parents[2]
CONFIRM = "동의하고 개발용 1건 유료 실행 승인"
STORAGE = Path("pilot-private-results/local-pilot-v09")
CODEX_MARKERS = ("CODEX_THREAD_ID", "CODEX_CI", "CODEX_SANDBOX", "CODEX_MANAGED_BY_NPM")


class Blocked(ValueError):
    """Only fixed codes may be exposed; exception detail is never printed."""


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def private_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 65536
        ):
            raise Blocked("PRIVATE_STORAGE_UNSAFE")
        return json.loads(os.read(fd, 65537))
    finally:
        os.close(fd)


def private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise Blocked("PRIVATE_STORAGE_UNSAFE")


def private_create(path, value):
    encoded = json.dumps(value, allow_nan=False).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(encoded)
        out.flush()
        os.fsync(out.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def owner_lock(directory):
    fd = os.open(directory / ".owner", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise Blocked("PRIVATE_STORAGE_UNSAFE")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Blocked("ALREADY_RUNNING") from None
        yield
    finally:
        os.close(fd)


def git_binding(root):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL).strip()

    if (
        git("branch", "--show-current") != b"main"
        or git("status", "--porcelain")
        or git("rev-parse", "HEAD") != git("rev-parse", "origin/main")
    ):
        raise Blocked("CLEAN_SYNCED_MAIN_REQUIRED")
    return git("rev-parse", "HEAD").decode()


def image_binding():
    if "DOCKER_HOST" in os.environ or "DOCKER_CONTEXT" in os.environ:
        raise Blocked("LOCAL_DOCKER_REQUIRED")
    context = json.loads(
        subprocess.check_output(["docker", "context", "inspect"], stderr=subprocess.DEVNULL)
    )[0]
    if not context["Endpoints"]["docker"]["Host"].startswith("unix://"):
        raise Blocked("LOCAL_DOCKER_REQUIRED")
    values = []
    for tag in ("rca-offline:phase4b3b", "rca-egress:mock"):
        info = json.loads(
            subprocess.check_output(["docker", "image", "inspect", tag], stderr=subprocess.DEVNULL)
        )[0]
        if info["Config"].get("Labels", {}).get("org.rca.source") != source_hash():
            raise Blocked("REBUILD_IMAGES_REQUIRED")
        values.append(info["Id"])
    return values


@dataclass(frozen=True, repr=False)
class PreparedPilot:
    root: Path
    config: dict = field(repr=False)
    budget: Budget
    plan: dict = field(repr=False)
    case: Path
    binding: str


def prepare(root=ROOT):
    root = Path(root).resolve()
    commit = git_binding(root)
    config_path = root / "configs/pilot-preflight-v0.8.json"
    config = json.loads(config_path.read_text())
    if config["purpose"] != "preflight-only" or config["public_reporting"] != "aggregate-only":
        raise Blocked("CONFIGURATION_INVALID")
    b = Budget(
        **{
            **config["case_and_total_budget"],
            "max_cost_usd": Decimal(config["case_and_total_budget"]["max_cost_usd"]),
        }
    )
    p = plan(config["model"], b, config["price_profile"])
    retention = config["retention"]
    if (
        b.max_calls > 8
        or b.max_concurrency != 1
        or b.max_retries != 0
        or set(retention)
        != {"journal_days_after_reconciliation", "artifact_days", "development_days", "delete_mode"}
        or retention["delete_mode"] != "manual"
        or any(
            type(retention[k]) is not int or retention[k] < 1
            for k in ("journal_days_after_reconciliation", "artifact_days", "development_days")
        )
    ):
        raise Blocked("CONFIGURATION_INVALID")
    ObservationLimits(**config["observation_limits"])
    r = config["runtime"]
    if (
        r["family_policy"] != "ipv4-only"
        or not 0 < r["http_timeout"] < b.timeout_seconds <= r["session_timeout"]
        or not 0 < r["dns_refresh"] < r["dns_lease"]
        or not 2 * r["http_timeout"] < r["dns_lease"]
        or not 0 < r["dns_timeout"] < r["dns_lease"]
    ):
        raise Blocked("CONFIGURATION_INVALID")
    cases = root / "development-data/pilot-v1/cases"
    manifest = cases / "manifest.json"
    proof = root / "development-data/pilot-v1/private/provenance.json"
    private_read(proof)
    declaration = json.loads(manifest.read_text())
    case = cases / declaration["directory"]
    evaluation = root / "phase1-data/official-evaluator-private-v2"
    mapping = evaluation / "selection.json"
    protected = root / "phase1-data/official-blind-private-v2/cases"
    verify_mapping(mapping, evaluation / "id-map.json", protected)
    development_case(cases, case, manifest, protected, provenance=proof, evaluation_mapping=mapping)
    for path in case.iterdir():
        info = os.lstat(path)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise Blocked("DEVELOPMENT_PERMISSIONS_INVALID")
    for signal in ("metrics", "logs", "traces"):
        table = pq.read_table(case / (signal + ".parquet"))
        if not table.num_rows or not table.num_columns:
            raise Blocked("TELEMETRY_MISSING")
    snapshot = {
        "commit": commit,
        "source": source_hash(),
        "config": digest(config_path),
        "manifest": digest(manifest),
        "provenance": digest(proof),
        "lock": digest(root / "uv.lock"),
        "images": image_binding(),
    }
    return PreparedPilot(
        root,
        config,
        b,
        p,
        case,
        hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
    )


def require_terminal():
    if (
        not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr))
        or any(name in os.environ for name in CODEX_MARKERS)
        or "DOCKER_HOST" in os.environ
        or "DOCKER_CONTEXT" in os.environ
    ):
        raise Blocked("DIRECT_LOCAL_TERMINAL_REQUIRED")


def notice(prepared):
    cfg = prepared.config
    return (
        "개발용 원본 독립성 검증 사례 1건만 실행합니다. 평가 20건은 전송하지 않습니다.\n"
        + json.dumps(
            {
                "model": cfg["model"],
                "case_and_total_budget": cfg["case_and_total_budget"],
                "runtime": cfg["runtime"],
                "observation_limits": cfg["observation_limits"],
                "retention": cfg["retention"],
            },
            ensure_ascii=False,
        )
        + "\n전송: 지침·schema·후보 목록·Metrics/Logs/Traces 관측 및 근거. 로그 마스킹은 완전한 PII 제거 보장이 아닙니다.\n"
        "OpenAI store=false는 ZDR이 아닙니다. 기본 abuse monitoring 내용 보존은 통상 최대 30일이며 예외·cache 정책이 있습니다.\n"
        "https://developers.openai.com/api/docs/guides/your-data\n"
        "모델 토큰 비용 계획 USD "
        + prepared.plan["estimated_max_model_cost_usd"]
        + ", software 예산 USD 0.05는 실제 청구 절대 상한이나 원격 취소 보장이 아닙니다.\n"
        "실제 인증/schema/count/usage/청구는 미검증입니다. 실패·미확인 과금은 자동 재시도하지 않습니다.\n"
        "계정 모델 접근·잔액·global Standard 가격/부가 요금·데이터 정책을 확인했고, 가정이 다르면 실행을 취소하십시오.\n"
        "승인 유효기간은 생성 후 1시간입니다. 로컬 보존/수동 삭제와 외부 전송·유료 실행·키 사용에 동의할 때만 아래 문구를 직접 입력하십시오.\n"
        "승인 후 저장소 .env의 OPENAI_API_KEY를 읽습니다. 키를 다시 입력하지 않으며 환경변수로 등록하지 않습니다.\n"
        "Codex/기록되는 터미널이 아닌 사용자 독립 터미널에서 실행 중임을 확인하십시오."
    )


def approval_value(prepared, controls):
    return {
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
        "retention": prepared.config["retention"],
        "settings_version": prepared.plan["settings_version"],
        "source_version": source_hash(),
        "provenance_digest": digest(
            prepared.root / "development-data/pilot-v1/private/provenance.json"
        ),
        "expires": time.time() + 3600,
        "runtime": controls,
        "total_budget": prepared.config["case_and_total_budget"],
        "observation_limits": prepared.config["observation_limits"],
    }


def command(prepared, approval, controls, fd):
    root = prepared.root
    args = [
        str(root / ".venv/bin/python"),
        "-m",
        "rca_agent.pilot",
        "--development-root",
        str(prepared.case.parent),
        "--case-directory",
        str(prepared.case),
        "--development-manifest",
        str(prepared.case.parent / "manifest.json"),
        "--provenance-file",
        str(root / "development-data/pilot-v1/private/provenance.json"),
        "--evaluation-mapping",
        str(root / "phase1-data/official-evaluator-private-v2/selection.json"),
        "--model",
        prepared.config["model"],
        "--price-profile",
        prepared.config["price_profile"],
    ]
    for key, value in prepared.config["case_and_total_budget"].items():
        args.extend(["--" + key.replace("_", "-"), str(value)])
    for flag, value in {
        "approval-file": approval,
        "credential-fd": fd,
        "journal-volume": controls["journal_volume"],
        "private-output": controls["private_output"],
        "dns-lease-seconds": controls["dns_lease"],
        "dns-refresh-seconds": controls["dns_refresh"],
        "dns-timeout-seconds": controls["dns_timeout"],
        "http-timeout-seconds": controls["http_timeout"],
        "session-timeout-seconds": controls["session_timeout"],
        "address-family-policy": controls["family_policy"],
    }.items():
        args.extend(["--" + flag, str(value)])
    return args + ["--live"]


def dotenv_secret(data):
    """Literal scalar only: never source, interpolate, execute, or populate os.environ."""
    value = None
    for line in data.decode("utf-8-sig").splitlines():
        entry = line.strip()
        if entry.startswith(("export ", "export\t")):
            entry = entry[6:].lstrip()
        name, separator, scalar = entry.partition("=")
        if name.strip() != "OPENAI_API_KEY":
            continue
        if value is not None:
            raise Blocked("ENV_KEY_DUPLICATE")
        match = re.fullmatch(
            r"""(?:'([^'\r\n]*)'|"([^"\r\n]*)"|([^\s'\"]+))(?:[ \t]+#[^\r\n]*)?""", scalar.strip()
        )
        if not separator or match is None:
            raise Blocked("ENV_KEY_INVALID")
        value = next(part for part in match.groups() if part is not None)
    if value is None:
        raise Blocked("ENV_KEY_MISSING")
    if not 1 <= len(value) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise Blocked("ENV_KEY_INVALID")
    return bytearray(value, "ascii")


def read_secret(root):
    """Read only the operator-owned .env, and only after final terminal consent."""
    try:
        fd = os.open(Path(root) / ".env", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise Blocked("ENV_FILE_MISSING") from None
    except OSError:
        raise Blocked("ENV_FILE_UNSAFE") from None
    data = bytearray()
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 65536
        ):
            raise Blocked("ENV_FILE_UNSAFE")
        data.extend(os.read(fd, 65537))
        after = os.fstat(fd)
        if (
            len(data) != info.st_size
            or len(data) > 65536
            or (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            raise Blocked("ENV_FILE_UNSAFE")
        try:
            return dotenv_secret(data)
        except UnicodeError:
            raise Blocked("ENV_KEY_INVALID") from None
    finally:
        data[:] = b"\0" * len(data)
        os.close(fd)


def invoke(prepared, approval, controls, raw):
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    read_fd, write_fd = os.pipe()
    proc = None
    try:
        if os.write(write_fd, raw) != len(raw):
            raise Blocked("CREDENTIAL_PIPE_FAILED")
        os.close(write_fd)
        write_fd = None
        raw[:] = b"\0" * len(raw)
        env = {
            k: os.environ[k]
            for k in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
            if k in os.environ
        }
        proc = subprocess.Popen(
            command(prepared, approval, controls, read_fd),
            cwd=prepared.root,
            env=env,
            pass_fds=(read_fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        os.close(read_fd)
        read_fd = None
        data, _ = proc.communicate(timeout=controls["session_timeout"] + 90)
        if len(data) > 262144:
            raise Blocked("MANUAL_REVIEW_REQUIRED")
        value = json.loads(data)
        if value.get("status") not in ("ANALYSIS_SUCCEEDED", "ANALYSIS_FAILED", "FAILED"):
            raise Blocked("MANUAL_REVIEW_REQUIRED")
        if (value["status"] == "ANALYSIS_SUCCEEDED") != (proc.poll() == 0):
            raise Blocked("MANUAL_REVIEW_REQUIRED")
        # Never echo the child's arbitrary diagnostics, strings, paths, or model output.
        result = {
            "status": value["status"],
            "billing_status": "UNRECONCILED",
            "actual_cost_usd": None,
        }
        for key in ("input_tokens", "output_tokens", "tool_calls", "iterations", "model_calls"):
            if type(value.get(key)) is int and 0 <= value[key] <= 10000000:
                result[key] = value[key]
        from rca_agent.public_report import SAFE_REASONS

        safe_failures = SAFE_REASONS | {
            "LIVE_BLOCKED",
            "INVALID_CONFIGURATION",
            "MANUAL_REVIEW_REQUIRED",
            "TLS_CERTIFICATE",
            "TLS_HOSTNAME",
            "TLS_PROTOCOL",
            "REDIRECT_DENIED",
            "HTTP_429",
            "HTTP_5XX",
            "HTTP_ERROR",
            "REFUSAL",
            "USAGE_UNAVAILABLE",
            "TIMEOUT",
            "DNS_LEASE_INVALID",
            "RESPONSE_INVALID",
            "PROXY_UNAVAILABLE",
        }
        if value.get("failure_type") in safe_failures:
            result["failure_type"] = value["failure_type"]
        signals = value.get("signals_observed")
        if isinstance(signals, list) and all(s in ("metrics", "logs", "traces") for s in signals):
            result["signals_observed"] = sorted(set(signals))
        cost = value.get("token_cost_upper_usd")
        if isinstance(cost, str) and len(cost) < 40:
            number = Decimal(cost)
            if number.is_finite() and number >= 0:
                result["token_cost_upper_usd"] = str(number)
        return result
    finally:
        raw[:] = b"\0" * len(raw)
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)
        if proc and proc.poll() is None:
            # SIGINT lets the trusted runner unwind Docker resources; unknown billing remains blocked.
            proc.send_signal(__import__("signal").SIGINT)
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if proc and proc.stdout:
            proc.stdout.close()


def run(prepared):
    require_terminal()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    base = prepared.root / "pilot-private-results"
    private_directory(base)
    directory = prepared.root / STORAGE
    private_directory(directory)
    with owner_lock(directory):
        attempt = directory / "attempt.json"
        if os.path.lexists(attempt) or os.path.lexists(directory / "approval.json"):
            raise Blocked("EXISTING_ATTEMPT_MANUAL_REVIEW")
        r = prepared.config["runtime"]
        probe = run_probe(
            r["family_policy"], r["dns_lease"], r["http_timeout"], dns_timeout=r["dns_timeout"]
        )
        if probe["status"] != "PASS":
            raise Blocked("WAN_TLS_BLOCKED")
        print(notice(prepared))
        if input(CONFIRM + ": ") != CONFIRM:
            raise Blocked("LIVE_BLOCKED")
        if prepare(prepared.root).binding != prepared.binding:
            raise Blocked("INPUT_CHANGED")
        output = directory / "artifacts"
        private_directory(output)
        controls = {
            **r,
            "journal_volume": "rca-pilot-journal-" + uuid.uuid4().hex,
            "private_output": str(output),
        }
        # Claim before approval/key/HTTP. A crash or cancellation must never silently start a new attempt.
        private_create(
            attempt,
            {
                "state": "CLAIMED_MANUAL_REVIEW_UNTIL_SETTLED",
                "binding": prepared.binding,
                "runtime": controls,
                "created": time.time(),
            },
        )
        approval = directory / "approval.json"
        raw = None
        created = False
        try:
            private_create(approval, approval_value(prepared, controls))
            created = True
            approved_profile(
                approval,
                prepared.plan,
                prepared.budget,
                prepared.root / "development-data/pilot-v1/private/provenance.json",
                source_hash(),
                controls,
                catalog=CATALOG,
            )
            raw = read_secret(prepared.root)
            if prepare(prepared.root).binding != prepared.binding:
                raise Blocked("INPUT_CHANGED")
            result = invoke(prepared, approval, controls, raw)
            private_create(directory / "outcome.json", result)
            return result
        finally:
            if raw is not None:
                raw[:] = b"\0" * len(raw)
            if created and approval.exists():
                approval.unlink()


def status(root=ROOT):
    """Reuse the recorded journal for inspection; never create a volume or issue HTTP."""
    directory = Path(root) / STORAGE
    if not os.path.lexists(directory):
        return {"status": "NOT_STARTED", "live": "LIVE_BLOCKED"}
    info = os.lstat(directory)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise Blocked("PRIVATE_STORAGE_UNSAFE")
    if not os.path.lexists(directory / "attempt.json"):
        return {"status": "NOT_STARTED", "live": "LIVE_BLOCKED"}
    attempt = private_read(directory / "attempt.json")
    volume = attempt["runtime"]["journal_volume"]
    if (
        not isinstance(volume, str)
        or not volume.startswith("rca-pilot-journal-")
        or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in volume)
    ):
        raise Blocked("PRIVATE_STORAGE_UNSAFE")
    try:
        info = json.loads(
            subprocess.check_output(
                ["docker", "volume", "inspect", volume], stderr=subprocess.DEVNULL
            )
        )[0]
    except (subprocess.SubprocessError, OSError):
        raise Blocked("JOURNAL_UNAVAILABLE_MANUAL_REVIEW") from None
    if info.get("Labels", {}).get("rca.pilot.journal") != "v1":
        raise Blocked("JOURNAL_UNAVAILABLE_MANUAL_REVIEW")
    code = "import json;from rca_agent.request_journal import readonly_summary;print(json.dumps(readonly_summary('/journal/requests.sqlite')))"
    raw = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network",
            "none",
            "--read-only",
            "--user",
            "10005:10002",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--log-driver",
            "none",
            "--mount",
            f"type=volume,src={volume},dst=/journal,readonly",
            "rca-egress:mock",
            "python",
            "-c",
            code,
        ],
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    counts = json.loads(raw)
    if set(counts) != {"completed", "manual_review", "sent"} or any(
        type(x) is not int or x < 0 for x in counts.values()
    ):
        raise Blocked("JOURNAL_UNAVAILABLE_MANUAL_REVIEW")
    result = {
        "status": "MANUAL_REVIEW_NO_AUTOMATIC_RERUN",
        "live": "LIVE_BLOCKED",
        "journal": counts,
        "billing_status": "UNRECONCILED",
    }
    outcome = directory / "outcome.json"
    if os.path.lexists(outcome):
        value = private_read(outcome)
        if value.get("status") in ("ANALYSIS_SUCCEEDED", "ANALYSIS_FAILED", "FAILED"):
            result["analysis_status"] = value["status"]
    return result


def main():
    parser = SafeParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--run",
        action="store_true",
        help="human local terminal only; final consent then load key from repository .env",
    )
    modes.add_argument(
        "--status", action="store_true", help="read existing private journal; never retry"
    )
    try:
        args = parser.parse_args()
        if args.status:
            print(json.dumps(status(), sort_keys=True))
            return
        if args.run:
            require_terminal()  # Fail before any local approval files or WAN connections.
        prepared = prepare()
        if not args.run and os.path.lexists(ROOT / STORAGE / "attempt.json"):
            raise Blocked("EXISTING_ATTEMPT_MANUAL_REVIEW")
        if args.run:
            result = run(prepared)
        else:
            result = {
                "status": "READY_OFFLINE",
                "live": "LIVE_BLOCKED",
                "model": prepared.config["model"],
                "max_model_requests": prepared.budget.max_calls,
                "max_count_requests": prepared.budget.max_calls,
                "estimated_max_model_cost_usd": prepared.plan["estimated_max_model_cost_usd"],
                "signals": ["metrics", "logs", "traces"],
                "original_case_disjoint": True,
            }
        print(json.dumps(result, sort_keys=True))
        if result["status"] != "ANALYSIS_SUCCEEDED" and args.run:
            raise SystemExit(2)
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - never print credential or private exception detail
        codes = {
            "PRIVATE_STORAGE_UNSAFE",
            "ALREADY_RUNNING",
            "CLEAN_SYNCED_MAIN_REQUIRED",
            "REBUILD_IMAGES_REQUIRED",
            "CONFIGURATION_INVALID",
            "DEVELOPMENT_PERMISSIONS_INVALID",
            "TELEMETRY_MISSING",
            "DIRECT_LOCAL_TERMINAL_REQUIRED",
            "EXISTING_ATTEMPT_MANUAL_REVIEW",
            "WAN_TLS_BLOCKED",
            "LIVE_BLOCKED",
            "INPUT_CHANGED",
            "INVALID_CREDENTIAL_FORMAT",
            "CREDENTIAL_PIPE_FAILED",
            "MANUAL_REVIEW_REQUIRED",
            "JOURNAL_UNAVAILABLE_MANUAL_REVIEW",
            "LOCAL_DOCKER_REQUIRED",
            "ENV_FILE_MISSING",
            "ENV_FILE_UNSAFE",
            "ENV_KEY_MISSING",
            "ENV_KEY_INVALID",
            "ENV_KEY_DUPLICATE",
        }
        reason = (
            str(exc)
            if isinstance(exc, Blocked) and str(exc) in codes
            else "PREFLIGHT_OR_MANUAL_REVIEW_REQUIRED"
        )
        print(json.dumps({"status": "LIVE_BLOCKED", "reason": reason}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
