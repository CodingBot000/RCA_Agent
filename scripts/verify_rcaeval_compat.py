"""Manual evaluator-side integration check; predictions are synthetic, not agent results."""

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

from rca_agent.rcaeval_compat import (
    _official_check,
    score_results,
    validate_bindings,
    validate_official_blind,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("blind", "evaluator", "official-source", "official-python"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    args = parser.parse_args()
    _official_check(args.official_source)
    sys.path.insert(0, str(args.official_source.resolve()))
    from RCAEval.agent.predictions import evaluate, load_answers
    from RCAEval.agent.toolkit import CaseToolkit

    validate_official_blind(args.blind)
    maps = json.loads((args.evaluator / "id-map.json").read_text())
    selected = json.loads((args.evaluator / "selection.json").read_text())
    answers = load_answers(str(args.evaluator / "re2-ob.answers.jsonl"))
    validate_bindings(maps, selected, answers)
    by_id = {a["case_id"]: a for a in answers}
    predictions = {}
    phase_rows, official_rows = [], []
    indicator_predictions, indicator_rows = {}, []
    for index, pair in enumerate(maps):
        official_id = pair["official_id"]
        task_dir = args.blind / "cases" / official_id
        task = json.loads((task_dir / "task.json").read_text())
        answer = by_id[official_id]
        correct = answer["service"]
        incorrect = [x for x in task["candidate_services"] if x != correct]
        if correct not in task["candidate_services"] or len(incorrect) < 3:
            raise ValueError("not enough services for synthetic scorer check")
        ranks = (
            [correct]
            if index < 10
            else ([incorrect[0], incorrect[1], correct] if index < 15 else incorrect[:3])
        )
        toolkit = CaseToolkit(str(task_dir))
        if (
            toolkit.describe_case()["case_id"] != official_id
            or not toolkit.list_metrics()["metrics_by_service"]
        ):
            raise ValueError("official toolkit rejected exported case")
        prediction = {"case_id": official_id, "ranks": ranks}
        predictions[official_id] = prediction
        ranking = [
            {
                "candidate": rank,
                "explanation": "synthetic test",
                "evidence_ids": ["telemetry-metadata"],
            }
            for rank in ranks
        ]
        phase_rows.append({"case_id": pair["phase_id"], "ranking": ranking, "tool_calls": []})
        official_rows.append({"case_id": official_id, "ranking": ranking, "tool_calls": []})
        indicator = f"{correct}_{answer['indicator']}"
        if indicator not in task["candidate_indicators"]:
            raise ValueError("correct indicator absent from candidate vocabulary")
        indicator_predictions[official_id] = {"case_id": official_id, "ranks": [indicator]}
        indicator_rows.append(
            {
                "case_id": official_id,
                "ranking": [
                    {
                        "candidate": indicator,
                        "explanation": "synthetic test",
                        "evidence_ids": ["telemetry-metadata"],
                    }
                ],
                "tool_calls": [],
            }
        )
    reference, _ = evaluate(answers, predictions)
    with tempfile.TemporaryDirectory(prefix="compat-check-", dir=args.evaluator) as temp:
        for scheme, rows in (("phase1", phase_rows), ("official", official_rows)):
            path = Path(temp) / f"{scheme}.jsonl"
            path.write_text("".join(json.dumps(x) + "\n" for x in rows))
            report = score_results(
                path, args.blind, args.evaluator, args.official_source, args.official_python, scheme
            )
            for metric in ("AC@1", "AC@3"):
                line = next(x for x in report.splitlines() if x.startswith(metric + ":"))
                values = re.findall(r"\d+\.\d+", line)
                expected = [
                    f"{reference['service'][metric]:.2f}",
                    f"{reference['indicator'][metric]:.2f}",
                ]
                if values != expected:
                    raise ValueError(f"{scheme} scorer mismatch for {metric}")
        indicator_ref, _ = evaluate(answers, indicator_predictions)
        phase_by_official = {item["official_id"]: item["phase_id"] for item in maps}
        phase_indicators = [
            dict(row, case_id=phase_by_official[row["case_id"]]) for row in indicator_rows
        ]
        for scheme, rows in (("phase1", phase_indicators), ("official", indicator_rows)):
            path = Path(temp) / f"indicators-{scheme}.jsonl"
            path.write_text("".join(json.dumps(x) + "\n" for x in rows))
            indicator_report = score_results(
                path, args.blind, args.evaluator, args.official_source, args.official_python, scheme
            )
            for metric in ("AC@1", "AC@3"):
                line = next(x for x in indicator_report.splitlines() if x.startswith(metric + ":"))
                values = re.findall(r"\d+\.\d+", line)
                expected = [
                    f"{indicator_ref['service'][metric]:.2f}",
                    f"{indicator_ref['indicator'][metric]:.2f}",
                ]
                if values != expected:
                    raise ValueError(f"{scheme} indicator scorer mismatch for {metric}")
        if indicator_ref["indicator"]["AC@1"] != 1.0:
            raise ValueError("indicator positive control did not score correctly")
    print("PASS: 20 ID bindings and official toolkit cases")
    print(
        "PASS: phase1 and official ID schemes match official Top-1/Top-3 service and indicator scores"
    )
    print("Synthetic oracle-derived predictions; not a measurement of RCA accuracy.")


if __name__ == "__main__":
    main()
