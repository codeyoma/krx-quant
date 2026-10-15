"""The signal-threshold review (#206): what choosing the Telegram signal settings needs.

Input: a store holding hand-made predictions, touch answers and final consensus (the grid
worked out by hand below), or what an engine on a fake clock stored over a morning with
signals on. Output: the review's text, and the signals each candidate rule gives, set
against those the engine sent and blocked by that rule.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from krx_quant.algorithms import Model
from krx_quant.baselines import Forecast
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.consensus import EQUAL_VERSION, FINAL, pool
from krx_quant.engine import Event, PredictionEngine
from krx_quant.features import FeatureVector
from krx_quant.lifecycle import Status
from krx_quant.predictions import NewPrediction, PredictionStore, Score, read_only
from krx_quant.pricing import Direction
from krx_quant.replay import replay_into
from krx_quant.signal_review import BarClose, read, rebuilt_closes, replay, signal_review, stored_closes
from krx_quant.signals import BLOCKED, SENT, SignalRule, Signals
from krx_quant.touch import TARGET_VERSION
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import CALENDAR, FEE, SYMBOL, at, vi
from tests.test_leaderboard_api import touch
from tests.test_replay import busy_morning

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
EARLIER, DAY = date(2026, 10, 12), date(2026, 10, 13)
SETTINGS = Settings("key", "secret", signal_thresholds=SignalRule(0.6, 2), target_version=2)


def on(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, at(f"{hhmm}:00").timetz())


def f(p_up: float, p_flat: float, p_down: float) -> Forecast:
    return Forecast(p_up, p_flat, p_down, 0.0)


# Each bar close by hand: newton's and gauss's forecasts, the final consensus (their equal-weight
# mean, as the engine pools before any fit), why it is blocked, and the touch answer.
N1, G1 = f(0.84, 0.10, 0.06), f(0.62, 0.30, 0.08)  # both up: (0.73, 0.20, 0.07)
G2 = f(0.42, 0.50, 0.08)  # with N1, one up, one flat: (0.63, 0.30, 0.07)
N3, G3 = f(0.10, 0.20, 0.70), f(0.06, 0.30, 0.64)  # both down: (0.08, 0.25, 0.67)
N4, G4 = f(0.74, 0.20, 0.06), f(0.52, 0.40, 0.08)  # both up: (0.63, 0.30, 0.07)
G5 = f(0.28, 0.40, 0.32)  # with N3, one down, one flat: (0.19, 0.30, 0.51)
# (newton, gauss, blocked, answer) at 09:01-09:06: the consensus had members on DAY.
WITH_MEMBERS = [
    (N1, G1, "special", UP),
    (N1, G2, None, UP),
    (N3, G3, None, FLAT),
    (N4, G4, None, DOWN),
    (N3, G5, None, DOWN),
    (N1, G1, None, UP),
]
# The day before, it had none (``no_members``): no algorithm was official yet.
WITHOUT_MEMBERS = [(N1, G1, UP), (N3, G3, DOWN), (N1, G2, FLAT)]


def mean(newton: Forecast, gauss: Forecast) -> Forecast:
    """The two pooled with equal weights, as the engine pools them."""
    return pool({"newton": newton, "gauss": gauss}, {"newton": 0.5, "gauss": 0.5})


def add(
    store: PredictionStore,
    algorithm: str,
    t: datetime,
    forecast: Forecast | None,
    answer: Direction,
    *,
    version: str = "v1",
    status: str = "ok",
    blocked: str | None = None,
    pooled: tuple[str, ...] | None = None,
) -> None:
    """One stored prediction at ``t``, scored on ``answer`` once its four minutes are over."""
    added = store.add_all(
        [
            NewPrediction(
                SYMBOL,
                algorithm,
                version,
                t,
                t + timedelta(seconds=1),
                forecast,
                t + timedelta(minutes=4),
                81_250,
                81_250,
                2,
                status=status,
                blocked=blocked,
                pooled=pooled,
            )
        ]
    )
    for pending in added.pending:
        store.score_all(
            [(pending.prediction_id, Score(81_250, answer, answer, None))], t + timedelta(minutes=4)
        )


def hand_made(db: Path) -> None:
    store = PredictionStore(db)
    for name in ("newton", "gauss"):
        store.add_status(name, on(EARLIER, "08:00"), None, Status.ACTIVE, "test")
    store.record_models([("newton", "v1", TARGET_VERSION), ("gauss", "v1", TARGET_VERSION)])
    for i, (n, g, answer) in enumerate(WITHOUT_MEMBERS):
        t = on(EARLIER, f"09:0{i + 1}")
        add(store, "newton", t, n, answer)
        add(store, "gauss", t, g, answer)
        add(store, FINAL, t, None, answer, version=EQUAL_VERSION, status="no_members")
        store.add_touches([touch(SYMBOL, t, answer)])
    for i, (n, g, blocked, answer) in enumerate(WITH_MEMBERS):
        t = on(DAY, f"09:0{i + 1}")
        add(store, "newton", t, n, answer)
        add(store, "gauss", t, g, answer)
        add(
            store,
            FINAL,
            t,
            mean(n, g),
            answer,
            version=EQUAL_VERSION,
            blocked=blocked,
            pooled=("newton", "gauss"),
        )
        store.add_touches([touch(SYMBOL, t, answer)])
    store.close()


def section(text: str, number: int) -> list[str]:
    [found] = [p for p in text.split("\n\n") if p.startswith(f"{number}. ")]
    return [line.strip() for line in found.splitlines()[1:]]


def cell(lines: Sequence[str], probability: float | str, agree: int) -> str:
    shown = probability if isinstance(probability, str) else f"{probability:.2f}"
    head = f"확률 ≥ {shown} · 같은 방향 ≥ {agree}:"
    [found] = [line for line in lines if line.startswith(head)]
    return found


def test_each_candidate_s_signals_and_hit_rate_as_worked_out_by_hand(tmp_path: Path) -> None:
    hand_made(tmp_path / "engine.db")

    text = signal_review(tmp_path / "engine.db", SETTINGS, DAY)

    assert text.splitlines()[0] == "신호 기준 검토 2026-10-12 – 2026-10-13 (2일, 예측이 있는 날)"
    stored = section(text, 4)
    # 09:01 is blocked (a VI): it does not hold 09:02 back. 09:02 calls up until 09:06, so 09:04
    # is not called again, 09:06 is; 09:03 calls down until 09:07, 09:05 within it.
    assert cell(stored, 0.5, 1) == (
        "확률 ≥ 0.50 · 같은 방향 ≥ 1: 신호 3번 (하루 3.0번) · 막힘 1 · 상승 2번 적중 100.0% [34.2–100.0%]"
        " · 하락 1번 적중 0.0% [0.0–79.3%] · 합쳐 66.7% [20.8–93.9%] · 정답 없음 0 · 닿음 손익 -1.20틱/번"
    )
    # With two agreeing, 09:02 (one up, one flat) is no signal: 09:04 is the up call.
    assert cell(stored, 0.5, 2).startswith(
        "확률 ≥ 0.50 · 같은 방향 ≥ 2: 신호 2번 (하루 2.0번) · 막힘 1 · 상승 1번 적중 0.0% [0.0–79.3%]"
        " · 하락 1번 적중 0.0% [0.0–79.3%] · 합쳐 0.0% [0.0–65.8%]"
    )
    assert cell(stored, 0.6, 2).endswith("(지금 설정)")
    # At 65%, 09:02 and 09:04 (63%) are not sure enough: 09:03 down, 09:06 up.
    assert " 신호 2번 (하루 2.0번) · 막힘 1 · 상승 1번 적중 100.0%" in cell(stored, 0.65, 1)
    assert " 신호 1번 (하루 1.0번) · 막힘 1 · 상승 1번 적중 100.0%" in cell(stored, 0.7, 2)
    assert " 신호 0번 (하루 0.0번) · 막힘 0 · 상승 0번 적중 — · 하락 0번 적중 — · 합쳐 —" in cell(
        stored, 0.8, 1
    )
    assert len([line for line in stored if line.startswith("확률")]) == 7 * 2  # 50-80% × 1-2 agreeing
    assert stored[0].startswith("최종 합의 6개(종목 × 봉 마감) · 2026-10-13 – 2026-10-13 1일 · 구성원 2–2개")
    assert stored[1] == "같은 봉 마감의 도달 정답: 상승 50.0% · 보합 16.7% · 하락 33.3% (6개)"
    assert stored[-1] == (
        "지금 설정(확률 ≥ 0.6 · 같은 방향 ≥ 2)을 다시 낸 것과 엔진이 이 규칙으로 남긴 신호: "
        "다시 낸 것 3, 남긴 것 없음(알림이 꺼져 있었거나 신호가 없었다)"
    )

    # Rebuilt with both official throughout: the day before adds 09:01 up (right) and 09:02
    # down (right); its 09:03 up is within 09:01's four minutes.
    rebuilt = section(text, 5)
    assert cell(rebuilt, 0.5, 1).startswith(
        "확률 ≥ 0.50 · 같은 방향 ≥ 1: 신호 5번 (하루 2.5번) · 막힘 1 · 상승 3번 적중 100.0% [43.9–100.0%]"
        " · 하락 2번 적중 50.0% [9.5–90.5%] · 합쳐 80.0% [37.6–96.4%]"
    )
    assert rebuilt[0].startswith("최종 합의 9개(종목 × 봉 마감) · 2026-10-12 – 2026-10-13 2일")


def test_a_setting_with_more_places_has_a_row_of_its_own(tmp_path: Path) -> None:
    """Input: the setting 0.604 beside the candidate 0.60. Output: two rows, each labelled with
    its own probability; the setting's is the one marked."""
    hand_made(tmp_path / "engine.db")
    settings = replace(SETTINGS, signal_thresholds=SignalRule(0.604, 2))

    stored = section(signal_review(tmp_path / "engine.db", settings, DAY), 4)

    assert not cell(stored, 0.6, 2).endswith("(지금 설정)")
    assert cell(stored, "0.604", 2).endswith("(지금 설정)")


