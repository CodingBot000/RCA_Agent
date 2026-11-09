"""Explicit evaluator-side selection, download and blind task preparation.

These filesystem operations do not create an operating-system isolation boundary.
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import urllib.request
from collections import Counter
from pathlib import Path

from rca_agent.contracts import ContractError, RCAInput

INDEX_SHA256 = "c49a288920dbba2e8e724679a14636d5c7eb2b45426bba14007ef79a6c0ab1bb"
REVISION = "afeacb11bcc94dadfd1c8f483ee4377b2b8b614e"
BASE = f"https://huggingface.co/datasets/phamquiluan/RCAEval/resolve/{REVISION}"
FAULTS = ("cpu", "delay", "disk", "loss", "mem", "socket")
FILES = ("metrics.parquet", "logs.parquet", "traces.parquet", "inject_time.txt")
CASE_ID_PATTERN = re.compile(r"re2-ob-v2-[0-9a-f]{32}\Z")
CASE_PATTERN = re.compile(r"re2ob_[a-z0-9-]+_(?:cpu|delay|disk|loss|mem|socket)_\d+\Z")


def _order(value: str) -> str:
    return hashlib.sha256(f"42:{value}".encode()).hexdigest()


def _legacy_select_cases(rows: list[dict]) -> list[dict]:
    """Reconstruct the publicly known seed-42 sample solely to exclude it."""
    rows = [r for r in rows if r.get("dataset") == "RE2-OB"]
    if len(rows) != 90 or len({r.get("case") for r in rows}) != 90:
        raise ValueError("expected 90 unique RE2-OB cases")
    groups = {fault: [r for r in rows if r.get("fault") == fault] for fault in FAULTS}
    if any(len(v) != 15 for v in groups.values()):
        raise ValueError("expected six fault strata of 15")
    if any(
        not isinstance(r.get("case"), str) or not CASE_PATTERN.fullmatch(r["case"]) for r in rows
    ):
        raise ValueError("invalid case path")
    if any(
        not isinstance(r.get("root_cause_service"), str) or not r["root_cause_service"]
        for r in rows
    ):
        raise ValueError("invalid service")
    quotas = {fault: 3 + (fault in sorted(FAULTS, key=_order)[:2]) for fault in FAULTS}
    service_counts: Counter = Counter()
    selected = []
    for fault in sorted(FAULTS, key=_order):
        available = list(groups[fault])
        for _ in range(quotas[fault]):
            item = min(
                available,
                key=lambda r: (service_counts[r["root_cause_service"]], _order(r["case"])),
            )
            available.remove(item)
            service_counts[item["root_cause_service"]] += 1
            selected.append(item)
    return sorted(selected, key=lambda r: _order(r["case"]))


def _keyed_order(key: bytes, domain: str, value: str) -> str:
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("a 32-byte evaluator-only selection key is required")
    return hmac.new(key, (domain + "\0" + value).encode(), hashlib.sha256).hexdigest()


def case_id(key: bytes, domain: str, source_case: str) -> str:
    return "re2-ob-v2-" + _keyed_order(key, domain, source_case)[:32]


def select_cases(rows: list[dict], key: bytes) -> list[dict]:
    """Select 20 non-legacy cases using a private key and balanced fault strata."""
    legacy = {r["case"] for r in _legacy_select_cases(rows)}
    pool = [r for r in rows if r.get("dataset") == "RE2-OB" and r["case"] not in legacy]
    groups = {f: [r for r in pool if r["fault"] == f] for f in FAULTS}
    order = sorted(FAULTS, key=lambda f: _keyed_order(key, "fault", f))
    quotas = {f: 3 + (f in order[:2]) for f in FAULTS}
    counts: Counter = Counter()
    selected = []
    for fault in order:
        available = list(groups[fault])
        if len(available) < quotas[fault]:
            raise ValueError("insufficient non-legacy cases")
        for _ in range(quotas[fault]):
            item = min(
                available,
                key=lambda r: (
                    counts[r["root_cause_service"]],
                    _keyed_order(key, "case", r["case"]),
                ),
            )
            available.remove(item)
            counts[item["root_cause_service"]] += 1
            selected.append(item)
    return sorted(selected, key=lambda r: _keyed_order(key, "phase-order", r["case"]))


def read_private_key(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ContractError("selection key must be a private regular file")
    key = path.read_bytes()
    if len(key) != 32:
        raise ContractError("selection key must be 32 bytes")
    return key


def read_index(path: Path) -> list[dict]:
    import pyarrow.parquet as pq

    return pq.read_table(
        path, columns=["dataset", "case", "fault", "root_cause_service"]
    ).to_pylist()


def download_file(url: str, path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise FileExistsError(path)
    with urllib.request.urlopen(url, timeout=120) as response, path.open("xb") as out:
        shutil.copyfileobj(response, out)


def prepare(destination: Path, key_path: Path | None = None) -> None:
    """Download a secret sample; an existing private key can reproduce it."""
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    index = destination / "cases.parquet"
    download_file(f"{BASE}/cases.parquet", index)
    if hashlib.sha256(index.read_bytes()).hexdigest() != INDEX_SHA256:
        raise ContractError("index does not match the pinned dataset revision")
    private = destination / "private"
    private.mkdir(mode=0o700)
    key = read_private_key(key_path) if key_path is not None else secrets.token_bytes(32)
    output_key = private / "selection.key"
    fd = os.open(output_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(key)
    selected = select_cases(read_index(index), key)
    raw = private / "raw"
    raw.mkdir(mode=0o700)
    mapping = []
    for item in selected:
        case_id_value = case_id(key, "phase-id", item["case"])
        target = raw / case_id_value
        target.mkdir(mode=0o700)
        for name in FILES:
            download_file(f"{BASE}/{item['case']}/{name}", target / name)
        mapping.append(
            {
                "case_id": case_id_value,
                "source_case": item["case"],
                "fault": item["fault"],
                "root_cause_service": item["root_cause_service"],
            }
        )
    answers = private / "mapping.json"
    answers.write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")
    answers.chmod(0o600)
    index.rename(private / "cases.parquet")


def validate_private_sample(private: Path) -> tuple[list[dict], bytes]:
    index = private / "cases.parquet"
    mapping_file = private / "mapping.json"
    if private.is_symlink() or index.is_symlink() or mapping_file.is_symlink():
        raise ContractError("private metadata must not be symlinks")
    if hashlib.sha256(index.read_bytes()).hexdigest() != INDEX_SHA256:
        raise ContractError("index does not match the pinned dataset revision")
    key = read_private_key(private / "selection.key")
    selected = select_cases(read_index(index), key)
    mapping = json.loads(mapping_file.read_text())
    expected = [
        {
            "case_id": case_id(key, "phase-id", item["case"]),
            "source_case": item["case"],
            "fault": item["fault"],
            "root_cause_service": item["root_cause_service"],
        }
        for item in selected
    ]
    if mapping != expected or len({item["case_id"] for item in expected}) != 20:
        raise ContractError("private mapping does not match secret selection")
    return mapping, key


def export_blind(raw: Path, output: Path) -> None:
    """Export transformed v2 data; host must mount only output read-only."""
    import pyarrow.parquet as pq

    from rca_agent.blind_privacy import shifted_time, transform_parquet

    if raw.is_symlink() or not raw.is_dir():
        raise ContractError("raw must be a directory")
    mapping, key = validate_private_sample(raw.parent)
    expected = {item["case_id"] for item in mapping}
    if {p.name for p in raw.iterdir()} != expected:
        raise ContractError("expected exactly 20 private cases")
    known_times = set(
        pq.read_table(raw.parent / "cases.parquet", columns=["inject_time"])[
            "inject_time"
        ].to_pylist()
    )
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for item in mapping:
        case_id_value = item["case_id"]
        case = raw / case_id_value
        if case.is_symlink() or not case.is_dir():
            raise ContractError("invalid case directory")
        if {p.name for p in case.iterdir()} != set(FILES):
            raise ContractError("unexpected or missing data")
        for name in FILES:
            path = case / name
            if path.is_symlink() or not path.is_file():
                raise ContractError("invalid data file")
        names = pq.read_schema(case / "metrics.parquet").names
        candidates = [n for n in names if n != "time"]
        original_time = int((case / "inject_time.txt").read_text().strip())
        target_time = shifted_time(key, item["source_case"], original_time, known_times)
        known_times.add(target_time)
        shift = target_time - original_time
        task = RCAInput.parse(
            {
                "case_id": case_id_value,
                "candidates": candidates,
                "evidence": [
                    {
                        "id": "telemetry-metadata",
                        "content": f"Telemetry files are available; inject_time={target_time}.",
                    }
                ],
            }
        )
        dest = output / case_id_value
        dest.mkdir(mode=0o700)
        for kind in ("metrics", "logs", "traces"):
            transform_parquet(
                case / f"{kind}.parquet",
                dest / f"{kind}.parquet",
                kind,
                shift,
                key,
                item["source_case"],
            )
        (dest / "inject_time.txt").write_text(f"{target_time}\n")
        (dest / "task.json").write_text(
            json.dumps(
                {
                    "case_id": task.case_id,
                    "candidates": list(task.candidates),
                    "evidence": [{"id": e.id, "content": e.content} for e in task.evidence],
                },
                indent=2,
            )
            + "\n"
        )


def load_task(case: Path) -> RCAInput:
    if case.is_symlink() or (case / "task.json").is_symlink():
        raise ContractError("symlink not allowed")
    return RCAInput.parse(json.loads((case / "task.json").read_text(encoding="utf-8")))


def validate_blind(root: Path) -> None:
    """Reject unexpected files before giving this directory to a container."""
    if root.is_symlink() or not root.is_dir():
        raise ContractError("blind directory must be a real directory")
    expected = {p.name for p in root.iterdir()}
    if len(expected) != 20 or any(not CASE_ID_PATTERN.fullmatch(name) for name in expected):
        raise ContractError("expected exactly 20 private v2 blind cases")
    for case_id in sorted(expected):
        case = root / case_id
        if case.is_symlink() or not case.is_dir():
            raise ContractError("invalid case directory")
        entries = list(case.iterdir())
        if {p.name for p in entries} != {*FILES, "task.json"}:
            raise ContractError("unexpected blind file")
        if any(p.is_symlink() or not p.is_file() for p in entries):
            raise ContractError("invalid blind file")
        if load_task(case).case_id != case_id:
            raise ContractError("case ID mismatch")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser(
        "download", help="explicitly download pinned index and selected 20 cases"
    )
    fetch.add_argument("destination", type=Path)
    fetch.add_argument("--key", type=Path, help="existing evaluator-only key for reproduction")
    export = sub.add_parser("export", help="copy prepared cases to a blind directory")
    export.add_argument("raw", type=Path)
    export.add_argument("output", type=Path)
    check = sub.add_parser("validate", help="validate a blind tree before mounting")
    check.add_argument("blind", type=Path)
    args = parser.parse_args()
    if args.command == "download":
        prepare(args.destination, args.key)
    elif args.command == "export":
        export_blind(args.raw, args.output)
    else:
        validate_blind(args.blind)


if __name__ == "__main__":
    main()
