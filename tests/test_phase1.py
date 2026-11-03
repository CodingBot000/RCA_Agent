import json
import sys
import types
from collections import Counter
from pathlib import Path

import pytest

from rca_agent.contracts import ContractError
from rca_agent.phase1 import FILES, REVISION, export_blind, load_task, select_cases


def sample_rows():
    return [
        {
            "dataset": "RE2-OB",
            "case": f"re2ob_service{service}_{fault}_{rep}",
            "fault": fault,
            "root_cause_service": f"service{service}",
        }
        for fault in ("cpu", "delay", "disk", "loss", "mem", "socket")
        for service in range(5)
        for rep in range(1, 4)
    ]


def test_selection_is_stable_balanced_and_sensitive_to_invalid_input():
    rows = sample_rows()
    first = select_cases(rows)
    assert first == select_cases(list(reversed(rows)))
    assert len(first) == len({r["case"] for r in first}) == 20
    assert sorted(Counter(r["fault"] for r in first).values()) == [3, 3, 3, 3, 4, 4]
    assert sorted(Counter(r["root_cause_service"] for r in first).values()) == [4] * 5
    with pytest.raises(ValueError):
        select_cases(rows[:-1])


def test_manifest_matches_selection_configuration():
    manifest = json.loads((Path(__file__).parents[1] / "manifests/re2-ob-20.json").read_text())
    assert manifest["source_revision"] == REVISION
    assert manifest["seed"] == 42
    assert manifest["count"] == len(set(manifest["case_ids"])) == 20
    assert manifest["case_ids"] == [f"re2-ob-{n:04d}" for n in range(1, 21)]


@pytest.fixture
def raw_cases(tmp_path, monkeypatch):
    package = types.ModuleType("pyarrow")
    package.__path__ = []
    parquet = types.ModuleType("pyarrow.parquet")
    parquet.read_schema = lambda path: types.SimpleNamespace(names=["time", "service_cpu"])
    monkeypatch.setitem(sys.modules, "pyarrow", package)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", parquet)
    root = tmp_path / "raw"
    root.mkdir()
    for n in range(1, 21):
        case = root / f"re2-ob-{n:04d}"
        case.mkdir()
        for name in FILES:
            (case / name).write_bytes(b"123" if name == "inject_time.txt" else b"telemetry")
    return root


def test_blind_export_and_existing_contract(raw_cases, tmp_path):
    out = tmp_path / "blind"
    export_blind(raw_cases, out)
    assert len(list(out.iterdir())) == 20
    task = load_task(out / "re2-ob-0001")
    assert task.case_id == "re2-ob-0001"
    assert task.candidates == ("service_cpu",)
    assert {p.name for p in (out / task.case_id).iterdir()} == {*FILES, "task.json"}
    assert not list(out.rglob("*answers*"))
    with pytest.raises(FileExistsError):
        export_blind(raw_cases, out)


def test_blind_export_rejects_symlinks(raw_cases, tmp_path):
    item = raw_cases / "re2-ob-0001" / "logs.parquet"
    item.unlink()
    item.symlink_to(tmp_path / "secret")
    with pytest.raises(ContractError):
        export_blind(raw_cases, tmp_path / "blind")


def test_rejects_traversal_case_in_index():
    rows = sample_rows()
    rows[0]["case"] = "../../answer"
    with pytest.raises(ValueError):
        select_cases(rows)


def test_explicit_download_only_selected_twenty(tmp_path, monkeypatch):
    from rca_agent import phase1

    urls = []

    def fake_download(url, path):
        urls.append(url)
        path.write_bytes(b"data")

    monkeypatch.setattr(phase1, "download_file", fake_download)
    monkeypatch.setattr(phase1, "read_index", lambda path: sample_rows())
    dest = tmp_path / "evaluation"
    phase1.prepare(dest)
    assert len(urls) == 1 + 20 * len(FILES)
    assert all(REVISION in url for url in urls)
    mapping = json.loads((dest / "private" / "mapping.json").read_text())
    assert len(mapping) == 20
    assert len({x["source_case"] for x in mapping}) == 20
    assert (dest / "private" / "cases.parquet").is_file()
    with pytest.raises(FileExistsError):
        phase1.prepare(dest)


def test_validate_blind_rejects_answer_files(raw_cases, tmp_path):
    from rca_agent.phase1 import validate_blind

    out = tmp_path / "blind"
    export_blind(raw_cases, out)
    validate_blind(out)
    (out / "re2-ob-0001" / "mapping.json").write_text("secret")
    with pytest.raises(ContractError):
        validate_blind(out)
