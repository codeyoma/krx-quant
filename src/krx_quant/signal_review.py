"""The signal-threshold review (``krx-quant signal-review``, #206): what choosing the Telegram
signal settings (``KRX_SIGNAL_MIN_PROBABILITY``, ``KRX_SIGNAL_MIN_AGREE``) needs, as text to
paste. A scheduled job runs it once, on the first night with an official algorithm, and
posts it on #206 (``scheduled.py``); an agent recommends from it, the user decides.

It reads the engine DB without writing to it (``predictions.read_only``, as ``diagnose``),
over the latest ``days`` days with predictions (as the leaderboard's ``20d``). The scored
predictions are read a day at a time, to stay within the server's memory.

1. The signal settings in force, the rules the engine started with in the period (``runs``
   ``start``), and the signals it kept (``signal_sent``, ``signal_blocked``).
2. The leaderboard, as ``/v1/leaderboard?period=20d`` makes it (``stats.leaderboard``: the
   touch answer; an algorithm's predictions by models of the engine's target only; special
   predictions left out), each entry with its status and, for each class it called (up,
   flat, down), how often that class happened.
3. The official algorithms' records since they became official, likewise.
4. The candidates over the final consensus the engine stored: for each final consensus
   probability (``PROBABILITIES`` and the one in force) and number of official algorithms
   agreeing on the move (1 to the most it pooled, and the one in force), the signals the
   engine's own rule gives (``signals.Signals``, run as it is: the move, the agreement, once
   per move until its target time, blocked where the engine stored why), with their hit rate
   on the touch answer, up and down apart (the engine sends no flat signal), and the touch P&L
   per signal (research only). A final consensus that pooled a model of another target (before
   it changed, #170) is left out. The cell of the settings in force is set beside the signals
   the engine kept by them: the same when the alerts were on throughout.
5. The same over the final consensus the official algorithms now would have given over the
   whole period as its only members: their stored predictions (by models of the engine's
   target), pooled as the engine pools (``consensus.pool``), by the weights in force at each
   bar close. Where the engine stored a final consensus, its reason not to act is kept;
   elsewhere one is guessed from what was stored: a special situation in the window (a
   sidecar, or one learned later, included) or no reference price.

Ratios come with 95% Wilson intervals. They count each prediction or signal as independent:
they are not (a window overlaps the next three, and the symbols move together), so the
intervals are narrower than they should be. Nothing is judged against a threshold.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS
from krx_quant.baselines import Forecast, baselines_for
from krx_quant.config import Settings
from krx_quant.consensus import FINAL, NAMES, NO_WEIGHTS, Weights, pool
from krx_quant.feeds.base import KST
from krx_quant.lifecycle import Standing, Status
from krx_quant.market_view import HORIZON
from krx_quant.predictions import days_with_predictions, iso, read_only, scored_rows
from krx_quant.pricing import Direction
from krx_quant.signals import BLOCKED, SENT, Blocked, Judged, SignalRule, Signals
from krx_quant.stats import (
    Entry,
    Interval,
    Prices,
    Scored,
    entry,
    leaderboard,
    mean_interval,
    on_target,
    scored_by_algorithm,
    split,
    touch_profit,
    wilson,
)
from krx_quant.touch import TARGET_VERSION

DEFAULT_DAYS = 20
# The final consensus probabilities tried, besides the one in force.
PROBABILITIES = (0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8)
STATUS_NAMES = {
    Status.PROBATION: "수습",
    Status.ACTIVE: "정식",
    Status.BENCH: "벤치",
    Status.RETIRED: "은퇴",
    Status.CLONE: "복제",
}
CALLED = {Direction.UP: "상승", Direction.FLAT: "보합", Direction.DOWN: "하락"}

Key = tuple[str, str]  # (symbol, bar close as stored)


@dataclass
class BarClose:
    """What ``Signals.final`` is given at a bar close: its final consensus by symbol, the
    members' forecasts each pooled, and why a symbol's signal is blocked."""

    at: datetime
    pooled: dict[str, Forecast] = field(default_factory=dict)
    members: dict[str, dict[str, Forecast]] = field(default_factory=dict)
    blocked: dict[str, Blocked] = field(default_factory=dict)


def replay(closes: Iterable[BarClose], rule: SignalRule) -> list[Judged]:
    """The signals ``rule`` judges over ``closes`` (in time order), by the engine's own
    ``Signals``: those sent (``blocked`` None) and those blocked."""
    judged: list[Judged] = []
    signals = Signals(rule, lambda _text: None)
    for c in closes:
        signals.final(c.at, c.at + HORIZON, c.pooled, c.members, c.blocked, judged.append)
    return judged


@dataclass(frozen=True)
class _Member:
    """An algorithm's stored prediction at a bar close."""

    forecast: Forecast
    on_target: bool  # by a model of the engine's target (any, before #170)
    mid_close: float | None  # the reference price at the bar close
    special: bool  # a special situation touches its window, as stored


