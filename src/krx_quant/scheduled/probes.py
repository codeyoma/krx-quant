"""The probe jobs (#500): each measures on the server what an outside source's data means (KIS,
Toss, the KRX Open API, the recordings) before the engine uses it (CODING_STANDARDS.md "External
data is probed before it is used"). ``JOBS`` (``krx_quant.scheduled``) lists them in the order they
run.
"""

from __future__ import annotations

from datetime import timedelta

from krx_quant.scheduled.core import FAILED, Job, Night, Target, _first_night, _first_trading_night, sections

# ③ The daily supply sources (#226): where KIS, and the Toss Open API when its client is set
# (#235), give the chart's daily supply indicators, asked once, on the first night after the
# deploy that brings the job (trading or not: the probe asks for the latest trading day and
# earlier ones). It only reads, spaced under each API's limit, with the cached access tokens
# (the collector's KIS one; Toss's one-per-client token, shared), and prints no market value,
# so its report can go on the public issue. KIS takes at most about 240 calls, Toss about 170,
# a few minutes; the KIS part stops at 15 minutes and the Toss part at 8 (each call at 15
# seconds), inside the half hour. Before the pre-validations, which may take the night.
SUPPLY_PROBE_ISSUE = 226


SUPPLY_PROBE = Job(
    name="probe-supply-226",
    due=_first_night,
    command=("probe-supply",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            SUPPLY_PROBE_ISSUE,
            note=(
                '에이전트가 이 출력으로 `docs/kis-feeds.md` "일별 수급 (#226)"과 `docs/toss-feeds.md`의 ⚠를 '
                "지우거나 고친다. 출력에는 시세 값이 없다(필드 이름, 행 수, 날짜, 표시만)."
            ),
        ),
    ),
)


# The daily kinds (#264): after the engine with open_to_close is deployed, on the first trading
# day's night, once: its 08:50 predictions and the answers written once the day's KRX day candle
# was stored (after 20:30), and that candle's open and close against the recorded KRX trades'
# opening and closing auction prints. It reads the engine DB read-only, the candles and the
# day's recording; it prints no price. Minutes at most; the half hour is a bound. Before the
# pre-validations, which may take the night.
DAILY_CHECK_ISSUE = 264

DAILY_CHECK = Job(
    name="daily-check-264",
    due=_first_trading_night,
    command=("daily-check", "--days", "1"),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            DAILY_CHECK_ISSUE,
            note=(
                "에이전트가 이 출력으로 첫 거래일의 08:50 예측과 채점이 돌았는지, KRX 일봉의 시가·종가가 "
                '녹화한 KRX 체결의 시가·종가 단일가와 같은지 확인하고 `docs/engine-api.md`의 "시가→종가"에 '
                "남긴다. 출력에는 가격이 없다(같음·다름과 개수만)."
            ),
        ),
    ),
)

# The structure check's turn of the month again (#270, #264): its first run counted KIS's 0 net
# buys (every day before NXT opened) as figures. They are none now (``supply.no_figure``), and
# section 4 heads each symbol with the days that have a foreign figure, the first and the last,
# which shows on the server's files where KIS's figures begin: the days Cox's flows have (#264).
# Once, on the first night after the deploy that brings it (trading or not); its header and
# section 4 go to #264. As the first run's, it reads files only; the half hour is a bound.
# Before the pre-validations.
STRUCTURE_SUPPLY = Job(
    name="structure-check-supply-264",
    due=_first_night,
    command=("structure-check",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            DAILY_CHECK_ISSUE,
            sections("구조 실측", "4. 월말·월초"),
            "출력 중 머리말과 4절(월말·월초 × 전날 외국인 순매수)만 싣는다. 에이전트가 이것으로 KIS 외국인 "
            "순매수가 있는 첫날(NXT 개장 2025-03-04로 본다, #270)을 확인하고 `docs/kis-feeds.md`와 "
            "Cox의 수급 입력 설명에 남긴다. 출력에는 수급 값이 없다(개수, 날짜, 비율만).",
        ),
    ),
)

