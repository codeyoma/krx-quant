"""Masking what the server posts to the public issue tracker (#215).

A scheduled job's output is posted as a comment on a public repository's issue. Before
that, everything that could name a secret or the server is replaced with ``[가림]``:

- every secret setting's value (``secrets_of``): the KIS app key and secret, the Telegram
  token and chat, the AWS keys, the macro API keys, the GitHub token, the Toss client ID and
  secret (#235), the KRX Open API key (#305), the Object Store (and its bucket), the S3 endpoint,
  ``KRX_BIND_ADDRESS`` and Grafana Cloud Loki's address, user and token (#358).
  Values shorter than ``MIN_SECRET`` characters are left alone: masking them would mangle
  numbers that only happen to be equal;
- whatever the repository's own check forbids (``forbidden.RULES``: KIS order codes,
  AWS, Telegram, JWT, GitHub tokens, private keys);
- Tailscale names (``*.ts.net``), IPv6 and IPv4 addresses.

``redact`` is a pure function: it is the one place that decides what a comment may show, and
what the log bundle an agent is handed (``redact_counted``, #356) and the lines sent to Loki
(``loki.py``, #358) may.
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

# A Tailscale name ends in ``.ts.net``: found by that end, then read back over its labels
# (``_tailnets``). The one pattern ``[\w-]+(?:\.[\w-]+)*\.ts\.net\b`` it replaced took time
# quadratic in a long run of word characters and dots: seconds for a 64 KB line, holding the
# interpreter all the while (#358: the engine masks its lines for Loki on a thread of its own).
_TS_NET = re.compile(r"\.ts\.net\b", re.IGNORECASE)
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
        settings.krx_openapi_key,
        settings.object_store,
        settings.s3_endpoint_url,
        settings.bind_address,
        settings.loki_url,
        settings.loki_user,
        settings.loki_token,
    ]
    if settings.object_store and settings.object_store.startswith("s3://"):
        values.append(urlsplit(settings.object_store).netloc)  # the bucket, named alone
    return tuple(v.strip() for v in values if v and len(v.strip()) >= MIN_SECRET)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """``text`` with ``secrets`` (those of ``MIN_SECRET`` characters or more), the forbidden
    patterns, Tailscale names and IP addresses replaced with ``MASK``."""
    return redact_counted(text, secrets)[0]


def redact_counted(text: str, secrets: Iterable[str] = ()) -> tuple[str, int]:
    """``redact``'s text, and how many values it replaced (the log bundle counts them, #356)."""
    count = 0
    for secret in sorted({s for s in secrets if len(s) >= MIN_SECRET}, key=len, reverse=True):
        found = text.count(secret)
        if found:
            text = text.replace(secret, MASK)
            count += found
    for _, pattern in RULES:
        text, found = pattern.subn(MASK, text)
        count += found
    text, found = _tailnets(text)
    count += found
    masked = 0

    def ipv6(match: re.Match[str]) -> str:
        nonlocal masked
        shown = _ipv6(match)
        masked += shown != match.group()
        return shown

    text = _IPV6.sub(ipv6, text)
    text, found = _IPV4.subn(MASK, text)
    return text, count + masked + found


def _tailnets(text: str) -> tuple[str, int]:
    """``text`` with each Tailscale name (labels of word characters and hyphens, dotted, then
    ``.ts.net``) masked, and how many; in time linear in ``text``."""
    parts: list[str] = []
    done = 0  # the text before this is settled
    found = 0
    for end in _TS_NET.finditer(text):
        start = end.start()
        while start > done and (text[start - 1] in "_-." or text[start - 1].isalnum()):
            start -= 1
        while start < end.start() and text[start] == ".":
            start += 1
        if start == end.start():  # no label before it
            continue
        parts += [text[done:start], MASK]
        done = end.end()
        found += 1
    if not found:
        return text, 0
    parts.append(text[done:])
    return "".join(parts), found


def _ipv6(match: re.Match[str]) -> str:
    """The match masked if it is an IPv6 address (shaped like one is not enough: 09:00:30)."""
    found = match.group()
    core = found.rstrip(".")
    try:
        ipaddress.IPv6Address(core)
    except ValueError:
        return found
    return MASK + found[len(core) :]
