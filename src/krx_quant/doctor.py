"""``krx-quant doctor`` (#322): what the server is doing, in one masked summary.

It reads only and prints no market value (no price, volume or flow): what a person on the
server, or an agent reading an issue, looks at first when something seems wrong.

1. The engine: its code (``commit``), when it started (``started_at``), its mode and the
   market phase.
2. KIS: whether the WebSocket is ``connected``, its last frame (``last_frame_at``,
   ``seconds_since_frame``, #311), and each feed and key's ``last_received_at``,
   ``seconds_since`` and open ``gap``, as ``/v1/status`` names them.
3. Today's PINGPONG heartbeats by hour, the last one and the longest silence between two
   (``raw/date=<today>/feed=_system/symbol=_``).
4. The latest WARNING and ERROR log lines, where the process can read the engine's memory: the
   collector keeps its own (``RecentLog``); the command, a process of its own, cannot, and says
   where the engine's whole lines are (Loki, #358; ``docker compose logs``). No runtime text
   reaches them, so no
   market value can (a raw KIS frame's excerpt in a parse error, a price in an exception): a
   line is the template the code logged with (``krx_quant``'s loggers only; every engine log
   call's message is a literal, a test checks), its values shown only when they are a date or
   time, a truth value, None or a name-shaped word (``H0UNCNT0``, ``kalman``) and ``…``
   otherwise, and an exception's type without its message. Another library's line shows its
   logger and level only.
5. The scheduled jobs (``state/scheduled.json``, #215): their last runs, the jobs that have not
   run to an end yet, the results not posted.
6. The free disk under the data and state directories, the disk alert's threshold and the disks
   it alerts now (#379, ``disk_watch``), and the memory.
7. The nightly training (#355, ``training_nights``): the last night the morning swap applied,
   the trading nights not applied since and the failures among them, and the latest night's
   outcome: its exit code and the last step it started.
8. The log lines sent to Grafana Cloud Loki (#358, ``loki.py``), as ``/v1/status``'s ``loki``
   says: on or off (and why), the lines sent, dropped and waiting, the last success and the last
   failure's kind (``HTTP 503``, never its text). Without the engine, what this command's
   settings say. (Section 8 was the log files under ``state/logs`` until #360 removed them.)
9. The model files (#361): how many and how large, in ``state/models`` (the models the engine
   runs, and ``versions/``) and in the store's ``models/``, with the algorithm that has the most
   (two each once pruned; the store may keep a third, a night's promoted model not applied yet).

The command reads the settings leniently (``doctor_settings``): a malformed one, which keeps
the engine from starting, is a finding at the top, and the rest comes from the data and state
directories, the API's address and the secrets to mask, read as they are.

The engine's state comes from its own API (``/v1/status``): this container's
(``docker compose exec engine krx-quant doctor``), else compose's engine service
(``docker compose run --rm engine doctor`` is a container of its own, on compose's network);
when neither answers, the engine DB's last start stands in. In the collector (the snapshot an
issue's label asks for, ``snapshot.py``) the engine is read directly. A part that cannot be
read says why; the rest is still printed. The whole is masked (``redaction.py``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant import kis, loki
from krx_quant.archive import partition_files
from krx_quant.config import Settings, SettingsError, read_values
from krx_quant.disk_watch import LABELS as DISK_LABELS
from krx_quant.disk_watch import alerted as disk_alerted
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import calendar_store
from krx_quant.model_shelf import Shelf, size_text
from krx_quant.object_store import open_store
from krx_quant.predictions import DB_NAME, iso, read_only
from krx_quant.redaction import redact, secrets_of
from krx_quant.scheduled import JOBS, STATE_FILE
from krx_quant.swap import MODELS, SWAP_AT
from krx_quant.training_nights import FILE as NIGHTS_FILE
from krx_quant.training_nights import TrainingNights

if TYPE_CHECKING:
    from krx_quant.engine import PredictionEngine
    from krx_quant.stream import LiveStatus

API_TIMEOUT = aiohttp.ClientTimeout(total=5)
# The Engine API of compose's ``engine`` service, from another container on compose's network
# (``docker compose run``): the service's name, and the port it listens on inside (compose.yaml).
COMPOSE_API = "http://engine:8080"
LOG_LINES = 20  # the latest WARNING and ERROR lines shown
LOG_KEEP = 200  # how many the collector keeps
LOG_WIDTH = 300  # a line longer than this is cut
QUIET_SECONDS = 60.0  # a feed key quiet this long is counted apart
LAST_RUNS = 12  # the scheduled jobs' latest runs shown
LAST_NIGHTS = 5  # the nightly training's latest nights shown
HOURS_PER_ROW = 8
_MB = 1024 * 1024
_GB = 1024 * _MB


@dataclass(frozen=True)
class Status:
    """What the engine said of itself (``/v1/status``'s body), where from, or why not."""

    body: Mapping[str, Any] | None
    source: str  # which API answered, or "the engine process"
    errors: tuple[str, ...] = ()  # the APIs that did not answer, and why


def api_candidates(settings: Settings) -> list[tuple[str, str]]:
    """The Engine APIs to ask, in order: (what it is, base address)."""
    host = settings.api_host.strip()
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"  # listening on every interface: this one answers in the same container
    if ":" in host:
        host = f"[{host}]"
    local = f"http://{host}:{settings.api_port or 8080}"
    found = [("이 컨테이너의 엔진 API", local)]
    if local != COMPOSE_API:
        found.append(("compose `engine` 서비스의 엔진 API", COMPOSE_API))
    return found


async def fetch_status(session: aiohttp.ClientSession, candidates: Sequence[tuple[str, str]]) -> Status:
    """``/v1/status`` from the first of ``candidates`` that answers."""
    errors: list[str] = []
    for name, base in candidates:
        try:
            async with session.get(f"{base.rstrip('/')}/v1/status", timeout=API_TIMEOUT) as resp:
                if resp.status != 200:
                    errors.append(f"{name}: HTTP {resp.status}")
                    continue
                body = await resp.json()
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as e:
            errors.append(f"{name}: {_error(e)}")
            continue
        if isinstance(body, dict):
            return Status(body, name, tuple(errors))
        errors.append(f"{name}: 상태가 아닌 응답({type(body).__name__})")
    return Status(None, "", tuple(errors))


def engine_status(engine: PredictionEngine, live: LiveStatus | None) -> Status:
    """The part of ``/v1/status`` the summary shows, read from the engine itself (in the
    collector, on the event loop's thread: the engine is its)."""
    now = engine.clock.now()
    body: dict[str, Any] = {
        "mode": "live",
        "now": iso(now),
        "commit": engine.commit,
        "started_at": iso(engine.started_at),
        "phase": engine.calendar.phase(now).value,
    }
    if live is not None:
        body["kis"] = live()
    body["loki"] = loki.status_json()  # this process's: the engine's (#358)
    return Status(body, "엔진 프로세스")


class RecentLog(logging.Handler):
    """The collector's latest WARNING and ERROR records, as lines, for its snapshots."""

    def __init__(self, keep: int = LOG_KEEP) -> None:
        super().__init__(logging.WARNING)
        self._lines: deque[str] = deque(maxlen=keep)
        self._counts: Counter[str] = Counter()
        self._guard = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = _log_line(record)
        except Exception:
            self.handleError(record)
            return
        with self._guard:
            self._lines.append(line)
            self._counts[record.levelname] += 1

    def lines(self) -> list[str]:
        with self._guard:
            return list(self._lines)

    def counts(self) -> dict[str, int]:
        with self._guard:
            return dict(self._counts)


# The engine's own loggers: their messages are literal templates (a test checks).
ENGINE_LOGGER = "krx_quant"
HIDDEN = "…"  # a logged value not shown
# A printf conversion in a log template (``%s``, ``%.1f``, ``%(name)s``, ``%%``).
_SPEC = re.compile(r"%(?:\([^)]*\))?[#0\- +]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?([diouxXeEfFgGcrsa%])")
# A value shown as it is: a name, such as a TR ID, a feed or an algorithm (a letter first, so no
# number, price or symbol code; no space, so no message).
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,39}")


