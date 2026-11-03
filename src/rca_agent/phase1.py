"""Explicit evaluator-side selection, download and blind task preparation.

These filesystem operations do not create an operating-system isolation boundary.
"""

import argparse
import hashlib
import json
import re
import shutil
import urllib.request
from collections import Counter
from pathlib import Path

from rca_agent.contracts import ContractError, RCAInput

REVISION = "afeacb11bcc94dadfd1c8f483ee4377b2b8b614e"
BASE = f"https://huggingface.co/datasets/phamquiluan/RCAEval/resolve/{REVISION}"
FAULTS = ("cpu", "delay", "disk", "loss", "mem", "socket")
FILES = ("metrics.parquet", "logs.parquet", "traces.parquet", "inject_time.txt")
CASE_PATTERN = re.compile(r"re2ob_[a-z0-9-]+_(?:cpu|delay|disk|loss|mem|socket)_\d+\Z")


def _order(value: str) -> str:
    return hashlib.sha256(f"42:{value}".encode()).hexdigest()


def select_cases(rows: list[dict]) -> list[dict]:
    """Deterministic balanced sample; return evaluator-only labels and paths."""
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


def prepare(destination: Path) -> None:
    """Download exactly 20 selected cases; destination must be new and private."""
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    index = destination / "cases.parquet"
    download_file(f"{BASE}/cases.parquet", index)
    selected = select_cases(read_index(index))
    private = destination / "private"
    private.mkdir(mode=0o700)
    raw = private / "raw"
    raw.mkdir(mode=0o700)
    mapping = []
    for n, item in enumerate(selected, 1):
        case_id = f"re2-ob-{n:04d}"
        target = raw / case_id
        target.mkdir(mode=0o700)
        for name in FILES:
            download_file(f"{BASE}/{item['case']}/{name}", target / name)
        mapping.append(
            {
                "case_id": case_id,
                "source_case": item["case"],
                "fault": item["fault"],
                "root_cause_service": item["root_cause_service"],
            }
        )
    answers = private / "mapping.json"
    answers.write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")
    answers.chmod(0o600)
    index.rename(private / "cases.parquet")


def export_blind(raw: Path, output: Path) -> None:
    """Export prepared data by copying; host must mount only output read-only."""
    import pyarrow.parquet as pq

    if raw.is_symlink() or not raw.is_dir():
        raise ContractError("raw must be a directory")
    expected = {f"re2-ob-{n:04d}" for n in range(1, 21)}
    if {p.name for p in raw.iterdir()} != expected:
        raise ContractError("expected exactly 20 prepared cases")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for case_id in sorted(expected):
        case = raw / case_id
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
        inject_time = int((case / "inject_time.txt").read_text().strip())
        task = RCAInput.parse(
            {
                "case_id": case_id,
                "candidates": candidates,
                "evidence": [
                    {
                        "id": "telemetry-metadata",
                        "content": f"Telemetry files are available; inject_time={inject_time}.",
                    }
                ],
            }
        )
        dest = output / case_id
        dest.mkdir(mode=0o700)
        for name in FILES:
            shutil.copyfile(case / name, dest / name)
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser(
        "download", help="explicitly download pinned index and selected 20 cases"
    )
    fetch.add_argument("destination", type=Path)
    export = sub.add_parser("export", help="copy prepared cases to a blind directory")
    export.add_argument("raw", type=Path)
    export.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "download":
        prepare(args.destination)
    else:
        export_blind(args.raw, args.output)


if __name__ == "__main__":
    main()