# KRX's net buys before NXT opened (#264, #297): the supply keeper asks the days before NXT of
# KIS's KRX investor query (``J``) once, at the collector's first start after the deploy that
# brings it (06:00); whether ``J`` has figures that far back only the server shows. Once, on the
# first night after that deploy (trading or not): ``probe-net-buys`` asks ``J`` and ``UN`` for
# 30 days to each of six days per symbol (36 calls for three, spaced as the supply probe's, a
# minute or so) and counts what the keeper stored. It prints counts, days and field looks, no
# market value, so it can go on the public issue. The half hour is a bound. Before the
# pre-validations: its count of the stored days before NXT says whether Cox's re-run (the flows'
# version 2, ``daily_features``) runs that night or waits for them (``Facts.pending_inputs``).
NET_BUYS_PROBE = Job(
    name="probe-net-buys-j-264",
    due=_first_night,
    command=("probe-net-buys",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            DAILY_CHECK_ISSUE,
            note=(
                "에이전트가 이 출력으로 KIS `J`가 NXT 개장(2025-03-04) 전의 순매수를 주는지, 수집이 "
                '그 날들을 저장했는지 확인하고 `docs/kis-feeds.md` "투자자별 순매수"의 ⚠와 Cox의 수급 입력 '
                "설명에 남긴다. 저장됐으면 같은 밤 `prevalidate-cox`가 그것으로 돌고, 아니면 기다린다. "
                "출력에는 시세 값이 없다(행 수, 날짜, 개수, 필드 모양만)."
            ),
        ),
    ),
)

# What the KRX day candle's close is (#264): the 2026-10-19 daily check found it unlike the
# recorded closing auction print (its open alike), and KRX's after-market opened on 2026-09-14.
# Once, on the first night after the deploy that brings it (trading or not: KIS gives the past
# days): ``probe-close`` asks KIS for each symbol's KRX one-minute bars of the last year's days
# (two or three pages a day, about 1,600 calls for three symbols, spaced a quarter second: seven
# minutes or so) and counts, before and from that day, whether the candle's close is the closing
# auction bar's or the day's last bar's, and the recorded print's. It prints counts and bar times,
# no price, so it can go on the public issue. The half hour is a bound. Before the
# pre-validations.
CLOSE_PROBE = Job(
    name="probe-close-264",
    due=_first_night,
    command=("probe-close",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            DAILY_CHECK_ISSUE,
            note=(
                "에이전트가 이 출력으로 KRX 일봉의 종가가 무엇인지(애프터마켓의 마지막 가격인지, "
                "15:30 종가 단일가인지) 애프터마켓(2026-09-14) 전과 뒤로 나눠 확인하고, "
                '`docs/engine-api.md` "시가→종가"와 `docs/kis-feeds.md`에 남긴다. 출력에는 가격이 없다'
                "(같음·다름과 개수, 봉 시각만)."
            ),
        ),
    ),
)

# The day candles asked again (#264): ``probe-close-264`` (2026-10-20) found the KRX day candle's
# close the closing auction's on every stored day but one per symbol, the one stored the same
# evening, while KIS's candle was still provisional. The collector now asks KIS again for each
# file's newest stored candles every run and replaced the stored ones KIS has otherwise once, at
# the first run after the deploy (``candles``). Once, on the first trading day's night after that
# deploy, so that evening's candle is stored provisional: ``probe-close`` again, which now also
# lists the days whose close is not the auction's, the newest stored days against KIS's candle as
# it is now, and the candles the collector replaced (the run and the fields). It prints counts,
# dates, bar times and field names, no price. The first run took 17 minutes; the 45 are a bound.
# Before the pre-validations.
CLOSE_REFRESH_PROBE = Job(
    name="probe-close-refresh-264",
    due=_first_trading_night,
    command=("probe-close",),
    timeout=timedelta(minutes=45),
    targets=(
        Target(
            DAILY_CHECK_ISSUE,
            note=(
                "에이전트가 이 출력으로 일봉 다시 받기(#264)가 됐는지 확인한다: 애프터마켓 뒤에 "
                "종가가 단일가 봉과 다른 날이 그날 저녁 저장한 날뿐인지, 그날 밤 KIS의 일봉이 아직 잠정인지, "
                "수집기가 바꾼 봉이 어느 실행(아침 시작 또는 20:30)에서 어느 필드였는지. "
                "그 결과를 `docs/kis-feeds.md` "
                '"기간별 시세"와 `docs/engine-api.md` "시가→종가"에 남긴다. 출력에는 가격이 없다'
                "(같음·다름과 개수, 날짜, 봉 시각, 필드 이름만)."
            ),
        ),
    ),
)

