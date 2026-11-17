"""Read-only local attempt investigation. No key reads, recovery, volume creation, or API calls."""

import ast
import hashlib
import itertools
import json
import os
import re
import stat
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from rca_agent.local_pilot import (
    STORAGE,
    SafeParser,
    private_create,
    private_directory,
)

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_COMMIT = "cf28490085e79de24a6803a4a9d329f4f2699b2f"
HISTORICAL_SOURCE = "433072afd8b25dd54e823d4427e4f9c3ae9d0ff38983de72e8768abe325004bc"
BUILD_LOG = Path("/tmp/rca-v09-build.log")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def command(args, root=ROOT):
    return subprocess.check_output(args, cwd=root, stderr=subprocess.DEVNULL, timeout=15)


def protected_records(root):
    directory = root / STORAGE
    info = os.lstat(directory)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("unsafe private directory")
    values, hashes = [], {}
    for name in ("attempt.json", "outcome.json"):
        fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_nlink != 1
                or before.st_size > 65536
            ):
                raise ValueError("unsafe record file")
            data = os.read(fd, 65537)
            after = os.fstat(fd)
            if len(data) != before.st_size or (before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ValueError("record changed while reading")
            values.append(json.loads(data))
            hashes[name] = sha(data)
        finally:
            os.close(fd)
    attempt, outcome = values
    expected = {
        "dns_lease",
        "dns_refresh",
        "dns_timeout",
        "family_policy",
        "http_timeout",
        "journal_volume",
        "private_output",
        "session_timeout",
    }
    if (
        set(attempt) != {"state", "binding", "runtime", "created"}
        or set(attempt["runtime"]) != expected
    ):
        raise ValueError("unexpected attempt schema")
    volume = attempt["runtime"]["journal_volume"]
    if not isinstance(volume, str) or not re.fullmatch(r"rca-pilot-journal-[a-z0-9-]+", volume):
        raise ValueError("invalid recorded volume")
    if not re.fullmatch(r"[0-9a-f]{64}", attempt["binding"]):
        raise ValueError("invalid binding")
    return attempt, outcome, hashes


def historical_source(root):
    names = (
        command(["git", "ls-tree", "-r", "--name-only", HISTORICAL_COMMIT, "src/rca_agent"], root)
        .decode()
        .splitlines()
    )
    h = hashlib.sha256()
    pilot = None
    for name in sorted(x for x in names if x.endswith(".py") and x.count("/") == 2):
        data = command(["git", "show", HISTORICAL_COMMIT + ":" + name], root)
        h.update(name.rsplit("/", 1)[-1].encode())
        h.update(data)
        if name == "src/rca_agent/pilot.py":
            pilot = data.decode()
    return h.hexdigest(), pilot


def calls(node, name):
    return [
        x
        for x in ast.walk(node)
        if isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id == name
    ]


def predispatch_barrier(source):
    """Check the exception boundary of the pinned historical CLI, not arbitrary Python safety."""
    try:
        main = next(
            x for x in ast.parse(source).body if isinstance(x, ast.FunctionDef) and x.name == "main"
        )
        outer = next(x for x in main.body if isinstance(x, ast.Try))
        live = next(
            x
            for x in outer.body
            if isinstance(x, ast.If) and isinstance(x.test, ast.Attribute) and x.test.attr == "live"
        )
        dispatch = next(
            x for x in live.body if isinstance(x, ast.Try) and calls(x, "LiveDeployment")
        )
        approvals = calls(main, "approved_profile")
        effect_calls = calls(main, "LiveDeployment") + calls(main, "run_session")
        if not approvals or not effect_calls or any(x.lineno >= dispatch.lineno for x in approvals):
            return False
        if any(x not in list(ast.walk(dispatch)) for x in effect_calls):
            return False
        handler = dispatch.handlers[0]
        if (
            len(dispatch.handlers) != 1
            or not isinstance(handler.type, ast.Name)
            or handler.type.id != "Exception"
        ):
            return False
        if len(handler.body) != 1 or not isinstance(handler.body[0], ast.Raise):
            return False
        raised = handler.body[0].exc
        return (
            isinstance(raised, ast.Call)
            and isinstance(raised.func, ast.Name)
            and raised.func.id == "PilotError"
            and len(raised.args) == 1
            and isinstance(raised.args[0], ast.Constant)
            and raised.args[0].value == "MANUAL_REVIEW_REQUIRED"
            and any(
                isinstance(x, ast.Constant) and x.value == "INVALID_CONFIGURATION"
                for x in ast.walk(outer.handlers[0])
            )
        )
    except (SyntaxError, StopIteration, IndexError, AttributeError):
        return False


def image_candidates(log=BUILD_LOG):
    if not log.exists() or log.is_symlink():
        return set()
    info = os.lstat(log)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 2000000:
        return set()
    result = set()
    for line in log.read_text().splitlines():
        if "exporting manifest list sha256:" in line:
            match = re.search(r"sha256:[0-9a-f]{64}", line)
            if match:
                result.add(match.group())
    return result


def binding_matches(root, attempt, image_ids, source):
    snapshot = {
        "commit": HISTORICAL_COMMIT,
        "source": source,
        "config": sha((root / "configs/pilot-preflight-v0.8.json").read_bytes()),
        "manifest": sha((root / "development-data/pilot-v1/cases/manifest.json").read_bytes()),
        "provenance": sha(
            (root / "development-data/pilot-v1/private/provenance.json").read_bytes()
        ),
        "lock": sha((root / "uv.lock").read_bytes()),
    }
    matches = []
    candidates = sorted(set(image_ids))
    if len(candidates) > 64:
        raise ValueError("too many evidence candidates")
    for pair in itertools.permutations(candidates, 2):
        value = sha(json.dumps({**snapshot, "images": list(pair)}, sort_keys=True).encode())
        if value == attempt["binding"]:
            matches.append(pair)
    return matches


def inspect_journal(prefix, volume):
    """Called only after volume inspect succeeds, preventing Docker's implicit creation."""
    code = """
import json,os,stat,sqlite3
p='/journal/requests.sqlite'
if not os.path.lexists(p): print(json.dumps({'status':'DATABASE_NOT_PRESENT','counts':None}));raise SystemExit(0)
i=os.lstat(p)
assert stat.S_ISREG(i.st_mode) and i.st_uid==os.getuid() and stat.S_IMODE(i.st_mode)==0o600 and i.st_nlink==1
c=sqlite3.connect('file:'+p+'?mode=ro',uri=True,timeout=1)
try: counts=dict(c.execute('SELECT state,COUNT(*) FROM requests GROUP BY state'))
finally: c.close()
assert not set(counts)-{'SENT','COMPLETED','MANUAL_REVIEW'}
print(json.dumps({'status':'READ_ONLY','counts':{k:counts.get(k,0) for k in ('SENT','COMPLETED','MANUAL_REVIEW')}}))
"""
    value = json.loads(
        command(
            prefix
            + [
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
            ]
        )
    )
    if value.get("status") == "DATABASE_NOT_PRESENT" and value.get("counts") is None:
        return value
    if (
        value.get("status") != "READ_ONLY"
        or set(value.get("counts", {})) != {"SENT", "COMPLETED", "MANUAL_REVIEW"}
        or any(type(x) is not int or x < 0 for x in value["counts"].values())
    ):
        raise ValueError("invalid journal counts")
    return value


def docker_inventory(volume):
    names = command(["docker", "context", "ls", "--format", "{{.Name}}"]).decode().splitlines()
    contexts = json.loads(command(["docker", "context", "inspect", *names]))
    rows, engines, images = [], set(), set()
    for c in contexts:
        row = {
            "local_unix": c.get("Endpoints", {})
            .get("docker", {})
            .get("Host", "")
            .startswith("unix://"),
            "reachable": False,
            "recorded_volume_present": None,
            "journal": None,
        }
        if row["local_unix"]:
            prefix = ["docker", "--context", c["Name"]]
            try:
                engines.add(command(prefix + ["info", "--format", "{{.ID}} "]).decode().strip())
                row["reachable"] = True
                names = (
                    command(prefix + ["volume", "ls", "--format", "{{.Name}}"])
                    .decode()
                    .splitlines()
                )
                row["managed_journal_name_count"] = sum(
                    x.startswith("rca-pilot-journal-") for x in names
                )
                row["recorded_volume_present"] = volume in names
                if row["recorded_volume_present"]:
                    info = json.loads(command(prefix + ["volume", "inspect", volume]))[0]
                    if info.get("Labels", {}).get("rca.pilot.journal") != "v1":
                        row["journal"] = {"status": "UNTRUSTED_VOLUME", "counts": None}
                    else:
                        row["journal"] = inspect_journal(prefix, volume)
                images.update(
                    command(
                        prefix
                        + [
                            "image",
                            "ls",
                            "--all",
                            "--filter",
                            "label=org.rca.source=" + HISTORICAL_SOURCE,
                            "--quiet",
                            "--no-trunc",
                        ]
                    )
                    .decode()
                    .splitlines()
                )
            except (subprocess.SubprocessError, OSError, ValueError):
                row["inspection_failed"] = True
        rows.append(row)
    return rows, len(engines), images


def classify(outcome, binding_match, barrier, rows):
    known_preflight = (
        outcome.get("status") == "FAILED"
        and outcome.get("failure_type") == "INVALID_CONFIGURATION"
        and set(outcome) == {"status", "failure_type", "billing_status", "actual_cost_usd"}
        and outcome.get("billing_status") == "UNRECONCILED"
        and outcome.get("actual_cost_usd") is None
    )
    complete = bool(rows) and all(
        x["local_unix"] and x["reachable"] and not x.get("inspection_failed") for x in rows
    )
    absent = complete and all(
        x["recorded_volume_present"] is False and x.get("managed_journal_name_count") == 0
        for x in rows
    )
    proven = known_preflight and binding_match and barrier
    return {
        "recorded_codepath": "CHILD_PRE_DISPATCH_FAILURE" if proven else "NOT_PROVEN",
        "parent_key_acquisition": "REACHED_BEFORE_CHILD_RESULT_IN_BOUND_GETPASS_VERSION"
        if proven
        else "UNVERIFIED",
        "proxy_creation_on_bound_codepath": "NOT_REACHED" if proven else "UNVERIFIED",
        "proxy_credential_fd_read_on_bound_codepath": "NOT_REACHED" if proven else "UNVERIFIED",
        "api_http_dispatch_on_bound_codepath": "NOT_REACHED" if proven else "UNVERIFIED",
        "journal_inventory_complete": complete,
        "journal_counts": None,
        "manual_recovery_plan_eligible": proven and absent,
        "execution_state": "LIVE_BLOCKED",
        "recovery_authorized": False,
        "billing_status": "UNRECONCILED",
        "provider_usage_queried": False,
        "original_exception_text": "NOT_RETAINED",
        "historical_engine_identity": "NOT_RECORDED",
    }


def investigate(root=ROOT):
    root = Path(root)
    attempt, outcome, hashes = protected_records(root)
    source, pilot = historical_source(root)
    rows, engines, cached = docker_inventory(attempt["runtime"]["journal_volume"])
    candidates = image_candidates() | cached
    matches = binding_matches(root, attempt, candidates, source)
    barrier = source == HISTORICAL_SOURCE and predispatch_barrier(pilot)
    report = classify(outcome, len(matches) == 1, barrier, rows)
    report.update(
        {
            "schema_version": "pilot-attempt-audit-1",
            "historical_commit": HISTORICAL_COMMIT if len(matches) == 1 else None,
            "historical_binding_matched": len(matches) == 1,
            "historical_exception_boundary_verified": barrier,
            "contexts": rows,
            "reachable_engine_count": engines,
            "historical_cached_image_count": len(cached),
            "build_image_candidate_count": len(candidates),
            "attempt_time_utc": datetime.fromtimestamp(attempt["created"], UTC).isoformat(),
            "trust_scope": "owner-protected local records, Git history, unmodified host and code; not provider billing evidence",
        }
    )
    after = {name: sha((root / STORAGE / name).read_bytes()) for name in hashes}
    if hashes != after:
        raise ValueError("records changed during read-only investigation")
    report["original_records_unchanged"] = True
    receipt = {
        "report": report,
        "private_record_sha256": hashes,
        "recorded_journal_volume": attempt["runtime"]["journal_volume"],
        "recorded_attempt_binding": attempt["binding"],
        "matched_image_ids": matches,
        "created_utc": datetime.now(UTC).isoformat(),
        "audit_script_sha256": sha(Path(__file__).read_bytes()),
        "audit_git_commit": command(["git", "rev-parse", "HEAD"], root).decode().strip(),
        "audit_git_dirty": bool(command(["git", "status", "--porcelain"], root).strip()),
        "recovery_approval": False,
    }
    return report, receipt


def write_receipt(root, value):
    base = Path(root) / "pilot-private-results"
    info = os.lstat(base)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.getuid()
    ):
        raise ValueError("unsafe receipt parent")
    target = base / "journal-audits"
    private_directory(target)
    private_create(target / (uuid.uuid4().hex + ".json"), value)


def main():
    parser = SafeParser(description=__doc__)
    parser.add_argument(
        "--record",
        action="store_true",
        help="write a new private diagnostic receipt; never authorizes recovery",
    )
    try:
        args = parser.parse_args()
        report, receipt = investigate()
        if args.record:
            write_receipt(ROOT, receipt)
        print(json.dumps(report, sort_keys=True))
    except Exception:  # noqa: BLE001 - never echo private paths, volume names, or payloads
        print(
            json.dumps(
                {
                    "execution_state": "LIVE_BLOCKED",
                    "audit_status": "INCOMPLETE_MANUAL_REVIEW",
                    "recovery_authorized": False,
                }
            )
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
