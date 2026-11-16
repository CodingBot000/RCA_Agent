"""Explicit approved runtime settings. No operator defaults for live execution."""

from dataclasses import asdict, dataclass

from rca_agent.graph import ObservationLimits
from rca_agent.model_broker import Budget, Pricing
from rca_agent.production_provider import LivePermit


@dataclass(frozen=True)
class RuntimeProfile:
    model: str
    case_budget: Budget
    total_budget: Budget
    pricing: Pricing
    permit: LivePermit
    session_timeout: float
    http_timeout: float
    observation_limits: ObservationLimits

    def __post_init__(self):
        self.permit.require()
        if self.model not in ("gpt-6-luna", "gpt-6.1-terra"):
            raise ValueError("unapproved model")
        if (
            self.case_budget.max_calls > 8
            or self.total_budget.max_calls > 8
            or self.case_budget.max_concurrency != 1
            or self.total_budget.max_concurrency != 1
            or self.case_budget.max_retries != 0
            or self.total_budget.max_retries != 0
            or self.case_budget.output_tokens_per_call != self.total_budget.output_tokens_per_call
            or not 0 < self.http_timeout < self.case_budget.timeout_seconds
            or not self.case_budget.timeout_seconds <= self.session_timeout < float("inf")
        ):
            raise ValueError("unsupported live execution limits")

    @property
    def identity(self):
        return ("openai", self.model, "pilot-live")

    def public_role_config(self):
        return {
            "identity": list(self.identity),
            "pricing": {
                "input_per_token": str(self.pricing.input_per_token),
                "output_per_token": str(self.pricing.output_per_token),
            },
            "live_mode": True,
            "permit": asdict(self.permit),
            "transport_timeout": self.http_timeout,
            "observation_limits": asdict(self.observation_limits),
        }
