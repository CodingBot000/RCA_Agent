import pytest

from rca_agent.contracts import ContractError, RCAInput
from rca_agent.isolation import check_task_tree, reject_leakage

pytestmark = pytest.mark.security


def test_blind_task_and_sibling_answer(tmp_path):
    task = tmp_path / "dataset"
    task.mkdir()
    (task / "task.json").write_text("{}")
    (tmp_path / "dataset.answers.jsonl").write_text("secret")
    check_task_tree(task)
    reject_leakage({"candidate_services": ["api"]}, ("hidden-secret",))


@pytest.mark.parametrize(
    "payload", [{"ground_truth": "db"}, {"nested": [{"Answer": "db"}]}, {"text": "secret-answer"}]
)
def test_reject_answer_leak(payload):
    with pytest.raises(ContractError):
        reject_leakage(payload, ("secret-answer",))


def test_input_rejects_unexpected_answer_field():
    with pytest.raises(ContractError):
        RCAInput.parse(
            {
                "case_id": "a",
                "candidates": ["x"],
                "evidence": [{"id": "e", "content": "ok"}],
                "answer": "x",
            }
        )


def test_reject_answer_file_inside_task(tmp_path):
    (tmp_path / "dataset.answers.jsonl").write_text("secret")
    with pytest.raises(ContractError):
        check_task_tree(tmp_path)


def test_reject_symlink(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    (task / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(ContractError):
        check_task_tree(task)
