import socket

import pytest

from rca_agent.dns_guard import AddressLease, resolve, rule_table


def test_resolver_rotation_and_expiry_do_not_pin_an_ip_forever():
    def resolver(host, port, **kwargs):
        assert host == "api.openai.com" and port == 443
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111", 443, 0, 0)),
        ]

    first = resolve(20, 100, resolver=resolver)
    first.authorize("1.1.1.1", 443, 101, 3)
    with pytest.raises(ValueError):
        first.authorize("1.1.1.1", 443, 119, 3)
    second = AddressLease(("8.8.8.8",), 140, 443)
    with pytest.raises(ValueError):
        second.authorize("1.1.1.1", 443, 121)
    with pytest.raises(ValueError):
        second.authorize("8.8.8.8", 80, 121)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:1.1.1.1", "ff02::1"]
)
def test_dns_private_gateway_mapped_and_multicast_rejected(address):
    with pytest.raises(ValueError):
        AddressLease((address,), 100, 443)


def test_kernel_rules_expire_established_flows_too():
    lease = AddressLease(("1.1.1.1", "2606:4700::1111"), 1700000000, 443)
    for family in (4, 6):
        rules = rule_table(lease, family)
        tcp = [line for line in rules.splitlines() if "-p tcp" in line]
        assert tcp and all("--datestop" in line for line in tcp)
        assert "--ctstate ESTABLISHED,RELATED -j ACCEPT" not in rules
    with pytest.raises(ValueError):
        resolve(10, 100, resolver=lambda *args, **kwargs: [])
