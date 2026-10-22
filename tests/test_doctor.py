"""``krx-quant doctor`` (#322): the server's state in one masked summary, with no market value.

The secret-looking values here are put together at run time, so the repository's own check
(``scripts/check_forbidden.py``) does not find them in this file.
"""

from __future__ import annotations

import ast
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from krx_quant import kis, loki
from krx_quant.cli import main
from krx_quant.config import Settings
from krx_quant.doctor import (
    COMPOSE_API,
    RecentLog,
    Status,
    api_candidates,
    doctor,
    doctor_settings,
    engine_status,
    fetch_status,
    report,
)
from krx_quant.feeds import RAW_SCHEMA
from krx_quant.feeds.base import KST
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.recorder import ParquetRecorder
from krx_quant.redaction import MASK, secrets_of
from krx_quant.scheduled import JOBS, STATE_FILE

NOW = datetime(2026, 10, 20, 10, 30, 0, tzinfo=KST)
COMMIT = "0123456789abcdef0123456789abcdef01234567"
APP_KEY = "P" + "k" * 35
GITHUB_TOKEN = "github_pat_" + "B" * 22 + "_" + "C" * 59
BIND_ADDRESS = "100.101.102.103"
PRICE = "71234.5"  # a market value the status carries that the summary must not show

PINGPONG = json.dumps({"header": {"tr_id": "PINGPONG", "datetime": "20261020093000"}})


def server(tmp_path: Path, **changes: Any) -> Settings:
    settings = Settings(
        kis_app_key=APP_KEY,
        kis_app_secret="S" + "s" * 179,
        github_token=GITHUB_TOKEN,
        bind_address=BIND_ADDRESS,
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        commit=COMMIT,
    )
    return replace(settings, **changes)


def status_body() -> dict[str, Any]:
    """``/v1/status`` as the engine gives it, with fields the summary leaves out."""
    return {
        "mode": "live",
        "now": "2026-10-20T10:30:00+09:00",
        "commit": COMMIT,
        "started_at": "2026-10-20T06:00:31+09:00",
        "phase": "regular",
        "session": {"open": "2026-10-20T09:00:00+09:00", "close": "2026-10-20T15:30:00+09:00"},
        "usd_futures": {"price": float(PRICE), "at": "2026-10-20T10:29:58+09:00", "previous_close": 1380.0},
        "special": [{"kind": "vi", "symbol": "005930", "start": "x", "end": None}],
        "kis": {
            "connected": True,
            "last_frame_at": "2026-10-20T10:29:57+09:00",
            "seconds_since_frame": 3.2,
            "feeds": [
                {
                    "feed": "H0UNCNT0",
                    "key": "005930",
                    "last_received_at": "2026-10-20T10:29:59.5+09:00",
                    "seconds_since": 0.5,
                    "gap": None,
                },
                {
                    "feed": "H0UNASP0",
                    "key": "005930",
                    "last_received_at": "2026-10-19T16:33:10+09:00",
                    "seconds_since": 64010.0,
                    "gap": f"disconnected: lost {BIND_ADDRESS}",
                },
                {
                    "feed": "H0MFCNT0",
                    "key": "A05",
                    "last_received_at": None,
                    "seconds_since": None,
                    "gap": None,
                },
            ],
        },
    }


def record_pingpongs(data_dir: Path, times: list[datetime]) -> None:
    recorder = ParquetRecorder(data_dir, 10_000, 3600)
    for at in times:
        recorder.record(
            "raw",
            NOW.date(),
            "_system",
            "_",
            {"received_at": at.astimezone(UTC), "message": PINGPONG},
            RAW_SCHEMA,
        )
    # A subscription answer of the same partition is not a heartbeat.
    answer = json.dumps({"header": {"tr_id": "H0UNCNT0", "tr_key": ""}, "body": {"rt_cd": "0", "msg1": "OK"}})
    recorder.record(
        "raw", NOW.date(), "_system", "_", {"received_at": NOW.astimezone(UTC), "message": answer}, RAW_SCHEMA
    )
    recorder.close()