# The single-stock ETFs' net assets (#305): the closing flow's demand of a product is
# A·(L² − L)·r, with A its net assets on the previous trading day, and no KIS query for them was
# measured. Once, on the first trading day's night after the deploy that brings it, so the night
# shows whether that day's values are in by then: ``probe-nav`` asks KIS's ETF/ETN current price,
# its daily, product and minute NAV trends for each of the 16 products of ``products.PRODUCTS``
# (about 100 calls spaced half a second, a minute or so) and compares the net asset total with
# the listed units × NAV. It prints counts, dates, times and field looks, no market value, so it
# can go on the public issue. The half hour is a bound. Before the pre-validations.
NAV_PROBE_ISSUE = 305

NAV_PROBE = Job(
    name="probe-nav-305",
    due=_first_trading_night,
    command=("probe-nav",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            NAV_PROBE_ISSUE,
            note=(
                "에이전트가 이 출력으로 상품마다 어느 조회가 순자산을 주는지, 지난날을 상장일"
                "(2026-05-27)까지 주는지, 그날 값이 언제 나오는지 정리해 `docs/kis-feeds.md` "
                '"ETF 순자산 (#305)"의 ⚠를 고치고, 순자산 출처를 정한다(KIS가 아닌 출처는 사용자가 정한다). '
                "그 전에는 순자산을 쓰지 않는다(2단계는 이 결과를 기다린다). 출력에는 시세 값이 없다"
                "(개수, 날짜, 시각, 필드 모양, 같음·다름만)."
            ),
        ),
    ),
)

# Cash dividends and the adjusted prices (#310): open_to_close_5d reads its window's two ends from
# KIS's adjusted KRX day candles; whether that adjustment takes cash dividends out, and whether
# KIS's rows flag the ex-dividend days, only KIS's answers show. Once, on the first night after the
# deploy that brings it (trading or not: KIS gives the past days): ``probe-dividends`` asks each
# symbol's ten years of KRX day candles adjusted and as traded (about 150 calls spaced half a
# second, a minute or two) and counts where their ratio steps, by size and month, with the steps'
# dates and the rows' ex-date flags. It prints counts, dates and flag values, no price, so it can go
# on the public issue. The half hour is a bound. Before the pre-validations.
DIVIDEND_PROBE_ISSUE = 310

DIVIDEND_PROBE = Job(
    name="probe-dividends-310",
    due=_first_night,
    command=("probe-dividends",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            DIVIDEND_PROBE_ISSUE,
            note=(
                "에이전트가 이 출력으로 KIS 수정주가가 현금배당을 빼는지(배당락일마다 작은 계단이 있는지), "
                "KIS 일봉의 `flng_cls_code`가 배당락일을 알려 주는지 보고 `docs/kis-feeds.md`와 "
                '`docs/engine-api.md` "5거래일 시가→종가"의 배당에 남긴다. 빼지 않고 배당락 표시가 있으면 '
                "수집이 그 값을 KRX 일봉에 두고 정답 경로의 `ex_dividend`를 채우는 PR을 연다. 출력에는 "
                "가격이 없다(개수, 날짜, 필드 값, 계단 크기의 구간만)."
            ),
        ),
    ),
)

# The KRX Open API's ETF daily trading service (#305): KIS gives the products' net assets and
# listed units for today only (``probe-nav-305``), so the user chose KRX's ``etf_bydd_trd`` (ETF
# 일별매매정보) for the days before (2026-10-22). Its key is the user's to apply for; once it is in
# the server's ``.env`` (``KRX_OPENAPI_KEY``, ``Facts.krx_openapi``), once, on the first night
# (trading or not: KRX gives a day from 08:00 on the next business day): ``probe-krx-etf`` asks the
# day before the listing, the listing day, a day a week and the last five trading days (about 30
# calls spaced a second), and KIS's current price and daily NAV for each product (about 60 calls
# spaced half a second), a minute or two. It prints counts, dates, field names and looks, same or
# different, no market value (KRX's terms forbid giving the data to anyone else), so it can go on
# the public issue. It stops asking at 10 minutes; the quarter hour is a bound, kept short as it
# runs before the simulations and pre-validations. A night without the key it waits, harmlessly.
KRX_ETF_PROBE_ISSUE = 305


def _first_night_with_the_krx_key(night: Night) -> bool:
    """Once, on the first night ``KRX_OPENAPI_KEY`` is set, trading or not; again each later
    night while its last run failed (a key whose service is not approved yet answers 401), so
    fixing the application needs no edit of the state file."""
    if not night.facts.krx_openapi:
        return False
    record = night.record
    return not night.ran or (record is not None and record.outcome == FAILED and record.day < night.day)


