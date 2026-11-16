import subprocess
import sys

import pytest

from rca_agent.model_broker import PreparedRequest
from rca_agent.request_journal import JournalBlocked, RequestJournal


def prepared(value=1):
    return PreparedRequest.from_payload(
        {"observation": value}, model="test-model", max_output_tokens=100
    )


def test_commit_and_restart_deny_duplicate_model_side_effect(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "requests.sqlite"
    with RequestJournal(path) as journal:
        digest = journal.sent(prepared(), "a" * 32, 10)
        journal.completed(digest, {"input_tokens": 10, "output_tokens": 20, "cost_usd": "0.1"})
        assert journal.summary()["completed"] == 1
        with pytest.raises(JournalBlocked):
            journal.sent(prepared(), "b" * 32, 10)
    with RequestJournal(path) as recovered:
        with pytest.raises(JournalBlocked):
            recovered.sent(prepared(), "c" * 32, 11)
        assert recovered.summary()["completed"] == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_process_death_after_sent_recovers_manual_review(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "requests.sqlite"
    code = """import os,sys
from rca_agent.request_journal import RequestJournal
from rca_agent.model_broker import PreparedRequest
j=RequestJournal(sys.argv[1]);j.sent(PreparedRequest.from_payload({'observation':1},model='test-model',max_output_tokens=100),'a'*32,10)
os._exit(7)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)], capture_output=True, check=False
    )
    assert result.returncode == 7 and not result.stdout
    with RequestJournal(path) as recovered:
        assert recovered.summary() == {"completed": 0, "sent": 0, "manual_review": 1}
        with pytest.raises(JournalBlocked):
            recovered.sent(prepared(), "d" * 32, 10)


def test_uncertain_usage_and_excess_are_never_reapproved(tmp_path):
    tmp_path.chmod(0o700)
    with RequestJournal(tmp_path / "requests.sqlite") as journal:
        first = journal.sent(prepared(), "a" * 32, 10)
        journal.uncertain(first)
        second = journal.sent(prepared(2), "b" * 32, 10)
        journal.completed(second, {"input_tokens": 11, "output_tokens": 101, "cost_usd": "0.2"})
        assert journal.summary()["manual_review"] == 2
        with pytest.raises(JournalBlocked):
            journal.sent(prepared(), "c" * 32, 10)


def test_journal_symlink_and_concurrent_owner_rejected(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "requests.sqlite"
    with RequestJournal(path), pytest.raises(JournalBlocked):
        RequestJournal(path)
    link = tmp_path / "other.sqlite"
    link.symlink_to(path)
    with pytest.raises(JournalBlocked):
        RequestJournal(link)
    assert b"observation" not in path.read_bytes()
