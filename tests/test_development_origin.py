import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rca_agent import development as dev
from rca_agent.phase1 import REVISION


def mapping(start):
    return [
        {
            "case_id": "re2-ob-v2-" + f"{n:032x}",
            "source_case": f"re2ob_api_cpu_{n}",
            "root_cause_service": "DO_NOT_EXPORT_GT",
            "fault": "cpu",
        }
        for n in range(start, start + 20)
    ]


def test_renamed_anonymous_case_still_rejected_if_original_overlaps(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    case = tmp_path / "case"
    case.mkdir()
    case_id = "re2-ob-v2-" + "f" * 32
    (case / "task.json").write_text(json.dumps({"case_id": case_id}))
    (case / "metrics.parquet").write_bytes(b"synthetic-only")
    evaluation = private / "evaluation.json"
    evaluation.write_text(json.dumps(mapping(1)))
    proof = {
        "version": 1,
        "revision": REVISION,
        "case_id": case_id,
        "source_case": mapping(1)[0]["source_case"],
        "identity_disjoint": True,
        "evaluation_mapping_digest": hashlib.sha256(evaluation.read_bytes()).hexdigest(),
        "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in case.iterdir()},
    }
    path = private / "proof.json"
    path.write_text(json.dumps(proof))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        dev.verify_provenance(case, path, evaluation)
    proof["source_case"] = "re2ob_api_cpu_999"
    path.write_text(json.dumps(proof))
    assert dev.verify_provenance(case, path, evaluation)
    (case / "metrics.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError):
        dev.verify_provenance(case, path, evaluation)


def test_real_preparation_interface_uses_only_disjoint_source_and_three_signals(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    evaluation = tmp_path / "evaluation"
    evaluation.mkdir()
    blind = tmp_path / "evaluation-blind"
    blind.mkdir()
    rows = mapping(1)
    emap = evaluation / "mapping.json"
    emap.write_text(json.dumps(rows))
    pairs = []
    for n, item in enumerate(rows, 200):
        official = "re2-ob-v2-" + f"{n:032x}"
        (blind / official).mkdir()
        pairs.append({"phase_id": item["case_id"], "official_id": official})
    ids = evaluation / "id-map.json"
    ids.write_text(json.dumps(pairs))
    available = mapping(100)
    (source / "mapping.json").write_text(json.dumps(available))
    (source / "cases.parquet").write_bytes(b"pinned-index-fixture")
    monkeypatch.setattr(
        dev, "INDEX_SHA256", hashlib.sha256((source / "cases.parquet").read_bytes()).hexdigest()
    )
    raw = source / "raw" / available[0]["case_id"]
    raw.mkdir(parents=True)
    pq.write_table(pa.table({"time": [10, 11], "api_cpu": [1.0, 2.0]}), raw / "metrics.parquet")
    pq.write_table(
        pa.table(
            {"timestamp": [10], "container_name": ["api"], "message": ["test@example.invalid"]}
        ),
        raw / "logs.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "time": ["original"],
                "traceID": ["abcdef0123456789"],
                "spanID": ["0123456789abcdef"],
                "parentSpanID": ["0"],
                "startTime": [10000000],
                "startTimeMillis": [10000],
                "serviceName": ["api"],
                "duration": [1],
            }
        ),
        raw / "traces.parquet",
    )
    out = tmp_path / "development"
    result = dev.prepare(source, emap, ids, blind, out)
    manifest = json.loads((out / "cases/manifest.json").read_text())
    case = out / "cases" / manifest["case_id"]
    assert result["original_case_disjoint"] and dev.verify_provenance(
        case, out / "private/provenance.json", emap
    )
    assert {p.name for p in case.iterdir()} == {
        "task.json",
        "metrics.parquet",
        "logs.parquet",
        "traces.parquet",
    }
    task = (case / "task.json").read_text()
    assert "DO_NOT_EXPORT_GT" not in task and "re2ob_api_cpu_100" not in task
    assert "[EMAIL]" in pq.read_table(case / "logs.parquet")["message"].to_pylist()[0]