@dataclass(frozen=True)
class _Final:
    """The final consensus the engine stored at a bar close."""

    status: str
    version: str  # the weights in force
    forecast: Forecast | None
    blocked: str | None
    pooled: tuple[str, ...]


@dataclass(frozen=True)
class _Answer:
    """A bar close's touch answer: its class (None: unscorable) and its stored path."""

    label: Direction | None
    path: str


@dataclass
class Review:
    """What the review reads from the engine DB, over its days (``read``)."""

    days: list[date]  # oldest first
    standings: dict[str, Standing]
    runs: list[tuple[str, datetime, dict[str, Any]]]  # (kind, at, detail) in the days
    scored: dict[str, list[Scored]]  # as the leaderboard reads them
    finals: dict[Key, _Final]
    members: dict[Key, dict[str, _Member]]
    answers: dict[Key, _Answer]
    weights: dict[str, Weights]


def signal_review(db: Path, settings: Settings, until: date, days: int = DEFAULT_DAYS) -> str:
    """The review of the latest ``days`` days with predictions up to ``until``."""
    rule = settings.signal_thresholds
    with read_only(db) as conn:
        data = None if conn is None else read(conn, settings, until, days)
    lines = (
        [f"신호 기준 검토 {data.days[0]} – {data.days[-1]} ({len(data.days)}일, 예측이 있는 날)"]
        if data is not None and data.days
        else ["신호 기준 검토: 데이터 없음"]
    )
    sections: list[tuple[str, list[str]]] = [("1. 신호 설정과 엔진이 남긴 신호", _settings(settings, data))]
    if data is not None and data.days:
        baselines = frozenset(b.name for b in baselines_for(settings.target_version))
        algorithms, base = split(data.scored, baselines)
        for name, standing in data.standings.items():  # registered, not scored yet: unranked
            if name not in _KINDS and standing.status is not Status.RETIRED:
                algorithms.setdefault(name, [])
        official = _official(data.standings)
        stored, left_out = stored_closes(data.finals, data.members)
        rebuilt = rebuilt_closes(data.finals, data.members, official, data.weights)
        sections += [
            (
                f"2. 순위표(도달 정답, 특수 상황 제외, 채점과 기준 예측마다 짝이 {settings.min_sample}개 "
                "이상이면 순위)",
                _board(algorithms, base, data.standings, settings),
            ),
            (
                "3. 정식 알고리즘: 정식이 된 뒤의 성적",
                _since_official(algorithms, base, data, official, settings),
            ),
            (
                "4. 기준 후보: 엔진이 저장한 최종 합의로(최종 합의 확률 이상 × 같은 방향 정식 알고리즘 수 "
                "이상)",
                _grid(stored, data, rule, settings.fee_rate, left_out=left_out, kept=True),
            ),
            (
                "5. 기준 후보: 지금 정식 알고리즘이 기간 내내 합의의 구성원이었다면"
                f"({', '.join(official) or '정식 없음'})",
                _grid(rebuilt, data, rule, settings.fee_rate),
            ),
        ]
    for title, body in sections:
        lines += ["", title, *(f"  {line}" for line in body or ["없음"])]
    return "\n".join(lines)


# The kinds' algorithms have statuses too (#179), but no consensus.
_KINDS = {a.name for a in KIND_ALGORITHMS}
_ORDER = {a.name: i for i, a in enumerate(ALGORITHMS)}


def _registration_order(names: Iterable[str]) -> list[str]:
    """``names`` in the order the engine registers them (its consensus pools them so), the
    unknown after, by name."""
    return sorted(names, key=lambda n: (_ORDER.get(n, len(_ORDER)), n))


def _official(standings: Mapping[str, Standing]) -> list[str]:
    """The direction algorithms official now (the consensus's members), in registration order."""
    return _registration_order(
        n for n, s in standings.items() if n not in _KINDS and s.status is Status.ACTIVE
    )


