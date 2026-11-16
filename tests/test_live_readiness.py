import hashlib
import json
import time
from dataclasses import asdict
from decimal import Decimal

import pytest

from rca_agent.approval import approved_profile
from rca_agent.docker_integration import fixture_budget
from rca_agent.model_broker import Pricing
from rca_agent.pilot import CATALOG, PRICE_PROFILE, plan
from rca_agent.production_provider import LivePermit, ProductionProvider, ProductionTLS


def approval(tmp_path):
    tmp_path.chmod(0o700)
    proof = tmp_path / "proof"
    proof.write_text("private-proof")
    proof.chmod(0o600)
    b = fixture_budget("none")
    p = plan("gpt-6-luna", b, PRICE_PROFILE)
    controls = {
        "session_timeout": 120,
        "http_timeout": 1,
        "journal_volume": "rca-pilot-journal-unit",
        "dns_lease": 60,
        "dns_refresh": 20,
        "dns_timeout": 5,
        "family_policy": "ipv4-only",
        "private_output": "private-output",
    }
    data = {
        "approved": True,
        "allow_api_key_use": True,
        "allow_provider_data_processing": True,
        "allow_unverified_api_contract": True,
        "retention_approved": True,
        "retention": {
            "journal_days_after_reconciliation": 90,
            "artifact_days": 7,
            "development_days": 14,
            "delete_mode": "manual",
        },
        "settings_version": p["settings_version"],
        "source_version": "a" * 64,
        "provenance_digest": hashlib.sha256(proof.read_bytes()).hexdigest(),
        "expires": time.time() + 1000,
        "runtime": controls,
        "total_budget": {**asdict(b), "max_cost_usd": str(b.max_cost_usd)},
        "observation_limits": {"max_records": 1, "max_chars": 4000},
    }
    path = tmp_path / "approval.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path, data, p, b, proof, controls


def test_explicit_private_approval_binds_all_declared_settings(tmp_path):
    path, _data, p, b, proof, controls = approval(tmp_path)
    profile = approved_profile(path, p, b, proof, "a" * 64, controls, catalog=CATALOG)
    assert profile.identity[0] == "openai" and profile.model == "gpt-6-luna"
    assert profile.public_role_config()["live_mode"] is True
    assert profile.pricing.input_per_token == CATALOG["gpt-6-luna"]["input"]


@pytest.mark.parametrize(
    "change",
    ["false", "expired", "settings", "source", "proof", "runtime", "permissions", "retention"],
)
def test_invalid_approval_never_activates_execution(tmp_path, change):
    path, data, p, b, proof, controls = approval(tmp_path)
    if change == "false":
        data["approved"] = False
    elif change == "expired":
        data["expires"] = 0
    elif change == "settings":
        data["settings_version"] = "changed"
    elif change == "source":
        data["source_version"] = "changed"
    elif change == "proof":
        proof.write_text("changed")
    elif change == "runtime":
        data["runtime"] = {**controls, "http_timeout": 2}
    elif change == "permissions":
        path.chmod(0o644)
    else:
        data["retention"]["delete_mode"] = "automatic-not-implemented"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        approved_profile(path, p, b, proof, "a" * 64, controls, catalog=CATALOG)


def test_no_credentials_or_transport_without_explicit_permit(tmp_path):
    called = []
    price = Pricing(Decimal("0.1"), Decimal("0.2"))
    provider = ProductionProvider(lambda *args: called.append("transport"), price, LivePermit())
    with pytest.raises(RuntimeError, match="LIVE_BLOCKED"):
        provider.input_upper_bound(None)
    transport = ProductionTLS(
        permit=LivePermit(),
        lease_provider=lambda: called.append("DNS"),
        key_reader=lambda: called.append("credential"),
        audit_path=tmp_path / "audit",
        timeout=1,
    )
    with pytest.raises(RuntimeError, match="LIVE_BLOCKED"):
        transport.forward("count", {})
    assert not called
    assert transport.context.check_hostname


def test_production_never_falls_back_to_mock(tmp_path):
    from rca_agent.docker_integration import run_session
    from rca_agent.integrated_worker import worker_handler

    with pytest.raises(ValueError, match="authenticated Proxy"):
        worker_handler({"live_mode": True})
    path, _data, p, b, proof, controls = approval(tmp_path)
    profile = approved_profile(path, p, b, proof, "a" * 64, controls, catalog=CATALOG)
    with pytest.raises(ValueError, match="Mock or missing"):
        run_session(runtime=profile)


def test_synthetic_secret_can_only_be_read_by_proxy_identity(tmp_path, monkeypatch):
    import os

    called = []
    sender = ProductionTLS(
        permit=LivePermit(True, True, True, True),
        lease_provider=lambda: None,
        key_reader=lambda: called.append("read") or "TEST_PROXY_CREDENTIAL",
        audit_path=tmp_path / "audit",
        timeout=1,
    )
    monkeypatch.setattr(os, "getuid", lambda: 10002)
    with pytest.raises(RuntimeError):
        sender.request_headers()
    assert not called
    monkeypatch.setattr(os, "getuid", lambda: 10005)
    assert sender.request_headers()["Authorization"] == "Bearer TEST_PROXY_CREDENTIAL"
    assert sender.request_headers()["Authorization"] == "Bearer TEST_PROXY_CREDENTIAL"
    assert called == ["read"] and "TEST_PROXY_CREDENTIAL" not in repr(sender)


def test_production_worker_formatter_never_receives_proxy_credential():
    import json

    from rca_agent.llm_reasoner import MODEL_INSTRUCTION, QUERY

    seen = []

    def transport(method, url, body):
        seen.append((method, url, json.loads(body)))
        return {"status": 200, "body": {"object": "response.input_tokens", "input_tokens": 10}}

    provider = ProductionProvider(
        transport, Pricing(Decimal(0), Decimal(0)), LivePermit(True, True, True, True)
    )
    prepared = provider.prepare(
        {
            "instruction": MODEL_INSTRUCTION,
            "schema": QUERY.json_schema(),
            "phase": "investigate",
            "context": {
                "case_id": "synthetic",
                "services": ["api"],
                "candidates": ["api_cpu"],
                "start": 1,
                "end": 2,
            },
            "observations": [],
        },
        model="gpt-6-luna",
        max_output_tokens=1000,
    )
    assert provider.input_upper_bound(prepared) == 10
    assert seen[0][0] == "POST" and seen[0][1] == "https://api.openai.com/v1/responses/input_tokens"
    assert "Authorization" not in json.dumps(seen) and "TEST_PROXY_CREDENTIAL" not in json.dumps(
        seen
    )
