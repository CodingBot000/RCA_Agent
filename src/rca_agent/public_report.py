"""Build an explicit allowlisted report; never serialize raw graph state as a public log."""

import re
from decimal import Decimal, InvalidOperation

SAFE_REASONS = frozenset(
    {
        "",
        "case_context_error",
        "execution_limit_mismatch",
        "invalid_model_response",
        "invalid_observation",
        "input_estimate_unavailable",
        "usage_unavailable",
        "timeout_usage_unknown",
        "concurrency_limit",
        "call_limit",
        "token_limit",
        "cost_limit",
        "budget_exceeded",
        "provider_error",
        "invalid_provider_response",
        "reasoner_error",
        "tool_error",
        "tool_call_limit",
        "iteration_limit",
        "invalid_or_insufficient_evidence",
        "observation_limit",
        "error",
        "insufficient_evidence",
    }
)


def public_run_report(state: dict) -> dict:
    metadata = state.get("model_metadata")
    if not isinstance(metadata, dict):
        raise TypeError("model metadata unavailable")
    for key in ("provider", "model", "code_version"):
        value = metadata.get(key)
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value)
            or value.lower().startswith(("sk-", "ghp_", "hf_", "bearer"))
        ):
            raise ValueError("unsafe model metadata")
    for key in ("calls", "input_tokens", "output_tokens", "total_tokens"):
        if type(metadata.get(key)) is not int or metadata[key] < 0:
            raise ValueError("invalid usage metadata")
    if metadata["total_tokens"] != metadata["input_tokens"] + metadata["output_tokens"]:
        raise ValueError("invalid token total")
    try:
        cost = Decimal(metadata["cost_usd"])
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise ValueError("invalid cost metadata") from exc
    if not cost.is_finite() or cost < 0:
        raise ValueError("invalid cost metadata")
    for key in ("calls", "iterations"):
        if type(state.get(key)) is not int or state[key] < 0:
            raise ValueError("invalid execution counts")
    reason = state.get("failure", "")
    if not isinstance(reason, str) or reason not in SAFE_REASONS:
        reason = "reasoner_error"
    allowed = (
        "provider",
        "model",
        "code_version",
        "calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost_usd",
    )
    return {
        "status": "failed" if reason else "ok",
        "failure": reason,
        "tool_calls": state.get("calls", 0),
        "iterations": state.get("iterations", 0),
        "model": {k: metadata[k] for k in allowed},
    }