# Reading


def _within(days: Sequence[date], column: str) -> tuple[str, list[str]]:
    """A condition on ``column`` (a time as stored) being on one of ``days``: the range lets
    SQLite use its index, the list leaves out the days in between."""
    marks = ", ".join("?" * len(days))
    return (
        f"{column} >= ? AND {column} < ? AND substr({column}, 1, 10) IN ({marks})",
        [
            iso(datetime.combine(min(days), time(), KST)),
            iso(datetime.combine(max(days) + timedelta(days=1), time(), KST)),
            *(d.isoformat() for d in days),
        ],
    )


def read(conn: sqlite3.Connection, settings: Settings, until: date, days: int) -> Review:
    """What the review needs of the latest ``days`` days with predictions up to ``until``."""
    conn.row_factory = sqlite3.Row  # as the store's rows (``select_predictions``)
    chosen = sorted(days_with_predictions(conn, until, days))
    standings = {
        r["algorithm"]: Standing(Status(r["status"]), datetime.fromisoformat(r["at"]))
        for r in conn.execute(
            "SELECT algorithm, status, at FROM statuses"
            " WHERE id IN (SELECT MAX(id) FROM statuses GROUP BY algorithm)"
        )
    }
    if not chosen:
        return Review([], standings, [], {}, {}, {}, {}, {})
    finals = _finals(conn, chosen)
    official = _official(standings)
    wanted = set(official) | {name for f in finals.values() for name in f.pooled}
    versions = {f.version for f in finals.values()}
    return Review(
        chosen,
        standings,
        _runs(conn, chosen),
        _scored(conn, chosen, settings.target_version),
        finals,
        _members(conn, chosen, wanted, settings.target_version),
        _answers(conn, chosen),
        _weights(conn, versions),
    )


def _runs(conn: sqlite3.Connection, days: Sequence[date]) -> list[tuple[str, datetime, dict[str, Any]]]:
    """The engine's starts and the signals it kept, on ``days``, oldest first."""
    on, params = _within(days, "at")
    out = []
    for kind, at, detail in conn.execute(
        f"SELECT kind, at, detail FROM runs WHERE kind IN (?, ?, ?) AND {on} ORDER BY at, id",
        ["start", SENT, BLOCKED, *params],
    ):
        try:
            read = json.loads(detail or "{}")
        except ValueError:
            continue
        out.append((kind, datetime.fromisoformat(at), read if isinstance(read, dict) else {}))
    return out


def _scored(conn: sqlite3.Connection, days: Sequence[date], target_version: int) -> dict[str, list[Scored]]:
    """Each predictor's scored predictions on ``days`` as the leaderboard reads them (special
    ones left out; an algorithm's by models of the engine's target only), a day at a time."""
    baselines = {b.name for b in baselines_for(target_version)} | set(NAMES)
    out: dict[str, list[Scored]] = {}
    for day in days:
        rows = scored_rows(conn, [day], None, False)
        if target_version >= 2:
            rows = on_target(rows, baselines, target_version)
        for name, records in scored_by_algorithm(rows).items():
            out.setdefault(name, []).extend(records)
    return out


def _finals(conn: sqlite3.Connection, days: Sequence[date]) -> dict[Key, _Final]:
    on, params = _within(days, "p.bar_close")
    pooled: dict[int, list[str]] = {}
    for prediction_id, name in conn.execute(
        f"SELECT q.prediction_id, q.algorithm FROM pooled q JOIN predictions p ON p.id = q.prediction_id"
        f" WHERE p.algorithm = ? AND {on} ORDER BY q.prediction_id, q.position",
        [FINAL, *params],
    ):
        pooled.setdefault(prediction_id, []).append(name)
    out: dict[Key, _Final] = {}
    for r in conn.execute(
        "SELECT p.id, p.symbol, p.bar_close, p.status, p.model_version, p.p_up, p.p_flat, p.p_down,"
        " p.expected_ticks, b.reason FROM predictions p LEFT JOIN blocks b ON b.prediction_id = p.id"
        f" WHERE p.algorithm = ? AND {on}",
        [FINAL, *params],
    ):
        made = r["status"] == "ok" and r["p_up"] is not None
        out[(r["symbol"], r["bar_close"])] = _Final(
            r["status"],
            r["model_version"],
            Forecast(r["p_up"], r["p_flat"], r["p_down"], r["expected_ticks"] or 0.0) if made else None,
            r["reason"],
            tuple(pooled.get(r["id"], ())),
        )
    return out


