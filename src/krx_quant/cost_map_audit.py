"""The cost map's audit (``krx-quant cost-map-audit``, #438): every ranked algorithm of the trader's
kinds, judged as the cost map judges the one it shows.

The cost map (``cost_map.py``, ``GET /v1/cost-map``) shows each kind's first-ranked algorithm besides
the consensus (``top``), ranked by the lower bound of its weakest lift over the kind's baselines. The
algorithm whose lift is the highest need not call the rises and falls most surely: another may pass
the cost the top fails, and not be seen. On 2026-11-01 every cell of the trader's cost map failed;
this audit looks behind the tops. It changes neither the cost map nor the trader.

It is exploratory, and says so at its head: a hidden candidate that passes is no leave to trade.
What the cost map shows instead of its top is the user's to decide, on the agent's proposal on #438,
and it is seen in paper first.

**What it reads.** Tonight's cost map of 005930 from the Engine API, the one the trader picks its
cells from (``selection.read_cost_map``: the last 20 days with predictions, special situations left
out), for each traded kind's (``TRADED_KINDS``) row, its days, and the map's minimum sample and fee.
Then each kind's scored predictions of 005930 on those days, from the engine's database read-only
(``predictions.read_only``), as the API reads them for its statistics (``api._stats``,
``api._kind_stats``): the same rows (``predictions.scored_rows``, ``predictions.kind_scored_rows``),
an algorithm's by models of the answers they are scored on (``stats.on_target``; the direction's
from touch target 2 on), split from the baselines', and ranked by the same leaderboard
(``stats.leaderboard``, ``kind_stats.leaderboard``). One kind's records are held at a time.

**Judged as the top.** Each ranked algorithm besides the consensus is put in the top's place (the
leaderboard with it ranked alone) and the cost map's own row is computed with it
(``cost_map.direction_row``, ``cost_map.kind_row``): its W, C, p* and verdicts are the cost map's
code's. So the first of them, the top the cost map shows (visible), has the cost map's numbers to
the last digit, and the others (hidden) are judged by the same rule. Each candidate also has the
hit rates of its rise calls and of its fall calls apart (Wilson 95%, as the leaderboard's precision
of these kinds, whose windows do not overlap), and the verdict the cost map's rule gives its rise
calls alone (``rise_verdict``): the trader buys only on a rise.

**The check.** The visible candidate's row is set against the cost map's (``COMPARED``: its top, W,
C, p*, days, bar closes): the same, or which differ. A difference means the database moved after
the cost map was computed, or the reads are not the API's; the text says so and the exit code is
``DIFFERS``.

No price is printed: hit rates, p*, W and C in ticks, counts and dates. A scheduled job runs it once
and posts it on #438 (``scheduled/trader.py``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time as clock
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

import aiohttp

from krx_quant.baselines import baselines_for
from krx_quant.consensus import NAMES as CONSENSUS
from krx_quant.cost_map import _verdict, direction_row, kind_row
from krx_quant.footprint import Meter
from krx_quant.kind_registry import kind_named
from krx_quant.kind_stats import KindEntry, scored_of_kind
from krx_quant.kind_stats import leaderboard as kind_leaderboard
from krx_quant.kind_stats import split as kind_split
from krx_quant.kinds import DIRECTION, Kind, KindScored
from krx_quant.predictions import kind_scored_rows, read_only, scored_rows
from krx_quant.pricing import Direction
from krx_quant.signal_review import _ratio
from krx_quant.stats import (
    Entry,
    Interval,
    Scored,
    leaderboard,
    on_target,
    scored_by_algorithm,
    split,
    wilson,
)
from krx_quant.trader import COST_MAP_PERIOD, SYMBOL, TRADED_KINDS
from krx_quant.trader.selection import CostMapError, read_cost_map

log = logging.getLogger(__name__)

TITLE = "비용 지도 감사 (#438)"
ISSUE = 438
# What the command's exit code says.
AUDITED = 0
NOT_READ = 1  # the cost map or the engine's database could not be read
DIFFERS = 2  # a visible candidate's numbers are not the cost map's (the audit is printed still)
# A row's figures the visible candidate's are set against (module doc "The check").
COMPARED = (
    "days",
    "unit",
    "bar_closes",
    "width",
    "reference_price",
    "tick",
    "cost",
    "breakeven",
    "breakeven_missing",
    "top",
)
COSTS = {"limit": "지정가", "market": "시장가"}
VERDICTS = {"pass": "pass", "fail": "fail", "few": "few(표본 부족)"}


@dataclass(frozen=True)
class Candidate:
    """A ranked algorithm of a kind, judged in the top's place."""

    algorithm: str
    visible: bool  # the cost map's top now
    judged: Mapping[str, Any]  # the cost map's predictor, as its row would show it as the top
    rise: Interval | None  # the hit rate of its rise calls (Wilson 95%); None: it called none
    fall: Interval | None  # of its fall calls
    rise_verdict: Mapping[str, str] | None  # the cost map's rule on its rise calls alone; None without p*


