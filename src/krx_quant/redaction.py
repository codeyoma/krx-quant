"""Masking what the server posts to the public issue tracker (#215).

A scheduled job's output is posted as a comment on a public repository's issue. Before
that, everything that could name a secret or the server is replaced with ``[가림]``:

- every secret setting's value (``secrets_of``): the KIS app key and secret, the Telegram
  token and chat, the AWS keys, the macro API keys, the GitHub token, the Toss client ID and
  secret (#235), the Object Store (and its bucket), the S3 endpoint and ``KRX_BIND_ADDRESS``.
  Values shorter than ``MIN_SECRET`` characters are left alone: masking them would mangle
  numbers that only happen to be equal;
- whatever the repository's own check forbids (``forbidden.RULES``: KIS order codes,
  AWS, Telegram, JWT, GitHub tokens, private keys);
- Tailscale names (``*.ts.net``), IPv6 and IPv4 addresses.

``redact`` is a pure function: it is the one place that decides what a comment may show.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from krx_quant.config import Settings
from krx_quant.forbidden import RULES

MASK = "[가림]"
MIN_SECRET = 8

_TAILNET = re.compile(r"[\w-]+(?:\.[\w-]+)*\.ts\.net\b", re.IGNORECASE)
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IPV4 = re.compile(rf"(?<![\d.]){_OCTET}(?:\.{_OCTET}){{3}}(?!\.?\d)")
# Anything shaped like an IPv6 address (two colons or more among hex digits); kept only if
# it is one (``ipaddress``), so times such as 09:00:30 stay.
_IPV6 = re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f.]*(?![\w:])")


def secrets_of(settings: Settings) -> tuple[str, ...]:
    """The settings' values a comment must not show."""
    values = [
        settings.kis_app_key,
        settings.kis_app_secret,
        settings.telegram_bot_token,
        settings.telegram_chat_id,
        settings.aws_access_key_id,
        settings.aws_secret_access_key,
        settings.ecos_api_key,
        settings.fred_api_key,
        settings.github_token,
        settings.toss_client_id,
        settings.toss_client_secret,
        settings.object_store,
        settings.s3_endpoint_url,
        settings.bind_address,
    ]
    if settings.object_store and settings.object_store.startswith("s3://"):
        values.append(urlsplit(settings.object_store).netloc)  # the bucket, named alone
    return tuple(v.strip() for v in values if v and len(v.strip()) >= MIN_SECRET)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """``text`` with ``secrets`` (those of ``MIN_SECRET`` characters or more), the forbidden
    patterns, Tailscale names and IP addresses replaced with ``MASK``."""
    for secret in sorted({s for s in secrets if len(s) >= MIN_SECRET}, key=len, reverse=True):
        text = text.replace(secret, MASK)
    for _, pattern in RULES:
        text = pattern.sub(MASK, text)
    text = _TAILNET.sub(MASK, text)
    text = _IPV6.sub(_ipv6, text)
    return _IPV4.sub(MASK, text)


def _ipv6(match: re.Match[str]) -> str:
    found = match.group()
    core = found.rstrip(".")
    try:
        ipaddress.IPv6Address(core)
    except ValueError:
        return found
    return MASK + found[len(core) :]