def _members(
    conn: sqlite3.Connection, days: Sequence[date], names: set[str], target_version: int
) -> dict[Key, dict[str, _Member]]:
    """The predictions ``names`` made (``ok``) on ``days``, by bar close."""
    out: dict[Key, dict[str, _Member]] = {}
    if not names:
        return out
    on, params = _within(days, "p.bar_close")
    marks = ", ".join("?" * len(names))
    for r in conn.execute(
        "SELECT p.symbol, p.algorithm, p.bar_close, p.p_up, p.p_flat, p.p_down, p.expected_ticks,"
        " m.target_version, o.mid_close, o.special FROM predictions p"
        " LEFT JOIN outcomes o ON o.prediction_id = p.id"
        " LEFT JOIN model_targets m ON m.algorithm = p.algorithm AND m.version = p.model_version"
        f" WHERE p.status = 'ok' AND p.algorithm IN ({marks}) AND {on}",
        [*sorted(names), *params],
    ):
        out.setdefault((r["symbol"], r["bar_close"]), {})[r["algorithm"]] = _Member(
            Forecast(r["p_up"], r["p_flat"], r["p_down"], r["expected_ticks"]),
            target_version < 2 or r["target_version"] == target_version,
            r["mid_close"],
            bool(r["special"]),
        )
    return out


def _answers(conn: sqlite3.Connection, days: Sequence[date]) -> dict[Key, _Answer]:
    """The touch answers of the bar closes on ``days`` (what every prediction is scored on)."""
    on, params = _within(days, "bar_close")
    return {
        (r["symbol"], r["bar_close"]): _Answer(
            None if r["class"] is None else Direction(r["class"]), r["path"]
        )
        for r in conn.execute(
            f"SELECT symbol, bar_close, class, path FROM touches WHERE target_version = ? AND {on}",
            [TARGET_VERSION, *params],
        )
    }


def _weights(conn: sqlite3.Connection, versions: set[str]) -> dict[str, Weights]:
    """The consensus weights of ``versions`` that were fitted (the rest are equal)."""
    out: dict[str, Weights] = {}
    names = sorted(versions)
    for start in range(0, len(names), 500):
        chunk = names[start : start + 500]
        marks = ", ".join("?" * len(chunk))
        fits = conn.execute(
            f"SELECT version, computed_at FROM consensus_fits WHERE version IN ({marks})", chunk
        )
        cells: dict[str, dict[tuple[str, str], dict[str, float]]] = {}
        for r in conn.execute(
            "SELECT version, symbol, slot, algorithm, weight FROM consensus_weights"
            f" WHERE version IN ({marks})",
            chunk,
        ):
            cells.setdefault(r["version"], {}).setdefault((r["symbol"], r["slot"]), {})[r["algorithm"]] = r[
                "weight"
            ]
        for version, computed_at in fits:
            out[version] = Weights(version, datetime.fromisoformat(computed_at), cells.get(version, {}))
    return out


# The bar closes the rule is run over


def _in_time_order(keys: Iterable[Key]) -> list[Key]:
    return sorted(keys, key=lambda k: (k[1], k[0]))


def stored_closes(
    finals: Mapping[Key, _Final], members: Mapping[Key, Mapping[str, _Member]]
) -> tuple[list[BarClose], int]:
    """The final consensus the engine stored with members, by bar close in time order, each
    with the members' stored forecasts it pooled; and how many (symbol, bar close) were left
    out: one that pooled a model of another target (before #170), or whose members' forecasts
    are not all there."""
    closes: dict[str, BarClose] = {}
    left_out = 0
    for key in _in_time_order(finals):
        symbol, close = key
        final = finals[key]
        if final.forecast is None:
            continue
        found = members.get(key, {})
        pooled = {name: found[name] for name in final.pooled if name in found}
        if not pooled or len(pooled) < len(final.pooled) or not all(m.on_target for m in pooled.values()):
            left_out += 1
            continue
        c = closes.setdefault(close, BarClose(datetime.fromisoformat(close)))
        c.pooled[symbol] = final.forecast
        c.members[symbol] = {name: m.forecast for name, m in pooled.items()}
        if final.blocked is not None:
            c.blocked[symbol] = Blocked(final.blocked)
    return list(closes.values()), left_out