def write_jobs(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "jobs": {
            JOBS[0].name: {
                "day": "2026-10-16",
                "outcome": "ok",
                "started_at": "2026-10-16T22:30:00+09:00",
                "commit": COMMIT,
                "exit_code": 0,
                "seconds": 72.0,
                "timed_out": False,
                "comments": {"203": "https://github.com/x/y/issues/203#issuecomment-1"},
            },
            JOBS[1].name: {
                "day": "2026-10-19",
                "outcome": "failed",
                "started_at": "2026-10-19T22:31:00+09:00",
                "commit": COMMIT,
                "exit_code": 1,
                "seconds": 3.0,
                "timed_out": False,
                "comments": {},
            },
        },
        "unposted": [
            {"job": JOBS[1].name, "day": "2026-10-19", "issue": 206, "body": "b", "label": [], "unlabel": []}
        ],
    }
    (state_dir / STATE_FILE).write_text(json.dumps(data))


def logs_with(*messages: tuple[int, str]) -> RecentLog:
    recent = RecentLog()
    logger = logging.getLogger("krx_quant.test_doctor")
    logger.addHandler(recent)
    try:
        for level, message in messages:
            logger.log(level, message)
    finally:
        logger.removeHandler(recent)
    return recent


def test_the_summary_has_its_eight_parts_with_the_fields_as_the_api_names_them(tmp_path: Path) -> None:
    settings = server(tmp_path)
    record_pingpongs(
        settings.data_dir,
        [datetime(2026, 10, 20, 8, 0, s, tzinfo=KST) for s in (0, 11, 22)]
        + [datetime(2026, 10, 20, 10, 29, 40, tzinfo=KST)],
    )
    write_jobs(settings.state_dir)
    logs = logs_with((logging.INFO, "not kept"), (logging.WARNING, "KIS WebSocket silent for 60s"))
    text = report(settings, NOW, Status(status_body(), "이 컨테이너의 엔진 API"), logs)

    titles = [line for line in text.splitlines() if line[:2] in {f"{n}." for n in range(1, 10)}]
    assert [t[:2] for t in titles] == ["1.", "2.", "3.", "4.", "5.", "6.", "7.", "8."]
    assert titles[-1].startswith("8. Loki 전송"), "the log folders' section went in #360"
    assert text.startswith("서버 상태 2026-10-20 10:30:00 KST")
    assert f"commit {COMMIT} · started_at 2026-10-20T06:00:31+09:00 (4시간 29분 전)" in text
    assert "mode live · phase regular" in text
    assert "connected true · last_frame_at 2026-10-20T10:29:57+09:00 · seconds_since_frame 3.2" in text
    assert "feeds 3개 · 60초 넘게 조용하거나 아직 안 온 키 2개 · 열린 gap 1개" in text
    assert "feed key last_received_at seconds_since gap" in text
    assert "H0UNCNT0 005930 10:29:59 0.5 -" in text
    assert f"H0UNASP0 005930 2026-10-19 16:33:10 64010.0 disconnected: lost {MASK}" in text
    assert "H0MFCNT0 A05 - - -" in text
    # Today's heartbeats: four of them, the subscription answer left out.
    assert "합계 4 · 마지막 10:29:40 (20초 전)" in text
    assert "PINGPONG 사이 가장 긴 간격: 08:00:22–10:29:40 (2시간 29분)" in text
    assert "08시 3 · 09시 0 · 10시 1" in text
    # The engine's own log lines: WARNING and up only.
    assert "WARNING 1줄 · ERROR 0줄" in text
    assert "WARNING krx_quant.test_doctor: KIS WebSocket silent for 60s" in text
    assert "not kept" not in text
    # The scheduled jobs: the latest first, the ones not run to an end, the result not posted.
    lines = text.splitlines()
    first, second = (
        i
        for i, line in enumerate(lines)
        if line.startswith((f"    {JOBS[0].name} · ", f"    {JOBS[1].name} · "))
    )
    assert lines[first].startswith(f"    {JOBS[1].name} · 2026-10-19 밤 · failed · 종료 코드 1")
    assert lines[second].startswith(f"    {JOBS[0].name} · 2026-10-16 밤 · ok · 종료 코드 0 · 1분 12초")
    assert lines[second].endswith("댓글 #203")
    assert f"끝까지 돈 적 없는 작업 {len(JOBS) - 2}개: {JOBS[2].name}" in text
    assert f"{JOBS[1].name} (2026-10-19 밤) → #206" in text
    assert "6. 디스크와 메모리" in text and "데이터 " in text
    # The nightly training (#355): nothing kept yet here.
    assert "마지막으로 적용된 밤 없음 · 그 뒤 적용되지 않은 거래일 밤 0번" in text
    assert "기록된 밤 없음(이 기록은 #355부터)" in text