def test_a_final_consensus_that_pooled_a_model_of_another_target_is_left_out(tmp_path: Path) -> None:
    hand_made(tmp_path / "engine.db")
    store = PredictionStore(tmp_path / "engine.db")
    store.record_models([("newton", "old", 1)])  # trained on the target before #170
    t = on(DAY, "09:07")
    add(store, "newton", t, N1, UP, version="old")
    add(store, "gauss", t, G1, UP)
    add(store, FINAL, t, mean(N1, G1), UP, version=EQUAL_VERSION, pooled=("newton", "gauss"))
    store.close()

    stored = section(signal_review(tmp_path / "engine.db", SETTINGS, DAY), 4)

    assert stored[0] == "옛 정답 모델을 합쳤거나 구성원의 예측이 없는 최종 합의 1개(종목 × 봉 마감)는 뺐다"
    assert stored[1].startswith("최종 합의 6개(종목 × 봉 마감)")


def test_the_settings_the_leaderboard_and_the_official_algorithms_records(tmp_path: Path) -> None:
    hand_made(tmp_path / "engine.db")

    text = signal_review(tmp_path / "engine.db", replace(SETTINGS, min_sample=5), DAY)

    assert section(text, 1) == [
        "지금 설정: KRX_SIGNAL_ALERT off · KRX_SIGNAL_MIN_PROBABILITY 0.6 · KRX_SIGNAL_MIN_AGREE 2",
        "기간 중 엔진이 시작할 때의 신호 규칙: 기록 없음",
        "엔진이 남긴 신호: 보냄 0 · 막힘 0",
        "지금 정식 알고리즘(합의 구성원): newton, gauss",
    ]
    board = section(text, 2)
    # newton called up 6 times (4 right) and down 3 times (2 right), never flat.
    [newton] = [line for line in board if " newton [정식] " in line]
    assert "newton [정식] 채점 9 · 정답률 66.7% [35.4–87.9%] · 상승 예측 6 적중 66.7% [30.0–90.3%]" in newton
    assert "보합 예측 0 적중 — · 하락 예측 3 적중 66.7% [20.8–93.9%]" in newton
    assert any(line.startswith("기준 baseline-flat 채점 0") for line in board)
    [since] = [line for line in section(text, 3) if line.startswith("newton:")]
    assert since.startswith("newton: 2026-10-12 08:00부터 정식 · 2일 채점 9 · 정답률 66.7%")


