"""A regime gate measured on #447's holdout (``krx-quant climate-gate``, #449).

#431's report (2026-10-12–10-30) had turing losing to the climate baseline when the market was
quiet (σ̂₄'s lowest tercile) and at lunch (the 12:00 and 12:30 slots). #449 fixed a gate on that
before the holdout's days: at a bar close in σ̂₄'s lowest tercile, or in the 12:00 or 12:30 slot,
it takes ``baseline-climate``'s prediction; anywhere else, turing's (``gated``). It is measured
here and not put in the engine: the user decides on the result.

- **The window, σ̂₄'s cuts, the slots** are #447's (``holdout.read_holdout``): the first ``DAYS``
  (10) days after 2026-10-30 with turing's predictions on answer version 3, each symbol's tercile
  cuts from the discovery period's σ̂₄ (``Holdout.tercile``), ``consensus.slot``. A bar close
  without σ̂₄, or of a symbol without cuts, is in no tercile: the gate hands it to the climate only
  at lunch.
- **The three arms** (``arms``): the gate, turing and the climate on the same bar closes, those
  both turing and the climate were scored on.
- **The primary metric:** each arm's three-class hit rate (up, flat, down).
- **The verdict** (``holdout.judge``): the gate's lift over turing, paired by symbol and bar
  close, taken a trading day at a time (a Student t interval over the days) at ``LEVEL``, 95%:
  one claim, so no Bonferroni. The gate is better than turing only when the lower end is above 0.
  With fewer than #447's minimums (``MIN_SCORED`` scored, pairs on ``DAYS`` days) it has too few
  and is not judged. Its lift over the climate is given the same way, and not judged.
- **The share** of the bar closes the gate handed to the climate, and why (σ̂₄'s lowest tercile,
  lunch, both).

Nothing is judged before the window is full. A scheduled job runs it on the night #447's does
(``scheduled.CLIMATE_GATE``) and posts it on #449. It reads the engine DB without writing to it
(``predictions.read_only``). No price, market value or σ̂₄ cut is printed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from krx_quant.climate import CLIMATE
from krx_quant.consensus import slot
from krx_quant.holdout import (
    AFTER,
    ALGORITHM,
    DAYS,
    DISCOVERY,
    FAILS,
    LUNCH,
    MIN_SCORED,
    STANDS,
    TARGET,
    TOO_FEW,
    Holdout,
    Judged,
    _ratio,
    _span,
    judge,
    read_holdout,
)
from krx_quant.predictions import read_only
from krx_quant.stats import Scored, points_text
from krx_quant.touch import TARGET_VERSION

# Fixed in #449 before the window's results: none of these is tuned.
GATE = "climate-gate"  # the gate's records, as the lifts pair them
QUIET = "low"  # σ̂₄'s tercile (``api.TERCILES``) the gate hands to the climate
LEVEL = 0.95  # one claim (better than turing): no Bonferroni


def gated(r: Scored, data: Holdout) -> bool:
    """Whether the gate takes the climate's prediction at ``r``'s bar close: in σ̂₄'s lowest tercile
    by its symbol's discovery cuts, or in the 12:00 or 12:30 slot."""
    return data.tercile(r) == QUIET or slot(r.bar_close) in LUNCH


@dataclass(frozen=True)
class Arms:
    """The gate, turing and the climate on the same bar closes, in turing's order (``arms``)."""

    gate: list[Scored]
    turing: list[Scored]
    climate: list[Scored]
    handed: int  # the bar closes the gate handed to the climate
    quiet: int  # those in σ̂₄'s lowest tercile
    lunch: int  # those in the 12:00 or 12:30 slot
    unsorted: int  # the bar closes in no tercile (no σ̂₄ there, or no cuts for the symbol)

    @property
    def both(self) -> int:
        return self.quiet + self.lunch - self.handed


def arms(data: Holdout) -> Arms:
    """The window's bar closes both turing and the climate were scored on, and at each the gate's
    record: the climate's where ``gated``, else turing's."""
    theirs = {(c.symbol, c.bar_close): c for c in data.climate}
    pairs = [(r, c) for r in data.records if (c := theirs.get((r.symbol, r.bar_close))) is not None]
    gate: list[Scored] = []
    handed = quiet = lunch = unsorted = 0
    for r, c in pairs:
        t = data.tercile(r)
        took = gated(r, data)
        gate.append(replace(c if took else r, algorithm=GATE))
        handed += took
        quiet += t == QUIET
        lunch += slot(r.bar_close) in LUNCH
        unsorted += t is None
    return Arms(gate, [r for r, _ in pairs], [c for _, c in pairs], handed, quiet, lunch, unsorted)


@dataclass(frozen=True)
class Compared:
    """The three arms (``compare``): the gate judged against turing (the verdict), and against the
    climate (not judged). Each one's ``accuracy`` is the gate's and ``baseline`` the other arm's,
    on the same bar closes."""

    arms: Arms
    over_turing: Judged
    over_climate: Judged


def compare(data: Holdout) -> Compared:
    a = arms(data)
    return Compared(a, judge(a.gate, a.turing, 1, LEVEL), judge(a.gate, a.climate, 1, LEVEL))


# Writing