@dataclass(frozen=True)
class KindAudit:
    """A traded kind's candidates, and its row against the cost map's."""

    kind: str
    title: str
    days: tuple[date, ...]  # the cost map's, as its row lists them
    row: Mapping[str, Any]  # the cost map's row as computed here, its top the visible candidate
    candidates: tuple[Candidate, ...]  # in the leaderboard's order, the visible one first
    differs: tuple[str, ...] | None  # the ``COMPARED`` figures unlike the cost map's; None: no row there


def audit(body: Mapping[str, Any], engine_db: Path, target_version: int) -> list[KindAudit]:
    """Each traded kind's candidates on the cost map ``body`` (005930's of ``COST_MAP_PERIOD``), from
    the engine's database at ``engine_db`` read-only (module doc). ``CostMapError`` when ``body`` is not
    that cost map, ``sqlite3.Error`` when the database cannot be read."""
    rows, min_sample, fee_rate = _the_cost_map(body)
    out = []
    with read_only(engine_db) as conn:
        if conn is None:
            raise sqlite3.OperationalError(f"no engine database at {engine_db}")
        conn.row_factory = sqlite3.Row  # as the store's rows
        for name in TRADED_KINDS:
            kind = kind_named(name)
            assert kind is not None, name
            theirs = rows.get(name)
            days = _days(theirs)
            if kind.name == DIRECTION:
                found = _direction(conn, kind, days, target_version, fee_rate, min_sample)
            else:
                found = _kind(conn, kind, days, fee_rate, min_sample)
            mine, candidates = found
            out.append(
                KindAudit(
                    kind=name,
                    title=kind.title,
                    days=days,
                    row=mine,
                    candidates=tuple(candidates),
                    differs=None if theirs is None else _differs(mine, theirs),
                )
            )
    return out


def _the_cost_map(body: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], int, float]:
    """``body``'s rows by kind, minimum sample and fee; ``CostMapError`` unless it is 005930's cost map
    of ``COST_MAP_PERIOD``, special situations left out, as the trader asks for it."""
    if not isinstance(body, Mapping) or not isinstance(body.get("rows"), list):
        raise CostMapError("no rows in the answer")
    asked = (SYMBOL, COST_MAP_PERIOD, "exclude")
    got = (body.get("symbol"), body.get("period"), body.get("special"))
    if got != asked:
        raise CostMapError(f"the cost map of {got!r}: not the trader's {asked!r}")
    min_sample, fee_rate = body.get("min_sample"), body.get("fee_rate")
    if isinstance(min_sample, bool) or not isinstance(min_sample, int):
        raise CostMapError(f"no minimum sample in the answer: {min_sample!r}")
    if isinstance(fee_rate, bool) or not isinstance(fee_rate, int | float):
        raise CostMapError(f"no fee rate in the answer: {fee_rate!r}")
    rows = {r["kind"]: r for r in body["rows"] if isinstance(r, Mapping) and isinstance(r.get("kind"), str)}
    return rows, min_sample, float(fee_rate)


def _days(row: Mapping[str, Any] | None) -> tuple[date, ...]:
    """A row's days, in its order (the latest first, as the API's query has them); ``CostMapError``
    when they cannot be read. None (no row): none."""
    if row is None:
        return ()
    try:
        return tuple(date.fromisoformat(d) for d in row.get("days") or ())
    except (TypeError, ValueError) as e:
        raise CostMapError(f"the {row.get('kind')} row's days cannot be read: {e}") from e


