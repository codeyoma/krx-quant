"""What a scheduled job posts on the public issue tracker is masked first (#215).

The secret-looking values here are put together at run time, so the repository's own check
(``scripts/check_forbidden.py``) does not find them in this file.
"""

from __future__ import annotations

import pytest

from krx_quant.config import Settings
from krx_quant.forbidden import RULES
from krx_quant.redaction import MASK, MIN_SECRET, redact, secrets_of

TELEGRAM_TOKEN = "1234567890" + ":" + "A" * 35
GITHUB_TOKEN = "github_pat_" + "B" * 22 + "_" + "C" * 59


def server_settings() -> Settings:
    return Settings(
        kis_app_key="P" + "k" * 35,
        kis_app_secret="S" + "s" * 179,
        telegram_bot_token=TELEGRAM_TOKEN,
        telegram_chat_id="987654321",
        aws_access_key_id="AKIA" + "Q" * 16,
        aws_secret_access_key="w" * 40,
        ecos_api_key="E" * 20,
        fred_api_key="f" * 32,
        github_token=GITHUB_TOKEN,
        object_store="s3://krx-quant-example/engine",
        bind_address="100.101.102.103",
    )


def test_every_secret_setting_is_masked_wherever_it_shows() -> None:
    settings = server_settings()
    secrets = secrets_of(settings)
    text = "\n".join(f"value {value} here" for value in secrets) + "\nthe bucket krx-quant-example failed"
    masked = redact(text, secrets)
    for value in secrets:
        assert value not in masked
    assert "krx-quant-example" not in masked
    assert masked.splitlines()[0] == f"value {MASK} here"


def test_the_github_token_is_not_in_the_settings_repr() -> None:
    assert GITHUB_TOKEN not in repr(server_settings())


def test_short_values_are_left_alone_not_to_mangle_numbers() -> None:
    settings = Settings(kis_app_key="", kis_app_secret="", telegram_chat_id="42")
    assert secrets_of(settings) == ()
    assert redact("42 rows, 1,128 windows", ["42"]) == "42 rows, 1,128 windows"
    short = "x" * (MIN_SECRET - 1)
    assert redact(f"{short} and {short}x", [short, f"{short}x"]) == f"{short} and {MASK}"


@pytest.mark.parametrize("address", ["100.64.0.7", "10.0.0.1", "192.168.1.20", "8.8.8.8"])
def test_ipv4_addresses_are_masked(address: str) -> None:
    assert (
        redact(f"listening on {address}:8080, then {address}.") == f"listening on {MASK}:8080, then {MASK}."
    )


@pytest.mark.parametrize(
    "address", ["fd7a:115c:a1e0::1234", "::1", "fe80::1", "::ffff:10.1.2.3", "2001:db8::8:800:200c:417a"]
)
def test_ipv6_addresses_are_masked(address: str) -> None:
    assert (
        redact(f"listening on [{address}]:8080, then {address}.")
        == f"listening on [{MASK}]:8080, then {MASK}."
    )


def test_tailscale_names_are_masked() -> None:
    assert redact("ssh ubuntu@krx-quant.tail1a2b3.ts.net now") == f"ssh ubuntu@{MASK} now"
    assert redact("KRX-QUANT.TAIL1A2B3.TS.NET") == MASK


@pytest.mark.parametrize(
    "text",
    [
        "09:00:30",
        "15:20",
        "2026-10-15T22:30:00+09:00",
        "p50 0.123 · p90 1.5 · 최대 43",
        "1,128 windows",
        "uv 0.12.23",
        "aa:bb:cc",
        "3.11.17",
        "std::vector",
    ],
)
def test_what_only_looks_like_an_address_stays(text: str) -> None:
    assert redact(text) == text


# One example of each forbidden pattern; a new rule needs one here.
FORBIDDEN = {
    "KIS order TR ID": "TTTC" + "0802U",
    "KIS order endpoint": "/uapi/domestic-stock/v1/trading/" + "order" + "-cash",
    "AWS access key": "AKIA" + "Z" * 16,
    "Telegram bot token": TELEGRAM_TOKEN,
    "JWT / KIS access token": "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + ".sig",
    "private key": "-----BEGIN " + "PRIVATE KEY-----",
    "KIS credential value": "KIS_APP_" + "KEY=" + "x" * 20,
    "Toss order endpoint": "/api/v1/" + "orders",
    "Toss credential value": "TOSS_CLIENT_" + "SECRET=" + "z" * 20,
    "GitHub token": "ghp_" + "y" * 36,
}


def test_every_forbidden_rule_has_an_example() -> None:
    assert set(FORBIDDEN) == {name for name, _ in RULES}


@pytest.mark.parametrize("name", sorted(FORBIDDEN))
def test_what_the_repository_forbids_is_masked(name: str) -> None:
    masked = redact(f"before {FORBIDDEN[name]} after")
    assert FORBIDDEN[name] not in masked
    assert masked.startswith("before ") and masked.endswith(" after")
    assert MASK in masked


def test_a_fine_grained_github_token_is_masked_without_the_setting() -> None:
    assert redact(f"token {GITHUB_TOKEN}") == f"token {MASK}"


def test_tailscale_names_end_where_their_labels_do() -> None:
    assert redact("a host.ts.net and b.c.ts.net.") == f"a {MASK} and {MASK}."
    assert redact("..ts.net and .ts.net stay") == "..ts.net and .ts.net stay"
    assert redact("(krx-quant.tail1a2b3.ts.net:443)") == f"({MASK}:443)"
    assert redact("x.ts.network") == "x.ts.network"


@pytest.mark.timeout(10)  # the pattern it replaced took about 10 s on each of these
@pytest.mark.parametrize("unit", ["x", "A.", "1.", "a.b", "1.2"])
def test_a_long_line_is_masked_in_linear_time(unit: str) -> None:
    # #358: the engine masks its lines for Loki on a thread of its own, which holds the
    # interpreter while a pattern runs; quadratic patterns would stall the engine on such a line.
    line = unit * (64 * 1024 // len(unit))
    assert redact(line + ".ts.net") == MASK
    assert redact(line) == line
