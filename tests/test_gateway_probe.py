"""Offline checks for option validation and fail-closed cleanup."""

import json

import pytest

from rca_agent import gateway_probe as probe


@pytest.mark.parametrize(
    "internal,driver,mode",
    [
        (False, "bridge", "isolated"),
        (True, "overlay", "isolated"),
        (True, "bridge", "nat"),
        (True, "bridge", None),
    ],
)
def test_unsupported_or_unapplied_option_is_rejected(internal, driver, mode):
    net = {"Internal": internal, "Driver": driver, "Options": {probe.OPTION: mode}}
    with pytest.raises(ValueError):
        probe.validate_network(net, True)


def test_isolated_option_and_baseline():
    probe.validate_network(
        {"Internal": True, "Driver": "bridge", "Options": {probe.OPTION: "isolated"}}, True
    )
    probe.validate_network({"Internal": True, "Driver": "bridge"}, False)


def test_failed_network_validation_removes_resources(monkeypatch):
    calls = []

    def docker(*args):
        calls.append(args)
        if args[0] == "version":
            return json.dumps({"Version": "synthetic"})
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Id": "synthetic"}])
        if args[:3] == ("network", "inspect", "bridge"):
            return json.dumps([{"IPAM": {"Config": [{"Gateway": "192.0.2.1"}]}}])
        if args[:2] == ("network", "inspect"):
            return json.dumps([{"Internal": False, "Driver": "bridge"}])
        return ""

    monkeypatch.setattr(probe, "docker", docker)
    with pytest.raises(ValueError):
        probe.run_probe()
    assert len([c for c in calls if c[:2] == ("rm", "--force")]) == 1
    assert calls[-1][:2] == ("network", "rm")