def test_it_shows_no_market_value_nor_a_secret_nor_the_server_s_address(tmp_path: Path) -> None:
    settings = server(tmp_path)
    logs = logs_with((logging.ERROR, f"refused key {APP_KEY} from {BIND_ADDRESS}: {GITHUB_TOKEN}"))
    text = report(settings, NOW, Status(status_body(), "엔진 프로세스"), logs)
    assert PRICE not in text and "71234" not in text and "1380" not in text
    assert "usd_futures" not in text and "previous_close" not in text
    for secret in (APP_KEY, GITHUB_TOKEN, BIND_ADDRESS):
        assert secret not in text
    assert f"refused key {MASK} from {MASK}: {MASK}" in text


def test_a_log_line_is_one_line_with_its_error_and_cut_when_long(tmp_path: Path) -> None:
    recent = RecentLog(keep=2)
    logger = logging.getLogger("krx_quant.test_doctor.long")
    logger.addHandler(recent)
    try:
        logger.warning("first\nsecond line of it")
        try:
            raise ValueError("bad frame\nwith more")
        except ValueError:
            logger.exception("could not normalize")
        logger.error("x" * 500)
    finally:
        logger.removeHandler(recent)
    first, second = recent.lines()  # the oldest went: two are kept
    assert first.endswith("ERROR krx_quant.test_doctor.long: could not normalize | ValueError")
    assert len(second) == 300 and second.endswith("…")
    assert recent.counts() == {"WARNING": 1, "ERROR": 2}


def test_no_market_value_reaches_the_retained_log_lines(tmp_path: Path) -> None:
    # A malformed KIS frame's parse error carries the frame's head, prices in it (#322 review).
    frame = "0|H0UNCNT0|x1|005930^093000^71234^5^-100^-0.14^71210.00^71000^71500^70900^71240^71230^3"
    with pytest.raises(kis.KisError) as caught:
        kis.parse_message(frame)
    assert "71234" in str(caught.value)
    recent = RecentLog()
    engine_log = logging.getLogger("krx_quant.collector")
    library_log = logging.getLogger("asyncio")
    engine_log.addHandler(recent)
    library_log.addHandler(recent)
    try:
        engine_log.warning("unparseable message kept raw: %s", caught.value)  # as _Stream._handle does
        try:
            raise ValueError(f"row {frame}")
        except ValueError:
            engine_log.exception("could not normalize %s frame; kept raw only", "H0UNCNT0")
        engine_log.warning("auction prices of %s on %s not kept: %d", "005930", NOW.date(), 71234)
        engine_log.warning("KIS WebSocket silent for %gs (since %s): %s", 60.0, NOW, True)
        library_log.error("Task exception was never retrieved: %s", frame)
    finally:
        engine_log.removeHandler(recent)
        library_log.removeHandler(recent)
    lines = recent.lines()
    assert not [line for line in lines if "71234" in line or "005930" in line or "71210" in line]
    unparseable, normalize, auction, silent, library = (line.split(": ", 1)[1] for line in lines)
    assert unparseable == "unparseable message kept raw: …"
    assert normalize == "could not normalize H0UNCNT0 frame; kept raw only | ValueError"
    assert auction == "auction prices of … on 2026-10-20 not kept: …"
    assert silent == "KIS WebSocket silent for …s (since 2026-10-20 10:30:00): True"
    assert library == "(krx_quant 밖의 로거라 메시지는 싣지 않는다)"
    text = report(server(tmp_path), NOW, Status(status_body(), "엔진 프로세스"), recent)
    assert "71234" not in text and "unparseable message kept raw: …" in text
    assert "코드의 메시지 틀만 싣는다" in text


def test_every_engine_log_message_is_a_literal_template() -> None:
    """What keeps runtime text out of the retained lines: a log call's message is the code's
    own literal, its values given apart (``log.warning("... %s", value)``), never an f-string or
    a string made at run time."""
    levels = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
    found = []
    for path in sorted(Path(__file__).parents[1].joinpath("src", "krx_quant").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            owner = node.func.value
            if node.func.attr not in levels or not (
                (isinstance(owner, ast.Name) and owner.id in ("log", "logger", "logging"))
                or (isinstance(owner, ast.Call) and "getLogger" in ast.unparse(owner.func))
            ):
                continue
            args = node.args[1:] if node.func.attr == "log" else node.args
            if not (args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str)):
                found.append(f"{path.name}:{node.lineno}: {ast.unparse(node)[:80]}")
    assert found == []