def _direction(
    conn: sqlite3.Connection,
    kind: Kind,
    days: Sequence[date],
    target_version: int,
    fee_rate: float,
    min_sample: int,
) -> tuple[dict[str, Any], list[Candidate]]:
    """The direction's row and candidates, its records read as ``api._stats`` reads them."""
    names = frozenset(b.name for b in baselines_for(target_version))
    rows = scored_rows(conn, days, SYMBOL, False)
    if target_version >= 2:
        rows = on_target(rows, names | set(CONSENSUS), target_version)
    algorithms, baselines = split(scored_by_algorithm(rows), names)
    del rows
    board = leaderboard(algorithms, baselines, min_sample, fee_rate)
    records: dict[str, list[Scored]] = {**algorithms, **baselines}

    def row_with(top: Entry | None) -> dict[str, Any]:
        shown = board if top is None else replace(board, ranked=[top])
        return direction_row(kind, shown, records, fee_rate, min_sample, days)

    def sides(r: Scored) -> tuple[int, int]:
        return _direction_side(r.predicted), _direction_side(r.outcome)

    return _candidates(board.ranked, row_with, records, sides, min_sample)


def _kind(
    conn: sqlite3.Connection, kind: Kind, days: Sequence[date], fee_rate: float, min_sample: int
) -> tuple[dict[str, Any], list[Candidate]]:
    """A kind's row and candidates, its records read as ``api._kind_stats`` reads them."""
    assert kind.sides is not None
    names = kind.baseline_names()
    rows = kind_scored_rows(conn, kind.name, kind.answer_version, days, SYMBOL, False)
    rows = on_target(rows, names, kind.answer_version)
    algorithms, baselines = kind_split(scored_of_kind(rows, kind), names)
    del rows
    board = kind_leaderboard(algorithms, baselines, min_sample, kind)  # the traded kinds do not overlap
    records: dict[str, list[KindScored]] = {**algorithms, **baselines}
    rise, fall = kind.index(kind.sides[0]), kind.index(kind.sides[1])

    def row_with(top: KindEntry | None) -> dict[str, Any]:
        shown = board if top is None else replace(board, ranked=[top])
        return kind_row(kind, shown, records, fee_rate, min_sample, days)

    def side(index: int) -> int:
        return 1 if index == rise else -1 if index == fall else 0

    def sides(r: KindScored) -> tuple[int, int]:
        return side(r.predicted), side(r.outcome)

    return _candidates(board.ranked, row_with, records, sides, min_sample)


def _direction_side(d: Direction) -> int:
    return 1 if d is Direction.UP else -1 if d is Direction.DOWN else 0


def _candidates(
    ranked: Sequence[Any],
    row_with: Callable[[Any], dict[str, Any]],
    records: Mapping[str, Sequence[Any]],
    sides: Callable[[Any], tuple[int, int]],
    min_sample: int,
) -> tuple[dict[str, Any], list[Candidate]]:
    """The cost map's row as it is (its top the first ranked algorithm besides the consensus), and
    each such algorithm judged in the top's place, in rank order (module doc "Judged as the top")."""
    row = row_with(None)
    breakeven = row["breakeven"]
    out: list[Candidate] = []
    for e in (e for e in ranked if e.algorithm not in CONSENSUS):
        judged = row_with(e)["top"]
        calls = [sides(r) for r in records.get(e.algorithm, [])]
        rises = [outcome for call, outcome in calls if call == 1]
        falls = [outcome for call, outcome in calls if call == -1]
        rise = wilson(sum(o == 1 for o in rises), len(rises))
        verdict = None
        if breakeven is not None:
            verdict = {c: _verdict(rise, len(rises), b["p_star"], min_sample) for c, b in breakeven.items()}
        out.append(
            Candidate(
                algorithm=e.algorithm,
                visible=not out,
                judged=judged,
                rise=rise,
                fall=wilson(sum(o == -1 for o in falls), len(falls)),
                rise_verdict=verdict,
            )
        )
    return row, out


def _differs(mine: Mapping[str, Any], theirs: Mapping[str, Any]) -> tuple[str, ...]:
    """The ``COMPARED`` figures of the row computed here unlike the cost map's, as JSON carries them."""
    ours = json.loads(json.dumps({k: mine.get(k) for k in COMPARED}))
    return tuple(k for k in COMPARED if ours[k] != theirs.get(k))


