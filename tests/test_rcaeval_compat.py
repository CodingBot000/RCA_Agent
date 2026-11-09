import json
from pathlib import Path

import pytest

from rca_agent.contracts import ContractError
from rca_agent.rcaeval_compat import (
    _separate,
    official_to_input,
    translate_predictions,
    validate_bindings,
    validate_official_blind,
)


def _task(case_id):
    return {
        "case_id": case_id,
        "candidate_services": ["api", "db"],
        "candidate_indicators": ["api_latency", "db_cpu"],
        "files": {"metrics": "metrics.csv", "logs": "logs.csv", "traces": "traces.csv"},
    }


@pytest.fixture
def fixture_data(tmp_path):
    blind = tmp_path / "blind"
    cases = blind / "cases"
    cases.mkdir(parents=True)
    ids = [f"re2-ob-{i:04d}" for i in range(1, 21)]
    (blind / "manifest.json").write_text(json.dumps({"case_ids": ids}))
    for oid in ids:
        dest = cases / oid
        dest.mkdir()
        (dest / "task.json").write_text(json.dumps(_task(oid)))
        for file in ("metrics.csv", "logs.csv", "traces.csv"):
            (dest / file).write_text("data")
    selection = [
        {
            "case_id": cid,
            "source_case": f"re2ob_svc_cpu_{i}",
            "root_cause_service": "svc",
            "fault": "cpu",
        }
        for i, cid in enumerate(ids, 1)
    ]
    answers = [{"case_id": ids[-i], "source": f"svc_cpu/{i}"} for i in range(1, 21)]
    id_map = [
        {"phase_id": row["case_id"], "official_id": answers[i]["case_id"]}
        for i, row in enumerate(selection)
    ]
    return blind, selection, answers, id_map


def _result(phase_id, candidate="api_latency", evidence="telemetry-metadata"):
    return {
        "case_id": phase_id,
        "ranking": [{"candidate": candidate, "explanation": "example", "evidence_ids": [evidence]}],
        "tool_calls": [],
    }


def test_official_candidates_include_services_and_normalized_indicators():
    task = official_to_input(_task("re2-ob-0020"), "re2-ob-0001")
    assert task.candidates == ("api", "db", "api_latency", "db_cpu")


def test_id_binding_rejects_swapped_mapping_even_when_sets_match(fixture_data):
    _, selection, answers, mapping = fixture_data
    validate_bindings(mapping, selection, answers)
    mapping[0]["official_id"], mapping[1]["official_id"] = (
        mapping[1]["official_id"],
        mapping[0]["official_id"],
    )
    with pytest.raises(ContractError, match="different source"):
        validate_bindings(mapping, selection, answers)


def test_translation_and_invalid_predictions(fixture_data):
    blind, _, _, mapping = fixture_data
    row = _result("re2-ob-0001")
    assert translate_predictions([row], mapping, blind, "phase1") == [
        {"case_id": "re2-ob-0020", "ranks": ["api_latency"]}
    ]
    for invalid in (
        [_result("re2-ob-9999")],
        [row, row],
        [_result("re2-ob-0001", "api_latency-90")],
        [_result("re2-ob-0001", evidence="invented")],
    ):
        with pytest.raises(ContractError):
            translate_predictions(invalid, mapping, blind, "phase1")


def test_official_blind_rejects_extra_secret_and_symlinks(fixture_data, tmp_path):
    blind, _, _, _ = fixture_data
    validate_official_blind(blind)
    secret = blind / "cases" / "re2-ob-0001" / "answers.jsonl"
    secret.write_text("secret")
    with pytest.raises(ContractError):
        validate_official_blind(blind)
    secret.unlink()
    target = blind / "cases" / "re2-ob-0001" / "metrics.csv"
    target.unlink()
    target.symlink_to(tmp_path / "secret")
    with pytest.raises(ContractError):
        validate_official_blind(blind)


def test_blind_and_evaluator_must_be_disjoint(tmp_path):
    with pytest.raises(ContractError):
        _separate(tmp_path / "private", tmp_path / "private" / "blind", tmp_path / "evaluation")


