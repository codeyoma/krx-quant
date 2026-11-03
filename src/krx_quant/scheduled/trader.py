"""The auto trader's jobs (#393, #500): its nightly report and re-fit, the mock and live accounts'
checks against KIS, and the measurements of its passive entry and of the cost map. ``JOBS``
(``krx_quant.scheduled``) lists them in the order they run.
"""

from __future__ import annotations

from datetime import timedelta

from krx_quant.process_log import Ran
from krx_quant.scheduled.core import FAILED, OK, Job, Night, Target
from krx_quant.trader.mock_check import DIFFERENT as MOCK_DIFFERENT
from krx_quant.trader.mock_check import SAME as MOCK_SAME
from krx_quant.trader.notice_layout import DONE as LAYOUT_DONE
from krx_quant.trader.notice_layout import PARTIAL as LAYOUT_PARTIAL
from krx_quant.trader.report import calls_agent

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


def _every_trading_night(night: Night) -> bool:
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
                "자동 매매의 밤 리포트(#402): 그날의 매매와 장부, 칸 고르기(매매할 칸이 없던 까닭), 정책별 "
                "모의 성적과 도전 정책 판정, 그 밤의 교체·내림·들어감·맞춘 정책, 실전 관문, 한도와 정지. "
                "그 아래는 밤 정책 맞추기(#399, #400)와 mock 연습 칸 고르기(#474)의 출력이다. "
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
)

# The mock mode's trades against KIS (#405, its third acceptance criterion): on the night of the
# first trading day the trader ran in mock mode (``Facts.mock_days``: a mock ledger that day),
# ``check-mock-trades`` compares the champion's orders of that day in the ledger with KIS's day order
# inquiry, read with the trader's own mock key, order by order (matched by KIS's order number): the
# side, the quantity, what filled and the fill price, as a difference in ticks. KIS's orders placed
# while the order probe ran that day are the probe's (#403, same account). It prints counts,
# quantities, sides, times and ticks, never an order number, an account, a key or a price. Again on
# each later mock trading night until a day had orders to compare (``mock_check.NO_ORDERS``) and
# both could be read (``UNREADABLE``); done once one was the same (``SAME``) or not (``DIFFERENT``).
# Seconds; five minutes is a bound. #405 stays open until its comment.
MOCK_CHECK_ISSUE = 405


def _a_mock_night_until_compared(night: Night) -> bool:
    """On the night of a trading day the trader ran in mock mode, until one compared its orders."""
    if not night.trading_day or night.day not in night.facts.mock_days:
        return False
    record = night.record
    if record is None or record.outcome not in (OK, FAILED):
        return True  # never ran to its end (a run a restart cut runs the next mock night)
    if record.exit_code in (MOCK_SAME, MOCK_DIFFERENT):
        return False
    return record.day < night.day


MOCK_CHECK = Job(
    name="check-mock-trades-405",
    due=_a_mock_night_until_compared,
    command=("check-mock-trades",),
    timeout=timedelta(minutes=5),
    targets=(
        Target(
            MOCK_CHECK_ISSUE,
            note=(
                "KIS 모의투자(mock) 첫 거래일의 매매를 장부(state/trader.sqlite3)와 "
                "KIS 주문 체결 조회로 견준다(#405의 세 번째 완료 조건). 주문마다 매수·매도, "
                "주문 수량, 체결 수량, 체결가 차이(틱)를 보이고, 주문 프로브(#403)가 돈 동안의 "
                "KIS 주문은 프로브의 것이라 세지 않는다. 결과가 `같다`면 에이전트가 #405를 닫는다. "
                "`다르다`면 다른 주문을 보고 고치는 PR을 연다. 다시 보기(읽기만): "
                "`docker compose run --rm engine check-mock-trades --day <날>`. "
                "출력에는 주문번호, 계좌번호, 키, 가격이 없다(건수, 수량, 시각, 틱만)."
            ),
        ),
    ),
)

# The mock server's fill notice (#484, #403): the order probe (``probe-kis-orders``) measured every
# step on KIS's mock server on 2026-10-30 but the fill notice, whose records had 23 fields where KIS's
# example lists 26 (``kis_orders.FILL_NOTICE_COLUMNS``), and it does not order while the trader is in
# mock mode (#405). So the trader in mock mode records its own orders' fill notices
# (``trader/fill_notices.py``, ``state/fill_notices.sqlite3``), and on a trading night with records not
# judged yet (``Facts.fill_notice_days``: one of a day after this job's last run), ``fill-notice-layout``
# joins each record to the one order of its day it names, in the ledger and on KIS's day order inquiry
# (the trader's own key, read-only), and prints each column's shape and how many records it equals of
# its order (``trader/notice_layout.py``): shapes, counts, matches and dates, never a value, an order
# number, an account or a price. The records add up across nights (30 days are kept). It stops once a
# run had a buy, a sell and a cancel each joined (``notice_layout.DONE``); before, it runs again on a
# trading night with new records (``PARTIAL``), and after a run that could not read the records, the
# ledger or a day's inquiry (``UNREADABLE``) or failed, the next trading night. A run that calls an
# agent (``label_if``): the last, or one that did not measure; a ``PARTIAL`` one is posted without the
# label. Seconds a day of records (one inquiry a day at the mock broker's pace); ten minutes is a bound.
#
# The order probe's session job ``probe-kis-orders-403`` (#403) left ``JOBS`` here (#484): its run of
# 2026-10-30 did every step but the fill notice, which this job measures instead, and left in, it would
# order again on the same mock account once the trader is back in paper. ``krx-quant probe-kis-orders``
# stays for a run by hand.
FILL_NOTICE_LAYOUT_ISSUE = 403


