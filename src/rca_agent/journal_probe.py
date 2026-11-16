"""Manual Docker journal restart tests; metadata survives Proxy process destruction."""

import json
import uuid

from rca_agent.docker_integration import run_session
from rca_agent.docker_probe import docker
from rca_agent.mock_egress import IMAGE, MockEgress


def summary(volume, recover=False):
    name = "rca-journal-recover-" + uuid.uuid4().hex
    code = (
        (
            "from rca_agent.request_journal import RequestJournal;import json;"
            "j=RequestJournal('/journal/requests.sqlite');print(json.dumps(j.summary()));j.close()"
        )
        if recover
        else (
            "import sqlite3,json;db=sqlite3.connect('file:/journal/requests.sqlite?mode=ro',uri=True);"
            "d=dict(db.execute('SELECT state,COUNT(*) FROM requests GROUP BY state'));"
            "print(json.dumps({'completed':d.get('COMPLETED',0),'manual_review':d.get('MANUAL_REVIEW',0),'sent':d.get('SENT',0)}))"
        )
    )
    try:
        result = docker(
            "run",
            "--name",
            name,
            "--pull=never",
            "--network",
            "none",
            "--read-only",
            "--user",
            "10005:10002",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--log-driver",
            "none",
            "--mount",
            f"type=volume,src={volume},dst=/journal",
            IMAGE,
            "python",
            "-c",
            code,
        )
        return json.loads(result)
    finally:
        docker("rm", "--force", name)


def run_probe():
    outcomes = []
    for fault in ("none", "journal_before_send", "journal_after_response"):
        volume = "rca-journal-" + uuid.uuid4().hex
        docker("volume", "create", volume)
        try:
            with MockEgress(fault, journal_volume=volume) as first:
                initial = run_session("none", extension=first)
                if fault == "none" and initial["status"] != "PASS":
                    raise ValueError("first execution failed")
                if fault != "none" and initial["status"] == "PASS":
                    raise ValueError("crash did not interrupt")
            recovered = summary(volume, True)
            if (fault == "none" and recovered["completed"] != 2) or (
                fault != "none" and recovered["manual_review"] != 1
            ):
                raise ValueError("recovery state incorrect")
            with MockEgress("none", journal_volume=volume) as second:
                repeated = run_session("none", extension=second)
                audit = second.audit()
                if repeated["status"] == "PASS" or audit["model_calls"] != 0:
                    raise ValueError("request re-executed after restart")
            outcomes.append(
                {
                    "fault": fault,
                    "first_status": initial["status"],
                    "recovered": recovered,
                    "restarted_model_http_calls": audit["model_calls"],
                }
            )
        finally:
            docker("volume", "rm", volume)
    return {"status": "PASS", "scenarios": outcomes, "live": "LIVE_BLOCKED"}


def main():
    try:
        result = run_probe()
    except Exception:  # noqa: BLE001 - no journal fingerprints or payloads in errors
        print(json.dumps({"status": "BLOCKED", "live": "LIVE_BLOCKED"}))
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