KRX_ETF_PROBE = Job(
    name="probe-krx-etf-305",
    due=_first_night_with_the_krx_key,
    command=("probe-krx-etf",),
    timeout=timedelta(minutes=15),
    targets=(
        Target(
            KRX_ETF_PROBE_ISSUE,
            note=(
                "에이전트가 이 출력으로 `docs/krx-openapi.md`의 ⚠(주소와 인증, 필드와 모양, 종목코드, "
                "상장일부터의 행, 값이 나오는 때, 상장좌수가 날마다인지, 순자산총액의 단위, KIS와 같은지)를 "
                "실측으로 고친다. 401·403이면 인증키나 서비스 이용 신청을 사용자에게 묻는다. 그다음 "
                "상장일부터의 순자산 채우기(백필)와 날마다의 수집을 만든다. 출력에는 시세 값이 없다"
                "(개수, 날짜, 필드 이름과 모양, 같음·다름만)."
            ),
        ),
    ),
)

# The WebSocket's gaps day by day (#344): on 2026-10-21 the silence watchdog (#311) closed the
# connection at 15:30:33, no frame having come since 15:29:33, around the closing auction's print.
# Whether that silence comes at every close (the watchdog would then cut the connection there every
# day, and its 60 seconds need another look: the user's decision) only the recording shows. Once, on
# the first night after the deploy that brings it (trading or not: it reads the days already
# recorded): ``probe-gaps`` summarizes every recorded day's gaps and the closing auction window's
# frames, trades and bars. It reads files only and prints no market value, so it can go on the
# public issue. A minute or two; the quarter hour is a bound, kept short as it runs before the
# simulations and pre-validations, which must start by 00:30 (the Breimans by 23:30). It is listed
# after the other probes, so on a night they are due too it never holds back the results #314,
# #264, #305 and #310 wait on.
GAPS_PROBE_ISSUE = 344

GAPS_PROBE = Job(
    name="probe-gaps-344",
    due=_first_night,
    command=("probe-gaps",),
    timeout=timedelta(minutes=15),
    targets=(
        Target(
            GAPS_PROBE_ISSUE,
            note=(
                "에이전트가 이 출력으로 15:30 무렵의 침묵이 매일 종가 단일가 때 오는지(날마다 종가 단일가 "
                "구간의 가장 긴 침묵과 겹친 끊김), 그날 종가 단일가 체결과 1분봉 채움이 녹화에 있는지, "
                "끊김이 언제 무슨 이유로 몇 피드에 왔는지 #344에 정리한다. 60초 기준"
                "(`KRX_WS_SILENCE_SECONDS`)과 종가 단일가 구간의 처리를 바꿀지는 사용자가 정한다. "
                "출력에는 시세 값이 없다(개수, 시각, 코드, 피드 이름, 키만)."
            ),
        ),
    ),
)

# The closing flow (#305): when KIS moves its ETF current price to the new day (the net assets'
# retries, ``net_assets``), and which one-minute bar, the integrated or the KRX one, gives the
# closing auction value the recorded trades do (the denominator takes recorded prints only until
# then, ``closing_flow``), only the server shows. Once, on the first trading day's night after the
# deploy that brings it, so the morning's retries, the 15:20 row and the evening's days are in:
# ``check-closing-flow`` summarizes the net asset rows by day with when they first came, the 15:20
# rows, the days kept, and on each day with a recorded print both bars' values against it (the KRX
# bar asked of KIS, one call a symbol and day, half a second apart). It prints no market value.
# Seconds; the quarter hour is a bound. After the gaps probe.
CLOSING_FLOW_ISSUE = 305

CLOSING_FLOW_CHECK = Job(
    name="check-closing-flow-305",
    due=_first_trading_night,
    command=("check-closing-flow",),
    timeout=timedelta(minutes=15),
    targets=(
        Target(
            CLOSING_FLOW_ISSUE,
            note=(
                "에이전트가 이 출력으로 KIS가 ETF 순자산을 언제 다음 날로 넘기는지(날마다 처음 받은 때)와 "
                "순자산을 쌓기 시작한 첫날, 통합(UN)·KRX(J) 1분봉의 단일가 봉이 녹화한 종가 단일가 "
                '체결대금과 같은지 보고 `docs/kis-feeds.md` "ETF 순자산 (#305)"의 ⚠를 고친다. 녹화와 늘 같은 '
                "봉이 있으면 그 봉으로 녹화가 없는 날의 분모를 채우는 PR을 연다(`docs/engine-api.md` "
                '"closing-flow"의 ⚠). 출력에는 시세 값이 없다(개수, 날짜, 시각, 봉과 녹화의 체결대금 비율만).'
            ),
        ),
    ),
)