def _a_trading_night_with_new_fill_notices(night: Night) -> bool:
    """On a trading night with fill notice records, until a run joined a buy, a sell and a cancel:
    again after a run that measured part (only once a record of a later day is kept), and the next
    trading night after one that could not read or failed."""
    days = night.facts.fill_notice_days
    if not night.trading_day or not days:
        return False
    record = night.record
    if record is None or record.outcome not in (OK, FAILED):
        return True  # never ran to its end (a run a restart cut runs the next trading night)
    if record.exit_code == LAYOUT_DONE or record.day >= night.day:
        return False
    return record.exit_code != LAYOUT_PARTIAL or max(days) > record.day


def fill_notice_layout_calls(ran: Ran) -> bool:
    """Whether a run of ``fill-notice-layout`` calls an agent: the last (every kind joined), or one
    that did not measure; not one that measured part."""
    return ran.exit_code != LAYOUT_PARTIAL or ran.timed_out


FILL_NOTICE_LAYOUT = Job(
    name="fill-notice-layout-403",
    due=_a_trading_night_with_new_fill_notices,
    command=("fill-notice-layout",),
    timeout=timedelta(minutes=10),
    label_if=fill_notice_layout_calls,
    targets=(
        Target(
            FILL_NOTICE_LAYOUT_ISSUE,
            note=(
                "모의 체결 통보의 칸 재기(#484): mock의 trader가 받은 체결 통보"
                "(state/fill_notices.sqlite3)의 레코드를 그날의 주문 하나(장부와 KIS 주문 체결 조회)에 "
                "묶어 칸마다 모양, 길이, 값의 가짓수와 그 주문의 값(주문번호, 원주문번호, 매수·매도 코드, "
                "주문 수량, 주문가, 종목, 시각, 계좌, HTS ID)과 같은 수, 주문마다 체결 수량의 합과 체결가의 "
                "가중 평균이 맞는 수를 보인다. 레코드는 밤을 넘어 쌓아 본다. 종료 코드 0: 매수, 매도, 취소가 "
                "모두 묶였다. 다시 돌지 않고 `ready-for-agent`를 붙인다. 에이전트가 이것으로 "
                '`docs/kis-feeds.md` "주문 (#403)"의 "체결 통보" ⚠에 23칸의 이름을 붙이는 PR을 연다'
                "(확실하지 않은 칸은 이름 없이 둔다). 3: 아직 다 묶이지 않았다. 라벨 없이 올리고 새 레코드가 "
                "있는 거래일 밤에 다시 돈다. 4: 기록, 장부나 KIS 조회를 읽지 못했다. 다음 거래일 밤에 다시 "
                "돈다. 다시 보기(읽기만): `docker compose run --rm engine fill-notice-layout`. 출력에는 값, "
                "주문번호, 계좌번호, HTS ID, 가격이 없다(모양, 개수, 일치 수, 날짜만)."
            ),
        ),
    ),
)

# The live account's answers (#408, CODING_STANDARDS "External data is probed before it is used"): the
# trader's broker reads the live server's answers by the fields measured on the mock server (#403), and
# no agent orders live. So on a trading night once the live key is set (``Facts.trader_live_kis``),
# ``probe-live-account`` reads the live account by inquiry calls alone (the price, the balance, the
# orderable cash, the day's order inquiry), checks every field the broker reads against how it reads
# it, and keeps the result in the trader's store; the live mode orders only once one passed
# (``live.readiness``'s ``live_probe``). It places no order. Its output holds codes, pages and field
# names, never a key, an account, an order number or a value. Seconds; five minutes is a bound. Each
# trading night until a run passes. The report issue (#417) gets it: #408 is closed by its PR.
LIVE_PROBE_ISSUE = 417


def _a_night_until_the_live_account_was_read(night: Night) -> bool:
    """On a trading night once the live key is set, until a run passed (exit 0)."""
    if not night.trading_day or not night.facts.trader_live_kis:
        return False
    record = night.record
    if record is None or record.outcome not in (OK, FAILED):
        return True
    return record.outcome != OK and record.day < night.day


