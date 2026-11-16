"""Evaluator-only real telemetry preparation and original-case separation proof."""

import hashlib
import json
import os
import re
import secrets
import stat
import uuid
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq

from rca_agent.blind_privacy import transform_parquet
from rca_agent.contracts import RCAInput
from rca_agent.phase1 import CASE_ID_PATTERN, CASE_PATTERN, INDEX_SHA256, REVISION


def private_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("unsafe evaluator metadata")
    return json.loads(path.read_text())


def source_identities(mapping, *, legacy=False):
    if not isinstance(mapping, list) or len(mapping) != 20:
        raise ValueError("evaluation mapping must contain 20 cases")
    result = {}
    for item in mapping:
        if (
            not isinstance(item, dict)
            or not (
                CASE_ID_PATTERN.fullmatch(item.get("case_id", ""))
                or (legacy and re.fullmatch(r"re2-ob-[0-9]{4}", item.get("case_id", "")))
            )
            or not CASE_PATTERN.fullmatch(item.get("source_case", ""))
        ):
            raise ValueError("invalid evaluator identity")
        if item["case_id"] in result:
            raise ValueError("duplicate evaluator ID")
        result[item["case_id"]] = item["source_case"]
    if len(set(result.values())) != 20:
        raise ValueError("duplicate original case")
    return result


def verify_mapping(mapping_path, id_map_path, blind_cases):
    identities = source_identities(private_json(mapping_path))
    pairs = private_json(id_map_path)
    if (
        len(pairs) != 20
        or {x["phase_id"] for x in pairs} != set(identities)
        or {x["official_id"] for x in pairs}
        != {p.name for p in Path(blind_cases).iterdir() if p.is_dir()}
    ):
        raise ValueError("evaluation binding mismatch")
    return set(identities.values())


def prepare(source_private, evaluation_mapping, official_ids, evaluation_blind, output):
    source_private = Path(source_private)
    output = Path(output)
    excluded = verify_mapping(evaluation_mapping, official_ids, evaluation_blind)
    # Revision proof is evaluator-side and is never copied into a model container.
    if hashlib.sha256((source_private / "cases.parquet").read_bytes()).hexdigest() != INDEX_SHA256:
        raise ValueError("dataset revision mismatch")
    available = source_identities(private_json(source_private / "mapping.json"), legacy=True)
    candidates = [
        (opaque, source) for opaque, source in available.items() if source not in excluded
    ]
    if not candidates:
        raise ValueError("no independent original source case")
    if output.exists() or output.is_symlink():
        raise ValueError("destination must be new")
    output.mkdir(mode=0o700, parents=True)
    private = output / "private"
    private.mkdir(mode=0o700)
    public = output / "cases"
    public.mkdir(mode=0o700)
    opaque, source = min(candidates, key=lambda item: item[0])
    original = source_private / "raw" / opaque
    paths = {kind: original / (kind + ".parquet") for kind in ("metrics", "logs", "traces")}
    for path in paths.values():
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError("invalid telemetry source")
    case_id = "re2-ob-v2-" + uuid.uuid4().hex
    target = public / case_id
    target.mkdir(mode=0o700)
    names = pq.read_schema(paths["metrics"]).names
    log_services = set(
        pq.read_table(paths["logs"], columns=["container_name"])["container_name"].to_pylist()
    )
    trace_services = set(
        pq.read_table(paths["traces"], columns=["serviceName"])["serviceName"].to_pylist()
    )
    shared = log_services & trace_services
    indicators = [n for n in names if n != "time"]
    common = [n for n in indicators if n.split("_", 1)[0] in shared]
    if not common:
        raise ValueError("no service with three telemetry signals")
    first = common[0]
    indicators = [first] + [n for n in indicators if n != first]
    minimum = pc.min(pq.read_table(paths["metrics"], columns=["time"])["time"]).as_py()
    shift = 1600000000 - int(minimum)
    key = secrets.token_bytes(32)
    for kind, path in paths.items():
        transform_parquet(path, target / (kind + ".parquet"), kind, shift, key, source)
    task = {
        "case_id": case_id,
        "candidates": indicators,
        "evidence": [
            {
                "id": "development-context",
                "content": "Three telemetry signals available; no evaluator labels provided.",
            }
        ],
    }
    RCAInput.parse(task)
    (target / "task.json").write_text(json.dumps(task))
    manifest = {"split": "development", "case_id": case_id, "directory": case_id}
    (public / "manifest.json").write_text(json.dumps(manifest))
    proof = {
        "version": 1,
        "revision": REVISION,
        "source_case": source,
        "case_id": case_id,
        "evaluation_mapping_digest": hashlib.sha256(
            Path(evaluation_mapping).read_bytes()
        ).hexdigest(),
        "identity_disjoint": source not in excluded,
        "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in target.iterdir()},
    }
    (private / "provenance.json").write_text(json.dumps(proof))
    (private / "provenance.json").chmod(0o600)
    for p in target.iterdir():
        p.chmod(0o600)
    (public / "manifest.json").chmod(0o600)
    return {
        "status": "PREPARED",
        "original_case_disjoint": True,
        "signals": ["metrics", "logs", "traces"],
    }


def verify_provenance(case, proof_path, evaluation_mapping):
    proof_info = os.lstat(proof_path)
    parent_info = os.lstat(Path(proof_path).parent)
    if (
        proof_info.st_uid != os.getuid()
        or stat.S_IMODE(proof_info.st_mode) != 0o600
        or parent_info.st_uid != os.getuid()
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise ValueError("evaluator-owned private provenance required")
    proof = private_json(proof_path)
    excluded = set(source_identities(private_json(evaluation_mapping)).values())
    task = private_json(Path(case) / "task.json")
    if (
        proof["version"] != 1
        or proof["revision"] != REVISION
        or proof["case_id"] != task["case_id"]
        or proof["source_case"] in excluded
        or not proof["identity_disjoint"]
        or proof["evaluation_mapping_digest"]
        != hashlib.sha256(Path(evaluation_mapping).read_bytes()).hexdigest()
        or proof["files"]
        != {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(case).iterdir()}
    ):
        raise ValueError("development provenance mismatch")
    return True


def main():
    import argparse

    p = argparse.ArgumentParser()
    for name in (
        "source-private",
        "evaluation-mapping",
        "official-ids",
        "evaluation-blind",
        "output",
    ):
        p.add_argument("--" + name, type=Path, required=True)
    args = p.parse_args()
    try:
        result = prepare(
            args.source_private,
            args.evaluation_mapping,
            args.official_ids,
            args.evaluation_blind,
            args.output,
        )
    except Exception:  # noqa: BLE001 - evaluator identities and labels never enter public errors
        print(json.dumps({"status": "BLOCKED", "reason": "development preparation failed"}))
        raise SystemExit(2) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
