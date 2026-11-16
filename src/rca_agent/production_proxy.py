"""Production Proxy role. No Mock CA, no environment credential lookup, explicit permit only."""

import os
import stat
import time

from rca_agent.dns_guard import lease_from_dict
from rca_agent.egress_policy import tls_context
from rca_agent.egress_proxy import EgressProxy
from rca_agent.integrated_worker import MAXIMUM, isolation
from rca_agent.ipc_protocol import parse_frame
from rca_agent.ipc_worker import _read, _write
from rca_agent.production_provider import LivePermit
from rca_agent.secure_uds import Credentials, serve


def load_lease(path="/lease/current.json"):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 10002
            or stat.S_IMODE(info.st_mode) != 0o640
            or info.st_nlink != 1
        ):
            raise ValueError("untrusted DNS lease")
        data = os.read(fd, 65537)
        if len(data) > 65536:
            raise ValueError("lease too large")
        return lease_from_dict(parse_frame(data, 65536))
    finally:
        os.close(fd)


class ProductionProxy(EgressProxy):
    def endpoint_context(self, config):
        self.permit = LivePermit(**config["permit"])
        self.permit.require()
        if (
            config.get("mode") != "production"
            or config.get("fault") not in (None, "none")
            or "ca_file" in config
            or "ip" in config
            or not config.get("journal_path")
        ):
            raise ValueError("production configuration rejected")
        self.lease = load_lease()
        if self.lease.mock:
            raise ValueError("Mock lease cannot authorize production")
        self.lease.authorize(self.lease.addresses[0], 443, time.time(), config["transport_timeout"])
        credential = config["api_credential"]
        if (
            not isinstance(credential, str)
            or not 1 <= len(credential) <= 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in credential)
        ):
            raise ValueError("invalid Proxy credential format")
        self._credential = credential
        return self.lease.addresses[0], 443, tls_context()

    def forward(self, operation, data):
        self.permit.require()
        try:
            lease = load_lease()
            if lease.mock:
                raise ValueError("production lease required")
            address = lease.addresses[self.calls % len(lease.addresses)]
            lease.authorize(address, 443, time.time(), self.forward_timeout)
        except (ValueError, OSError):
            self.record_error("DNS_LEASE_INVALID")
            raise
        self.ip = address
        return super().forward(operation, data)

    def request_headers(self):
        self.permit.require()
        if os.getuid() != 10005:
            raise ValueError("Proxy identity required")
        return {"Content-Type": "application/json", "Authorization": "Bearer " + self._credential}


def main():
    try:
        config = _read(MAXIMUM)
        proxy = ProductionProxy(config)
        checks = isolation(config, "proxy")
        _write({"type": "ready", "checks": checks}, MAXIMUM)
        if not all(checks.values()):
            raise ValueError("Proxy isolation failed")
        serve(
            "/proxychannel/proxy.sock",
            Credentials(config["session"], bytes.fromhex(config["key"])),
            proxy,
            peer_uid=10001,
            peer_gid=10002,
        )
    except Exception:  # noqa: BLE001 - no key, TLS or request exception contents in logs
        _write({"type": "error", "reason": "production_proxy_failed"}, MAXIMUM)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
