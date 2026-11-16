"""Host-owned expiring DNS leases and atomic per-family netns rules. No HTTP/API calls."""

import ipaddress
import json
import socket
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

HOST = "api.openai.com"


@dataclass(frozen=True)
class AddressLease:
    addresses: tuple[str, ...]
    expires: int
    port: int
    mock: bool = False

    def __post_init__(self):
        if not self.addresses or len(self.addresses) > 64 or type(self.expires) is not int:
            raise ValueError("invalid address lease")
        if self.port != (8443 if self.mock else 443):
            raise ValueError("unapproved port")
        for value in self.addresses:
            addr = ipaddress.ip_address(value)
            if (
                addr.is_multicast
                or addr.is_unspecified
                or addr.is_link_local
                or getattr(addr, "ipv4_mapped", None)
            ):
                raise ValueError("unsafe DNS address")
            if not self.mock and (not addr.is_global or addr.is_reserved):
                raise ValueError("nonpublic DNS address")

    def authorize(self, address, port, now, request_timeout=0):
        if (
            address not in self.addresses
            or port != self.port
            or now + request_timeout >= self.expires
        ):
            raise ValueError("lease expired or destination not approved")

    def to_dict(self):
        return asdict(self)


def resolve(lease_seconds, now, *, resolver=socket.getaddrinfo):
    if type(lease_seconds) is not int or lease_seconds < 1:
        raise ValueError("explicit lease interval required")
    answers = resolver(HOST, 443, type=socket.SOCK_STREAM)
    addresses = tuple(sorted({str(ipaddress.ip_address(row[4][0])) for row in answers}))
    return AddressLease(addresses, int(now) + lease_seconds, 443)


def rule_table(lease, family):
    if family not in (4, 6):
        raise ValueError("invalid address family")
    # xt_time is UTC by default. Subtract one because --datestop is inclusive to the second.
    stop = datetime.fromtimestamp(lease.expires - 1, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    rows = ["*filter", ":INPUT DROP [0:0]", ":FORWARD DROP [0:0]", ":OUTPUT DROP [0:0]"]
    if family == 4:
        rows.append("-A INPUT -p icmp --icmp-type destination-unreachable -j ACCEPT")
        rows.append(
            "-A OUTPUT -p icmp --icmp-type destination-unreachable -m addrtype --dst-type LOCAL -j ACCEPT"
        )
    else:
        rows.append("-A INPUT -p ipv6-icmp --icmpv6-type destination-unreachable -j ACCEPT")
        rows.append(
            "-A OUTPUT -p ipv6-icmp --icmpv6-type destination-unreachable -m addrtype --dst-type LOCAL -j ACCEPT"
        )
        rows.append("-A INPUT -p ipv6-icmp --icmpv6-type packet-too-big -j ACCEPT")
        for kind in ("neighbor-solicitation", "neighbor-advertisement"):
            rows.append(
                f"-A INPUT -i eth0 -p ipv6-icmp --icmpv6-type {kind} -m hl --hl-eq 255 -j ACCEPT"
            )
            rows.append(
                f"-A OUTPUT -o eth0 -p ipv6-icmp --icmpv6-type {kind} -m hl --hl-eq 255 -j ACCEPT"
            )
    for value in lease.addresses:
        if ipaddress.ip_address(value).version != family:
            continue
        rows.append(
            f"-A OUTPUT -p tcp -d {value} --dport {lease.port} -m time --datestop {stop} -j ACCEPT"
        )
        rows.append(
            f"-A INPUT -p tcp -s {value} --sport {lease.port} -m conntrack --ctstate ESTABLISHED -m time --datestop {stop} -j ACCEPT"
        )
    rows += [
        "-A OUTPUT -j REJECT --reject-with "
        + ("icmp-admin-prohibited" if family == 4 else "icmp6-adm-prohibited"),
        "COMMIT",
        "",
    ]
    return "\n".join(rows)


def apply_script(lease):
    # No unbounded ESTABLISHED exception: even existing flows expire in kernel rules.
    tables = {4: rule_table(lease, 4), 6: rule_table(lease, 6)}
    return f"""
import subprocess
rules={tables!r}
empty='*filter\\n:INPUT DROP [0:0]\\n:FORWARD DROP [0:0]\\n:OUTPUT DROP [0:0]\\nCOMMIT\\n'
def put(binary,text): subprocess.run([binary,'--wait'],input=text,text=True,check=True)
try:
 for binary in ('iptables-restore','ip6tables-restore'): put(binary,empty)
 put('iptables-restore',rules[4]);put('ip6tables-restore',rules[6])
except BaseException:
 for binary in ('iptables-restore','ip6tables-restore'): put(binary,empty)
 raise
print('lease_applied')
"""


def lease_from_dict(data):
    if set(data) != {"addresses", "expires", "port", "mock"}:
        raise ValueError("invalid lease")
    return AddressLease(tuple(data["addresses"]), data["expires"], data["port"], data["mock"])


def main():
    import argparse
    import time

    parser = argparse.ArgumentParser()
    parser.add_argument("--lease-seconds", type=int, required=True)
    args = parser.parse_args()
    try:
        lease = resolve(args.lease_seconds, time.time())
        print(
            json.dumps(
                {
                    "status": "DNS_RESOLVED_ONLY",
                    "address_count": len(lease.addresses),
                    "expires": lease.expires,
                    "ttl_source": "operator lease, DNS TTL unavailable",
                    "http_requests": 0,
                    "live": "LIVE_BLOCKED",
                }
            )
        )
    except Exception:  # noqa: BLE001 - no resolver exception or address disclosure
        print(json.dumps({"status": "BLOCKED", "live": "LIVE_BLOCKED"}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
