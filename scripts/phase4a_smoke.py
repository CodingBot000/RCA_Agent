"""Evaluator-free, read-only smoke of observations on supplied blind cases."""

import argparse
from collections import Counter
from pathlib import Path

from rca_agent.graph import CaseContext, Limits, ObservationLimits, ScriptedReasoner, run_agent
from rca_agent.phase1 import CASE_ID_PATTERN
from rca_agent.telemetry import EvidenceRegistry, TelemetryTools


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blind", required=True, type=Path)
    args = parser.parse_args()
    counts = Counter()
    cases = sorted(p for p in args.blind.iterdir() if CASE_ID_PATTERN.fullmatch(p.name))
    for case in cases:
        try:
            tools = TelemetryTools(args.blind, case.name, EvidenceRegistry(case.name))
            context = CaseContext.from_tools(tools)
            indicator = next(x for x in context.candidates if x.endswith("_cpu"))
            service = indicator.split("_", 1)[0]
            reasoner = ScriptedReasoner(
                [
                    {
                        "kind": "metrics",
                        "service": service,
                        "start": context.start,
                        "end": context.end,
                        "limit": 1,
                        "indicator": indicator,
                    }
                ],
                [
                    {
                        "candidate": indicator,
                        "explanation": "Smoke test: observed a metric value, not a causal conclusion.",
                        "evidence_ids": ["$latest"],
                        "claims": [
                            {
                                "evidence_id": "$latest",
                                "record_index": 0,
                                "field": indicator,
                                "value": "$observed",
                            }
                        ],
                    }
                ],
            )
            state = run_agent(tools, reasoner, Limits(1, 1), ObservationLimits(1, 4096))
            if state["result"] is not None and state["history"][0]["observation"].records:
                counts["success"] += 1
            else:
                counts["failed"] += 1
        except (OSError, ValueError, StopIteration, KeyError, TypeError):
            counts["failed"] += 1
    print(
        f"cases={len(cases)} success={counts['success']} failed={counts['failed']} unverified={max(0, 20 - len(cases))}"
    )
    if len(cases) != 20 or counts["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
