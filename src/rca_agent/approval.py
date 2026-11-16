"""Host-only operator approval binding; no approval is created automatically."""

import hashlib
import json
import os
import stat
import time
from decimal import Decimal
from pathlib import Path

from rca_agent.graph import ObservationLimits
from rca_agent.model_broker import Budget, Pricing
from rca_agent.production_provider import LivePermit
from rca_agent.runtime_profile import RuntimeProfile


def approved_profile(path, plan, budget, provenance, source_version, controls, *, catalog):
    path = Path(path)
    info = os.lstat(path)
    parent = os.lstat(path.parent)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise ValueError("private operator approval required")
    value = json.loads(path.read_text())
    expected = {
        "approved",
        "allow_api_key_use",
        "allow_provider_data_processing",
        "retention",
        "allow_unverified_api_contract",
        "retention_approved",
        "settings_version",
        "source_version",
        "provenance_digest",
        "expires",
        "runtime",
        "total_budget",
        "observation_limits",
    }
    if (
        set(value) != expected
        or any(
            value[k] is not True
            for k in (
                "approved",
                "allow_api_key_use",
                "allow_provider_data_processing",
                "allow_unverified_api_contract",
                "retention_approved",
            )
        )
        or value["settings_version"] != plan["settings_version"]
        or value["source_version"] != source_version
        or value["provenance_digest"] != hashlib.sha256(Path(provenance).read_bytes()).hexdigest()
        or type(value["expires"]) not in (int, float)
        or not time.time() < value["expires"] < float("inf")
        or value["runtime"] != controls
    ):
        raise ValueError("approval missing, expired or differs from declared execution")
    retention = value["retention"]
    if (
        not isinstance(retention, dict)
        or set(retention)
        != {"journal_days_after_reconciliation", "artifact_days", "development_days", "delete_mode"}
        or retention["delete_mode"] != "manual"
        or any(
            type(retention[k]) is not int or retention[k] < 1
            for k in ("journal_days_after_reconciliation", "artifact_days", "development_days")
        )
    ):
        raise ValueError("explicit supported retention policy required")
    total = Budget(
        **{**value["total_budget"], "max_cost_usd": Decimal(value["total_budget"]["max_cost_usd"])}
    )
    observation = ObservationLimits(**value["observation_limits"])
    permit = LivePermit(True, True, True, True)
    prices = catalog[plan["model"]]
    return RuntimeProfile(
        plan["model"],
        budget,
        total,
        Pricing(prices["input"], prices["output"]),
        permit,
        controls["session_timeout"],
        controls["http_timeout"],
        observation,
    )