def rebuilt_closes(
    finals: Mapping[Key, _Final],
    members: Mapping[Key, Mapping[str, _Member]],
    official: Sequence[str],
    weights: Mapping[str, Weights],
) -> list[BarClose]:
    """The final consensus ``official`` (in registration order) would have given at each bar
    close they predicted at by models of the engine's target, as the engine pools it: by the
    weights in force there (the stored final consensus's; equal without one). Blocked where
    the engine stored why; without a stored final consensus, where a special situation touches
    the window or there is no reference price."""
    closes: dict[str, BarClose] = {}
    for key in _in_time_order(members):
        symbol, close = key
        found = members[key]
        present = {name: found[name] for name in official if name in found and found[name].on_target}
        if not present:
            continue
        final = finals.get(key)
        fitted = NO_WEIGHTS if final is None else weights.get(final.version, NO_WEIGHTS)
        c = closes.setdefault(close, BarClose(datetime.fromisoformat(close)))
        forecasts = {name: m.forecast for name, m in present.items()}
        c.pooled[symbol] = pool(forecasts, fitted.of(symbol, c.at, list(forecasts)))
        c.members[symbol] = forecasts
        if final is not None and final.forecast is not None:
            why = None if final.blocked is None else Blocked(final.blocked)
        elif any(m.special for m in present.values()):
            why = Blocked.SPECIAL
        elif any(m.mid_close is None for m in present.values()):
            why = Blocked.NO_REFERENCE
        else:
            why = None
        if why is not None:
            c.blocked[symbol] = why
    return list(closes.values())


# Writing


def _probability(p: float) -> str:
    """A candidate's probability: two places, more when the setting has them (0.604 is not
    0.60, a row of its own)."""
    text = f"{p:.2f}"
    return text if float(text) == p else repr(p)


def _ratio(i: Interval | None) -> str:
    if i is None:
        return "—"
    if i.low is None or i.high is None:
        return f"{i.value:.1%}"
    return f"{i.value:.1%} [{i.low * 100:.1f}–{i.high * 100:.1f}%]"


def _points(value: float | None) -> str:
    """A difference of ratios, in percentage points."""
    return "—" if value is None else f"{value * 100:+.1f}%p"


def _rule(rule: Mapping[str, Any] | SignalRule | None) -> str:
    if rule is None:
        return "꺼짐"
    if isinstance(rule, SignalRule):
        return f"확률 ≥ {rule.min_probability:g} · 같은 방향 ≥ {rule.min_agree}"
    return f"확률 ≥ {rule.get('min_probability')} · 같은 방향 ≥ {rule.get('min_agree')}"


def _settings(settings: Settings, data: Review | None) -> list[str]:
    rule = settings.signal_thresholds
    lines = [
        f"지금 설정: KRX_SIGNAL_ALERT {'on' if settings.signal_rule is not None else 'off'} · "
        f"KRX_SIGNAL_MIN_PROBABILITY {rule.min_probability:g} · KRX_SIGNAL_MIN_AGREE {rule.min_agree}"
    ]
    if data is None:
        return lines
    starts: list[str] = []
    last: object = object()
    for kind, at, detail in data.runs:
        if kind == "start" and detail.get("signal_rule") != last:
            last = detail.get("signal_rule")
            starts.append(f"{at:%Y-%m-%d %H:%M} {_rule(detail.get('signal_rule'))}")
    lines.append("기간 중 엔진이 시작할 때의 신호 규칙: " + (" → ".join(starts) or "기록 없음"))
    sent = [d for kind, _, d in data.runs if kind == SENT]
    blocked = [d for kind, _, d in data.runs if kind == BLOCKED]
    moves = Counter(str(d.get("direction")) for d in sent)
    reasons = Counter(str(d.get("blocked")) for d in blocked)
    lines.append(
        f"엔진이 남긴 신호: 보냄 {len(sent):,}"
        + (f" (상승 {moves['up']:,} · 하락 {moves['down']:,})" if sent else "")
        + f" · 막힘 {len(blocked):,}"
        + (f" ({' · '.join(f'{r} {n:,}' for r, n in sorted(reasons.items()))})" if blocked else "")
    )
    official = _official(data.standings)
    lines.append(f"지금 정식 알고리즘(합의 구성원): {', '.join(official) or '없음'}")
    return lines