def test_export_and_score_end_to_end_without_network(tmp_path, monkeypatch):
    from rca_agent import rcaeval_compat as compat
    from rca_agent.phase1 import FILES

    private = tmp_path / "private"
    private.mkdir()
    rows = [
        {"case": f"re2ob_svc_cpu_{i}", "fault": "cpu", "root_cause_service": "svc"}
        for i in range(1, 21)
    ]
    selection = [
        {
            "case_id": f"re2-ob-{i:04d}",
            "source_case": r["case"],
            "fault": r["fault"],
            "root_cause_service": r["root_cause_service"],
        }
        for i, r in enumerate(rows, 1)
    ]
    (private / "mapping.json").write_text(json.dumps(selection))
    (private / "cases.parquet").write_text("mock index")
    for row in selection:
        case = private / "raw" / row["case_id"]
        case.mkdir(parents=True)
        for name in FILES:
            (case / name).write_text("123")
    import hashlib

    monkeypatch.setattr(compat, "INDEX_SHA256", hashlib.sha256(b"mock index").hexdigest())
    monkeypatch.setattr(compat, "_official_check", lambda source: None)
    monkeypatch.setattr(compat, "read_index", lambda path: rows)
    monkeypatch.setattr(compat, "select_cases", lambda rows: rows)
    monkeypatch.setattr(compat, "_csv_from_parquet", lambda src, dst: dst.write_text("mock csv"))

    def official_stub(source, python, args):
        if args[0] == "export":
            out = Path(args[args.index("--out") + 1])
            root = out / "re2-ob"
            root.joinpath("cases").mkdir(parents=True)
            answers = []
            for i, row in enumerate(reversed(selection), 1):
                oid = f"re2-ob-{i:04d}"
                d = root / "cases" / oid
                d.mkdir()
                (d / "task.json").write_text(json.dumps(_task(oid)))
                for file in ("metrics.csv", "logs.csv", "traces.csv"):
                    (d / file).write_text("mock csv")
                answers.append(
                    {
                        "case_id": oid,
                        "source": compat._source_path(row),
                        "service": "svc",
                        "fault": "cpu",
                    }
                )
            (root / "manifest.json").write_text(
                json.dumps({"case_ids": [a["case_id"] for a in answers]})
            )
            (out / "re2-ob.answers.jsonl").write_text(
                "".join(json.dumps(a) + "\n" for a in answers)
            )
            return None
        predictions = Path(args[args.index("--predictions") + 1]).read_text()
        assert json.loads(predictions)["case_id"] == "re2-ob-0020"
        return type("Completed", (), {"stdout": "official scoring reached"})()

    monkeypatch.setattr(compat, "_run_official", official_stub)
    blind = tmp_path / "blind"
    evaluator = tmp_path / "evaluator"
    compat.build_export(private, blind, evaluator, tmp_path / "source", tmp_path / "python")
    compat.validate_official_blind(blind)
    assert not list(blind.rglob("*answers*"))
    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps(_result("re2-ob-0001")) + "\n")
    assert (
        compat.score_results(
            results, blind, evaluator, tmp_path / "source", tmp_path / "python", "phase1"
        )
        == "official scoring reached"
    )
    mapping = json.loads((evaluator / "id-map.json").read_text())
    mapping[0]["official_id"], mapping[1]["official_id"] = (
        mapping[1]["official_id"],
        mapping[0]["official_id"],
    )
    (evaluator / "id-map.json").write_text(json.dumps(mapping))
    with pytest.raises(ContractError, match="different source"):
        compat.score_results(
            results, blind, evaluator, tmp_path / "source", tmp_path / "python", "phase1"
        )


def test_score_cli_passes_explicit_id_scheme(monkeypatch, capsys):
    import sys

    from rca_agent import rcaeval_compat as compat

    seen = []
    monkeypatch.setattr(compat, "score_results", lambda *args: seen.append(args[-1]) or "ok")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tool",
            "score",
            "--blind",
            "b",
            "--evaluator",
            "e",
            "--official-source",
            "s",
            "--official-python",
            "p",
            "--results",
            "r",
            "--id-scheme",
            "official",
        ],
    )
    compat.main()
    assert seen == ["official"]
    assert capsys.readouterr().out == "ok"


def test_official_ids_are_explicitly_distinct_from_phase_ids(fixture_data):
    blind, _, _, mapping = fixture_data
    assert (
        translate_predictions([_result("re2-ob-0001")], mapping, blind, "official")[0]["case_id"]
        == "re2-ob-0001"
    )
    assert (
        translate_predictions([_result("re2-ob-0001")], mapping, blind, "phase1")[0]["case_id"]
        == "re2-ob-0020"
    )
    with pytest.raises(ContractError):
        translate_predictions([_result("re2-ob-0001")], mapping, blind, "ambiguous")


def test_export_rejects_changed_index_before_official_execution(tmp_path, monkeypatch):
    from rca_agent import rcaeval_compat as compat

    private = tmp_path / "private"
    private.mkdir()
    (private / "cases.parquet").write_bytes(b"not the pinned index")
    (private / "mapping.json").write_text("[]")
    monkeypatch.setattr(compat, "_official_check", lambda source: None)
    with pytest.raises(ContractError, match="pinned dataset revision"):
        compat.build_export(
            private,
            tmp_path / "blind",
            tmp_path / "evaluator",
            tmp_path / "source",
            tmp_path / "python",
        )
    assert not (tmp_path / "blind").exists()