LIVE_PROBE = Job(
    name="probe-live-account-408",
    due=_a_night_until_the_live_account_was_read,
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

# The passive entry (#432): the trader buys at the best ask, so its cells are judged at a market
# order's round trip, and none passes. A buy resting at the best bid is a tick cheaper but may not
# fill, and fills best when the price goes against it. Before that is decided, it is measured on the
# recordings: on the first trading day's night after the deploy that brings it (its days are the
# cost map's 20, recorded already), and again each trading night after a run that failed (the cost
# map or the engine's database not read, exit 1) until one measures, ``measure-passive-entry`` reads
# tonight's cost map from the collector's Engine API (``KRX_TRADER_ENGINE_URL``, as ``trader-report``
# does), every cell's signals from the engine's database (read-only), and each day's recorded quotes
# and KRX trades of 005930 a day at a time; it prints the fill rates under two fill assumptions, the
# hit rates of the filled and the unfilled beside the market entry's, the net in ticks per filled
# trade and per signal, and the counts, by cell and kind. It changes nothing: the trader's ordering
# stays. No price, only ticks, ratios, seconds and counts, so it can go on the public issue. Seconds
# (the cost map waited for two minutes at most; a server-sized day, 570,000 quotes and 300,000 KRX
# trades, read in 0.05 s on a Mac); the quarter hour is a bound, kept short as it runs before the
# simulations and pre-validations, which must start by 00:30.
PASSIVE_ENTRY_ISSUE = 432


def _a_trading_night_until_measured(night: Night) -> bool:
    """On a trading night, until a run measured (exit 0): the next trading night again after one that
    failed or was cut."""
    if not night.trading_day:
        return False
    record = night.record
    if record is None or record.outcome not in (OK, FAILED):
        return True
    return record.outcome != OK and record.day < night.day


PASSIVE_ENTRY = Job(
    name="passive-entry-432",
    due=_a_trading_night_until_measured,
    command=("measure-passive-entry",),
    timeout=timedelta(minutes=15),
    targets=(
        Target(
            PASSIVE_ENTRY_ISSUE,
            note=(
                "대기 매수(매수 1호가) 측정(#432): 비용 지도의 칸마다, 종류마다 trader가 살 신호를 녹화된 "
                "호가와 KRX 체결로 시장가 진입(매도 1호가)과 대기 진입(매수 1호가, 시간 장벽에 취소)으로 "
                "흉내 낸 결과다. 체결 가정은 둘이다: 보수적(그 가격 아래 체결), 낙관적(그 가격 이하 체결). "
                "에이전트가 체결률, 체결된 것과 미체결의 맞힘률(역선택), 신호당·체결당 순손익(틱)으로 "
                "대기 진입을 넣을지 추천하고, 사용자가 정한다. 넣는 것은 별도 티켓이다. 다시 돌리기(읽기만): "
                "`docker compose run --rm engine measure-passive-entry`. 출력에는 시세 값과 가격이 없다"
                "(틱, 비율, 초, 건수만)."
            ),
        ),
    ),
)

# The cost map's audit (#438): the cost map shows each kind's first-ranked algorithm only, so another
# ranked one might pass the cost the top fails, unseen. Once, on the first night after the deploy that
# brings it (trading or not: the cost map's 20 days are stored already), and again the next night
# after a run that failed (the cost map or the engine's database not read, exit 1; the visible
# candidate unlike the cost map's top, exit 2), ``cost-map-audit`` reads tonight's cost map from the
# collector's Engine API (``KRX_TRADER_ENGINE_URL``, as ``measure-passive-entry`` does) and the engine's
# database read-only, and judges every ranked algorithm of the trader's kinds by the cost map's own
# code. It changes nothing: the cost map and the trader stay. No price, only hit rates, p*, ticks,
# counts and dates, so it can go on the public issue. The cost map waits two minutes at most and the
# rest reads 005930's 20 days a kind at a time, as the cost map does; the half hour is a bound. Before
# the simulations and pre-validations, which must start by 00:30.
COST_MAP_AUDIT_ISSUE = 438


def _a_night_until_audited(night: Night) -> bool:
    """On any night, until a run audited (exit 0): the next night again after one that failed or was
    cut."""
    record = night.record
    if record is None or record.outcome not in (OK, FAILED):
        return True
    return record.outcome != OK and record.day < night.day


COST_MAP_AUDIT = Job(
    name="cost-map-audit-438",
    due=_a_night_until_audited,
    command=("cost-map-audit",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            COST_MAP_AUDIT_ISSUE,
            note=(
                "비용 지도 감사(#438): trader의 종류(방향, 장벽 3종)마다 순위표에 오른 알고리즘을 모두 비용 "
                "지도의 계산으로 판정했다. 보임은 비용 지도의 1위, 숨음은 그 밖이다. 탐색용이다: 숨은 후보가 "
                "통과해도 매매 허가가 아니다. 숨은 후보가 시장가 왕복으로 통과하면, 에이전트가 비용 지도가 "
                "1위 하나 대신 무엇을 보일지 안을 이 이슈에 남기고 `ready-for-human`으로 돌린다. 사용자가 "
                "정한다. 바꾸면 paper로 먼저 본다. 통과한 숨은 후보가 없으면 그 숫자를 정리해 남기고 "
                "이슈를 닫는다. 대조가 다르면(종료 코드 2) 그 종류의 숫자를 믿지 않고 원인을 찾는다. "
                "다시 돌리기(읽기만): "
                "`docker compose run --rm engine cost-map-audit`. 출력에는 가격이 없다(맞힘률, p*, 틱, "
                "개수, 날짜만)."
            ),
        ),
    ),
)
