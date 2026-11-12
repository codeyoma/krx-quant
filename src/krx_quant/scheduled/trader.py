"""The auto trader's jobs (#393, #500): its nightly report and re-fit, the fill notice's columns 10
and 17 (#648), and the live account's read against KIS. ``JOBS`` (``krx_quant.scheduled``) lists
them in the order they run.

The mock day's check against KIS, the passive entry's measurement and the cost map's audit ran,
posted their results and left ``JOBS`` (#505), as a one-off job does once it has done its work.
The mock check's command stays for a run by hand; the other two left with their code once
concluded (#559). The mock fill notice's layout left too once it had joined every kind of order
(#580); its command stays.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from krx_quant.process_log import Ran
from krx_quant.scheduled.core import FAILED, OK, Attachment, Job, Night, Reads, Target, again_on_new_code
from krx_quant.trader.mock_check import trader_in_mock
from krx_quant.trader.notice_columns import APART, PARTIAL, apart_orders
from krx_quant.trader.report import PRACTICE_HEADING, PRACTICE_TRADES, calls_agent

# The trader's night (#399, #400, #402, #393 "정책과 자기 개선", "리포트와 에이전트"): every trading
# day's night, after the nightly training, ``refit-policy --report`` first judges, on a review night
# alone, the cohort of challengers that ran in paper beside the champion for the cycle, on the
# trader's records, and promotes one that beat it (#400, #473, ``promotion.promote``; after the
# market, so never during a session), then reads tonight's cost map
# from the collector's Engine API (``KRX_TRADER_ENGINE_URL``: compose sets the engine service's own,
# http://engine:8080), each passing cell's stored predictions and answers of its 20 days from the
# engine's database (read-only, a cell at a time), simulates the trader's trades on the answers' paths
# at each point of a small grid and keeps a new policy version in state/trader.sqlite3: the first one
# the champion (an empty one, on a store without one, before a fit with cells), a later one waiting
# for the next cohort; one equal to the champion, a running challenger or a policy waiting is not
# kept. On a review night the night's end forms the next cycle's cohort from the newest three
# waiting (#399, #400, #473, ``refit.py``, ``cohort.py``). Then it
# prints the day's report (#402, ``trader/report.py``, read from the same store) above the re-fit's
# own output: the day's ledger and the champion's trades, why no cell traded when none did, each
# policy's paper record and judgment, what the night changed, the live gate, the limits, halts and
# pauses. It predicts nothing again: it replays the cost map's 20 recorded days by the paper trader's
# rules (#470, ``trader/replay.py``), a day's recording at a time, once a cell the cost map passes
# (four cells of 20 server-sized days in about 5 s on a Mac, about 200 MB at the peak); its peak
# resident memory and a day's highest are in its output. Its output holds cells, thresholds, probabilities,
# counts, ticks, shares, P&L and limits in won, log growth and times, no price, account, order number
# nor key. The ten minutes are a bound (the cost map, computed by the collector, is waited for at most
# two): it runs before the simulations and pre-validations, which must start by 00:30.
#
# One comment a night on the report issue (#417, opened by #402 and left open), which gets
# ``ready-for-agent`` only on the nights the report calls an agent (``report.calls_agent``: the week's
# last trading day, a loss limit reached that day, a change of the champion, the live gate passed, the
# re-fit failed) or the job failed; another night's comment is a record and changes no label. No label
# is taken off: a question an agent left for the user there (``ready-for-human``) stays. The
# re-fit used to post its own comment each night (``refit-policy-399``, #399, #416): it is folded in
# here so that the night's one comment is the one the label points at, and the judgment and the gate
# are not posted twice. An agent reads it as ``docs/agents/trader-report.md`` says.
TRADER_REPORT_ISSUE = 417


def _every_trading_night(night: Night[None]) -> bool:
    """Every trading day's night, once; a run a restart cut runs the next trading night."""
    if not night.trading_day:
        return False
    record = night.record
    return record is None or record.outcome not in (OK, FAILED) or record.day != night.day


