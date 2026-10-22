"""Log lines to Grafana Cloud Loki (#358): batches by size and by time, tried again after a wait
that doubles, a capped buffer that drops the oldest and counts them, masked before they go,
nothing sent without the three settings, a child's lines under its own labels, and what is left
sent at a process's end.

The fake Loki (``tests/loki_fake.py``) is an aiohttp server on the test's event loop; the
shipper's thread posts to it, so a test that runs a command (``cli.main``, which blocks) runs it in
a worker thread. A child's lines through the engine's runner are ``tests/test_process_log.py``'s.

The secret-looking values here are put together at run time, so the repository's own check
(``scripts/check_forbidden.py``) does not find them in this file.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import urllib.error
from collections.abc import Callable, Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from krx_quant import loki
from krx_quant.cli import main
from krx_quant.config import Settings
from krx_quant.doctor import Status, report
from krx_quant.feeds.base import KST
from krx_quant.loki import (
    LINE_BYTES,
    TEMPLATE_ONLY,
    Shipper,
    Target,
    backoff,
    bounded,
    cut,
    error_kind,
    level_of,
    target_of,
)
from krx_quant.process_log import formatter
from krx_quant.redaction import MASK
from tests.loki_fake import TOKEN, USER, FakeLoki
from tests.test_subscription_limit import until

NIGHT = date(2026, 10, 22)
APP_KEY = "P" + "k" * 35
GITHUB_TOKEN = "github_pat_" + "B" * 22 + "_" + "C" * 59
BIND_ADDRESS = "100.101.102.103"
TAILNET = "krx-quant.tail" + "1234" + ".ts.net"
TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?\+09:00$")  # the schema's


@pytest.fixture
def fake(fake_loki: FakeLoki) -> FakeLoki:
    return fake_loki


@pytest.fixture
def shippers() -> Iterator[Callable[..., Shipper]]:
    """Shippers to ``Target``s, closed at the end."""
    made: list[Shipper] = []

    def make(url: str, **options: Any) -> Shipper:
        shipper = Shipper(Target(url, USER, TOKEN), **{"first_retry": 0.01, "jitter": lambda: 1.0, **options})
        made.append(shipper)
        return shipper

    yield make
    for shipper in made:
        shipper.close(0)


# The settings


def test_all_three_settings_or_none_and_an_https_address() -> None:
    assert target_of({}) == (None, "KRX_LOKI_URL, KRX_LOKI_USER and KRX_LOKI_TOKEN are not set")
    target, why = target_of({loki.URL: "https://logs.example.test", loki.USER: USER})
    assert target is None and why == "KRX_LOKI_TOKEN is not set: all three are needed"
    _, why = target_of({loki.USER: USER, loki.TOKEN: " "})
    assert why == "KRX_LOKI_URL and KRX_LOKI_TOKEN are not set: all three are needed"
    plain = {loki.URL: "http://logs.example.test", loki.USER: USER, loki.TOKEN: TOKEN}
    assert target_of(plain) == (None, "KRX_LOKI_URL must be an https:// address")
    target, why = target_of({**plain, loki.URL: "https://logs.example.test/loki/api/v1/push/"})
    assert why is None and target is not None
    assert target.push_url == "https://logs.example.test/loki/api/v1/push"
    assert target.authorization == "Basic " + base64.b64encode(f"{USER}:{TOKEN}".encode()).decode()
    assert TOKEN not in repr(target) and USER not in repr(target)
    local, _ = target_of({**plain, loki.URL: "http://127.0.0.1:3100"})
    assert local is not None and local.url == "http://127.0.0.1:3100"  # a test's fake only


def test_the_settings_are_read_masked_and_kept_out_of_the_repr() -> None:
    settings = Settings.from_env(
        {loki.URL: "https://logs-prod-000.grafana.example", loki.USER: "12345678", loki.TOKEN: TOKEN},
        None,
        require_kis=False,
    )
    assert (settings.loki_url, settings.loki_user, settings.loki_token) == (
        "https://logs-prod-000.grafana.example",
        "12345678",
        TOKEN,
    )
    assert TOKEN not in repr(settings) and "grafana.example" not in repr(settings)
    text = report(settings, datetime(2026, 10, 23, 10, 0, tzinfo=KST), Status(None, "", ()))
    assert TOKEN not in text and "logs-prod-000" not in text


# Batches


async def test_lines_go_in_batches_of_a_size_in_order(
    fake: FakeLoki, shippers: Callable[..., Shipper]
) -> None:
    shipper = shippers(fake.url, batch_lines=3, every=60.0)
    for n in range(7):
        shipper.put(loki.ENGINE, "info", f"line {n}")
    await until(lambda: shipper.status().sent == 6)
    assert fake.batches() == [3, 3] and shipper.status().buffered == 1  # the 7th waits for its time
    shipper.flush()
    await until(lambda: shipper.status().sent == 7)
    assert fake.batches() == [3, 3, 1]
    assert [line for _, line in fake.lines()] == [f"line {n}" for n in range(7)]
    assert fake.lines()[0][0] == {"service": "krx-quant", "process": "engine", "level": "info"}


async def test_a_batch_is_cut_by_its_bytes(fake: FakeLoki, shippers: Callable[..., Shipper]) -> None:
    shipper = shippers(fake.url, batch_bytes=200, every=60.0)
    for n in range(4):
        shipper.put(loki.ENGINE, "info", f"{n}" * 100)
    await until(lambda: shipper.status().sent == 4)
    assert fake.batches() == [2, 2]


async def test_a_line_waits_at_most_the_batch_time(fake: FakeLoki, shippers: Callable[..., Shipper]) -> None:
    shipper = shippers(fake.url, every=0.1)
    shipper.put(loki.ENGINE, "warning", "first")
    shipper.put(loki.COMMAND, "info", "second")
    await until(lambda: shipper.status().sent == 2)  # no flush: the time sent them
    assert fake.batches() == [2]
    assert [(labels["process"], labels["level"], line) for labels, line in fake.lines()] == [
        ("engine", "warning", "first"),
        ("command", "info", "second"),
    ]


async def test_two_lines_at_one_time_keep_their_order(
    fake: FakeLoki, shippers: Callable[..., Shipper]
) -> None:
    shipper = shippers(fake.url, every=60.0)
    for n in range(3):
        shipper.put(loki.ENGINE, "info", f"line {n}", at=1_792_700_000.0)
    shipper.flush()
    await until(lambda: shipper.status().sent == 3)
    (stream,) = fake.taken[0]["streams"]
    times = [int(ns) for ns, _ in stream["values"]]
    assert times == sorted(set(times)) and [line for _, line in stream["values"]] == [
        "line 0",
        "line 1",
        "line 2",
    ]


# Failures


def test_the_wait_doubles_up_to_its_cap() -> None:
    assert [backoff(n, 1.0, 60.0) for n in range(1, 9)] == [1, 2, 4, 8, 16, 32, 60, 60]
    assert backoff(10_000, 1.0, 60.0) == 60.0


async def test_a_failed_push_is_sent_again_until_loki_takes_it(
    fake: FakeLoki, shippers: Callable[..., Shipper], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, "krx_quant.loki")
    fake.answers.extend([503, 503])
    shipper = shippers(fake.url, every=60.0)
    shipper.put(loki.ENGINE, "error", "one")
    shipper.put(loki.ENGINE, "error", "two")
    shipper.flush()
    await until(lambda: shipper.status().sent == 2)
    assert len(fake.bodies) == 3 and len(set(fake.bodies)) == 1, "the same batch, three times"
    status = shipper.status()
    assert (status.dropped, status.failures, status.last_error) == (0, 0, "HTTP 503")
    assert status.last_success_at is not None and status.last_error_at is not None
    said = [r.getMessage() for r in caplog.records if r.name == "krx_quant.loki"]
    assert said == [
        "Loki: could not send the log lines (HTTP 503); trying again, the lines wait in the buffer",
        "Loki: sending again after 2 failures in a row; 0 lines dropped meanwhile",
    ]


async def test_a_batch_loki_refuses_is_dropped_and_counted(
    fake: FakeLoki, shippers: Callable[..., Shipper]
) -> None:
    fake.answers.append(400)
    shipper = shippers(fake.url, every=60.0)
    shipper.put(loki.ENGINE, "info", "refused")
    shipper.put(loki.ENGINE, "info", "refused too")
    shipper.flush()
    await until(lambda: shipper.status().dropped == 2)
    shipper.put(loki.ENGINE, "info", "taken")
    shipper.flush()
    await until(lambda: shipper.status().sent == 1)
    assert [line for _, line in fake.lines()] == ["taken"]
    assert shipper.status().last_error == "HTTP 400"


async def test_the_buffer_keeps_the_newest_lines_and_counts_the_dropped(
    fake: FakeLoki, shippers: Callable[..., Shipper], caplog: pytest.LogCaptureFixture
) -> None:
    shipper = shippers(fake.url, every=60.0, max_lines=5)
    for n in range(8):
        shipper.put(loki.ENGINE, "info", f"line {n}")
    status = shipper.status()
    assert (status.dropped, status.buffered) == (3, 5)
    shipper.flush()
    await until(lambda: shipper.status().sent == 5)
    assert [line for _, line in fake.lines()] == [f"line {n}" for n in range(3, 8)]
    assert any(r.getMessage().startswith("Loki: 3 lines dropped") for r in caplog.records)


async def test_the_buffer_holds_its_bytes_too(fake: FakeLoki, shippers: Callable[..., Shipper]) -> None:
    shipper = shippers(fake.url, every=60.0, max_bytes=1000)
    for n in range(5):
        shipper.put(loki.ENGINE, "info", f"{n}" * 300)  # 364 bytes each as counted
    assert (shipper.status().dropped, shipper.status().buffered) == (3, 2)


async def test_an_unreachable_loki_does_not_hold_the_process_up(shippers: Callable[..., Shipper]) -> None:
    shipper = shippers("http://127.0.0.1:9", every=60.0)  # nothing listens there
    shipper.put(loki.ENGINE, "info", "never sent")
    shipper.flush()
    await until(lambda: shipper.status().failures > 0)
    assert shipper.status().last_error == "URLError(ConnectionRefusedError)"
    assert await asyncio.to_thread(shipper.close, 0.2) == 1  # given up at its time, counted
    shipper.put(loki.ENGINE, "info", "after the end")
    assert shipper.status().dropped == 2


def test_a_failure_is_shown_by_its_kind_never_its_text() -> None:
    refused = urllib.error.URLError(ConnectionRefusedError(61, f"refused by {BIND_ADDRESS}"))
    assert error_kind(refused) == "URLError(ConnectionRefusedError)"
    assert error_kind(TimeoutError("timed out")) == "TimeoutError"


# Masking


async def test_no_secret_nor_address_leaves_and_the_token_is_only_in_the_header(
    fake: FakeLoki, shippers: Callable[..., Shipper]
) -> None:
    shipper = shippers(fake.url, every=60.0, secrets=(APP_KEY, TOKEN))
    shipper.put(
        loki.ENGINE, "error", f"refused key {APP_KEY} from {BIND_ADDRESS} via {TAILNET}: {GITHUB_TOKEN}"
    )
    shipper.put(loki.ENGINE, "info", f"the push token {TOKEN} [fd7a:115c:a1e0::1]")
    # A secret across the cut: masked first, so no part of it is left.
    shipper.put(loki.ENGINE, "info", "x" * (LINE_BYTES - 60) + APP_KEY + "y" * 100)
    shipper.flush()
    await until(lambda: shipper.status().sent == 3)
    sent = b"".join(fake.bodies).decode()
    for value in (APP_KEY, APP_KEY[:16], BIND_ADDRESS, TAILNET, GITHUB_TOKEN, TOKEN, "fd7a:115c"):
        assert value not in sent
    first, second, long = (line for _, line in fake.lines())
    assert first == f"refused key {MASK} from {MASK} via {MASK}: {MASK}"
    assert second == f"the push token {MASK} [{MASK}]"
    assert len(long.encode()) <= LINE_BYTES and long.endswith("bytes)")
    assert fake.auth == ["Basic " + base64.b64encode(f"{USER}:{TOKEN}".encode()).decode()]


def test_a_long_line_is_cut_and_says_so() -> None:
    assert cut("short") == "short"
    line = "가" * LINE_BYTES  # three bytes each
    shown = cut(line)
    assert len(shown.encode()) <= LINE_BYTES and shown.endswith(f"(cut: {3 * LINE_BYTES:,} bytes)")


def test_a_long_line_is_cut_at_a_space_before_it_waits_and_is_masked() -> None:
    # The cut never splits a value the masking takes: what is left of it would show.
    line = "a " * 10 + APP_KEY + "z" * 100
    assert bounded(line, limit=80) == "a a a a a a a a a a" + f" … (cut: {len(line):,} bytes)"
    assert bounded("x" * 100, limit=60) == " … (cut: 100 bytes)"
    assert bounded("short") == "short"
    korean = "가나 " * 30_000  # 7 bytes a word: about 210 KB in 90,000 characters
    cut_korean = bounded(korean)
    assert len(cut_korean.encode()) <= LINE_BYTES and cut_korean.endswith(f"(cut: {7 * 30_000:,} bytes)")


async def test_an_oversized_record_waits_cut_counted_in_bytes_and_within_the_cap(
    fake: FakeLoki, shippers: Callable[..., Shipper]
) -> None:
    # A training's whole output logged as one record (swap._train), in Korean: three bytes a letter.
    shipper = shippers(fake.url, every=60.0, max_bytes=LINE_BYTES * 5 // 2)  # two cut lines fit
    shipper.put(loki.ENGINE, "info", "학습 " * 200_000)  # 1.4 MB
    status = shipper.status()
    assert (status.buffered, status.dropped) == (1, 0)
    for _ in range(3):
        shipper.put(loki.ENGINE, "info", "가나 " * 15_000)  # 105 KB each, counted so, cut to 64 KB
    status = shipper.status()
    assert (status.dropped, status.buffered) == (2, 2), "the cap counts bytes, and holds"
    shipper.flush()
    await until(lambda: shipper.status().sent == 2)
    for _, line in fake.lines():
        assert len(line.encode()) <= LINE_BYTES and line.endswith("bytes)")


async def test_a_record_of_several_lines_goes_as_an_entry_a_line(fake: FakeLoki) -> None:
    handler, _ = loki.start(fake.values(), (), loki.COMMAND, "command=train", formatter(), every=60.0)
    assert handler is not None
    try:
        raise ZeroDivisionError("division by zero")
    except ZeroDivisionError:
        logging.getLogger("krx_quant.swap").exception("nightly training of %s failed:\n%s", NIGHT, "a\nb")
    assert await asyncio.to_thread(loki.stop) == 0
    taken = fake.lines()
    assert taken[0][1].startswith("command=train 20") and taken[0][1].endswith(
        "ERROR krx_quant.swap: nightly training of 2026-10-22 failed:"
    )
    assert [line for _, line in taken[1:3]] == ["command=train a", "command=train b"]
    assert taken[3][1] == "command=train Traceback (most recent call last):"
    assert taken[-1][1] == "command=train ZeroDivisionError: division by zero"
    assert {labels["level"] for labels, _ in taken} == {"error"}


async def test_a_record_marked_template_only_goes_without_its_values(
    fake: FakeLoki, caplog: pytest.LogCaptureFixture
) -> None:
    handler, _ = loki.start(fake.values(), (), loki.ENGINE, "", formatter(), every=60.0)
    assert handler is not None
    toss = logging.getLogger("krx_quant.supply")
    toss.warning("%s supply (%s) of %s not updated: %s", "toss", "foreign", "005930", ValueError("'1,234'"),
                 extra=TEMPLATE_ONLY)  # fmt: skip
    toss.warning("%d%% done", 50)
    assert await asyncio.to_thread(loki.stop) == 0
    (marked, plain) = (line for _, line in fake.lines())
    assert marked.endswith(
        "WARNING krx_quant.supply: … supply (…) of … not updated: … (values on the server only)"
    )
    assert "1,234" not in marked and "005930" not in marked
    assert plain.endswith("WARNING krx_quant.supply: 50% done")
    assert "'1,234'" in caplog.text, "stdout keeps it whole"


# What goes, and labelled how


def test_a_child_s_line_takes_its_own_level_or_the_one_before() -> None:
    assert level_of("2026-10-22 21:30:00,123 ERROR krx_quant.nightly: failed") == "error"
    assert level_of("Traceback (most recent call last):", "error") == "error"
    assert level_of("a report line") == "info"
    assert level_of("2026-10-22 21:30:00,123 WARNING krx_quant.x: y", "error") == "warning"


async def test_the_engine_sends_a_heartbeat_to_loki_only(
    fake: FakeLoki, caplog: pytest.LogCaptureFixture
) -> None:
    handler, _ = loki.start(fake.values(), (), loki.ENGINE, "", formatter(), every=0.05)
    assert handler is not None
    beating = asyncio.create_task(loki.keep_heartbeat(0.01))
    try:
        await until(lambda: any("heartbeat: sent" in line for _, line in fake.lines()))
    finally:
        beating.cancel()
    labels, line = next((labels, line) for labels, line in fake.lines() if "heartbeat" in line)
    assert labels == {"service": "krx-quant", "process": "engine", "level": "info"}
    assert re.search(r" INFO krx_quant\.loki: heartbeat: sent \d+, dropped 0, buffered \d+$", line)
    assert "heartbeat" not in caplog.text, "stdout does not have it"


# A command, from its start to its end


def command_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, values: dict[str, str]) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    for name, value in values.items():
        monkeypatch.setenv(name, value)


async def test_a_command_s_lines_go_by_its_end_with_its_start_and_end_lines(
    fake: FakeLoki, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)  # as the command's own logging is
    command_env(monkeypatch, tmp_path, fake.values())
    assert await asyncio.to_thread(main, ["doctor", "--api", "http://localhost:9"]) == 0
    taken = fake.lines()  # main sent what was left before it returned
    assert {labels["process"] for labels, _ in taken} == {"command"}
    assert all(line.startswith("command=doctor ") for _, line in taken)
    assert "krx-quant doctor --api http://localhost:9 started (pid " in taken[0][1]
    assert (
        taken[-1][0]["level"] == "info"
        and "krx-quant doctor --api http://localhost:9 ended: exit 0" in taken[-1][1]
    )
    assert any(
        line.endswith("INFO krx_quant.process_log: sending the log lines to Loki too (process=command)")
        for _, line in taken
    )
    assert loki.active() is None


async def test_nothing_is_sent_without_all_three_settings(
    fake: FakeLoki, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    command_env(monkeypatch, tmp_path, {loki.URL: fake.url, loki.USER: USER})
    assert await asyncio.to_thread(main, ["doctor", "--api", "http://localhost:9"]) == 0
    assert fake.bodies == [], "had it sent anything, the end would have sent the rest"
    assert "not sending the log lines to Loki: KRX_LOKI_TOKEN is not set: all three are needed" in caplog.text
    assert loki.status_json()["enabled"] is False


async def test_a_command_whose_loki_does_not_answer_ends_and_says_what_loki_missed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    command_env(monkeypatch, tmp_path, {loki.URL: "http://127.0.0.1:9", loki.USER: USER, loki.TOKEN: TOKEN})
    monkeypatch.setattr(loki, "EXIT_SECONDS", 0.3)
    assert await asyncio.to_thread(main, ["doctor", "--api", "http://localhost:9"]) == 0
    said = [r.getMessage() for r in caplog.records if r.name.startswith("krx_quant.")]
    assert "Loki: could not send the log lines (URLError(ConnectionRefusedError)); trying again," in " ".join(
        said
    )
    assert any(re.fullmatch(r"\d+ log lines were not sent to Loki; only stdout had them", m) for m in said)
    assert TOKEN not in caplog.text
    assert loki.active() is None


# The status


async def test_the_status_says_what_was_sent_as_the_api_s_fields(fake: FakeLoki) -> None:
    off = loki.status_json()
    assert off == {
        "enabled": False,
        "reason": off["reason"],
        "sent": 0,
        "dropped": 0,
        "buffered": 0,
        "last_success_at": None,
        "last_error": None,
        "last_error_at": None,
    }
    fake.answers.append(503)
    handler, _ = loki.start(fake.values(), (), loki.ENGINE, "", formatter(), every=60.0, first_retry=0.01)
    assert handler is not None
    handler.shipper.put(loki.ENGINE, "info", "one")
    handler.shipper.flush()
    await until(lambda: loki.status_json()["sent"] == 1)
    on = loki.status_json()
    assert (on["enabled"], on["reason"], on["dropped"], on["last_error"]) == (True, None, 0, "HTTP 503")
    assert on["buffered"] >= 0  # its own warning about the failure waits for the next batch
    assert TIME.match(on["last_success_at"]) and TIME.match(on["last_error_at"])


def doctor_status(loki_body: dict[str, Any] | None) -> Status:
    body: dict[str, Any] = {"mode": "live", "commit": "abc", "started_at": None, "phase": "closed"}
    if loki_body is not None:
        body["loki"] = loki_body
    return Status(body, "엔진 프로세스")


def test_doctor_shows_the_engine_s_shipping(tmp_path: Path) -> None:
    now = datetime(2026, 10, 23, 10, 0, 0, tzinfo=KST)
    settings = Settings("", "", state_dir=tmp_path / "state", data_dir=tmp_path / "data")
    shipping = {
        "enabled": True,
        "reason": None,
        "sent": 12345,
        "dropped": 7,
        "buffered": 3,
        "last_success_at": "2026-10-23T09:59:57+09:00",
        "last_error": "HTTP 503",
        "last_error_at": "2026-10-23T03:00:01+09:00",
    }
    text = report(settings, now, doctor_status(shipping))
    assert "8. Loki 전송 (#358, /v1/status의 loki)" in text
    assert "켜짐 · 보낸 줄 12,345 · 버린 줄 7 · 버퍼 3줄" in text
    assert "마지막 성공 09:59:57 (3초 전)" in text
    assert "마지막 오류 HTTP 503 (03:00:01)" in text
    off = {**shipping, "enabled": False, "reason": "KRX_LOKI_TOKEN is not set: all three are needed"}
    text = report(settings, now, doctor_status(off))
    assert "8. Loki 전송 (#358, /v1/status의 loki): 꺼짐(KRX_LOKI_TOKEN is not set" in text
    text = report(settings, now, Status(None, "", ("x: refused",)))
    assert "엔진 API에 닿지 않아 엔진의 전송 상태는 모른다" in text
    assert "이 명령이 읽은 설정: 꺼짐(KRX_LOKI_URL, KRX_LOKI_USER and KRX_LOKI_TOKEN are not set)" in text
    configured = Settings(
        "",
        "",
        state_dir=tmp_path / "state",
        loki_url="https://logs-prod-000.grafana.example",
        loki_user=USER,
        loki_token=TOKEN,
    )
    text = report(configured, now, doctor_status(None))
    assert "/v1/status에 loki가 없다(#358 전의 엔진)" in text and "이 명령이 읽은 설정: 켜짐" in text
