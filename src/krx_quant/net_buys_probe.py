"""``krx-quant probe-net-buys``: whether KIS's KRX investor query has the net buys before NXT
opened, and what the engine stored of them (#264, #297).

KIS's integrated investor query (``FHPTJ04160001`` ``UN``) answers 0 shares for every net buy
before NXT opened (2025-03-04): there was no integrated market. The supply keeper now asks the
days before of the KRX query (``J``, ``supply.INVESTORS_KRX``), the whole market then, once.
Whether ``J`` has figures that far back was not measured: the 2026-10-16 probe (#226) asked
recent days only. This asks, for each predicted symbol, ``J`` and ``UN`` for the 30 trading
days to each of a few days (``WINDOWS``: two before the single-stock years, the year before
NXT, the last month before it, its first month, and the latest), and says per answer:

- the rows and the days they span, and the look of the three fields (``probe_fields.flags``);
- how many days have a figure (a net buy other than 0) for individuals, institutions and
  foreigners, and the first and last day with one;
- on the days from NXT's opening that both answered, how often ``J`` equals ``UN``, field by
  field (equal and different counts: ``UN`` adds NXT's trades, so they should differ).

Then it reads the engine's stored KIS supply (``state/supply/symbol=<s>/kis.parquet``): the days
before NXT opened and from it, which market each day's net buys came from, how many have a
figure of each class, the first and last; and on the days before NXT, how often KIS's KRX
figures equal Toss's individual and institution net buys (counts only: Toss's values never
leave the server).

It prints no market value, so its report can go on the public issue (the scheduled job
``probe-net-buys-j-264``). Calls are spaced ``supply_probe.PACE`` apart, under the REST limit
the collector shares, with the cached access token while it is valid (no early refresh).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

import aiohttp

from krx_quant import supply_probe
from krx_quant.auth import tokens_and_client
from krx_quant.config import Settings
from krx_quant.kis import KisError, RestClient, rest_session
from krx_quant.probe_fields import MISSING, day_of, flags, present, same
from krx_quant.supply import KIS, NET_MARKET, NXT_OPENED, TOSS, read_supply
from krx_quant.supply_probe import INVESTOR_DAILY, kis_error, parse

Out = Callable[[str], None]

MARKETS = ("J", "UN")
# The days asked to (the 30 trading days up to each): None is the day the probe runs.
WINDOWS: tuple[tuple[str, date | None], ...] = (
    ("2016", date(2016, 12, 30)),
    ("2020", date(2020, 6, 30)),
    ("2024-06", date(2024, 6, 28)),
    ("2025-02", date(2025, 2, 28)),
    ("2025-03", date(2025, 3, 31)),  # NXT's first month: both sides of its opening
    ("최근", None),
)
# The investor query's fields and the stored ones, by class.
CLASSES = (
    ("개인", "prsn_ntby_qty", "individual_net"),
    ("기관", "orgn_ntby_qty", "institution_net"),
    ("외국인", "frgn_ntby_qty", "foreign_net"),
)
MAX_SYMBOLS = 5
MAX_FAILED_IN_A_ROW = 10  # then KIS is down or refusing: the rest is not asked


def _figure(value: Any) -> bool:
    """A net buy other than 0 (KIS's 0 before NXT opened is its "no figure")."""
    if not present(value):
        return False
    try:
        return float(value) != 0
    except ValueError:
        return False


def _span(days: Sequence[date]) -> str:
    return f"{min(days)} – {max(days)}" if days else "없음"


@dataclass
class Asked:
    """One answer: its dated rows by day, or KIS's error."""

    error: str | None = None
    rows: dict[date, Mapping[str, Any]] = field(default_factory=dict)


def answer_line(asked: Asked) -> str:
    """An answer's rows, the fields' look, the figures of each class and their first and last day."""
    if asked.error is not None:
        return asked.error
    if not asked.rows:
        return "OK, 0행"
    days = sorted(asked.rows)
    looks = flags(list(asked.rows.values()))
    fields = ", ".join(f"{name} {looks.get(name, MISSING)}" for _, name, _ in CLASSES)
    counts = "·".join(
        f"{label} {sum(_figure(r.get(name)) for r in asked.rows.values())}" for label, name, _ in CLASSES
    )
    with_figure = [d for d in days if any(_figure(asked.rows[d].get(name)) for _, name, _ in CLASSES)]
    return (
        f"OK, {len(days)}행 {days[0]}..{days[-1]} · {fields} · 값 {counts} · 값 있는 날 {_span(with_figure)}"
    )


def agreement_line(krx: Asked, integrated: Asked) -> str | None:
    """On the days from NXT's opening both answered, J's net buys against UN's (counts only);
    None without such a day."""
    common = sorted(d for d in krx.rows.keys() & integrated.rows.keys() if d >= NXT_OPENED)
    if not common:
        return None
    parts = []
    for label, name, _ in CLASSES:
        equal = sum(same(krx.rows[d].get(name), integrated.rows[d].get(name)) for d in common)
        parts.append(f"{label} 같음 {equal}·다름 {len(common) - equal}")
    shared = f"{NXT_OPENED}부터 같은 날 {len(common)}일, {common[0]} – {common[-1]}"
    return f"J = UN({shared}): " + " · ".join(parts)


def stored_lines(state_dir: Path, symbol: str) -> list[str]:
    """The stored KIS supply's net buys before NXT opened and from it, and KRX's against Toss's."""
    rows = read_supply(state_dir, symbol, KIS)
    if not rows:
        return [f"  {symbol}: 저장된 KIS 수급 없음"]
    lines = []
    for title, part in (
        ("NXT 개장 전", [r for r in rows if r["date"] < NXT_OPENED]),
        ("NXT 개장부터", [r for r in rows if r["date"] >= NXT_OPENED]),
    ):
        if not part:
            lines.append(f"  {symbol} {title}: 없음")
            continue
        markets = {m: sum(r.get(NET_MARKET) == m for r in part) for m in MARKETS}
        unmarked = sum(r.get(NET_MARKET) not in MARKETS for r in part)
        market = "·".join(f"{m} {n:,}" for m, n in markets.items())
        if unmarked:
            market += f"·없음 {unmarked:,}"
        counts = "·".join(
            f"{label} {sum(r.get(name) is not None for r in part):,}" for label, _, name in CLASSES
        )
        figured = [r["date"] for r in part if any(r.get(name) is not None for _, _, name in CLASSES)]
        lines.append(
            f"  {symbol} {title} {len(part):,}일({_span([r['date'] for r in part])}): 시장 {market}"
            f" · 값 {counts} · 값 있는 날 {_span(figured)}"
        )
    toss = {r["date"]: r for r in read_supply(state_dir, symbol, TOSS) if r["date"] < NXT_OPENED}
    parts = []
    for label, _, name in CLASSES[:2]:  # Toss's foreigners are the registered ones only
        both = [
            (r[name], toss[r["date"]][name])
            for r in rows
            if r["date"] in toss
            and r.get(NET_MARKET) == "J"
            and r.get(name) is not None
            and toss[r["date"]].get(name) is not None
        ]
        equal = sum(a == b for a, b in both)
        parts.append(f"{label} 같은 날 {len(both):,}일 중 같음 {equal:,}·다름 {len(both) - equal:,}")
    lines.append(f"  {symbol} NXT 개장 전 KIS J와 토스: " + " · ".join(parts))
    return lines


class _Asker:
    def __init__(self, client: RestClient) -> None:
        self._client = client
        self.calls = 0
        self.failed = 0
        self._failed_in_a_row = 0

    async def ask(self, market: str, symbol: str, end: date) -> Asked:
        if self._failed_in_a_row >= MAX_FAILED_IN_A_ROW:
            return Asked(error=f"묻지 않음: {MAX_FAILED_IN_A_ROW}번 잇달아 실패")
        if self.calls:
            await asyncio.sleep(supply_probe.PACE)
        self.calls += 1
        try:
            body, more = await self._client.get_page(
                INVESTOR_DAILY.tr, INVESTOR_DAILY.path, INVESTOR_DAILY.query(market, symbol, end), ""
            )
        except KisError as e:
            error = kis_error(e)
        except (aiohttp.ClientError, OSError, ValueError) as e:  # OSError: timeouts too
            error = f"error {type(e).__name__}"
        else:
            answer = parse(INVESTOR_DAILY, body, more)
            if not answer.body_keys:
                self._failed_in_a_row = 0
                rows: dict[date, Mapping[str, Any]] = {}
                for row in answer.rows:
                    day = day_of(row, INVESTOR_DAILY.date_field)
                    if day is not None and day <= end:
                        rows.setdefault(day, row)
                return Asked(rows=rows)
            # KIS said OK, but the rows are not where they belong: no usable answer, a failure.
            error = f"OK, 행 목록이 아니다(본문 키: {', '.join(answer.body_keys)})"
        self.failed += 1
        self._failed_in_a_row += 1
        return Asked(error=error)


async def probe_net_buys(settings: Settings, today: date, out: Out = print) -> tuple[int, int]:
    """Ask J and UN for each symbol and window, print the report and the stored days; the calls
    made, and how many failed (KIS's errors, and answers without the row list)."""
    symbols = settings.symbols[:MAX_SYMBOLS]
    # The cached token while it is valid: no early refresh beside the running collector.
    settings = replace(settings, token_refresh_margin_seconds=0.0)
    timeout = aiohttp.ClientTimeout(total=supply_probe.REQUEST_TIMEOUT)
    out(f"KIS 투자자 순매수 KRX(J)·통합(UN) 확인 (#264) {today}: 종목 {', '.join(symbols)}")
    out(
        f"{INVESTOR_DAILY.tr} {INVESTOR_DAILY.title}을 J와 UN으로 묻고, 엔진이 저장한 KIS 수급을 센다."
        " 시세 값은 없다(행 수, 날짜, 개수, 필드 모양만)."
    )
    out(
        '"값" = 0이 아닌 순매수: '
        + " · ".join(f"{label} {name}" for label, name, _ in CLASSES)
        + f". NXT 개장 {NXT_OPENED}."
    )
    out("")
    async with rest_session(timeout=timeout) as session:
        tokens, client = tokens_and_client(session, settings, lambda: datetime.now().astimezone())
        asker = _Asker(client)
        out("1. 조회(묻는 날까지 30거래일)")
        try:
            await tokens.get()
        except (KisError, aiohttp.ClientError, OSError, ValueError) as e:
            asker.calls += 1
            asker.failed += 1
            reason = kis_error(e) if isinstance(e, KisError) else f"error {type(e).__name__}"
            out(f"  멈춤: KIS 접근 토큰이 없다: {reason}")
        else:
            for symbol in symbols:
                for label, end in WINDOWS:
                    day = end or today
                    answers = {m: await asker.ask(m, symbol, day) for m in MARKETS}
                    for market, asked in answers.items():
                        out(f"  {symbol} {label}(~{day}) {market}: {answer_line(asked)}")
                    compared = agreement_line(answers["J"], answers["UN"])
                    if compared is not None:
                        out(f"  {symbol} {label}(~{day}) {compared}")
    out("")
    out(f"2. 저장된 KIS 수급(state/supply/symbol=<종목>/{KIS}.parquet, 시장은 {NET_MARKET})")
    for symbol in symbols:
        for line in stored_lines(settings.state_dir, symbol):
            out(line)
    out("")
    out(f"KIS: {asker.calls}번 조회, {asker.failed}번 실패")
    return asker.calls, asker.failed