# The KRX Open API's net assets (#305, ``krx_net_assets``): the collector fetches every trading day
# from the listing at its start and the trading day before each morning from 08:05. Their probe
# (``probe-krx-etf-305``) found ``INVSTASST_NETASST_TOTAMT`` equal to the same row's ``LIST_SHRS ×
# NAV`` on 169 of 400 rows; the engine reads the total as it is (the fund's own net assets), and
# which day's listed units it matches (creations and redemptions listed a day later, or a day
# earlier) only every day's rows show. Once, on the first trading day's night with the key, after
# the deploy that brings it, so the start's backfill and that morning's fetch are in:
# ``check-krx-net-assets`` reads the state file only (no call) and prints counts, dates, times and
# product codes, no value (KRX's terms forbid passing its data on). Seconds; the quarter hour is a
# bound. After the closing flow check.
KRX_NET_ASSETS_ISSUE = 305


def _first_trading_night_with_the_krx_key(night: Night) -> bool:
    """Once, on the first trading day's night ``KRX_OPENAPI_KEY`` is set."""
    return night.facts.krx_openapi and night.trading_day and not night.ran


KRX_NET_ASSETS_CHECK = Job(
    name="check-krx-net-assets-305",
    due=_first_trading_night_with_the_krx_key,
    command=("check-krx-net-assets",),
    timeout=timedelta(minutes=15),
    targets=(
        Target(
            KRX_NET_ASSETS_ISSUE,
            note=(
                "에이전트가 이 출력으로 상장일부터 거래일마다 KRX 행이 있는지(1절), 전 거래일 값이 "
                "아침 몇 시에 들어왔는지(2절), 순자산총액이 어느 날의 상장좌수 × NAV와 같은지(3절: "
                "t+1이면 설정·환매가 다음 거래일에 상장된다), KRX와 KIS의 순자산이 같은지(4절)를 보고 "
                "`docs/krx-openapi.md`의 "
                '"순자산총액과 상장좌수 × NAV"와 "언제 나오나"의 ⚠를 고친다. A를 순자산총액 그대로 쓰는 것을 '
                "바꿀 근거가 나오면 #305에 남긴다. 출력에는 값이 없다(개수, 날짜, 시각, 상품 코드만)."
            ),
        ),
    ),
)

# The member frames short of fields (#381): around 08:00 on 2026-10-23 and 10-26 a few integrated
# member frames (``H0UNMBC0``) came with 72, 67 and 61 fields instead of 78, and the collector kept
# them raw only with an ERROR (which alerts). Whether they follow a rule the normalization can take
# (a block of fields left out) or are cut short decides step 2, and only the raw frames show it.
# Once, on the first night after the deploy that brings it (trading or not: it reads the days
# already recorded): ``probe-member-frames`` reads every recorded day's member frames (about 14,000
# a day) and the other WebSocket feeds' frames in the minutes around the short ones. It reads files
# only and prints counts, times, symbols, field counts and field shapes, never a value, so it can go
# on the public issue. Seconds to a minute or two; the 20 minutes are a bound, kept short as it
# runs before the simulations and pre-validations, which must start by 00:30 (the Breimans by
# 23:30). Listed after the other probes, so it never holds back the results they wait on.
MEMBER_FRAMES_ISSUE = 381

MEMBER_FRAMES_PROBE = Job(
    name="probe-member-frames-381",
    due=_first_night,
    command=("probe-member-frames",),
    timeout=timedelta(minutes=20),
    targets=(
        Target(
            MEMBER_FRAMES_ISSUE,
            note=(
                "에이전트가 이 출력으로 필드가 78개가 아닌 회원사 프레임이 언제(날마다, 시마다) 왔는지, "
                "칸의 모양이 다음 78칸 프레임과 어디서 갈라지는지(정해진 칸이 빠지는지, 잘렸는지), "
                "같은 때 다른 피드에도 필드 수가 맞지 않는 프레임이나 `_unparsed` 프레임(둘로 나뉜 프레임의 "
                "뒷부분)이 있었는지 #381에 정리하고 2단계(그 모양을 정규화할지, WARNING으로 낮추고 셀지)를 "
                "정한다. 사용자의 결정이 필요하면 이슈에 묻는다. 출력에는 시세 값이 없다"
                "(개수, 시각, 종목코드, 필드 수, 칸의 모양만)."
            ),
        ),
    ),
)