def test_without_a_database_only_the_settings(tmp_path: Path) -> None:
    on_ = replace(SETTINGS, signal_rule=SignalRule(0.6, 2))
    text = signal_review(tmp_path / "none.db", on_, DAY)

    assert text.splitlines()[:3] == [
        "신호 기준 검토: 데이터 없음",
        "",
        "1. 신호 설정과 엔진이 남긴 신호",
    ]
    assert "KRX_SIGNAL_ALERT on" in text and not (tmp_path / "none.db").exists()


# Against the engine


class Cycling(FixedAlgorithm):
    """Predicts ``forecasts`` in turn, one a bar close."""

    def __init__(self, name: str, feature: str, forecasts: Sequence[Forecast]) -> None:
        super().__init__(name, features=(feature,), unique=(feature,))
        self._turns = itertools.cycle(forecasts)

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        return next(self._turns)


def members() -> list[Cycling]:
    """Three members whose consensus wanders over the thresholds."""
    return [
        Cycling("m0", "velocity", [f(0.7, 0.2, 0.1), f(0.3, 0.4, 0.3), f(0.55, 0.3, 0.15), f(0.1, 0.2, 0.7)]),
        Cycling(
            "m1", "acceleration", [f(0.8, 0.1, 0.1), f(0.6, 0.3, 0.1), f(0.1, 0.3, 0.6), f(0.2, 0.2, 0.6), N4]
        ),
        Cycling("m2", "relative_volume", [f(0.3, 0.4, 0.3), f(0.75, 0.15, 0.1), f(0.05, 0.25, 0.7)]),
    ]


