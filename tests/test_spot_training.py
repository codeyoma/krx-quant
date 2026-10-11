"""The nightly training on EC2 Spot, as the always-on server sees it (#23): the night's
result waited for in the Object Store and summarized to Telegram, and what the Spot run
needs from the server published there.

Input: promotion lists appearing in a local Object Store, the time, the server's settings
and market calendar. Output: the Telegram messages sent, what the wait reports, and what a
``krx-quant train`` run on another machine trains with.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import aiohttp
import boto3
import pytest
from moto import mock_aws

from krx_quant.cli import main
from krx_quant.collector import run_collector
from krx_quant.config import Settings
from krx_quant.fake_kis import FakeKis
from krx_quant.market_calendar import MarketCalendar
from krx_quant.notify import Alerts
from krx_quant.object_store import LocalObjectStore
from krx_quant.swap import await_spot_night, first_spot_wait, publish_training_inputs, spot_night_deadline
from tests.test_collector import KST, settings_for
from tests.test_nightly import DAYS, archive_bars
from tests.test_nightly import NIGHT as LAST_DAY
from tests.test_subscription_limit import until
from tests.test_swap import MORNING, NIGHT, TRAINED, at, night_list

CALENDAR = MarketCalendar({NIGHT: True, MORNING: True})


@pytest.fixture
async def alerts(kis: FakeKis) -> AsyncIterator[Alerts]:
    async with aiohttp.ClientSession() as session:
        sent = Alerts(session, "123:fake", "42", kis.rest_url)
        yield sent
        await sent.close()


def ticking(start: datetime, step: timedelta) -> Callable[[], datetime]:
    """A clock that moves ``step`` each time it is read."""
    moments = iter(start + n * step for n in range(10_000))
    return lambda: next(moments)


async def test_the_night_s_summary_is_sent_once_its_list_is_in_the_store(
    tmp_path: Path, kis: FakeKis, alerts: Alerts
) -> None:
    store = LocalObjectStore(tmp_path / "store")
    checks = 0

    def now() -> datetime:
        # The Spot run finishes while the server waits.
        nonlocal checks
        checks += 1
        if checks == 3:
            night_list(tmp_path / "store", finalized=at(NIGHT, "22:10"), promoted={"newton": TRAINED})
        return at(NIGHT, "21:30") + timedelta(minutes=checks)

    failure = await await_spot_night(
        store, CALENDAR, NIGHT, alerts, now, tmp_path / "state", poll_seconds=0.01
    )

    assert failure is None
    await alerts.close()
    [message] = kis.telegram_messages
    assert message["text"].startswith(f"[알림] {NIGHT} 밤 학습"), "as the training on the server sends it"


async def test_a_night_without_a_list_by_the_morning_deadline_says_so(
    tmp_path: Path, kis: FakeKis, alerts: Alerts
) -> None:
    store = LocalObjectStore(tmp_path / "store")

    clock = ticking(at(NIGHT, "21:30"), timedelta(hours=2))
    failure = await await_spot_night(
        store, CALENDAR, NIGHT, alerts, clock, tmp_path / "state", poll_seconds=0.01
    )

    assert failure is not None and f"{MORNING} 07:30" in failure, failure
    await alerts.close()
    assert kis.telegram_messages == [], "the training alert is the caller's"


async def test_a_night_s_summary_is_sent_once_whatever_the_restarts(
    tmp_path: Path, kis: FakeKis, alerts: Alerts
) -> None:
    store = LocalObjectStore(tmp_path / "store")
    night_list(tmp_path / "store", finalized=at(NIGHT, "22:10"), promoted={"newton": TRAINED})

    # The collector restarted after the night was done, and waited for it again.
    for _ in range(2):
        found = await await_spot_night(
            store, CALENDAR, NIGHT, alerts, lambda: at(NIGHT, "23:00"), tmp_path / "state", poll_seconds=0.01
        )
        assert found is None

    await alerts.close()
    assert len(kis.telegram_messages) == 1


class Flaky(LocalObjectStore):
    """A store that cannot be reached for its first few reads."""

    def __init__(self, root: Path, failures: int) -> None:
        super().__init__(root)
        self.failures = failures

    def digest(self, key: str) -> str | None:
        if self.failures:
            self.failures -= 1
            raise OSError("connection reset")
        return super().digest(key)


async def test_a_store_that_fails_for_a_while_does_not_end_the_wait(
    tmp_path: Path, kis: FakeKis, alerts: Alerts
) -> None:
    night_list(tmp_path / "store", finalized=at(NIGHT, "22:10"), promoted={"newton": TRAINED})
    store = Flaky(tmp_path / "store", failures=3)

    found = await await_spot_night(
        store, CALENDAR, NIGHT, alerts, lambda: at(NIGHT, "23:00"), tmp_path / "state", poll_seconds=0.01
    )

    assert found is None
    await alerts.close()
    assert len(kis.telegram_messages) == 1


def test_a_server_restarted_in_the_morning_still_waits_for_last_night() -> None:
    # The 06:00 update restarts the collector: the night is still due until 07:30.
    assert first_spot_wait(CALENDAR, time(21, 30), at(MORNING, "06:00")) == at(NIGHT, "21:30")
    assert first_spot_wait(CALENDAR, time(21, 30), at(MORNING, "07:40")) == at(MORNING, "21:30")


async def test_with_the_spot_training_off_for_the_cost_cap_a_missing_night_is_no_failure(
    tmp_path: Path, kis: FakeKis, alerts: Alerts
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "cost_guard.json").write_text(
        json.dumps({"month": "2026-10", "warned": [50, 80], "stopped": True, "stop_told": True})
    )
    clock = ticking(at(NIGHT, "21:30"), timedelta(hours=2))

    failure = await await_spot_night(
        LocalObjectStore(tmp_path / "store"), CALENDAR, NIGHT, alerts, clock, state, poll_seconds=0.01
    )

    assert failure is None
    await alerts.close()
    assert kis.telegram_messages == []


def test_a_night_on_a_friday_is_waited_for_until_monday_morning() -> None:
    friday, monday = date(2026, 10, 16), date(2026, 10, 19)

    assert spot_night_deadline(MarketCalendar({friday: True, monday: True}), friday) == at(monday, "07:30")


def test_a_spot_run_trains_with_the_server_s_symbols_and_calendar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store_root = tmp_path / "store"
    for day in DAYS:
        archive_bars(store_root, day, symbol="000660")
    server_state = tmp_path / "server-state"
    server_state.mkdir()
    calendar = {"refreshed_on": "2026-10-11", "open_days": {"2026-10-09": False, "2026-10-12": True}}
    (server_state / "market_calendar.json").write_text(json.dumps(calendar))
    server_env = {
        "KRX_SYMBOLS": "000660",
        "KRX_STATE_DIR": str(server_state),
        "KRX_OBJECT_STORE": str(store_root),
    }
    server = Settings.from_env(server_env, None, require_kis=False)

    publish_training_inputs(LocalObjectStore(store_root), server)

    # The Spot machine knows only the store; its defaults would train 005930, which has no bars.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_OBJECT_STORE", str(store_root))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "spot-state"))
    monkeypatch.delenv("KRX_SYMBOLS", raising=False)
    assert main(["train", "--from-store"]) == 0, capsys.readouterr().err
    assert (store_root / f"promotions/{LAST_DAY}.json").exists()
    assert json.loads((tmp_path / "spot-state" / "market_calendar.json").read_text()) == calendar


async def test_with_the_training_on_spot_the_collector_publishes_its_inputs_and_awaits_the_night(
    kis: FakeKis, tmp_path: Path
) -> None:
    store_root = tmp_path / "store"
    trained = date(2026, 10, 8)
    night_list(store_root, finalized=datetime(2026, 10, 8, 22, 5, tzinfo=KST), promoted={}, day=trained)
    settings = replace(
        settings_for(kis, tmp_path),
        object_store=str(store_root),
        train_at=time(21, 30),
        train_on="spot",
        telegram_bot_token="123:fake",
        telegram_chat_id="42",
        telegram_api_url=kis.rest_url,
    )

    stop = asyncio.Event()
    clock = lambda: datetime(2026, 10, 8, 22, 30, tzinfo=KST)  # noqa: E731
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=clock))
    try:
        await until(lambda: any(f"{trained} 밤 학습" in m["text"] for m in kis.telegram_messages))
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)

    # Not trained here: the summary is the Spot night's, not a failure of its own run.
    assert not any("실패" in m["text"] for m in kis.telegram_messages)
    assert json.loads((store_root / "training/settings.json").read_text())["KRX_SYMBOLS"] == "005930"


def test_the_server_logs_docker_in_to_ecr_with_its_own_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    with mock_aws():
        token = boto3.client("ecr", region_name="ap-southeast-2").get_authorization_token()
        user, password = (
            base64.b64decode(token["authorizationData"][0]["authorizationToken"]).decode().split(":")
        )

        assert main(["registry-password"]) == 0

    # For `docker login --username AWS --password-stdin` (deploy/push-image.sh).
    assert user == "AWS" and capsys.readouterr().out.strip() == password
