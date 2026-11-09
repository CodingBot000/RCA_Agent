import hashlib
import json
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rca_agent.contracts import ContractError
from rca_agent.phase1 import (
    FILES,
    _legacy_select_cases,
    case_id,
    export_blind,
    load_task,
    select_cases,
    validate_blind,
)


def sample_rows():
    return [
        {
            "dataset": "RE2-OB",
            "case": f"re2ob_service{service}_{fault}_{rep}",
            "fault": fault,
            "root_cause_service": f"service{service}",
            "inject_time": 1_700_000_000 + index,
        }
        for index, (fault, service, rep) in enumerate(
            (fault, service, rep)
            for fault in ("cpu", "delay", "disk", "loss", "mem", "socket")
            for service in range(5)
            for rep in range(1, 4)
        )
    ]


def test_private_selection_excludes_public_sample_and_requires_key():
    rows = sample_rows()
    key = b"a" * 32
    selected = select_cases(rows, key)
    assert selected == select_cases(list(reversed(rows)), key)
    assert selected != select_cases(rows, b"b" * 32)
    assert len(selected) == len({r["case"] for r in selected}) == 20
    assert not {r["case"] for r in selected} & {r["case"] for r in _legacy_select_cases(rows)}
    assert sorted(Counter(r["fault"] for r in selected).values()) == [3, 3, 3, 3, 4, 4]
    assert len({r["root_cause_service"] for r in selected}) == 5
    assert case_id(key, "phase-id", selected[0]["case"]) != case_id(
        key, "official-id", selected[0]["case"]
    )
    with pytest.raises(ValueError):
        select_cases(rows, b"short")
    with pytest.raises(ValueError):
        select_cases(rows[:-1], key)


@pytest.fixture
def raw_cases(tmp_path, monkeypatch):
    from rca_agent import phase1

    private = tmp_path / "private"
    raw = private / "raw"
    raw.mkdir(parents=True)
    key = b"k" * 32
    (private / "selection.key").write_bytes(key)
    (private / "selection.key").chmod(0o600)
    rows = sample_rows()
    index = private / "cases.parquet"
    pq.write_table(pa.Table.from_pylist(rows), index)
    monkeypatch.setattr(phase1, "INDEX_SHA256", hashlib.sha256(index.read_bytes()).hexdigest())
    selected = select_cases(rows, key)
    mapping = []
    for item in selected:
        cid = case_id(key, "phase-id", item["case"])
        case = raw / cid
        case.mkdir()
        t = item["inject_time"]
        pq.write_table(
            pa.table({"time": [t - 1, t], "service_cpu": [1.0, 2.0]}), case / "metrics.parquet"
        )
        pq.write_table(
            pa.table(
                {
                    "timestamp": [t],
                    "container_name": ["service"],
                    "message": [
                        "ip 10.2.3.4 uuid 123e4567-e89b-12d3-a456-426614174000 epoch 1700000000 user user@example.com date 2024/01/16 03:38:29"
                    ],
                }
            ),
            case / "logs.parquet",
        )
        pq.write_table(
            pa.table(
                {
                    "time": ["12:34", "12:34"],
                    "traceID": ["trace123", "trace123"],
                    "spanID": ["span-one", "span-two"],
                    "parentSpanID": ["", "span-one"],
                    "startTimeMillis": [t * 1000, t * 1000 + 10],
                    "startTime": [t * 1000000, t * 1000000 + 10000],
                    "duration": [5, 6],
                }
            ),
            case / "traces.parquet",
        )
        (case / "inject_time.txt").write_text(str(t))
        mapping.append(
            {
                "case_id": cid,
                "source_case": item["case"],
                "fault": item["fault"],
                "root_cause_service": item["root_cause_service"],
            }
        )
    (private / "mapping.json").write_text(json.dumps(mapping))
    return raw, mapping