def test_a_malformed_setting_is_a_finding_not_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A setting the engine refuses (it is down for it): the summary still prints (#322 review).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_TRAIN_AT", "late")
    monkeypatch.setenv("KIS_APP_KEY", APP_KEY)
    monkeypatch.setenv("KRX_COMMIT", COMMIT)
    (tmp_path / "state").mkdir()
    store = PredictionStore(tmp_path / "state" / DB_NAME)
    store.add_run("start", NOW, None, json.dumps({"commit": COMMIT}))
    store.close()
    assert main(["doctor", "--api", "http://127.0.0.1:9"]) == 0
    out = capsys.readouterr().out
    assert "설정 오류: KRX_TRAIN_AT must be HH:MM or off, got 'late'" in out
    assert "엔진은 이 설정으로 시작하지 못한다" in out
    assert f"DB의 마지막 시작(runs, kind start): 2026-10-20T10:30:00+09:00, commit {COMMIT}" in out
    assert f"이 명령의 이미지 커밋(KRX_COMMIT): {COMMIT}" in out
    assert "5. 예약 작업 (state/scheduled.json, KRX_JOBS_AT ?" in out and "6. 디스크와 메모리" in out
    assert APP_KEY not in out


def test_the_lenient_settings_mask_what_the_full_ones_do(tmp_path: Path) -> None:
    env = {
        "KIS_APP_KEY": APP_KEY,
        "KIS_APP_SECRET": "S" + "s" * 179,
        "TELEGRAM_BOT_TOKEN": "1234567890" + ":" + "A" * 35,
        "TELEGRAM_CHAT_ID": "987654321",
        "AWS_ACCESS_KEY_ID": "AKIA" + "Q" * 16,
        "AWS_SECRET_ACCESS_KEY": "w" * 40,
        "KRX_ECOS_API_KEY": "E" * 20,
        "KRX_FRED_API_KEY": "f" * 32,
        "KRX_GITHUB_TOKEN": GITHUB_TOKEN,
        "TOSS_CLIENT_ID": "T" * 24,
        "TOSS_CLIENT_SECRET": "t" * 48,
        "KRX_OPENAPI_KEY": "K" * 40,
        "KRX_OBJECT_STORE": "s3://krx-quant-example/engine",
        "KRX_S3_ENDPOINT_URL": "https://objects.example.test",
        "KRX_BIND_ADDRESS": BIND_ADDRESS,
        "KRX_LOKI_URL": "https://logs-prod-000.grafana.example",
        "KRX_LOKI_USER": "12345678",
        "KRX_LOKI_TOKEN": "glc_" + "L" * 40,
        "KRX_DATA_DIR": str(tmp_path / "data"),
        "KRX_API_PORT": "8081",
    }
    full, error = doctor_settings(env, None)
    assert error is None
    lenient, error = doctor_settings({**env, "KRX_TARGET_VERSION": "2"}, None)
    assert error is not None and error.startswith("KRX_TARGET_VERSION 2")
    assert secrets_of(lenient) == secrets_of(full) and len(secrets_of(full)) == 19
    assert (lenient.data_dir, lenient.state_dir, lenient.api_port) == (tmp_path / "data", Path("state"), 8081)
    bad_port, _ = doctor_settings({**env, "KRX_API_PORT": "soon"}, None)
    assert bad_port.api_port == 8080


def test_the_command_cannot_read_the_engine_s_log_and_says_how_to(tmp_path: Path) -> None:
    text = report(server(tmp_path), NOW, Status(status_body(), "x"))
    assert "엔진이 기억하는 줄을 읽지 못한다" in text
    assert "docker compose logs --since 2h engine" in text
    assert "엔진의 전체 줄은 Grafana Cloud Loki에 있다(8절, docs/agents/server-logs.md)" in text  # #358
    assert "state/logs" not in text and "logs-bundle" not in text  # gone in #360


def test_without_the_api_it_says_why_and_reads_the_last_start_from_the_db(tmp_path: Path) -> None:
    settings = server(tmp_path)
    settings.state_dir.mkdir(parents=True)
    store = PredictionStore(settings.state_dir / DB_NAME)
    store.add_run("start", NOW - timedelta(hours=1), None, json.dumps({"commit": "feedface" * 5}))
    store.close()
    status = Status(None, "", ("이 컨테이너의 엔진 API: ClientConnectorError: refused",))
    text = report(settings, NOW, status)
    assert "1. 엔진: 엔진 API에 닿지 않는다" in text
    assert "이 컨테이너의 엔진 API: ClientConnectorError: refused" in text
    assert f"DB의 마지막 시작(runs, kind start): 2026-10-20T09:30:00+09:00, commit {'feedface' * 5}" in text
    assert f"이 명령의 이미지 커밋(KRX_COMMIT): {COMMIT}" in text
    assert "2. KIS: 엔진 API에 닿지 않아 모른다" in text
    assert "3. PINGPONG 오늘 (raw/date=2026-10-20/feed=_system/symbol=_): 기록 없음" in text
    assert "돈 작업이 없다" in text


def test_a_state_file_it_cannot_read_is_said_and_left_where_it_is(tmp_path: Path) -> None:
    settings = server(tmp_path)
    settings.state_dir.mkdir(parents=True)
    (settings.state_dir / STATE_FILE).write_text("{broken")
    text = report(settings, NOW, Status(status_body(), "x"))
    assert (
        "5. 예약 작업 (state/scheduled.json, KRX_JOBS_AT off" in text and "읽지 못함(JSONDecodeError" in text
    )
    assert (settings.state_dir / STATE_FILE).read_text() == "{broken"  # not set aside: it only reads
    assert "6. 디스크와 메모리" in text


def test_a_part_that_fails_leaves_the_others(tmp_path: Path) -> None:
    body = status_body()
    body["kis"]["feeds"] = [{"feed": "X", "key": "Y", "seconds_since": "soon"}]  # not a number
    text = report(server(tmp_path), NOW, Status(body, "x"))
    assert "2. KIS: 읽지 못함(TypeError" in text
    assert "1. 엔진 (/v1/status, x)" in text and "3. PINGPONG" in text and "6. 디스크와 메모리" in text


def test_the_apis_asked_are_this_container_s_then_compose_s_engine() -> None:
    inside = Settings(kis_app_key="", kis_app_secret="", api_host="0.0.0.0", api_port=8080)
    assert api_candidates(inside) == [
        ("이 컨테이너의 엔진 API", "http://127.0.0.1:8080"),
        ("compose `engine` 서비스의 엔진 API", COMPOSE_API),
    ]
    elsewhere = Settings(kis_app_key="", kis_app_secret="", api_host="::1", api_port=9000)
    assert api_candidates(elsewhere)[0][1] == "http://[::1]:9000"


@asynccontextmanager
async def engine_api(body: Any, status: int = 200) -> AsyncIterator[str]:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_get("/v1/status", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        yield str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


async def test_the_status_comes_from_the_first_api_that_answers() -> None:
    async with engine_api(status_body()) as up, engine_api({}, status=503) as broken:
        async with aiohttp.ClientSession() as session:
            status = await fetch_status(
                session, [("closed", "http://127.0.0.1:9"), ("broken", broken), ("engine", up)]
            )
    assert status.body is not None and status.body["commit"] == COMMIT
    assert status.source == "engine"
    assert status.errors[0].startswith("closed: ClientConnector")
    assert status.errors[1] == "broken: HTTP 503"


async def test_the_command_prints_the_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no .env of the checkout
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_COMMIT", COMMIT)
    async with engine_api(status_body()) as url:
        text = await doctor(Settings.from_env(dotenv_path=None, require_kis=False), NOW, url)
    assert "1. 엔진 (/v1/status, --api로 준 엔진 API)" in text
    assert "H0UNCNT0 005930" in text


def test_the_cli_command_runs_without_kis_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("KIS_APP_KEY", raising=False)
    assert main(["doctor", "--api", "http://127.0.0.1:9"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("서버 상태 ")
    assert "1. 엔진: 엔진 API에 닿지 않는다" in out and "--api로 준 엔진 API: ClientConnector" in out


def test_the_engine_s_own_status_has_the_fields_of_v1_status() -> None:
    class Calendar:
        def phase(self, at: datetime) -> Any:
            class Phase:
                value = "regular"

            return Phase()

    class Clock:
        def now(self) -> datetime:
            return NOW

    class Engine:
        commit = COMMIT
        started_at = NOW - timedelta(hours=4)
        calendar = Calendar()
        clock = Clock()

    def live() -> dict[str, Any]:
        return {"connected": True, "last_frame_at": None, "seconds_since_frame": None, "feeds": []}

    status = engine_status(cast(Any, Engine()), live)
    assert status.source == "엔진 프로세스"
    assert status.body == {
        "mode": "live",
        "now": "2026-10-20T10:30:00+09:00",
        "commit": COMMIT,
        "started_at": "2026-10-20T06:30:00+09:00",
        "phase": "regular",
        "kis": live(),
        "loki": loki.status_json(),  # this process's: off here (#358)
    }
