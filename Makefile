.PHONY: check test eval harness-verify
check:
	uv run --locked --no-sync ruff check .
	uv run --locked --no-sync ruff format --check .
	uv run --locked --no-sync python scripts/sync_skills.py
	uv run --locked --no-sync pytest

test:
	uv run --locked --no-sync pytest

eval:
	@echo 'Manual RCAEval workflow: see README.md. No evaluation runs automatically.'

harness-verify:
	uv run --locked --no-sync python scripts/harness_verify.py