def test_blind_export_hides_original_identifiers_and_preserves_relative_data(raw_cases, tmp_path):
    raw, mapping = raw_cases
    out = tmp_path / "blind"
    export_blind(raw, out)
    validate_blind(out)
    assert len(list(out.iterdir())) == 20
    case_id_value = mapping[0]["case_id"]
    src, dst = raw / case_id_value, out / case_id_value
    task = load_task(dst)
    assert task.case_id == case_id_value and task.candidates == ("service_cpu",)
    before = int((src / "inject_time.txt").read_text())
    after = int((dst / "inject_time.txt").read_text())
    assert after != before and after not in {r["inject_time"] for r in sample_rows()}
    metrics = pq.read_table(dst / "metrics.parquet").to_pydict()
    assert metrics["time"] == [after - 1, after] and metrics["service_cpu"] == [1.0, 2.0]
    traces = pq.read_table(dst / "traces.parquet").to_pydict()
    assert traces["startTime"] == [after * 1000000, after * 1000000 + 10000]
    assert traces["startTimeMillis"] == [after * 1000, after * 1000 + 10]
    assert traces["spanID"][0] == traces["parentSpanID"][1] != "span-one"
    assert traces["traceID"][0] == traces["traceID"][1] != "trace123"
    assert traces["time"] == ["[redacted]", "[redacted]"]
    logs = pq.read_table(dst / "logs.parquet").to_pydict()
    assert logs["timestamp"] == [after]
    assert "10.2.3.4" not in logs["message"][0] and "1700000000" not in logs["message"][0]
    assert "user@example.com" not in logs["message"][0]
    assert "2024/01/16" not in logs["message"][0] and "03:38:29" not in logs["message"][0]
    assert not list(out.rglob("*answers*"))
    assert not list(out.rglob("*.key"))
    with pytest.raises(FileExistsError):
        export_blind(raw, out)


def test_blind_rejects_legacy_and_raw_symlinks(raw_cases, tmp_path):
    raw, mapping = raw_cases
    item = raw / mapping[0]["case_id"] / "logs.parquet"
    item.unlink()
    item.symlink_to(tmp_path / "secret")
    with pytest.raises(ContractError):
        export_blind(raw, tmp_path / "blind")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    for i in range(1, 21):
        (legacy / f"re2-ob-{i:04d}").mkdir()
    with pytest.raises(ContractError):
        validate_blind(legacy)


def test_rejects_traversal_case_in_index():
    rows = sample_rows()
    rows[0]["case"] = "../../answer"
    with pytest.raises(ValueError):
        select_cases(rows, b"k" * 32)


def test_key_permissions_and_tampering(raw_cases):
    from rca_agent.phase1 import validate_private_sample

    raw, _ = raw_cases
    key = raw.parent / "selection.key"
    key.chmod(0o644)
    with pytest.raises(ContractError, match="private regular file"):
        validate_private_sample(raw.parent)
    key.chmod(0o600)
    key.write_bytes(b"z" * 32)
    with pytest.raises(ContractError, match="secret selection"):
        validate_private_sample(raw.parent)


def test_explicit_download_only_selected_twenty(tmp_path, monkeypatch):
    from rca_agent import phase1

    urls = []

    def fake_download(url, path):
        urls.append(url)
        path.write_bytes(b"data")

    monkeypatch.setattr(phase1, "download_file", fake_download)
    monkeypatch.setattr(phase1, "INDEX_SHA256", hashlib.sha256(b"data").hexdigest())
    monkeypatch.setattr(phase1, "read_index", lambda path: sample_rows())
    dest = tmp_path / "evaluation"
    phase1.prepare(dest)
    assert len(urls) == 1 + 20 * len(FILES)
    mapping = json.loads((dest / "private" / "mapping.json").read_text())
    assert len(mapping) == 20 and all(
        phase1.CASE_ID_PATTERN.fullmatch(x["case_id"]) for x in mapping
    )
    key = dest / "private" / "selection.key"
    assert key.stat().st_mode & 0o077 == 0
    second = tmp_path / "reproduction"
    phase1.prepare(second, key)
    assert json.loads((second / "private" / "mapping.json").read_text()) == mapping
    with pytest.raises(FileExistsError):
        phase1.prepare(dest)


def test_validate_blind_rejects_answer_files(raw_cases, tmp_path):
    raw, mapping = raw_cases
    out = tmp_path / "blind"
    export_blind(raw, out)
    (out / mapping[0]["case_id"] / "mapping.json").write_text("secret")
    with pytest.raises(ContractError):
        validate_blind(out)


def test_rejects_tampered_index_before_export(raw_cases, tmp_path):
    raw, _ = raw_cases
    index = raw.parent / "cases.parquet"
    index.write_bytes(b"tampered")
    with pytest.raises(ContractError, match="pinned dataset revision"):
        export_blind(raw, tmp_path / "blind")
    assert not (tmp_path / "blind").exists()


def test_public_manifest_is_explicitly_historical():
    from rca_agent.phase1 import CASE_ID_PATTERN

    path = Path(__file__).parents[1] / "manifests/re2-ob-20.json"
    manifest = json.loads(path.read_text())
    assert manifest["status"] == "PUBLIC_LEGACY_NOT_BLIND"
    assert manifest["seed"] == 42
    assert all(not CASE_ID_PATTERN.fullmatch(x) for x in manifest["case_ids"])