# The text: hit rates, p*, ticks, counts and dates, no price.


def report(body: Mapping[str, Any], audits: Sequence[KindAudit], run: Mapping[str, Any] | None = None) -> str:
    lines = [
        f"{TITLE}: 순위표 1위에 가린 알고리즘도 비용 기준을 넘는지",
        "탐색용이다. 숨은 후보가 통과해도 매매 허가가 아니다. 비용 지도와 trader는 바뀌지 않는다. "
        "비용 지도가 1위 대신 무엇을 보일지는 사용자가 정하고, 바꾸면 paper로 먼저 본다.",
        f"비용 지도: {body.get('symbol')}, {body.get('period')}, 특수 상황 제외, as_of {body.get('as_of')}, "
        f"최소 표본 {body.get('min_sample')}, 수수료 한쪽 {float(body.get('fee_rate') or 0) * 100:g}%. "
        "trader가 08:30에 읽는 것과 같은 질의다.",
        "후보: 그 종류 순위표에 오른 알고리즘(합의 제외), 순위대로. 보임은 비용 지도가 보이는 1위(top), "
        "숨음은 그 밖이다. 숨은 후보도 1위 자리에 두고 비용 지도의 계산(`cost_map.py`) 그대로 판정한다: "
        "상승·하락 예측을 합친 맞힘률의 95% Wilson 하한이 p*보다 크면 pass, 아니면 fail, 예측 수가 최소 "
        "표본보다 적으면 few.",
        "상승·하락: 그 예측만의 맞힘률(95% Wilson)과 예측 수. 상승만: 상승 예측의 맞힘률에 같은 규칙을 "
        "적용한 판정이다(trader는 상승 예측에만 산다). 비용 지도에는 없다.",
        "대조: 보이는 후보의 숫자와 그 줄의 W, C, p*, 날, 봉 마감을 비용 지도의 것과 견준다. "
        "가격은 싣지 않는다(맞힘률, p*, 틱, 개수, 날짜만).",
    ]
    for a in audits:
        lines += ["", *_kind_lines(a)]
    lines += ["", *_summary(audits)]
    if run is not None:
        lines.append(f"  실행: {run['seconds']:.1f}초, 최대 RSS {_mb(run.get('peak_rss_mb'))}")
    return "\n".join(lines)


def _kind_lines(a: KindAudit) -> list[str]:
    row = a.row
    span = f"{min(a.days)} – {max(a.days)} ({len(a.days)}일)" if a.days else "예측이 있는 날 없음"
    head = f"{a.kind} {a.title}: {span} · 봉 마감 {row.get('bar_closes', 0):,}"
    breakeven, cost, width = row.get("breakeven"), row.get("cost"), row.get("width")
    if breakeven is None:
        head += f" · p* 없음({row.get('breakeven_missing')})"
    else:
        assert cost is not None and width is not None
        head += (
            f" · W {width:g}틱 · C "
            + ", ".join(f"{COSTS[c]} {v:.2f}틱" for c, v in cost.items())
            + " · p* "
            + ", ".join(f"{COSTS[c]} {b['p_star']:.1%}" for c, b in breakeven.items())
        )
    lines = [head]
    if a.differs is None:
        lines.append("  대조: 비용 지도에 이 종류의 줄이 없다")
    elif a.differs:
        lines.append(f"  대조: 비용 지도와 다르다: {', '.join(a.differs)}. 이 종류의 숫자는 믿지 않는다")
    else:
        lines.append("  대조: 비용 지도와 같다")
    if not a.candidates:
        lines.append("  순위에 오른 알고리즘 없음")
    for c in a.candidates:
        j = c.judged
        lines.append(
            f"  {'보임' if c.visible else '숨음'} {j['rank']}위 {c.algorithm}: 판정 {_verdicts(j['verdict'])}"
            f" · 예측 {j['calls']:,} · 맞힘률 {_ratio(_interval(j['precision']))}"
        )
        lines.append(f"    상승 {_side(c.rise)} · 하락 {_side(c.fall)} · 상승만 {_verdicts(c.rise_verdict)}")
    return lines