TITLE = f"{ALGORITHM}·기후 국면 문 홀드아웃 판정 (#449)"
CLAIM = f"문이 {ALGORITHM}보다 낫다({ALGORITHM} 대비 거래일 {LEVEL:.0%} 하한 > 0)"


def climate_gate(db: Path) -> str:
    """The gate, turing and the climate compared on the window, as text to paste; nothing judged
    before it is full."""
    with read_only(db) as conn:
        data = None if conn is None else read_holdout(conn)
    if data is None:
        return f"{TITLE}: 데이터 없음"
    first, last = DISCOVERY
    span = f"{data.days[0]} – {data.days[-1]}, " if data.days else ""
    lunch = "·".join(LUNCH)
    lines = [
        TITLE,
        f"창: #447과 같다. {AFTER} 뒤 {ALGORITHM}의 정답 버전 {TARGET} 예측이 있는 첫 {DAYS}거래일, "
        f"고정 창이다(최근 N일이 아니다). 지금 {span}{len(data.days)}/{DAYS}일.",
        f"예측: 실시간, 도달 정답(정답 버전 {TARGET_VERSION}), 특수 상황 제외, {ALGORITHM}은 "
        f"정답 버전 {TARGET}으로 학습한 모델의 것만. #431의 리포트, #447과 같다.",
        f"문: 봉 마감이 σ̂₄ 하위 삼분위이거나 {lunch} 칸이면 {CLIMATE}의 예측을, 아니면 {ALGORITHM}의 "
        "예측을 쓴다(#449에 고정). 칸은 consensus.slot이다(봉 마감 12:01–13:00이 12:00·12:30 칸). "
        "엔진에는 넣지 않는다.",
    ]
    if not data.full:
        lines.append(f"창이 아직 차지 않았다. 판정하지 않는다({DAYS}일이 차면 판정한다).")
        return "\n".join(lines)
    c = compare(data)
    a = c.arms
    n = len(a.gate)
    lines += [
        f"σ̂₄ 삼분위 경계: #447과 같다. 종목마다 발견 기간 {first} – {last}의 봉 마감 σ̂₄로 정했다"
        f"(종목 {len(data.cuts)}개, 돌 때마다 DB에서 다시 낸다). 같은 봉 마감 {n:,}개 중 {a.unsorted:,}개는 "
        f"σ̂₄나 종목의 경계가 없어 어느 삼분위에도 들지 않는다({lunch} 칸이 아니면 {ALGORITHM}의 예측).",
        f"비교: 세 팔(문, {ALGORITHM}, {CLIMATE})을 {ALGORITHM}과 {CLIMATE}가 모두 채점된 같은 봉 마감에서 "
        f"잰다({ALGORITHM}의 채점 {len(data.records):,}개 중). 주 지표는 세 분류(상승·보합·하락) 맞힘률이다. "
        "lift는 같은 (종목, 봉 마감)끼리 짝지은 정답률 차이다.",
        f"판정: 문의 {ALGORITHM} 대비 lift를 거래일마다 낸 {LEVEL:.0%} t 구간(주장 하나라 "
        f"본페로니 없음)으로만 한다. 하한 > 0일 때만 '{STANDS}'(문이 {ALGORITHM}보다 낫다), "
        f"아니면 '{FAILS}'이다. 짝이 {MIN_SCORED}개 미만이거나 짝이 있는 거래일이 {DAYS}일보다 "
        f"적으면 '{TOO_FEW}'이고 판정하지 않는다(#447과 같은 최소 표본). {CLIMATE} 대비는 같은 "
        f"방법으로 적고 판정하지 않는다. 짝 구간(같은 {LEVEL:.0%})은 예측을 서로 독립으로 센다"
        "(4분 창이 겹쳐 실제보다 좁다). 참고용이다.",
        "가격, 시세 값, σ̂₄ 경계 값은 싣지 않는다.",
        "",
        f"기후로 넘긴 봉 마감: {a.handed:,} / {n:,} ({_ratio(a.handed / n if n else None)})"
        f" · σ̂₄ 하위 삼분위 {a.quiet:,} · {lunch} 칸 {a.lunch:,} · 둘 다 {a.both:,}",
        f"맞힘률(세 분류, 같은 봉 마감 {n:,}개): 문 {_ratio(c.over_turing.accuracy)}"
        f" · {ALGORITHM} {_ratio(c.over_turing.baseline)} · {CLIMATE} {_ratio(c.over_climate.baseline)}",
        lift_line(f"문 − {ALGORITHM}", c.over_turing, judged=True),
        lift_line(f"문 − {CLIMATE}", c.over_climate, judged=False),
        "",
        f"{CLAIM}: {c.over_turing.verdict}",
    ]
    return "\n".join(lines)


def lift_line(name: str, j: Judged, judged: bool) -> str:
    head = f"{name}: 짝 {j.lift.n:,} · {j.days}일"
    if j.verdict == TOO_FEW:
        return f"{head} · {TOO_FEW}"
    line = (
        f"{head} · lift {points_text(j.lift.value)} · 짝 구간 {_span(j.lift)} · 거래일 구간 {_span(j.by_day)}"
    )
    return f"{line} · {j.verdict}" if judged else line