def trader_report_calls(ran: Ran) -> bool:
    """Whether the trader's night calls an agent (#402): its report says so, or the run failed."""
    return ran.exit_code != 0 or ran.timed_out or calls_agent(ran.stdout)


TRADER_REPORT = Job(
    name="trader-report",
    due=_every_trading_night,
    command=("refit-policy", "--report"),
    timeout=timedelta(minutes=10),
    label_if=trader_report_calls,
    unlabel=(),  # a question an agent put to the user on the issue (``ready-for-human``) stays
    targets=(
        Target(
            TRADER_REPORT_ISSUE,
            note=(
                "자동 매매의 밤 리포트(#402): 그날의 매매와 장부, 출처별 손익(#560), 칸 고르기(매매할 칸이 "
                "없던 까닭), 정책별 모의 성적과 도전 정책 판정, 그 밤의 교체·내림·들어감·맞춘 정책, "
                "실전 관문, 한도와 정지. "
                "그 아래는 밤 정책 맞추기(#399, #400)와 mock 연습 칸 고르기(#474)의 출력이고, 맨 아래 접힌 "
                "블록은 그날 연습 매매의 매매별 줄이다. "
                "`에이전트 차례:` 줄이 까닭을 말하는 밤"
                "(그 주 마지막 거래일, 한도 정지, 챔피언 교체, 관문 통과, 맞추기 실패)과 "
                "작업이 실패한 밤에만 "
                "`ready-for-agent`를 붙인다. 에이전트가 할 일은 `docs/agents/trader-report.md`에 있다. "
                "리포트만 다시 보기(읽기만, 정책을 맞추지 않는다): "
                "`docker compose run --rm engine trader-report --day <날>`. 출력에는 시세 값, 계좌번호, "
                "주문번호, 키가 없다(칸, 확률, 틱, 수량, 건수, 순손익과 한도(원), 시각만)."
            ),
        ),
    ),
    # #560: the mock practice's trades a line each, folded under the report, which says their sum.
    attach=(Attachment(PRACTICE_HEADING, PRACTICE_TRADES),),
)

# The report of 2026-11-11 again (#691): that night's ``trader-report`` failed in its pick section (a
# verdict horizon read as the practice cell, no lower bound nor p*) after the re-fit had run, so the
# day's report was never posted. On the first night after the deploy that brings the fix, trading or
# not, once (again on other code if it fails): ``trader-report --day 2026-11-11`` prints it read-only
# from the store, re-fitting nothing. Right after the night's own report. Its output is the report's:
# no price, account, order number nor key.
REPORT_AGAIN = Job(
    name="trader-report-1111-691",
    due=again_on_new_code,
    command=("trader-report", "--day", "2026-11-11"),
    timeout=timedelta(minutes=5),
    label_if=trader_report_calls,
    unlabel=(),  # as the night's report: a question left for the user stays
    targets=(
        Target(
            TRADER_REPORT_ISSUE,
            note=(
                "2026-11-11의 밤 리포트를 다시 보인다(#691): 그 밤 `trader-report`는 정책 맞추기 뒤 "
                "칸 고르기 절에서 실패해(판정의 첫 지평을 연습 칸으로 읽고 없는 하한 − p*를 뺐다) "
                "리포트를 올리지 못했다. 고친 엔진이 배포된 뒤 첫 밤에 한 번, 저장소에서 읽기만 한다"
                "(정책을 맞추지 않는다). 다시 돌리기: "
                "`docker compose run --rm engine trader-report --day 2026-11-11`. "
                "출력에는 시세 값, 계좌번호, 주문번호, 키가 없다."
            ),
        ),
    ),
)