def _by_class(records: Sequence[Scored]) -> str:
    """For each class called, how many and how often it happened."""
    parts = []
    for d in Direction:
        called = [r.correct for r in records if r.predicted is d]
        parts.append(f"{CALLED[d]} 예측 {len(called):,} 적중 {_ratio(wilson(sum(called), len(called)))}")
    return " · ".join(parts)


def _entry_line(head: str, e: Entry, records: Sequence[Scored]) -> str:
    s = e.summary
    parts = [f"{head} 채점 {s.n:,}"]
    if s.n:
        parts += [f"정답률 {_ratio(s.accuracy)}", _by_class(records)]
        if e.lifts:
            parts.append(
                f"가장 약한 lift {_points(e.weakest_lift)} (짝 하한 {_points(e.lift_low)} · "
                f"거래일 하한 {_points(e.day_low)})"
            )
        per = s.pnl.per_trade
        parts.append(f"닿음 손익 {'—' if per is None else f'{per.value:+.2f}'}틱/번 ({s.pnl.trades:,}번)")
    return " · ".join(parts)


def _status(standings: Mapping[str, Standing], name: str) -> str:
    s = standings.get(name)
    if s is None:
        return "합의" if name in NAMES else "상태 없음"
    return STATUS_NAMES[s.status]


def _board(
    algorithms: dict[str, list[Scored]],
    baselines: dict[str, list[Scored]],
    standings: Mapping[str, Standing],
    settings: Settings,
) -> list[str]:
    board = leaderboard(algorithms, baselines, settings.min_sample, settings.fee_rate)
    lines = [
        _entry_line(
            f"{e.rank}위 {e.algorithm} [{_status(standings, e.algorithm)}]", e, algorithms[e.algorithm]
        )
        for e in board.ranked
    ]
    lines += [
        _entry_line(
            f"순위 없음 {e.algorithm} [{_status(standings, e.algorithm)}]", e, algorithms[e.algorithm]
        )
        for e in board.unranked
    ]
    lines += [_entry_line(f"기준 {e.algorithm}", e, baselines[e.algorithm]) for e in board.baselines]
    return lines


def _since_official(
    algorithms: Mapping[str, list[Scored]],
    baselines: dict[str, list[Scored]],
    data: Review,
    official: Sequence[str],
    settings: Settings,
) -> list[str]:
    start = datetime.combine(data.days[0], time(), KST)
    lines = []
    for name in official:
        since = data.standings[name].since
        head = f"{name}: {since.astimezone(KST):%Y-%m-%d %H:%M}부터 정식"
        if since < start:
            head += f"(기간 첫날 {data.days[0]}부터 셈)"
        records = [r for r in algorithms.get(name, []) if r.bar_close >= since]
        if not records:
            lines.append(f"{head} · 정식이 된 뒤 채점된 예측 없음")
            continue
        days = len({r.bar_close.astimezone(KST).date() for r in records})
        e = entry(name, records, baselines, settings.fee_rate)
        lines.append(_entry_line(f"{head} · {days}일", e, records))
    return lines


def _grid(
    closes: Sequence[BarClose],
    data: Review,
    rule: SignalRule,
    fee_rate: float,
    *,
    left_out: int = 0,
    kept: bool = False,
) -> list[str]:
    """Each candidate's signals over ``closes``; with ``kept``, the cell in force against the
    signals the engine kept."""
    lines = (
        [f"옛 정답 모델을 합쳤거나 구성원의 예측이 없는 최종 합의 {left_out:,}개(종목 × 봉 마감)는 뺐다"]
        if left_out
        else []
    )
    if not closes:
        return lines
    pairs = [(symbol, c) for c in closes for symbol in c.pooled]
    days = sorted({c.at.date() for c in closes})
    sizes = [len(c.members[symbol]) for symbol, c in pairs]
    labels = Counter(
        a.label for symbol, c in pairs if (a := data.answers.get((symbol, iso(c.at)))) and a.label is not None
    )
    labeled = sum(labels.values())
    lines += [
        f"최종 합의 {len(pairs):,}개(종목 × 봉 마감) · {days[0]} – {days[-1]} {len(days)}일 · "
        f"구성원 {min(sizes)}–{max(sizes)}개 · 신호는 상승·하락뿐이다(보합 신호는 없다)",
        "같은 봉 마감의 도달 정답: "
        + (
            " · ".join(f"{CALLED[d]} {labels[d] / labeled:.1%}" for d in Direction) + f" ({labeled:,}개)"
            if labeled
            else "없음"
        ),
    ]
    probabilities = sorted({*PROBABILITIES, rule.min_probability})
    agrees = sorted({*range(1, max(sizes) + 1), rule.min_agree})
    prices: dict[str, Prices | None] = {}
    for probability in probabilities:
        for agree in agrees:
            candidate = SignalRule(probability, agree)
            judged = replay(closes, candidate)
            line = _cell(candidate, judged, data.answers, prices, len(days), fee_rate)
            lines.append(line + (" (지금 설정)" if candidate == rule else ""))
    if kept:
        lines.append(_against_kept(replay(closes, rule), closes, data, rule))
    return lines