def _summary(audits: Sequence[KindAudit]) -> list[str]:
    hidden = [(a, c) for a in audits for c in a.candidates if not c.visible]
    lines = [f"요약: 숨은 후보 {len(hidden)}개({sum(1 for a in audits if len(a.candidates) > 1)}종류)."]

    def passing(verdict: Callable[[Candidate], Mapping[str, str] | None]) -> str:
        found = [
            f"{a.kind} {c.judged['rank']}위 {c.algorithm}"
            for a, c in hidden
            if (verdict(c) or {}).get("market") == "pass"
        ]
        return ", ".join(found) if found else "없음"

    lines.append(f"  시장가 왕복으로 통과(pass)한 숨은 후보: {passing(lambda c: c.judged['verdict'])}")
    lines.append(f"  상승만 시장가 왕복으로 통과한 숨은 후보: {passing(lambda c: c.rise_verdict)}")
    visible = [f"{a.kind} {c.algorithm}" for a in audits for c in a.candidates[:1]]
    passed = [
        f"{a.kind} {c.algorithm}"
        for a in audits
        for c in a.candidates[:1]
        if (c.judged["verdict"] or {}).get("market") == "pass"
    ]
    shown = ", ".join(passed) if passed else "없음"
    lines.append(f"  보이는 후보 {len(visible)}개 가운데 시장가 왕복으로 통과: {shown}")
    different = [a.kind for a in audits if a.differs]
    if different:
        lines.append(f"  비용 지도와 다른 종류: {', '.join(different)}(종료 코드 {DIFFERS})")
    return lines


def _verdicts(verdict: Mapping[str, str] | None) -> str:
    if verdict is None:
        return "없음(p* 없음)"
    return ", ".join(f"{COSTS[c]} {VERDICTS.get(v, v)}" for c, v in verdict.items())


def _side(i: Interval | None) -> str:
    return "예측 없음" if i is None else f"{_ratio(i)} ({i.n:,})"


def _interval(found: Mapping[str, Any] | None) -> Interval | None:
    if found is None:
        return None
    return Interval(found["value"], found["low"], found["high"], found["n"])


def _mb(value: float | None) -> str:
    return "모름" if value is None else f"{value:.0f} MB"


# The command.


async def run_audit(engine_db: Path, engine_url: str, target_version: int, meter: Meter | None = None) -> int:
    """``krx-quant cost-map-audit``: tonight's cost map from the Engine API at ``engine_url``, the
    audit from the engine's database at ``engine_db``, its text printed. ``AUDITED``, ``NOT_READ``
    or ``DIFFERS`` (module doc)."""
    meter = meter or Meter()
    started = clock.monotonic()
    meter.reset()
    try:
        async with aiohttp.ClientSession() as session:
            body = await read_cost_map(session, engine_url)
    except CostMapError as e:
        print(f"{TITLE}: 비용 지도를 읽지 못해 감사하지 않았다: {e}")
        log.error("cost map audit: could not read the cost map (%s)", e)
        return NOT_READ
    if not engine_db.exists():
        print(f"{TITLE}: 엔진 DB가 없어({engine_db.name}) 감사하지 않았다")
        return NOT_READ
    try:
        audits = audit(body, engine_db, target_version)
    except CostMapError as e:
        print(f"{TITLE}: 트레이더의 비용 지도가 아니어서 감사하지 않았다: {e}")
        return NOT_READ
    except sqlite3.Error as e:
        print(f"{TITLE}: 엔진 DB를 읽지 못해 감사하지 않았다: {e}")
        log.error("cost map audit: %s", e)
        return NOT_READ
    run = {"seconds": clock.monotonic() - started, "peak_rss_mb": meter.peak_mb()}
    print(report(body, audits, run))
    hidden = sum(1 for a in audits for c in a.candidates if not c.visible)
    passing = sum(
        1
        for a in audits
        for c in a.candidates
        if not c.visible and (c.judged["verdict"] or {}).get("market") == "pass"
    )
    differs = [a.kind for a in audits if a.differs]
    log.info(
        "cost map audit: %s hidden candidate(s), %s passing at the market's round trip; differs: %s",
        hidden,
        passing,
        ", ".join(differs) or "none",
    )
    return DIFFERS if differs else AUDITED