def _log_line(record: logging.LogRecord) -> str:
    """A record as the snapshot may show it: no runtime text, so no market value (#322)."""
    at = datetime.fromtimestamp(record.created, KST)
    if record.name == ENGINE_LOGGER or record.name.startswith(f"{ENGINE_LOGGER}."):
        text = _template(record)
    else:
        text = "(krx_quant 밖의 로거라 메시지는 싣지 않는다)"
    if record.exc_info and record.exc_info[1] is not None:
        text += f" | {type(record.exc_info[1]).__name__}"  # its message may hold anything
    line = f"{at:%m-%d %H:%M:%S} {record.levelname} {record.name}: {text}"
    return line if len(line) <= LOG_WIDTH else line[: LOG_WIDTH - 1] + "…"


def _template(record: logging.LogRecord) -> str:
    """The record's message template with each value shown only if it is safe (``_shown``)."""
    if not isinstance(record.msg, str):
        return HIDDEN
    args = record.args
    values = iter(args) if isinstance(args, tuple) else iter(())

    def fill(match: re.Match[str]) -> str:
        if match.group(1) == "%":
            return "%"
        if isinstance(args, Mapping):
            return HIDDEN
        return _shown(next(values, HIDDEN))

    return _first_line(_SPEC.sub(fill, record.msg))


