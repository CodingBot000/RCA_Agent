"""Evaluator-side bridge to a pinned official RCAEval source checkout.

Requires the optional phase1 dependency and an independently provisioned official
Python environment. This module never invokes a model or downloads data.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from rca_agent.contracts import ContractError, RCAInput, RCAResult, read_jsonl
from rca_agent.phase1 import CASE_ID_PATTERN, export_blind, validate_private_sample
from rca_agent.phase1 import case_id as private_case_id

OFFICIAL_COMMIT = "90b63a46ac1e969f1fa9015318b51e455c62f8c4"
ID_PATTERN = CASE_ID_PATTERN


def official_to_input(task, phase_id):
    """Convert official task candidates to the existing strict input contract."""
    if not isinstance(task, dict) or not ID_PATTERN.fullmatch(phase_id):
        raise ContractError("invalid task")
    services = task.get("candidate_services")
    indicators = task.get("candidate_indicators")
    if not isinstance(services, list) or not isinstance(indicators, list):
        raise ContractError("missing official candidates")
    if not all(isinstance(c, str) and c and c == c.strip().lower() for c in services + indicators):
        raise ContractError("invalid official candidate")
    if len(set(services)) != len(services) or len(set(indicators)) != len(indicators):
        raise ContractError("duplicate official candidate")
    candidates = list(dict.fromkeys(services + indicators))
    return RCAInput.parse(
        {
            "case_id": phase_id,
            "candidates": candidates,
            "evidence": [
                {
                    "id": "telemetry-metadata",
                    "content": "Official RCAEval blind telemetry is available.",
                }
            ],
        }
    )


def _source_path(row):
    service, fault = row["root_cause_service"], row["fault"]
    case = row["source_case"]
    if not re.fullmatch(r"[a-z0-9-]+", service) or not re.fullmatch(r"[a-z0-9]+", fault):
        raise ContractError("invalid source metadata")
    match = re.fullmatch(rf"re2ob_{re.escape(service)}_{re.escape(fault)}_(\d+)", case)
    if not match:
        raise ContractError("source does not match labels")
    return f"{service}_{fault}/{match.group(1)}"


def _csv_from_parquet(src, dst):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from pyarrow import csv

    data = pq.ParquetFile(src)
    with pa.OSFile(str(dst), "wb") as sink, csv.CSVWriter(sink, data.schema_arrow) as writer:
        for batch in data.iter_batches(batch_size=32768):
            writer.write_batch(batch)


def _official_check(source):
    result = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    status = subprocess.run(
        ["git", "-C", str(source), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    if result.stdout.strip() != OFFICIAL_COMMIT or status.stdout.strip():
        raise ContractError("official source must be clean and pinned to the verified commit")


def _run_official(source, python, args):
    _official_check(source)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source.resolve())
    env["OPENBLAS_NUM_THREADS"] = "1"
    return subprocess.run(
        [str(python), "-m", "RCAEval.agent", *args],
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )


def _separate(*paths):
    resolved = [p.resolve() for p in paths]
    if any(
        a == b or a in b.parents or b in a.parents
        for i, a in enumerate(resolved)
        for b in resolved[i + 1 :]
    ):
        raise ContractError("private and blind paths must be separate")


def build_export(private, blind, evaluator, source, python):
    """Build official blind data, and put ID mapping and labels in evaluator only."""
    _separate(private, blind, evaluator)
    _official_check(source)
    mapping, key = validate_private_sample(private)
    if blind.exists() or blind.is_symlink() or evaluator.exists() or evaluator.is_symlink():
        raise FileExistsError("output already exists")
    evaluator.mkdir(mode=0o700, parents=True)
    with tempfile.TemporaryDirectory(prefix="official-stage-", dir=evaluator) as temp:
        stage = Path(temp)
        source_root = stage / "source"
        source_root.mkdir()
        sanitized = stage / "sanitized"
        export_blind(private / "raw", sanitized)
        for row in mapping:
            case = sanitized / row["case_id"]
            if case.is_symlink() or not case.is_dir():
                raise ContractError("invalid sanitized case")
            dest = source_root / _source_path(row)
            dest.mkdir(parents=True)
            for original, target in (
                ("metrics.parquet", "data.csv"),
                ("logs.parquet", "logs.csv"),
                ("traces.parquet", "traces.csv"),
            ):
                _csv_from_parquet(case / original, dest / target)
            shutil.copyfile(case / "inject_time.txt", dest / "inject_time.txt")
        exported = stage / "exported"
        _run_official(
            source,
            python,
            [
                "export",
                "--dataset",
                "re2-ob",
                "--out",
                str(exported),
                "--data-root",
                str(source_root),
                "--copy",
            ],
        )
        task_dir = exported / "re2-ob"
        answers_file = exported / "re2-ob.answers.jsonl"
        answers = read_jsonl(answers_file)
        manifest = json.loads((task_dir / "manifest.json").read_text())
        by_source = {_source_path(r): r for r in mapping}
        if (
            len(by_source) != 20
            or len(answers) != 20
            or len({a.get("source") for a in answers}) != 20
        ):
            raise ContractError("duplicate or missing source mapping")
        id_map = []
        official_ids = set()
        for answer in answers:
            row = by_source.get(answer.get("source"))
            old_official_id = answer.get("case_id")
            if (
                row is None
                or not isinstance(old_official_id, str)
                or not re.fullmatch(r"re2-ob-\d{4}", old_official_id)
            ):
                raise ContractError("unknown official source or ID")
            if (
                answer.get("service") != row["root_cause_service"]
                or answer.get("fault") != row["fault"]
            ):
                raise ContractError("answer differs from private mapping")
            old_dir = task_dir / "cases" / old_official_id
            official_task = json.loads((old_dir / "task.json").read_text())
            if official_task.get("case_id") != old_official_id:
                raise ContractError("official task ID mismatch")
            official_id = private_case_id(key, "official-id", row["source_case"])
            if official_id in official_ids:
                raise ContractError("duplicate official ID")
            official_ids.add(official_id)
            official_task["case_id"] = official_id
            (old_dir / "task.json").write_text(json.dumps(official_task, indent=2) + "\n")
            old_dir.rename(task_dir / "cases" / official_id)
            answer["case_id"] = official_id
            official_to_input(official_task, row["case_id"])
            id_map.append({"phase_id": row["case_id"], "official_id": official_id})
        # Upstream orders IDs by source paths; sort by opaque keyed IDs instead.
        answers.sort(key=lambda a: a["case_id"])
        id_map.sort(key=lambda item: item["official_id"])
        manifest["case_ids"] = [a["case_id"] for a in answers]
        (task_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        answers_file.write_text("".join(json.dumps(answer) + "\n" for answer in answers))
        if (
            len({x["official_id"] for x in id_map}) != 20
            or {x["phase_id"] for x in id_map} != {r["case_id"] for r in mapping}
            or manifest.get("case_ids") != [a["case_id"] for a in answers]
        ):
            raise ContractError("mapping or manifest is not one-to-one")
        validate_official_blind(task_dir)
        validate_bindings(id_map, mapping, answers)
        (evaluator / "id-map.json").write_text(json.dumps(id_map, indent=2) + "\n")
        shutil.copyfile(answers_file, evaluator / "re2-ob.answers.jsonl")
        shutil.copyfile(private / "mapping.json", evaluator / "selection.json")
        blind.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(task_dir), str(blind))


def translate_predictions(rows, id_map, blind, id_scheme):
    if (
        len(id_map) != 20
        or len({m.get("phase_id") for m in id_map}) != 20
        or len({m.get("official_id") for m in id_map}) != 20
    ):
        raise ContractError("invalid ID mapping")
    if id_scheme not in {"phase1", "official"}:
        raise ContractError("explicit ID scheme required")
    maps = {
        m["phase_id"] if id_scheme == "phase1" else m["official_id"]: m["official_id"]
        for m in id_map
    }
    if any(
        not isinstance(a, str)
        or not ID_PATTERN.fullmatch(a)
        or not isinstance(b, str)
        or not ID_PATTERN.fullmatch(b)
        for a, b in maps.items()
    ):
        raise ContractError("invalid mapped ID")
    manifest = json.loads((blind / "manifest.json").read_text())
    if (
        set(manifest.get("case_ids", [])) != set(maps.values())
        or len(manifest.get("case_ids", [])) != 20
    ):
        raise ContractError("manifest differs from ID mapping")
    results = []
    seen = set()
    for row in rows:
        phase_id = row.get("case_id")
        if phase_id not in maps or phase_id in seen:
            raise ContractError("unknown or duplicate phase case ID")
        seen.add(phase_id)
        official_id = maps[phase_id]
        task = json.loads((blind / "cases" / official_id / "task.json").read_text())
        if task.get("case_id") != official_id:
            raise ContractError("official task ID mismatch")
        result = RCAResult.parse(row, official_to_input(task, phase_id))
        results.append({"case_id": official_id, "ranks": [r.candidate for r in result.ranking]})
    return results


def validate_bindings(id_map, selection, answers):
    """Reject a swapped ID even when the two ID sets still match."""
    if len(id_map) != 20 or len(selection) != 20 or len(answers) != 20:
        raise ContractError("mapping must cover 20 cases")
    by_phase = {row["case_id"]: _source_path(row) for row in selection}
    by_official = {row["case_id"]: row["source"] for row in answers}
    if len(by_phase) != 20 or len(by_official) != 20:
        raise ContractError("duplicate selection or answer ID")
    if (
        len({m.get("phase_id") for m in id_map}) != 20
        or len({m.get("official_id") for m in id_map}) != 20
    ):
        raise ContractError("duplicate ID mapping")
    for mapping in id_map:
        phase = mapping.get("phase_id")
        official = mapping.get("official_id")
        if (
            phase not in by_phase
            or official not in by_official
            or by_phase[phase] != by_official[official]
        ):
            raise ContractError("phase ID points to a different source case")


def validate_official_blind(root):
    """Fail closed on unexpected files before a read-only container mount."""
    if (
        root.is_symlink()
        or not root.is_dir()
        or {x.name for x in root.iterdir()} != {"manifest.json", "cases"}
    ):
        raise ContractError("invalid official blind root")
    manifest_path = root / "manifest.json"
    cases = root / "cases"
    if manifest_path.is_symlink() or cases.is_symlink() or not cases.is_dir():
        raise ContractError("invalid official blind entries")
    manifest = json.loads(manifest_path.read_text())
    ids = manifest.get("case_ids", [])
    if (
        len(ids) != 20
        or len(set(ids)) != 20
        or any(not isinstance(x, str) or not ID_PATTERN.fullmatch(x) for x in ids)
        or ids != sorted(ids)
    ):
        raise ContractError("invalid official manifest IDs")
    if {p.name for p in cases.iterdir()} != set(ids):
        raise ContractError("official cases do not match manifest")
    for case_id in ids:
        case = cases / case_id
        if case.is_symlink() or not case.is_dir():
            raise ContractError("invalid official case")
        entries = list(case.iterdir())
        if {p.name for p in entries} != {"task.json", "metrics.csv", "logs.csv", "traces.csv"}:
            raise ContractError("unexpected official case files")
        if any(p.is_symlink() or not p.is_file() for p in entries):
            raise ContractError("invalid official file")
        task = json.loads((case / "task.json").read_text())
        if task.get("case_id") != case_id or task.get("files") != {
            "metrics": "metrics.csv",
            "logs": "logs.csv",
            "traces": "traces.csv",
        }:
            raise ContractError("invalid official task")
        official_to_input(task, case_id)


def score_results(results_path, blind, evaluator, source, python, id_scheme):
    _separate(blind, evaluator)
    validate_official_blind(blind)
    id_map = json.loads((evaluator / "id-map.json").read_text())
    answers = read_jsonl(evaluator / "re2-ob.answers.jsonl")
    if [a.get("case_id") for a in answers] != json.loads((blind / "manifest.json").read_text()).get(
        "case_ids"
    ):
        raise ContractError("answers and manifest mismatch")
    selection = json.loads((evaluator / "selection.json").read_text())
    validate_bindings(id_map, selection, answers)
    rows = translate_predictions(read_jsonl(results_path), id_map, blind, id_scheme)
    with tempfile.TemporaryDirectory(prefix="score-", dir=evaluator) as temp:
        predictions = Path(temp) / "predictions.jsonl"
        predictions.write_text("".join(json.dumps(r) + "\n" for r in rows))
        result = _run_official(
            source,
            python,
            [
                "evaluate",
                "--tasks",
                str(blind.resolve()),
                "--answers",
                str((evaluator / "re2-ob.answers.jsonl").resolve()),
                "--predictions",
                str(predictions.resolve()),
            ],
        )
        return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("export", "score"):
        p = sub.add_parser(name)
        p.add_argument("--blind", required=True, type=Path)
        p.add_argument("--evaluator", required=True, type=Path)
        p.add_argument("--official-source", required=True, type=Path)
        p.add_argument("--official-python", required=True, type=Path)
        p.add_argument("--private" if name == "export" else "--results", required=True, type=Path)
        if name == "score":
            p.add_argument("--id-scheme", choices=("phase1", "official"), required=True)
    task_parser = sub.add_parser("input", help="convert an official task into RCAInput JSON")
    task_parser.add_argument("--blind", required=True, type=Path)
    task_parser.add_argument("--case-id", required=True)
    check = sub.add_parser("validate")
    check.add_argument("--blind", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "input":
        validate_official_blind(args.blind)
        if not ID_PATTERN.fullmatch(args.case_id):
            raise ContractError("invalid official case ID")
        task = json.loads((args.blind / "cases" / args.case_id / "task.json").read_text())
        result = official_to_input(task, args.case_id)
        print(
            json.dumps(
                {
                    "case_id": result.case_id,
                    "candidates": list(result.candidates),
                    "evidence": [{"id": e.id, "content": e.content} for e in result.evidence],
                }
            )
        )
    elif args.command == "validate":
        validate_official_blind(args.blind)
    elif args.command == "export":
        build_export(
            args.private, args.blind, args.evaluator, args.official_source, args.official_python
        )
    else:
        print(
            score_results(
                args.results,
                args.blind,
                args.evaluator,
                args.official_source,
                args.official_python,
                args.id_scheme,
            ),
            end="",
        )


if __name__ == "__main__":
    main()
