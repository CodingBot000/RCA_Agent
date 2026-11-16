"""Application destination policy only. Live network execution is disabled."""

import ssl
from urllib.parse import urlsplit

LIVE_ENABLED = False


def validate_destination(url):
    value = urlsplit(url)
    if (
        value.scheme != "https"
        or value.hostname != "api.openai.com"
        or value.port not in (None, 443)
        or value.username
        or value.password
        or value.fragment
        or value.query
        or value.path not in ("/v1/responses", "/v1/responses/input_tokens")
    ):
        raise ValueError("unapproved API destination")


def tls_context():
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def live_client():
    raise RuntimeError("LIVE_BLOCKED: OS egress allowlist unverified")