# A VI learned a second after the 09:05 bar close (displayed 3 seconds after): it blocks
# 09:05's call, and 09:06-09:07 are not predicted.
MORNING: list[Event] = sorted(
    [*busy_morning(end="09:30:30"), vi(at("09:05:02"), "09:05:01", "09:07:30")],
    key=lambda e: e[1]["received_at"],
)
RULES = [SignalRule(0.5, 1), SignalRule(0.5, 3), SignalRule(0.55, 2), SignalRule(0.6, 1), SignalRule(0.7, 2)]


async def morning(db: Path, rule: SignalRule) -> None:
    """The engine over the morning with ``rule``'s signals on, the members official."""
    store = PredictionStore(db)
    algorithms = members()
    for a in algorithms:
        store.add_status(a.name, at("08:00:00"), None, Status.ACTIVE, "test")
    store.mark_target(TARGET_VERSION, at("08:00:00"))
    clock = FakeClock(at("09:00:00"))
    engine = PredictionEngine(
        store, CALENDAR, (SYMBOL,), FEE, clock, algorithms=algorithms, signals=Signals(rule, lambda _: None)
    )
    await replay_into(engine, clock, MORNING, until=at("09:30:30"), display_delay=timedelta(seconds=3))
    store.close()


def kept(db: Path) -> set[tuple[str, str, str, str | None]]:
    """The signals the engine sent and blocked: (symbol, bar close, move, why blocked)."""
    store = PredictionStore(db)
    try:
        runs = [*store.runs(SENT, at("00:00:00")), *store.runs(BLOCKED, at("00:00:00"))]
    finally:
        store.close()
    details = [json.loads(r.detail) for r in runs]
    return {(d["symbol"], d["bar_close"], d["direction"], d["blocked"]) for d in details}


def judged(closes: Sequence[BarClose], rule: SignalRule) -> set[tuple[str, str, str, str | None]]:
    """The signals ``rule`` judges over ``closes``, as ``kept`` reads the engine's."""
    return {
        (
            j.signal.symbol,
            j.signal.bar_close.isoformat(),
            j.signal.direction.value,
            None if j.blocked is None else j.blocked.value,
        )
        for j in replay(closes, rule)
    }


async def test_each_candidate_gives_the_signals_the_engine_sends_by_it(tmp_path: Path) -> None:
    for i, rule in enumerate(RULES):
        await morning(tmp_path / f"{i}.db", rule)
    settings = Settings("key", "secret", target_version=1)  # the stand-ins' models are of no target

    with read_only(tmp_path / "0.db") as conn:
        assert conn is not None
        data = read(conn, settings, at("00:00:00").date(), 20)
    closes, left_out = stored_closes(data.finals, data.members)
    assert left_out == 0 and len(closes) == 28  # 09:01-09:30 but 09:06-09:07, in the VI
    # The members were official all morning, before any fit: rebuilt, the consensus is the same.
    rebuilt = rebuilt_closes(data.finals, data.members, ["m0", "m1", "m2"], data.weights)

    for i, rule in enumerate(RULES):
        engine_s = kept(tmp_path / f"{i}.db")
        assert judged(closes, rule) == engine_s, rule
        assert judged(rebuilt, rule) == engine_s, rule
        assert ("005930", "2026-10-12T09:05:00+09:00", "up", "special") in engine_s  # the VI blocks it
    assert len({frozenset(kept(tmp_path / f"{i}.db")) for i in range(len(RULES))}) == len(RULES)

    # The review run by the settings in force sets them beside the signals the engine kept.
    for i, rule in enumerate(RULES):
        text = signal_review(
            tmp_path / f"{i}.db", replace(settings, signal_rule=rule, signal_thresholds=rule), DAY
        )
        assert section(text, 4)[-1].endswith(" · 같다"), rule