# The mock server's fill notice (#484, #403): ``fill-notice-layout-403`` joined the mock trader's
# recorded fill notices (``trader/fill_notices.py``) to the orders they tell of and measured each
# column (``trader/notice_layout.py``). Its run of 2026-11-04 joined a buy, a sell and a cancel (exit 0)
# and #531 named the 23 columns (``kis_orders.MOCK_FILL_NOTICE_COLUMNS``), so it left ``JOBS`` (#580,
# #505); ``krx-quant fill-notice-layout`` stays for a run by hand. The recorder goes on recording in
# mock mode until the fill quantity (column 10) is told from the order's (17) (the user's decision,
# 2026-11-05): the job below tells them.
#
# The order probe's session job ``probe-kis-orders-403`` (#403) left ``JOBS`` here (#484): its run of
# 2026-10-30 did every step but the fill notice, which the job above measured instead, and left in, it
# would order again on the same mock account once the trader is back in paper. ``krx-quant
# probe-kis-orders`` stays for a run by hand. What its condition asked stays for a session job that
# orders on the mock account (``read_mock_session``): such a job waited for the trader's keys, and did
# not run while the trader last started in mock mode (it may hold 005930 and order on that account).


@dataclass(frozen=True)
class MockSessionFacts:
    """What a session job ordering on the mock account asks (#403, #405): a small read, beside the live
    engine in the market's hours."""

    trader_kis: bool = False  # the trader's KIS key, secret and account are set (``Settings.trader_kis``)
    trader_mock: bool = False  # the trader last started in mock mode (``mock_check.trader_in_mock``)


async def read_mock_session(reads: Reads) -> MockSessionFacts:
    settings = reads.sources.settings
    return MockSessionFacts(settings.trader_kis, await asyncio.to_thread(trader_in_mock, settings.state_dir))


# The fill notice's columns 10 and 17 (#648): every mock order was of one share on 2026-11-04, so the
# two held the same value; they differ only on an order of more than one share filled in part or in
# several fills, which the trader's mock places only once a champion with cells trades by its loss
# budget (the practice buys one share, ``selection.PRACTICE_SHARES``). So on the first trading night
# with ``notice_columns.APART`` (20) orders of more than one share that something filled of on the
# recorded days (``read_fill_notice_apart``, the ledger's count), ``fill-notice-columns`` measures
# each column against them (read-only: the records, the ledger, each day's KIS inquiry with the
# trader's key) and counts the orders whose notice was lost while the recorder was disconnected. It
# runs once: a run with fewer than 20 of those orders joined to a notice record (3: the condition
# counts the ledger's), one that could not read (4), failed or was cut runs again the next trading
# night, and 3 posts without a label. The
# user then decides whether the recorder stays. Its output holds counts, column numbers, dates and
# times: no value, order number, account, HTS ID or price.
FILL_NOTICE_COLUMNS_ISSUE = 648


async def read_fill_notice_apart(reads: Reads) -> int:
    """How many of the mock trader's orders of more than one share that something filled of are on the
    days the fill notice recorder kept (``notice_columns.apart_orders``)."""
    return await asyncio.to_thread(apart_orders, reads.sources.settings.state_dir)


def _once_enough_orders_tell_the_columns_apart(night: Night[int]) -> bool:
    """On a trading night with ``APART`` telling orders recorded, until a run succeeded (exit 0); a
    failed run, the next trading night."""
    if not night.trading_day or night.facts < APART:
        return False
    record = night.record
    if record is None or record.outcome not in (OK, FAILED):
        return True
    return record.outcome != OK and record.day < night.day


def fill_notice_columns_calls(ran: Ran) -> bool:
    """Whether a run calls an agent: but too few orders joined to a notice (3), each one does."""
    return ran.exit_code != PARTIAL or ran.timed_out