def _shown(value: object) -> str:
    """A logged value as the snapshot shows it: a date or time, a truth value, None, or a
    name-shaped word as it is; anything else (numbers, messages, rows, frames) ``…``."""
    if value is None or isinstance(value, bool):
        return str(value)
    if isinstance(value, datetime):
        return f"{value.astimezone(KST):%Y-%m-%d %H:%M:%S}" if value.tzinfo else value.isoformat()
    if isinstance(value, date | time | timedelta):
        return str(value)
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, str) and _NAME.fullmatch(value):
        return value
    return HIDDEN


def doctor_settings(
    env: Mapping[str, str] | None = None, dotenv_path: Path | None = Path(".env")
) -> tuple[Settings, str | None]:
    """The settings ``doctor`` reads, and the configuration error if there is one. A setting
    that keeps the engine from starting must not keep its summary from printing: then only
    what the summary reads is taken, as it is (the directories, the API's address, the commit,
    the secrets to mask), and the error is returned to be shown."""
    try:
        return Settings.from_env(env, dotenv_path, require_kis=False), None
    except SettingsError as e:
        error = str(e)
    values = read_values(env, dotenv_path)
    try:
        port = int(values.get("KRX_API_PORT", "8080"))
    except ValueError:
        port = 8080

    def text(name: str) -> str | None:
        return values.get(name, "").strip() or None

    settings = Settings(
        kis_app_key=text("KIS_APP_KEY") or "",
        kis_app_secret=text("KIS_APP_SECRET") or "",
        # Every secret ``redaction.secrets_of`` masks (a test keeps the two alike).
        telegram_bot_token=text("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=text("TELEGRAM_CHAT_ID"),
        aws_access_key_id=text("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=text("AWS_SECRET_ACCESS_KEY"),
        ecos_api_key=text("KRX_ECOS_API_KEY"),
        fred_api_key=text("KRX_FRED_API_KEY"),
        github_token=text("KRX_GITHUB_TOKEN"),
        toss_client_id=text("TOSS_CLIENT_ID"),
        toss_client_secret=text("TOSS_CLIENT_SECRET"),
        krx_openapi_key=text("KRX_OPENAPI_KEY"),
        object_store=text("KRX_OBJECT_STORE"),
        s3_endpoint_url=text("KRX_S3_ENDPOINT_URL"),
        bind_address=text("KRX_BIND_ADDRESS"),
        loki_url=text("KRX_LOKI_URL"),
        loki_user=text("KRX_LOKI_USER"),
        loki_token=text("KRX_LOKI_TOKEN"),
        data_dir=Path(values.get("KRX_DATA_DIR", "data")),
        state_dir=Path(values.get("KRX_STATE_DIR", "state")),
        api_host=values.get("KRX_API_HOST", "127.0.0.1"),
        api_port=port if 0 < port < 65536 else 8080,
        commit=text("KRX_COMMIT") or "unknown",
    )
    return settings, error


async def doctor(
    settings: Settings, now: datetime, api: str | None = None, *, config_error: str | None = None
) -> str:
    """The command's summary: the engine read through its API (``api``, or the candidates)."""
    candidates = [("--api로 준 엔진 API", api)] if api else api_candidates(settings)
    async with aiohttp.ClientSession() as session:
        status = await fetch_status(session, candidates)
    return report(settings, now, status, config_error=config_error)


def report(
    settings: Settings,
    now: datetime,
    status: Status,
    logs: RecentLog | None = None,
    *,
    process: str = "이 명령",
    config_error: str | None = None,
) -> str:
    """The summary, masked; ``logs`` are the engine's (None: this process cannot read them),
    ``process`` names whose memory ``VmRSS`` is, ``config_error`` is the settings' error
    (``doctor_settings``)."""
    head = (
        f"서버 상태 {now.astimezone(KST):%Y-%m-%d %H:%M:%S} KST "
        "(krx-quant doctor: 읽기만 한다, 시세 값은 싣지 않는다)"
    )
    if config_error is not None:
        head += (
            f"\n\n설정 오류: {config_error}\n"
            "  엔진은 이 설정으로 시작하지 못한다(configuration error, 종료 코드 2). 아래는 데이터·상태 폴더,"
            " 엔진 API 주소, 가릴 비밀값만 그대로 읽어 낸 것이다(KRX_JOBS_AT 등 나머지 설정은 모른다)"
        )
    parts: list[tuple[str, Callable[[], list[str]]]] = [
        ("1. 엔진", lambda: _engine(settings, status, now)),
        ("2. KIS", lambda: _kis(status, now)),
        ("3. PINGPONG", lambda: _pingpong(settings, now)),
        ("4. 최근 WARNING·ERROR 로그", lambda: _logs(logs)),
        ("5. 예약 작업", lambda: _jobs(settings, known=config_error is None)),
        ("6. 디스크와 메모리", lambda: _resources(settings, process, known=config_error is None)),
        ("7. 밤 학습", lambda: _training(settings, now)),
        ("8. Loki 전송", lambda: _loki(settings, status, now)),
        ("9. 모델 파일", lambda: _models(settings)),
    ]
    sections = [head]
    for title, part in parts:
        try:
            lines = part()
        except Exception as e:  # one part that cannot be read leaves the others
            lines = [f"{title}: 읽지 못함({_error(e)})"]
        sections.append("\n".join(lines))
    return redact("\n\n".join(sections) + "\n", secrets_of(settings))


def _engine(settings: Settings, status: Status, now: datetime) -> list[str]:
    body = status.body
    if body is None:
        lines = ["1. 엔진: 엔진 API에 닿지 않는다(엔진이 멈췄거나 아직 뜨는 중)"]
        lines += [f"  {why}" for why in status.errors]
        lines.append(f"  DB의 마지막 시작(runs, kind start): {_last_start(settings.state_dir / DB_NAME)}")
        lines.append(f"  이 명령의 이미지 커밋(KRX_COMMIT): {settings.commit}")
        return lines
    started = _when(body.get("started_at"))
    ago = f" ({_span((now - started).total_seconds())} 전)" if started is not None else ""
    lines = [f"1. 엔진 (/v1/status, {status.source})"]
    lines.append(f"  commit {body.get('commit')} · started_at {body.get('started_at')}{ago}")
    lines.append(f"  mode {body.get('mode')} · phase {body.get('phase')} · now {body.get('now')}")
    lines += [f"  (닿지 않은 곳: {why})" for why in status.errors]
    return lines


def _last_start(db: Path) -> str:
    with read_only(db) as conn:
        if conn is None:
            return "DB 없음"
        row = conn.execute(
            "SELECT at, detail FROM runs WHERE kind = 'start' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return "없음"
    try:
        commit = json.loads(row[1] or "{}").get("commit", "?")
    except (ValueError, AttributeError):
        commit = "?"
    return f"{row[0]}, commit {commit}"


def _kis(status: Status, now: datetime) -> list[str]:
    body = status.body
    if body is None:
        return ["2. KIS: 엔진 API에 닿지 않아 모른다. 3절의 마지막 PINGPONG이 마지막 프레임을 대신한다"]
    live = body.get("kis")
    if not isinstance(live, Mapping):
        return [f"2. KIS: /v1/status에 kis가 없다(mode {body.get('mode')})"]
    lines = ["2. KIS (/v1/status의 kis)"]
    lines.append(
        f"  connected {_json(live.get('connected'))} · last_frame_at {live.get('last_frame_at')}"
        f" · seconds_since_frame {_seconds(live.get('seconds_since_frame'))}"
    )
    feeds = [f for f in live.get("feeds") or () if isinstance(f, Mapping)]
    quiet = sum(1 for f in feeds if (s := f.get("seconds_since")) is None or s > QUIET_SECONDS)
    gaps = sum(1 for f in feeds if f.get("gap"))
    lines.append(
        f"  feeds {len(feeds)}개 · {QUIET_SECONDS:g}초 넘게 조용하거나 아직 안 온 키 {quiet}개"
        f" · 열린 gap {gaps}개"
    )
    if feeds:
        lines.append("  feed key last_received_at seconds_since gap")
        today = now.astimezone(KST).date()
        for f in sorted(feeds, key=lambda f: (str(f.get("feed")), str(f.get("key")))):
            lines.append(
                f"  {f.get('feed')} {f.get('key')} {_clock(f.get('last_received_at'), today)}"
                f" {_seconds(f.get('seconds_since'))} {f.get('gap') or '-'}"
            )
    return lines


def _pingpong(settings: Settings, now: datetime) -> list[str]:
    today = now.astimezone(KST).date()
    where = f"raw/date={today.isoformat()}/feed=_system/symbol=_"
    partition = settings.data_dir / where
    title = f"3. PINGPONG 오늘 ({where})"
    if not partition.is_dir():
        return [f"{title}: 기록 없음"]
    times = sorted(_pingpongs(partition))
    if not times:
        return [f"{title}: 없음"]
    last = times[-1]
    lines = [
        title,
        f"  합계 {len(times)} · 마지막 {last:%H:%M:%S} ({_span((now - last).total_seconds())} 전)",
    ]
    if len(times) > 1:
        gap, start = max((b - a, a) for a, b in zip(times, times[1:], strict=False))
        lines.append(
            f"  PINGPONG 사이 가장 긴 간격: {start:%H:%M:%S}–{start + gap:%H:%M:%S}"
            f" ({_span(gap.total_seconds())})"
        )
    by_hour = Counter(t.hour for t in times)
    hours = range(times[0].hour, max(last.hour, now.astimezone(KST).hour) + 1)
    cells = [f"{h:02d}시 {by_hour.get(h, 0)}" for h in hours]
    rows = [" · ".join(cells[i : i + HOURS_PER_ROW]) for i in range(0, len(cells), HOURS_PER_ROW)]
    lines.append("  시간별(KST): " + "\n               ".join(rows))
    lines.append(
        f"  파일은 {settings.flush_seconds:g}초(KRX_FLUSH_SECONDS)마다 쓰여,"
        " 그 뒤에 온 것은 아직 없을 수 있다"
    )
    return lines


def _pingpongs(partition: Path) -> Iterator[datetime]:
    """When each PINGPONG of the partition was received, in KST."""
    for path in partition_files(partition):
        try:
            for batch in pq.ParquetFile(path).iter_batches(columns=["received_at", "message"]):
                data = batch.to_pydict()
                for at, text in zip(data["received_at"], data["message"], strict=True):
                    if "PINGPONG" not in text:
                        continue
                    try:
                        message = kis.parse_message(text)
                    except kis.KisError:
                        continue
                    if isinstance(message, kis.SystemMessage) and message.is_pingpong:
                        yield at.astimezone(KST)
        except (FileNotFoundError, pa.ArrowInvalid, OSError):
            continue  # merged away in between, or written as it was read


def _logs(logs: RecentLog | None) -> list[str]:
    if logs is None:
        return [
            "4. 최근 WARNING·ERROR 로그: 이 명령은 엔진과 다른 프로세스라 엔진이 기억하는 줄을 읽지 못한다",
            "  엔진의 전체 줄은 Grafana Cloud Loki에 있다(8절, docs/agents/server-logs.md)",
            "  서버에서: docker compose logs --since 2h engine | grep -E ' (WARNING|ERROR) '",
            "  이슈에 needs-server-snapshot 라벨을 붙여 받는 요약에는 엔진이 기억하는 최근 줄이 실린다",
        ]
    counts = logs.counts()
    lines = logs.lines()
    title = (
        f"4. 최근 WARNING·ERROR 로그 (엔진 시작 뒤 WARNING {counts.get('WARNING', 0)}줄 · "
        f"ERROR {counts.get('ERROR', 0) + counts.get('CRITICAL', 0)}줄"
    )
    if not lines:
        return [f"{title}): 없음"]
    shown = lines[-LOG_LINES:]
    return [
        f"{title}, 마지막 {len(shown)}줄)",
        "  코드의 메시지 틀만 싣는다: 값은 날짜·시각, 참거짓, 이름 모양의 글자만, 나머지는 …, 예외는 종류만."
        " 전체는 Grafana Cloud Loki(8절)와 서버의 docker compose logs engine",
        *(f"  {line}" for line in shown),
    ]


def _jobs(settings: Settings, *, known: bool = True) -> list[str]:
    path = settings.state_dir / STATE_FILE
    jobs_at = "?" if not known else "off" if settings.jobs_at is None else f"{settings.jobs_at:%H:%M}"
    title = f"5. 예약 작업 (state/{STATE_FILE}, KRX_JOBS_AT {jobs_at}, 목록 {len(JOBS)}개)"
    try:
        data = json.loads(path.read_text())  # read as it is: never set aside, as the scheduler would
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as e:
        return [f"{title}: 읽지 못함({_error(e)})"]
    if not isinstance(data, dict):
        return [f"{title}: 읽지 못함(객체가 아니다)"]
    records = {name: r for name, r in (data.get("jobs") or {}).items() if isinstance(r, dict)}
    lines = [title]
    latest = sorted(records.items(), key=lambda item: str(item[1].get("started_at", "")), reverse=True)
    if latest:
        lines.append(f"  마지막 실행(최근 {min(len(latest), LAST_RUNS)}개):")
        for name, r in latest[:LAST_RUNS]:
            code = "없음" if r.get("exit_code") is None else r.get("exit_code")
            comments = " ".join(f"#{n}" for n in sorted(r.get("comments") or {}, key=str))
            line = (
                f"    {name} · {r.get('day')} 밤 · {r.get('outcome')} · 종료 코드 {code} · "
                f"{_span(r.get('seconds'))} · commit {str(r.get('commit', ''))[:12]}"
            )
            if r.get("timed_out"):
                line += " · 제한 시간에 멈춤"
            lines.append(line + (f" · 댓글 {comments}" if comments else ""))
    else:
        lines.append("  돈 작업이 없다")
    ended = {name for name, r in records.items() if r.get("outcome") in ("ok", "failed")}
    waiting = [job.name for job in JOBS if job.name not in ended]
    lines.append(
        f"  끝까지 돈 적 없는 작업 {len(waiting)}개" + (f": {', '.join(waiting)}" if waiting else "")
    )
    unposted = [u for u in data.get("unposted") or () if isinstance(u, dict)]
    if unposted:
        lines.append(f"  아직 올리지 못한 결과 {len(unposted)}개:")
        lines += [
            f"    {u.get('job')} ({u.get('day')} 밤) → #{u.get('issue')}"
            + (" (댓글은 올림, 라벨만 남음)" if u.get("comment") else "")
            for u in unposted
        ]
    else:
        lines.append("  아직 올리지 못한 결과: 없음")
    return lines


def _training(settings: Settings, now: datetime) -> list[str]:
    nights = TrainingNights(settings.state_dir)
    found = nights.fields(calendar_store(settings).calendar, SWAP_AT, now)
    today = now.astimezone(KST).date()
    lines = [f"7. 밤 학습 (state/{NIGHTS_FILE}, state/models/applied.json)"]
    missed = found["not_applied"]
    lines.append(
        f"  마지막으로 적용된 밤 {found['last_applied'] or '없음'}"
        f" · 그 뒤 적용되지 않은 거래일 밤 {found['nights_not_applied']}번"
        + (f": {', '.join(missed)}" if missed else "")
    )
    outcomes = nights.outcomes()
    if not outcomes:
        lines.append("  기록된 밤 없음(이 기록은 #355부터)")
        return lines
    shown = [outcomes[d] for d in sorted(outcomes)[-LAST_NIGHTS:]]
    lines.append(f"  최근 밤(최근 {len(shown)}개):")
    for o in reversed(shown):
        if o.ok:
            lines.append(f"    {o.day} 끝남 · {_clock(o.ended_at.isoformat(), today)}")
            continue
        code = "없음" if o.exit_code is None else o.exit_code
        lines.append(
            f"    {o.day} 실패 · {_clock(o.ended_at.isoformat(), today)} · 종료 코드 {code}"
            f" · 마지막으로 시작한 단계 {o.step or '없음'}"
        )
    return lines


def _resources(settings: Settings, process: str, known: bool = True) -> list[str]:
    lines = ["6. 디스크와 메모리"]
    seen: dict[int, str] = {}
    for name, path in (("데이터", settings.data_dir), ("상태", settings.state_dir)):
        try:
            device = os.stat(path).st_dev
            if device in seen:
                lines.append(f"  {name} {path}: {seen[device]}와 같은 디스크")
                continue
            usage = shutil.disk_usage(path)
        except FileNotFoundError:
            lines.append(f"  {name} {path}: 없음")
            continue
        except OSError as e:
            lines.append(f"  {name} {path}: 읽지 못함({_error(e)})")
            continue
        seen[device] = name
        lines.append(
            f"  {name} {path}: 여유 {usage.free / _GB:.1f} GB / 전체 {usage.total / _GB:.1f} GB"
            f" ({usage.free / usage.total:.0%} 여유)"
        )
    lines.append(_disk_alert(settings, known))
    info = _meminfo(Path("/proc/meminfo"))
    if info:
        total, available = info.get("MemTotal"), info.get("MemAvailable")
        swap_total, swap_free = info.get("SwapTotal", 0), info.get("SwapFree", 0)
        lines.append(
            f"  서버 메모리(/proc/meminfo): 사용 가능 {_mb(available)} / 전체 {_mb(total)}"
            f" · 스왑 사용 {_mb(swap_total - swap_free)} / {_mb(swap_total)}"
        )
    else:
        lines.append("  서버 메모리: /proc/meminfo를 읽지 못함")
    used, limit = (
        _read_int(Path("/sys/fs/cgroup/memory.current")),
        _read_text(Path("/sys/fs/cgroup/memory.max")),
    )
    if used is not None:
        cap = "없음" if limit in (None, "max") else _mb(int(limit)) if limit.isdigit() else limit
        lines.append(f"  컨테이너(cgroup): 사용 {_mb(used)} / 한도 {cap}")
    rss = _meminfo(Path("/proc/self/status")).get("VmRSS")
    if rss is not None:
        lines.append(f"  {process} 프로세스(VmRSS): {_mb(rss)}")
    return lines


def _disk_alert(settings: Settings, known: bool) -> str:
    """The disk alert's threshold (#379) and the disks alerted now, as the state folder keeps them."""
    low = ", ".join(DISK_LABELS[name] for name in disk_alerted(settings.state_dir)) or "없음"
    if not known:
        rule = "기준은 모른다(설정 오류)"
    elif settings.disk_min_free_percent > 0:
        rule = f"여유가 {settings.disk_min_free_percent:g}%보다 작으면 텔레그램"
    else:
        rule = "꺼짐"
    return f"  디스크 알림(KRX_DISK_MIN_FREE_PERCENT, #379): {rule} · 지금 알림 중: {low}"


def _models(settings: Settings) -> list[str]:
    """The model files (#361): their number and size, on the server and in the store. Names and
    counts only: no model's contents."""
    lines = ["9. 모델 파일 (#361: 알고리즘마다 지금 모델과 되돌리기가 갈 바로 전 모델만 남긴다)"]
    root = settings.state_dir / "models"
    running = [p for p in root.glob("*.json") if p.is_file() and p.name not in _NOT_MODELS]
    kept = [p for p in root.glob("versions/*/*.json") if p.is_file()]
    lines.append(
        f"  state/models: 지금 모델 {len(running)}개 {_size(p.stat().st_size for p in running)}"
        f" · versions/ {len(kept)}개 {_size(p.stat().st_size for p in kept)}"
        + _most(Counter(p.parent.name for p in kept))
    )
    store = open_store(settings)
    if store is None:
        lines.append("  학습 저장소: 없음(KRX_OBJECT_STORE)")
        return lines
    try:
        stored = [o for o in store.objects(MODELS) if o.key.endswith(".json")]
    except Exception as e:  # S3 unreachable, or refused: the section says so
        lines.append(f"  학습 저장소 {MODELS}: 읽지 못함({type(e).__name__})")
        return lines
    lines.append(
        f"  학습 저장소 {MODELS}: {len(stored)}개 {_size(o.size for o in stored)}"
        + _most(Counter(o.key.removeprefix(MODELS).partition("/")[0] for o in stored))
    )
    refused = Shelf(settings.state_dir).store_refused()
    if refused is not None:
        lines.append(
            f"  학습 저장소가 모델 지우기를 거부함(마지막으로 {refused} 밤, AccessDenied 등): 엔진 IAM"
            " 사용자의 정책에 engine/models/*의 s3:DeleteObject를 넣는다(operations.md 2.1)."
            " 그동안 파일은 그대로 남는다"
        )
    return lines


# The files in ``state/models`` that are not a model the engine runs (``model_shelf``).
_NOT_MODELS = frozenset({"history.json", "applied.json", "store_refused.json"})


def _size(sizes: Iterable[int]) -> str:
    return size_text(sum(sizes))


def _most(by_algorithm: Counter[str]) -> str:
    """How many algorithms the files are of, and the one with the most."""
    if not by_algorithm:
        return ""
    name, count = max(by_algorithm.items(), key=lambda item: (item[1], item[0]))
    return f" (알고리즘 {len(by_algorithm)}개, 가장 많은 {name} {count}개)"


def _loki(settings: Settings, status: Status, now: datetime) -> list[str]:
    """The lines' copy in Loki (#358): the engine's ``/v1/status`` ``loki``; without it, what this
    command's settings say. Counts, times and the failure's kind only."""
    title = "8. Loki 전송 (#358, /v1/status의 loki)"
    shipping = None if status.body is None else status.body.get("loki")
    if not isinstance(shipping, Mapping):
        target, why = loki.target_of(
            {
                loki.URL: settings.loki_url or "",
                loki.USER: settings.loki_user or "",
                loki.TOKEN: settings.loki_token or "",
            }
        )
        if status.body is None:
            known = "엔진 API에 닿지 않아 엔진의 전송 상태는 모른다"
        else:
            known = "/v1/status에 loki가 없다(#358 전의 엔진)"
        setting = "켜짐(KRX_LOKI_URL·KRX_LOKI_USER·KRX_LOKI_TOKEN 있음)" if target else f"꺼짐({why})"
        return [title, f"  {known}", f"  이 명령이 읽은 설정: {setting}"]
    if not shipping.get("enabled"):
        return [f"{title}: 꺼짐({shipping.get('reason')}). 로그는 stdout(docker compose logs)에만 있다"]
    today = now.astimezone(KST).date()
    lines = [
        title,
        f"  켜짐 · 보낸 줄 {_count(shipping.get('sent'))} · 버린 줄 {_count(shipping.get('dropped'))}"
        f" · 버퍼 {_count(shipping.get('buffered'))}줄",
    ]
    success = _when(shipping.get("last_success_at"))
    if success is None:
        lines.append("  마지막 성공: 아직 없음")
    else:
        lines.append(
            f"  마지막 성공 {_clock(shipping.get('last_success_at'), today)}"
            f" ({_span((now - success).total_seconds())} 전)"
        )
    error = shipping.get("last_error")
    lines.append(
        f"  마지막 오류 {error} ({_clock(shipping.get('last_error_at'), today)})"
        if error
        else "  마지막 오류: 없음"
    )
    lines.append(
        "  버린 줄과 보내지 못한 줄은 Loki에 없다(stdout에만 있었다: docker compose logs)."
        " 조회: docs/agents/server-logs.md"
    )
    return lines


def _count(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else "?"


def _meminfo(path: Path) -> dict[str, int]:
    """``/proc/meminfo``'s (or ``/proc/self/status``'s) sizes in kB, in bytes."""
    found: dict[str, int] = {}
    try:
        text = path.read_text()
    except OSError:
        return found
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        fields = rest.split()
        if len(fields) == 2 and fields[1] == "kB" and fields[0].isdigit():
            found[name.strip()] = int(fields[0]) * 1024
    return found


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    text = _read_text(path)
    return int(text) if text is not None and text.isdigit() else None


def _mb(size: int | None) -> str:
    return "?" if size is None else f"{size / _MB:,.0f} MB"


def _when(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text)) if text else None
    except ValueError:
        return None


def _clock(text: Any, today: date) -> str:
    """An API time as KST: its time of day if it is today's, else the date too."""
    at = _when(text)
    if at is None:
        return "-" if text is None else str(text)
    local = at.astimezone(KST)
    return f"{local:%H:%M:%S}" if local.date() == today else f"{local:%Y-%m-%d %H:%M:%S}"


def _seconds(value: Any) -> str:
    return f"{value:.1f}" if isinstance(value, int | float) else "-"


def _json(value: Any) -> str:
    return json.dumps(value)


def _span(seconds: Any) -> str:
    if not isinstance(seconds, int | float):
        return "?"
    total = round(seconds)
    if total < 0:
        return f"{total}초"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}일 {hours}시간"
    if hours:
        return f"{hours}시간 {minutes}분"
    if minutes:
        return f"{minutes}분 {secs}초"
    return f"{secs}초"


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ""


def _error(e: BaseException) -> str:
    text = _first_line(str(e))
    return f"{type(e).__name__}: {text[:160]}" if text else type(e).__name__