def _cell(
    rule: SignalRule,
    judged: Sequence[Judged],
    answers: Mapping[Key, _Answer],
    prices: dict[str, Prices | None],
    days: int,
    fee_rate: float,
) -> str:
    sent = [j.signal for j in judged if j.blocked is None]
    hits: dict[Direction, list[bool]] = {Direction.UP: [], Direction.DOWN: []}
    unlabeled = 0
    profits: list[float] = []
    for s in sent:
        answer = answers.get((s.symbol, iso(s.bar_close)))
        if answer is None or answer.label is None:
            unlabeled += 1
            continue
        hits[s.direction].append(answer.label is s.direction)
        if answer.path not in prices:
            prices[answer.path] = Prices.of(json.loads(answer.path))
        if (profit := touch_profit(s.direction, prices[answer.path], fee_rate)) is not None:
            profits.append(profit)
    both = hits[Direction.UP] + hits[Direction.DOWN]
    mean = mean_interval(profits)
    return " · ".join(
        [
            f"확률 ≥ {_probability(rule.min_probability)} · 같은 방향 ≥ {rule.min_agree}:"
            f" 신호 {len(sent):,}번 (하루 {len(sent) / days:.1f}번)",
            f"막힘 {len(judged) - len(sent):,}",
            *(
                f"{CALLED[d]} {sum(1 for s in sent if s.direction is d):,}번 적중 "
                f"{_ratio(wilson(sum(hits[d]), len(hits[d])))}"
                for d in (Direction.UP, Direction.DOWN)
            ),
            f"합쳐 {_ratio(wilson(sum(both), len(both)))}",
            f"정답 없음 {unlabeled:,}",
            f"닿음 손익 {'—' if mean is None else f'{mean.value:+.2f}'}틱/번",
        ]
    )


def _against_kept(
    judged: Sequence[Judged], closes: Sequence[BarClose], data: Review, rule: SignalRule
) -> str:
    """The cell in force against the signals the engine kept by the same rule at the same bar
    closes: the same when the alerts were on throughout."""
    within = {(symbol, iso(c.at)) for c in closes for symbol in c.pooled}
    replayed = {
        (
            j.signal.symbol,
            iso(j.signal.bar_close),
            j.signal.direction.value,
            None if j.blocked is None else j.blocked.value,
        )
        for j in judged
    }
    same_rule = {"min_probability": rule.min_probability, "min_agree": rule.min_agree}
    kept = {
        (d.get("symbol"), d.get("bar_close"), d.get("direction"), d.get("blocked"))
        for kind, _, d in data.runs
        if kind in (SENT, BLOCKED)
        and d.get("rule") == same_rule
        and (d.get("symbol"), d.get("bar_close")) in within
    }
    head = f"지금 설정({_rule(rule)})을 다시 낸 것과 엔진이 이 규칙으로 남긴 신호: "
    if not kept:
        return head + (
            f"다시 낸 것 {len(replayed):,}, 남긴 것 없음(알림이 꺼져 있었거나 신호가 없었다)"
            if replayed
            else "둘 다 없음"
        )
    sent = (sum(1 for r in replayed if r[3] is None), sum(1 for k in kept if k[3] is None))
    counts = f"보냄 {sent[0]:,} · 기록 {sent[1]:,}, 막힘 {len(replayed) - sent[0]:,}"
    counts += f" · 기록 {len(kept) - sent[1]:,}"
    if replayed == kept:
        return head + counts + " · 같다"
    return (
        head
        + counts
        + f" · 다르다: 다시 낸 것에만 {len(replayed - kept):,}, 기록에만 {len(kept - replayed):,}"
        + "(기간 중 알림이 꺼져 있었거나 규칙이 바뀌었으면 다를 수 있다)"
    )