# The night futures (#264): the collector records the KOSPI200 night session's trades and book
# (18:00–06:00) from the deploy that brings it; what only the server shows is whether KIS
# streams them for this app key, under the contract code asked for, in the layout of KIS's
# example. Once, on the first night after a night the collector subscribed for from its start
# (the evening after the first trading day's night): ``probe-night`` reads that night's files,
# and the masters. It prints counts, times, field names and codes, no market value, so it can
# go on the public issue. One night's frames take seconds to read; the half hour is a bound.
# Before the pre-validations.
NIGHT_PROBE_ISSUE = 264


def _after_a_subscribed_night(night: Night) -> bool:
    """Once, on the first night after one the night futures were subscribed for from 18:00."""
    return not night.ran and any(day < night.day for day in night.facts.night_futures)


NIGHT_PROBE = Job(
    name="probe-night-264",
    due=_after_a_subscribed_night,
    command=("probe-night",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            NIGHT_PROBE_ISSUE,
            note=(
                '에이전트가 이 출력으로 `docs/kis-feeds.md` "야간선물 (#264)"의 ⚠(종목코드 규칙, '
                "실제로 오는지, 필드 배치, 시각 형식)를 고친다. 출력에는 시세 값이 없다"
                "(개수, 시각, 필드 이름, 종목코드만)."
            ),
        ),
    ),
)

# The auctions' expected fill (#304): the pre-open briefing shows KRX's expected opening price
# from 08:30 to 09:00, read from the integrated book's ANTC_* fields, which the collector
# normalizes from the deploy that brings it. The raw layer kept every frame before that, so the
# days already recorded show whether the feed carries the price then, and whether it is KRX's
# auction's (against the KRX day candle's open and close, in ticks). Once, on the first trading
# day's night, so that day's normalized rows show the new columns too. It prints counts, times,
# codes and tick differences, no market value, so it can go on the public issue. Ten days of
# raw book frames take a few minutes to read; the half hour is a bound. Before the
# pre-validations.
# The result goes to #314 (the follow-up of #304, which closed with the screen): what to do with
# it is written there.
EXPECTED_OPEN_ISSUE = 314

EXPECTED_OPEN_PROBE = Job(
    name="probe-expected-open-314",
    due=_first_trading_night,
    command=("probe-expected-open",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            EXPECTED_OPEN_ISSUE,
            note=(
                "에이전트가 이 출력으로 통합 호가의 예상 체결(`ANTC_*`)이 08:30–09:00에 오는지, KRX 시가 "
                '단일가의 것인지 보고 `docs/events.md` "호가"의 예상 체결 열 설명과 `docs/kis-feeds.md`에 '
                '남긴다. 장전 브리핑(#304)의 예상 시가는 그 결과를 따른다(이 이슈의 "에이전트가 할 일"). '
                "출력에는 시세 값이 없다(개수, 시각, 코드, 틱 차이만)."
            ),
        ),
    ),
)

# The same probe again, KRX's own (#314). The first run (the night of 2026-10-20) counted section
# 4's times and section 5's gaps over every venue code; for 009150 the opening auction's window
# also carried NXT's '2', so its gaps to the open may be NXT's. From the deploy that brings this,
# sections 4 and 5 are KRX's ('1') only and the other codes' times are shown apart. Once, on the
# first trading day's night after it, over the latest recorded days; the half hour as above.
EXPECTED_OPEN_KRX_PROBE = Job(
    name="probe-expected-open-krx-314",
    due=_first_trading_night,
    command=("probe-expected-open",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            EXPECTED_OPEN_ISSUE,
            note=(
                "첫 출력(2026-10-20 밤)의 4·5절은 거래소 구분 코드와 상관없이 셌다. 이번 4·5절은 "
                "KRX(`1`) 것만 세고, 다른 코드의 시각은 괄호로 따로 보인다. 에이전트가 이 출력으로 "
                '009150의 시가 차이를 고치고 `docs/kis-feeds.md` "피드별 메모"의 "거르지 않은 값" 표시를 '
                "지운다(005930·000660은 그 구간에 `1`만 있었다). 종가 쪽 차이는 #342가 다시 받은 "
                "일봉과 견준 것이다. 그날 저녁 저장한 날만 잠정 종가이므로 그날을 빼고 "
                '"종가 쪽" 문단을 고친다. 출력에는 시세 값이 없다'
                "(개수, 시각, 코드, 틱 차이만)."
            ),
        ),
    ),
)