FILL_NOTICE_COLUMNS = Job(
    name="fill-notice-columns-648",
    due=_once_enough_orders_tell_the_columns_apart,
    facts=read_fill_notice_apart,
    command=("fill-notice-columns",),
    timeout=timedelta(minutes=10),
    label_if=fill_notice_columns_calls,
    targets=(
        Target(
            FILL_NOTICE_COLUMNS_ISSUE,
            note=(
                "체결 통보 칸 10과 17 가르기(#648): mock trader의 체결 통보 기록기(#484)가 남긴 레코드를 "
                "수량이 1주보다 많고 체결이 있는 주문(조건: 그런 주문 20건이 기록된 첫 거래일 밤)에 "
                "묶어, 칸마다 "
                "그 주문의 주문 수량과 같은 레코드 수와 체결 레코드의 합이 체결 수량과 같은 주문 수를 "
                "센다. 판정은 예제대로(칸 10 체결 수량, 칸 17 주문 수량), 반대, 어느 쪽도 아님, 가려지지 "
                "않음이다. 끊긴 동안 통보가 빠진 주문 수도 센다. 에이전트는 이 수로 사용자에게 기록기를 "
                "지울지, 매시간 끊김 WARNING을 INFO로 낮출지 묻는다. 종료 코드 3은 통보 레코드가 묶인 그런 "
                "주문이 20건에 못 미쳤다는 뜻이고, 라벨 없이 올리고 다음 거래일 밤에 다시 돈다. "
                "다시 돌리기(읽기만): "
                "`docker compose run --rm engine fill-notice-columns`. 출력에는 값, 주문번호, 계좌번호, "
                "HTS ID, 가격이 없다(개수, 칸 번호, 날짜, 시간만)."
            ),
        ),
    ),
)


# The live account's answers (#408, CODING_STANDARDS "External data is probed before it is used"): the
# trader's broker reads the live server's answers by the fields measured on the mock server (#403), and
# no agent orders live. So on a trading night once the live key is set (``read_live_kis``),
# ``probe-live-account`` reads the live account by inquiry calls alone (the price, the balance, the
# orderable cash, the day's order inquiry), checks every field the broker reads against how it reads
# it, and keeps the result in the trader's store; the live mode orders only once one passed
# (``live.readiness``'s ``live_probe``). It places no order. Its output holds codes, pages and field
# names, never a key, an account, an order number or a value. Seconds; five minutes is a bound. Each
# trading night until a run passes. The report issue (#417) gets it: #408 is closed by its PR.
LIVE_PROBE_ISSUE = 417


async def read_live_kis(reads: Reads) -> bool:
    """Whether the trader's live key, secret and account are set (``Settings.trader_live_kis``, #408)."""
    return reads.sources.settings.trader_live_kis


def _a_night_until_the_live_account_was_read(night: Night[bool]) -> bool:
    """On a trading night once the live key is set, until a run passed (exit 0)."""
    if not night.trading_day or not night.facts:
        return False
    record = night.record
    if record is None or record.outcome not in (OK, FAILED):
        return True
    return record.outcome != OK and record.day < night.day


LIVE_PROBE = Job(
    name="probe-live-account-408",
    due=_a_night_until_the_live_account_was_read,
    facts=read_live_kis,
    command=("probe-live-account",),
    timeout=timedelta(minutes=5),
    targets=(
        Target(
            LIVE_PROBE_ISSUE,
            note=(
                "실전 계좌 읽기 프로브(#408): 실전 키로 조회만 해서(현재가, 잔고, 매수가능조회, "
                "그날 주문 체결 조회) trader가 읽는 필드가 실전의 답에 있고 그 꼴인지 본다. 주문은 "
                "하나도 보내지 않는다. `같다`면 live 모드의 실행 점검 `live_probe`가 통과한다. "
                "`다르다`면 에이전트가 `kis_orders.py`의 읽기를 고치는 PR을 열고 `docs/kis-feeds.md` "
                '"실전 서버 (#408)"를 고친다. 체결가·체결 수량은 첫 실전 날에 trader가 본다. '
                "다시 돌리기(조회만): `docker compose run --rm engine probe-live-account`. "
                "출력에는 키, 계좌번호, 주문번호, 시세 값이 없다(코드, 쪽, 필드 이름과 꼴만)."
            ),
        ),
    ),
)
