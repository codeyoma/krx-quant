# 녹화 데이터 형식

수집기는 KIS에서 받은 메시지를 두 계층으로 녹화한다.

| 계층 | 내용 | 용도 |
|---|---|---|
| `raw` | 받은 메시지 원문 그대로 | 정규화 로직이 바뀌어도 이벤트를 다시 만들 수 있게 한다 |
| `events` | 피드별로 정규화한 이벤트 | 피처 계산, 재생, 학습의 입력 |

그 밖에 `gaps`(결측 구간)와 `bars`(REST로 받은 1분봉) 계층이 있다. 둘 다 아래에 설명한다. 일·주·월·년봉(`candles`)과, 녹화 폴더가 아닌 상태 폴더에 두는 일별 수급(`state/supply/`, #226)도 아래에 있다.

## 저장 위치

```
<KRX_DATA_DIR>/<계층>/date=<YYYY-MM-DD>/feed=<피드>/symbol=<종목>/part-<HHMMSS>-<id>.parquet
```

- `date`는 메시지를 받은 시각의 한국 날짜다.
  - 야간선물(18:00–다음 날 06:00, #264)도 같다. 그래서 한 밤이 두 날짜에 걸친다: 저녁 날짜의 18:00–23:59와 다음 날짜의 00:00–06:00. 금요일 밤의 새벽은 토요일 파티션이다. 한 밤을 읽으려면 두 날짜를 받은 시각(`received_at`)으로 거른다(`krx-quant probe-night`가 그렇게 읽는다).
- `feed`는 KIS TR ID다(예: `H0UNCNT0`). 피드 목록과 키는 [kis-feeds.md](kis-feeds.md)에 있다. KIS 제어 메시지(구독 응답, PINGPONG)는 `raw` 계층의 `feed=_system`에 남는다. 형식을 알아볼 수 없는 메시지는 `raw` 계층의 `feed=_unparsed`에 남는다.
- 한도로 잘린 프로그램매매, 선물, 달러선물, 미국 주간거래, 옵션을 대신하는 REST 조회는 events를 대신하는 WebSocket 피드 이름으로 쓴다. 그래서 이 다섯 피드의 events에는 REST로 받은 행도 있을 수 있다. 그 응답 원문은 raw의 REST TR 이름(`FHPPG04650101`, `FHMIF10000000`, `HHDFS00000300`)에 남는다(#28).
- events의 `symbol` 파티션은 이벤트의 `symbol`이다. REST 응답의 raw는 조회한 키로 쓴다.

  > **바뀜** (2026-10-11, #65): events의 `feed`는 그 행을 만든 TR이었다 → 잘린 피드를 대신하는 REST 조회의 행은 대신하는 WebSocket 피드 이름으로 쓴다. REST 조회의 events `symbol` 파티션은 조회한 키였다 → 이벤트의 `symbol`이다(미국 주간거래를 실시간 피드처럼 티커로 쓰기 위해서다. 그 전 REST 피드는 둘이 같아서 바뀐 파일은 없다). 이유: 선물, 달러선물, 옵션의 대체 조회가 같은 TR(`FHMIF10000000`)이라 TR 이름으로 쓰면 스키마가 다른 행이 한 폴더에 섞인다. 대신하는 피드 이름으로 쓰면 엔진, 재생, 학습이 고칠 것 없이 읽는다.
- 메시지는 항상 `raw`에 먼저 기록된다. 정규화에 실패한 메시지는 `raw`에만 남고, 수집은 멈추지 않는다.
- 한 파티션은 행이 `KRX_FLUSH_ROWS`만큼 쌓이거나 `KRX_FLUSH_SECONDS`가 지나면 새 part 파일로 쓰인다. 종료할 때도 남은 행을 쓴다.
- part 파일은 임시 이름으로 쓴 뒤 이름을 바꾼다. 그래서 프로세스가 중간에 죽어도 반쯤 쓰인 part 파일은 남지 않는다. fsync는 하지 않으므로 전원이 꺼지는 경우까지 보장하지는 않는다.
- 장중에는 파티션마다 part 파일이 여러 개 생긴다. 장 마감 업로드(`krx-quant archive`, 수집기는 장 마감 15분 뒤와 21:00에 자동으로 한다)가 파티션마다 part 파일을 `data.parquet` 하나로 합치고 Object Store에 같은 경로로 올린다.
  - `data.parquet`의 Parquet 메타데이터(`krx_quant.parts`)에 합친 part 파일 이름이 남는다. 그래서 합친 뒤 지우지 못한 part 파일이 있어도 두 번 합치지 않는다. 그 뒤에 생긴 part 파일은 다음 업로드 때 합친다.
  - 파티션의 `.uploaded.json`에 올린 파일의 크기, 수정 시각, SHA-256을 남긴다. 바뀌지 않은 파일은 다시 올리지 않는다.
  - 10분 넘게 남아 있는 `.tmp` 파일(쓰다가 죽은 흔적)은 이때 지운다.
  - 밤의 야간선물은 저녁 날짜의 21:00 업로드에 그때까지의 행이 들어가고, 나머지(21:00–23:59)와 다음 날짜의 새벽(00:00–06:00)은 다음 날의 장 마감 15분 뒤와 21:00 업로드에 들어간다. 업로드는 휴장일에도 21:00에 돈다.
- 디렉터리는 Hive 파티션 형식이라 `pyarrow.dataset`이나 DuckDB로 바로 읽을 수 있다.

## raw

| 열 | 타입 | 설명 |
|---|---|---|
| `received_at` | timestamp(us, UTC) | 수신 시각 |
| `message` | string | 받은 메시지 원문 |

## events: 체결 (`H0UNCNT0` 통합, `H0STCNT0` KRX, `H0NXCNT0` NXT)

KIS 레코드 하나가 이벤트 하나다. 한 메시지에 레코드가 여러 개 들어 있으면 여러 행이 된다.

| 열 | 타입 | KIS 필드 | 설명 |
|---|---|---|---|
| `symbol` | string | `MKSC_SHRN_ISCD` | 종목코드 |
| `event_time` | timestamp(s, Asia/Seoul) | `BSOP_DATE` + `STCK_CNTG_HOUR` | 체결 시각(초 단위) |
| `received_at` | timestamp(us, UTC) | | 수신 시각 |
| `venue` | string | | 피드별로 `UN`(통합, 체결별 거래소 정보 없음), `KRX`, `NXT`. ETF는 KRX 전용 피드로만 받아서 `KRX`다. |
| `price` | int64 | `STCK_PRPR` | 체결가(원) |
| `qty` | int64 | `CNTG_VOL` | 체결량(주) |
| `side` | string | `CNTG_CLS_CODE` | 체결 방향: `1` → `buy`(매수 체결), `5` → `sell`(매도 체결), 그 외 → `unknown` |
| `cum_volume` | int64 | `ACML_VOL` | 누적 거래량 |
| `ask1` | int64 | `ASKP1` | 체결 시점 최우선 매도호가 |
| `bid1` | int64 | `BIDP1` | 체결 시점 최우선 매수호가 |
| `session_code` | string | `NEW_MKOP_CLS_CODE` | 장운영 구분 코드(원문) |
| `halted` | bool | `TRHT_YN` | 거래정지 여부 |
| `hour_cls` | string | `HOUR_CLS_CODE` | 시간 구분 코드(원문) |
| `vi_ref_price` | int64 | `VI_STND_PRC` | VI 기준가 |
| `market_cls_code` | string | `MARKET_CLS_CODE` | 시장 구분 코드(원문). 실데이터에서 모든 체결이 `2`였다. 체결별 거래소를 구분하지 않는다. |

## events: 호가 (`H0UNASP0`, 국내주식 실시간호가 통합)

| 열 | 타입 | KIS 필드 | 설명 |
|---|---|---|---|
| `symbol` | string | `MKSC_SHRN_ISCD` | 종목코드 |
| `event_time` | timestamp(s, Asia/Seoul) | `BSOP_HOUR` | 호가 시각. KIS가 날짜를 주지 않아 수신 시각의 한국 날짜를 붙인다. |
| `received_at` | timestamp(us, UTC) | | 수신 시각 |
| `hour_cls` | string | `HOUR_CLS_CODE` | 시간 구분 코드(원문) |
| `ask_px_1` … `ask_px_10` | int64 | `ASKP1` … `ASKP10` | 매도호가 1–10단계 |
| `bid_px_1` … `bid_px_10` | int64 | `BIDP1` … `BIDP10` | 매수호가 1–10단계 |
| `ask_qty_1` … `ask_qty_10` | int64 | `ASKP_RSQN1` … `ASKP_RSQN10` | 매도호가 잔량 |
| `bid_qty_1` … `bid_qty_10` | int64 | `BIDP_RSQN1` … `BIDP_RSQN10` | 매수호가 잔량 |
| `total_ask_qty` | int64 | `TOTAL_ASKP_RSQN` | 총 매도호가 잔량 |
| `total_bid_qty` | int64 | `TOTAL_BIDP_RSQN` | 총 매수호가 잔량 |
| `cum_volume` | int64 | `ACML_VOL` | 누적 거래량 |
| `krx_midpoint_px` | int64 | `KMID_PRC` | KRX 중간가 주문 가격 |
| `krx_midpoint_qty` | int64 | `KMID_TOTAL_RSQN` | KRX 중간가 주문 총 잔량 |
| `krx_midpoint_cls` | string | `KMID_CLS_CODE` | KRX 중간가 구분 코드(원문, 의미 미확인) |
| `nxt_midpoint_px` | int64 | `NMID_PRC` | NXT 중간가 주문 가격 |
| `nxt_midpoint_qty` | int64 | `NMID_TOTAL_RSQN` | NXT 중간가 주문 총 잔량 |
| `nxt_midpoint_cls` | string | `NMID_CLS_CODE` | NXT 중간가 구분 코드(원문, 의미 미확인) |

## events: 프로그램매매 (`H0UNPGM0`, 통합)

당일 누적 값이다.

| 열 | 타입 | KIS 필드 | 설명 |
|---|---|---|---|
| `symbol` | string | `MKSC_SHRN_ISCD` | 종목코드 |
| `event_time` | timestamp(s, Asia/Seoul) | `STCK_CNTG_HOUR` | 시각(날짜는 수신 날짜) |
| `received_at` | timestamp(us, UTC) | | 수신 시각 |
| `sell_qty`, `sell_value` | int64 | `SELN_CNQN`, `SELN_TR_PBMN` | 프로그램 매도 수량·금액 |
| `buy_qty`, `buy_value` | int64 | `SHNU_CNQN`, `SHNU_TR_PBMN` | 프로그램 매수 수량·금액 |
| `net_qty`, `net_value` | int64 | `NTBY_CNQN`, `NTBY_TR_PBMN` | 순매수 수량·금액 |
| `sell_order_qty`, `buy_order_qty` | int64 | `SELN_RSQN`, `SHNU_RSQN` | 매도·매수 호가 잔량 |
| `whole_net_qty` | int64 | `WHOL_NTBY_QTY` | 전체 순매수 호가 잔량 |

한도로 잘리면 REST `FHPPG04650101`(종목별 프로그램매매추이)이 이 피드를 대신한다. 응답에서 시각이 가장 늦은 행을 쓴다. `event_time`은 `bsop_hour`, 매도·매수·순매수 열은 `whol_smtn_seln_vol`, `whol_smtn_seln_tr_pbmn`, `whol_smtn_shnu_vol`, `whol_smtn_shnu_tr_pbmn`, `whol_smtn_ntby_qty`, `whol_smtn_ntby_tr_pbmn`이다. 호가 잔량 세 열은 응답에 없어 null이다([kis-feeds.md](kis-feeds.md)).

## events: 회원사 (`H0UNMBC0` 통합, REST `FHKST01010600`)

두 형식 모두 같은 스키마로 기록한다. 메시지에 시각이 없어 `received_at`만 있다.

| 열 | 타입 | 설명 |
|---|---|---|
| `symbol` | string | 종목코드 |
| `received_at` | timestamp(us, UTC) | 수신 시각 |
| `{sell,buy}_member_{1..5}_no` | string | 매도·매수 상위 5개 회원사 번호 |
| `{sell,buy}_member_{1..5}_name` | string | 회원사 이름 |
| `{sell,buy}_member_{1..5}_qty` | int64 | 회원사별 누적 수량 |
| `{sell,buy}_member_{1..5}_foreign` | bool | 외국계 회원사 여부 |
| `foreign_sell_qty`, `foreign_buy_qty`, `foreign_net_qty` | int64 | 외국계 회원사 합계 매도·매수·순매수 |

## events: 업종지수 (`H0UPCNT0`, REST `FHPUP02100000`)

`symbol`은 업종코드다. REST 형식은 시각을 주지 않아 `event_time`에 수신 시각을 쓴다. 저녁 장 마감 뒤 KIS가 지수마다 보내는 마감 확정값은 시각이 `999999`로 와서(2026-10-08 20:05 실측), 이것도 수신 시각을 쓴다. 선물과 옵션도 시각이 `999999`로 오면 수신 시각을 쓴다. 그 밖에 시각으로 읽을 수 없는 값은 정규화하지 않고 raw에만 남긴다.

> **바뀜** (2026-10-08, #35): 시각이 `999999`인 레코드는 정규화에 실패해(`ValueError: hour must be in 0..23`) raw에만 남았다 → 수신 시각을 쓴다. 이유: 이 레코드가 그날의 마감 확정값(종가, 거래량, 상승·하락 종목 수)이다. 2026-10-08 20:05에 받은 지수 3개(`0001`, `2001`, `0013`)의 마감 확정값은 이 수정 전에 받아서 서버의 raw에만 있고 events에는 없다. raw에서 events를 다시 만드는 명령은 아직 없다.

| 열 | 타입 | 설명 |
|---|---|---|
| `symbol`, `event_time`, `received_at` | | 업종코드, 시각, 수신 시각 |
| `value` | float64 | 지수 |
| `change_pct` | float64 | 전일 대비 등락률(%) |
| `cum_volume`, `cum_value` | int64 | 누적 거래량·거래대금 |
| `advancers`, `decliners`, `unchanged` | int64 | 상승(상한 포함)·하락(하한 포함)·보합 종목 수 |

## events: 선물 (`H0IFCNT0` KOSPI200, `H0CFCNT0` 달러선물)

가격은 소수 둘째 자리로 반올림한다. KIS가 달러선물 가격에 부동소수 잡음을 섞어 보내기 때문이다.

| 열 | 타입 | KIS 필드 | 설명 |
|---|---|---|---|
| `symbol` | string | `futs_shrn_iscd` | 선물 코드 |
| `event_time`, `received_at` | | `bsop_hour` | 시각, 수신 시각 |
| `price` | float64 | `futs_prpr` | 체결가 |
| `change_pct` | float64 | `futs_prdy_ctrt` | 전일 대비 등락률(%). #12부터 기록한다. 그 전 파일에는 이 열이 없다. |
| `qty`, `cum_volume` | int64 | `last_cnqn`, `acml_vol` | 체결량, 누적 거래량 |
| `open_interest` | int64 | `hts_otst_stpl_qty` | 미결제약정 |
| `theory_price`, `market_basis` | float64 | `hts_thpr`, `mrkt_basis` | 이론가, 시장 베이시스 |
| `ask1`, `bid1`, `ask_qty1`, `bid_qty1` | | `futs_askp1` … | 최우선 호가와 잔량 |
| `total_ask_qty`, `total_bid_qty` | int64 | | 총 호가 잔량 |

한도로 잘리면 REST `FHMIF10000000`(선물옵션 시세, KOSPI200 `F`, 달러선물 `CF`)이 이 피드를 대신한다. 응답에 시각이 없어 `event_time`은 수신 시각이다. `price`, `change_pct`, `cum_volume`, `open_interest`, `theory_price`, `market_basis`는 같은 이름의 필드에서 온다. `qty`와 호가 열(`ask1` … `total_bid_qty`)은 응답에 없어 null이다.

## events: 야간선물 체결 (`H0MFCNT0`, #264)

KRX 야간시장(18:00–다음 날 06:00)의 KOSPI200 선물 체결이다. 스키마는 위 "선물"과 같다. 열마다 같은 KIS 필드에서 온다. KIS 필드는 49개로 주간의 50개에서 `dscs_bltr_acml_qty`만 빠졌다([kis-feeds.md](kis-feeds.md#야간선물-264)).

- `event_time`: `bsop_hour`에는 날짜가 없고 밤은 자정을 넘는다. 그래서 받은 시각(`received_at`)에 가장 가까운 날의 그 시각이다. 00:00:00.4에 받은 23:59:59 체결은 전날 23:59:59다. 파티션 날짜(받은 날)와 `event_time`의 날짜가 다를 수 있다. `999999`는 받은 초다.
- REST 대체가 없다. 그래서 이 피드의 행은 모두 WebSocket에서 왔다(`qty`와 호가 열이 비지 않는다).

## events: 야간선물 호가 (`H0MFASP0`, #264)

KRX 야간시장의 KOSPI200 선물 5단계 호가다. KIS는 0.2초 간격으로 거른다. 가격은 소수 둘째 자리로 반올림한다. `event_time`은 위 체결과 같은 규칙이다.

| 열 | 타입 | KIS 필드 | 설명 |
|---|---|---|---|
| `symbol` | string | `futs_shrn_iscd` | 선물 코드 |
| `event_time` | timestamp(s, Asia/Seoul) | `bsop_hour` | 호가 시각(받은 시각에 가장 가까운 날) |
| `received_at` | timestamp(us, UTC) | | 수신 시각 |
| `ask_px_1` … `ask_px_5` | float64 | `futs_askp1` … `futs_askp5` | 매도호가 1–5단계 |
| `bid_px_1` … `bid_px_5` | float64 | `futs_bidp1` … `futs_bidp5` | 매수호가 1–5단계 |
| `ask_count_1` … `ask_count_5` | int64 | `askp_csnu1` … `askp_csnu5` | 매도호가 건수 |
| `bid_count_1` … `bid_count_5` | int64 | `bidp_csnu1` … `bidp_csnu5` | 매수호가 건수 |
| `ask_qty_1` … `ask_qty_5` | int64 | `askp_rsqn1` … `askp_rsqn5` | 매도호가 잔량(계약) |
| `bid_qty_1` … `bid_qty_5` | int64 | `bidp_rsqn1` … `bidp_rsqn5` | 매수호가 잔량(계약) |
| `total_ask_count`, `total_bid_count` | int64 | `total_askp_csnu`, `total_bidp_csnu` | 총 매도·매수호가 건수 |
| `total_ask_qty`, `total_bid_qty` | int64 | `total_askp_rsqn`, `total_bidp_rsqn` | 총 매도·매수호가 잔량 |
| `total_ask_qty_change`, `total_bid_qty_change` | int64 | `total_askp_rsqn_icdc`, `total_bidp_rsqn_icdc` | 총 잔량 증감 |

## events: 미국 주식 (`HDFSCNT0`)

`symbol`은 티커(`NVDA`)이고 `key`는 KIS 키(`RBAQNVDA`)다. raw 계층의 `symbol` 파티션은 키다.

| 열 | 타입 | KIS 필드 | 설명 |
|---|---|---|---|
| `event_time` | timestamp(s, Asia/Seoul) | `KYMD` + `KHMS` | 한국 시각 |
| `local_date`, `local_time` | string | `XYMD`, `XHMS` | 현지 날짜·시각 |
| `price`, `bid`, `ask` | float64 | `LAST`, `PBID`, `PASK` | 체결가, 매수·매도호가 |
| `qty`, `cum_volume` | int64 | `EVOL`, `TVOL` | 체결량, 누적 거래량 |
| `bid_qty`, `ask_qty` | int64 | `VBID`, `VASK` | 호가 잔량 |
| `session` | string | `MTYP` | 1 정규장, 2 장전, 3 장후 |

한도로 잘리면 REST `HHDFS00000300`(해외주식 현재체결가, `EXCD=BAQ` 등)이 이 피드를 대신한다. 응답에 시각이 없어 `event_time`은 수신 시각이다. `price`는 `last`, `cum_volume`은 `tvol`이다. `local_date`, `local_time`, `qty`, `bid`, `ask`, `bid_qty`, `ask_qty`, `session`은 null이다.

## events: KOSPI200 옵션 (`H0IOCNT0`)

| 열 | 타입 | KIS 필드 | 설명 |
|---|---|---|---|
| `symbol` | string | `optn_shrn_iscd` | 옵션 코드 |
| `event_time`, `received_at` | | `bsop_hour` | 시각, 수신 시각 |
| `price`, `theory_price`, `ask1`, `bid1` | float64 | `optn_prpr`, `hts_thpr`, … | 체결가, 이론가, 최우선 호가 |
| `qty`, `cum_volume`, `open_interest` | int64 | `last_cnqn`, `acml_vol`, `hts_otst_stpl_qty` | 체결량, 누적 거래량, 미결제약정 |
| `implied_vol` | float64 | `hts_ints_vltl` | 내재변동성(%) |
| `delta`, `gamma`, `vega`, `theta` | float64 | `delta`, `gama`, `vega`, `theta` | 그릭스 |

한도로 잘리면 REST `FHMIF10000000`(선물옵션 시세, 시장 코드 `O`)이 이 피드를 대신한다. 응답에 시각이 없어 `event_time`은 수신 시각이다. 가격은 `futs_prpr`, 델타는 `delta_val`이고, 나머지는 위와 같은 필드다. `qty`, `ask1`, `bid1`은 null이다.

## events: REST 스냅샷

| 피드 | 열 |
|---|---|
| `FHKST01010100` 시세(체결 대체) | `symbol`, `received_at`, `price`, `cum_volume`, `change_pct`. 주식은 통합(`UN`), ETF는 KRX(`J`) 시장으로 조회한다. |
| `FHKST01010200` 호가 스냅샷(호가 대체) | `symbol`, `event_time`(`aspr_acpt_hour`), `received_at`, `price`, `ask_px_1..10`, `bid_px_1..10`, `ask_qty_1..10`, `bid_qty_1..10`, `total_ask_qty`, `total_bid_qty`, `vi_cls_code` |
| `HHPTJ04160200` 추정 가집계 | `symbol`, `received_at`, `slot`(`bsop_hour_gb`), `foreign_net_qty`, `institution_net_qty`, `total_net_qty` |
| `FHPST01390000` VI 현황 | 응답의 VI마다 한 행: `symbol`, `event_time`(발동 시각 `cntg_vi_hour`), `received_at`, `end_time`(해제 시각 `vi_cncl_hour`, 아직이면 비어 있음), `active`(`vi_cls_code`가 `N`이 아님), `kind`(`static`, `dynamic`, `both`), `price`(발동 가격), `count`(그날 그 종목의 VI 횟수) |

> **바뀜** (2026-10-11, #56): 추정 가집계의 빈 칸(그 시간대에 아직 입력되지 않은 투자자)을 0으로 기록했다 → null로 기록한다. 이유: 외국인과 기관은 입력 시각이 달라서(외국인 09:30, 기관 10:00 등), 0으로 두면 Turing이 앞 시간대의 실제 추정 대신 0을 읽었다. 그 전에 녹화된 날의 0은 빈 칸일 수 있다.

VI 현황은 같은 응답이 반복되면 기록하지 않는다. 응답이 바뀔 때마다 그날의 VI 전체가 raw와 events에 한 번씩 남는다.

프로그램매매, 선물, 달러선물, 미국 주간거래, 옵션을 대신하는 REST 조회는 이 표에 없다. 대신하는 피드의 이름과 스키마로 기록하기 때문이다(위 각 피드). 이 조회들은 새 소식만 기록한다. 같은 응답이 반복되면 기록하지 않고, 첫 응답도 기록하지 않는다(언제 값인지 알 수 없다. [kis-feeds.md](kis-feeds.md#rest-대체-조회)).

REST 응답 원문은 raw 계층에 `{"tr_id", "params", "body"}` JSON으로 남는다.

## gaps: 결측 구간

녹화가 빠진 구간이다. `gaps/date=<시작 날짜>/feed=<TR>/symbol=<키>/`에 기록한다.
- **`disconnected: <원인>`**: WebSocket 연결이 끊긴 구간이다. 끊긴 시각에 시작하고, 재연결 후 해당 구독이 승인된 시각에 끝난다.
- **`stale`**: 연속매매 시간(Market Calendar 기준. 보통 09:00–15:20, 연초 첫 거래일과 수능일은 10:00부터)에 피드가 기준 시간보다 오래 조용했던 구간이다. 다음 메시지가 온 시각에 끝난다. 기준 시간은 피드마다 다르다(`health.STALE_SECONDS`).
  - 시작은 개장 시각, 구독(또는 REST 조회)을 시작한 시각, 마지막 수신 시각 중 가장 늦은 시각이다. 그래서 NXT 프리마켓에 받은 데이터는 침묵 시간에 들어가지 않는다.
  - 그날 한 번도 데이터가 오지 않은 피드는 KIS가 거래일이라고 확인한 날에만 판정한다. 휴장일에는 판정하지 않는다.
  - 미국 주간거래(`HDFSCNT0`)는 자기 시간표(미국 서머타임이 아니면 10:00 시작, 미국 휴일 휴장)를 따라서, 그날 데이터가 한 번 온 뒤에만 판정한다.
  - 야간선물(`H0MFCNT0`, `H0MFASP0`, #264)은 판정하지 않는다. 18:00–06:00에만 오므로 연속매매 시간 내내 조용하다. 끊긴 구간(`disconnected`)은 다른 피드처럼 남는다.
- 수집기가 멈출 때까지 열려 있던 구간은 `end`가 비어 있다.
- **호가와 틱 체결의 결측은 채울 수 없다.** 과거 호가·틱을 주는 곳이 없어서, 이 계층의 구간은 그대로 남는다. 같은 구간의 1분봉은 `bars` 계층에 REST로 다시 받아 둔다.
- 결측 구간의 1분봉 채움 여부는 `bars` 계층에 그날 그 종목 파일이 있는지로 안다. 결측 구간은 아직 SQLite(`data_gaps`)로 옮기지 않았다.

| 열 | 타입 | 설명 |
|---|---|---|
| `feed` | string | TR ID |
| `symbol` | string | 구독 키 |
| `start` | timestamp(us, Asia/Seoul) | 시작 |
| `end` | timestamp(us, Asia/Seoul), null 가능 | 끝 |
| `reason` | string | `disconnected: …` 또는 `stale` |

## bars: 1분봉 (REST 백필)

Backfiller가 KIS REST로 받은 1분봉이다. 주식과 ETF는 주식일별분봉조회(`FHKST03010230`)로 받는다. 녹화가 아니라 조회라서, 녹화가 빠진 구간의 1분봉도 여기 있다.

```
<KRX_DATA_DIR>/bars/date=<YYYY-MM-DD>/feed=FHKST03010230/symbol=<종목>/bars.parquet
```

- 거래일마다 종목별로 파일 하나다. 그날의 모든 장(KRX 애프터마켓, NXT는 20:00 종료)이 끝난 뒤 20:30부터 하루치를 통째로 받아 쓴다. 그래서 파일이 있으면 그날 1분봉은 완전하다.
- 같은 날을 다시 받으면 파일을 통째로 바꾼다. 몇 번 받아도 결과가 같다(멱등).
- 첫 실행에 지난 365일(`KRX_BACKFILL_DAYS`)의 모든 거래일을 받는다. 그다음부터는 파일이 없는 거래일만 받는다(서버가 멈췄던 날, 새로 추가한 종목). 최신 날부터 받는다.
- 대상은 `KRX_SYMBOLS`와 `KRX_ETF_SYMBOLS`, 그리고 KOSPI200 선물과 달러선물(아래 "선물 연속 1분봉")이다.
- KIS가 봉을 하나도 주지 않은 날(보관 기간 밖, 거래정지, 일시 오류)은 저장하지 않는다. 다음 실행에 다시 묻는다.
- 엔진 API의 `GET /v1/symbols/{s}/bars?before=`가 이 파일에서 지난 날의 봉을 읽는다(#108, [engine-api.md](engine-api.md)).

| 열 | 타입 | KIS 필드 | 설명 |
|---|---|---|---|
| `symbol` | string | | 종목코드 |
| `bar_time` | timestamp(s, Asia/Seoul) | `stck_bsop_date` + `stck_cntg_hour` | 봉이 시작하는 분. 09:00 봉에 시가 동시호가 체결이 들어간다. |
| `venue` | string | | `UN`(통합 KRX+NXT, 08:00–19:59) 또는 `KRX`(NXT에서 거래하지 않는 ETF 등, 09:00–15:30) |
| `open`, `high`, `low`, `close` | int64 | `stck_oprc`, `stck_hgpr`, `stck_lwpr`, `stck_prpr` | 시가, 고가, 저가, 종가(원) |
| `volume` | int64 | `cntg_vol` | 봉 거래량(주) |
| `cum_value` | int64 | `acml_tr_pbmn` | 그날 누적 거래대금(원). 봉별 거래대금이 아니다. |

### 선물 연속 1분봉

KOSPI200 선물과 달러선물은 KIS 선물옵션 분봉조회(`FHKIF03020200`)로 받는다. 날마다 그날의 최근월물을 받아, 상품마다 연속 시계열 하나로 쓴다. 월물 교체 규칙은 [kis-feeds.md](kis-feeds.md#과거-1분봉)에 있다.

```
<KRX_DATA_DIR>/bars/date=<YYYY-MM-DD>/feed=FHKIF03020200/symbol=<KOSPI200F|USDF>/bars.parquet
```

- 파일 쓰기, 다시 받기, 빈 응답 처리, 요청 간격은 주식 1분봉과 같다.
- 봉은 08:45–15:34 매분 봉과 15:45 종가 단일가 봉이다.
- 최종거래일에는 다음 월물을 받는다. 만기 월물은 그날 일찍 끝나고(KOSPI200 15:20, 달러 11:30), 다음 월물 거래가 이미 더 많다.

| 열 | 타입 | KIS 필드 | 설명 |
|---|---|---|---|
| `symbol` | string | | 연속 시계열 이름: `KOSPI200F`, `USDF` |
| `bar_time` | timestamp(s, Asia/Seoul) | `stck_bsop_date` + `stck_cntg_hour` | 봉이 시작하는 분 |
| `contract` | string | | 그 봉을 받은 월물 코드(예: `A01612`, `175W10`) |
| `open`, `high`, `low`, `close` | float64 | `futs_oprc`, `futs_hgpr`, `futs_lwpr`, `futs_prpr` | 시가, 고가, 저가, 종가(지수 포인트, 원/달러) |
| `volume` | int64 | `cntg_vol` | 봉 거래량(계약) |
| `cum_value` | int64 | `acml_tr_pbmn` | 그날 그 월물의 누적 거래대금(원). KIS는 KOSPI200 선물 값을 천원 단위로 줘서 1,000을 곱한다. |

> **바뀜** (2026-10-10, #43): KOSPI200 선물의 `cum_value`를 KIS가 준 천원 단위 그대로 쓰던 것(9f440ab) → 원으로 바꿔 쓴다. 이유: 다른 시리즈처럼 원으로 맞추기 위해서다(250ba59). 같은 PR에서 월물 교체도 "최종거래일 다음 날" → "최종거래일"로 바뀌었다([kis-feeds.md](kis-feeds.md#과거-1분봉)).

## candles: 일·주·월·년봉 (REST, #111, #112)

`CandleKeeper`가 KIS 국내주식기간별시세(`FHKST03010100`, `FID_PERIOD_DIV_CODE` `D`·`W`·`M`·`Y`)로 받은 봉이다. 차트의 "일·주·월·년" 보기가 KIS에 있는 만큼 거슬러 보게 하려고 둔다.

```
<KRX_DATA_DIR>/candles/period=<day|week|month|year>/symbol=<종목>/candles.parquet
```

- 봉의 날짜(`date`)는 기간이 시작하는 날이다: 일봉은 그날, 주봉은 월요일, 월봉은 1일, 년봉은 1월 1일. KIS가 기간의 첫날로 붙이든 마지막 거래일로 붙이든 이렇게 맞춘다.
- 끝난 봉만 저장한다. 일봉은 그날 장이 다 끝난 뒤(20:30), 주·월·년봉은 마지막 끝난 거래일이 다음 기간으로 넘어간 뒤다. 예: 화요일 밤에는 지난주까지의 주봉만 있다. 그 사이의 봉은 화면이 일봉과 오늘의 1분봉으로 만든다.

- 대상은 `KRX_SYMBOLS`(`/v1/symbols`의 종목)다. 종목·기간마다 파일 하나에 끝난 봉을 오래된 것부터 모두 담는다.
- 첫 실행에 KIS에 있는 전체 기간을 받는다. 마지막 끝난 거래일부터 한 번에 100개씩 거슬러 가며, 더 오래된 봉이 오지 않을 때까지 묻는다.
- 그다음부터는 저장된 마지막 봉의 기간부터 마지막 끝난 거래일까지를 묻고 새로 끝난 봉만 더한다. 새 봉이 없는 실행(주말, 아침 재시작)에도 저장된 마지막 날은 다시 묻는다. 그 봉이 KIS의 것과 다르면(액면분할 같은 수정) 전체 기간을 다시 받아 파일을 바꾼다.
- 가격은 수정주가다(`FID_ORG_ADJ_PRC=0`, 증권사 차트와 같다). 1분봉(`bars`)은 원래 가격이라, 수정이 일어난 날 이전 구간은 두 계층의 가격이 다를 수 있다.
- 시장은 1분봉과 같이 통합(`UN`)을 먼저 묻고, 봉이 하나도 없으면 KRX(`J`)를 묻는다. 한 종목은 처음 받은 시장을 계속 쓴다.
- 일봉은 그날의 모든 장이 끝난 뒤(20:30, 1분봉 백필 다음)에만 저장한다. 그 전의 오늘 일봉은 화면이 그날의 1분봉으로 만든다. 주·월·년봉은 위처럼 그 기간이 끝난 뒤에 저장한다.
- 요청 간격은 1분봉 백필과 같은 제한기 하나를 함께 쓴다(`KRX_BACKFILL_MAX_PER_SECOND`). 백필에서 일봉으로 넘어갈 때도 몰리지 않는다.
- 파일은 통째로 바꾼다(임시 파일에 쓰고 이름 바꾸기). KIS가 아무 봉도 주지 않으면 저장하지 않고 다음 실행에 다시 묻는다.
- 매일 21:00 업로드와 장 마감 뒤 업로드 때 바뀌었으면 S3의 같은 경로에 올린다.
- 엔진 API의 `GET /v1/symbols/{s}/candles?period=day`가 이 파일을 읽는다([engine-api.md](engine-api.md)).

| 열 | 타입 | KIS 필드 | 설명 |
|---|---|---|---|
| `symbol` | string | | 종목코드 |
| `date` | date32 | `stck_bsop_date` | 기간이 시작하는 날(일봉은 거래일) |
| `venue` | string | | `UN`(통합 KRX+NXT) 또는 `KRX`(KRX 정규장) |
| `open`, `high`, `low`, `close` | int64 | `stck_oprc`, `stck_hgpr`, `stck_lwpr`, `stck_clpr` | 수정 시가, 고가, 저가, 종가(원) |
| `volume` | int64 | `acml_vol` | 거래량(주) |
| `value` | int64 | `acml_tr_pbmn` | 거래대금(원) |

## supply: 일별 수급 (REST, #226)

`SupplyKeeper`(`supply.py`)가 KIS와 토스증권 Open API에서 받은 일별 수급 8종이다. 차트의 하단 지표가 엔진 API `GET /v1/symbols/{s}/supply`로 읽는다([engine-api.md](engine-api.md)). 출처와 받는 규칙은 [kis-feeds.md](kis-feeds.md#수집-226)에 있다.

```
<KRX_STATE_DIR>/supply/symbol=<종목>/kis.parquet
<KRX_STATE_DIR>/supply/symbol=<종목>/toss.parquet
```

- **데이터 폴더(`KRX_DATA_DIR`)가 아니라 상태 폴더(`KRX_STATE_DIR`)에 둔다.** 토스의 값은 사용자 자신의 매매에만 쓰고 제3자에게 넘기면 안 되기 때문이다([toss-feeds.md](toss-feeds.md#인증과-접속)). 업로드(`archive`)는 데이터 폴더만 올리므로 S3에 가지 않는다. 학습도 읽지 않는다. 저장소와 공개 이슈에도 올리지 않는다(테스트와 데모의 값은 지어낸 것이다).
- 출처마다 파일 하나에 하루 한 행, 오래된 날부터 담는다. API가 두 파일을 날짜로 합친다: 필드마다 KIS 값이 있으면 그것, 없으면 토스 값이다(외국인 보유수량·지분율은 토스 파일에만, 외국인 순매수는 KIS 파일에만 있다).
- 대상은 `KRX_SYMBOLS`(`/v1/symbols`의 종목)다. 마지막 끝난 거래일부터 10년 전까지만 남긴다.
- 실행마다 조회의 가장 최근 쪽을 다시 받아 그 날들의 값을 바꾼다. 그래서 같은 날의 값이 나중에 바뀔 수 있다(출처가 고친 값). 파일은 통째로 바꾼다(임시 파일 `.<이름>.tmp`에 쓰고 이름 바꾸기). 받은 것이 없거나 바뀐 것이 없으면 쓰지 않는다.
- 값이 모두 비는 날은 남기지 않는다. 신용잔고의 날은 결제일이다(KIS `stlm_date`, 토스 `date`).

`kis.parquet`

| 열 | 타입 | KIS 필드 | 설명 |
|---|---|---|---|
| `date` | date32 | `stck_bsop_date`, `bsop_date`, `stlm_date` | 그날(신용잔고는 결제일) |
| `short_volume` | int64 | `FHPST04830000` `J`의 `ssts_cntg_qty` | 공매도 거래량(주) |
| `individual_net`, `institution_net`, `foreign_net` | int64 | `FHPTJ04160001` `UN`의 `prsn_ntby_qty`, `orgn_ntby_qty`, `frgn_ntby_qty` | 개인·기관계·외국인 순매수량(주, 순매도는 음수) |
| `lending_balance` | int64 | `HHPST074500C0`의 `rmnd_stcn` | 대차잔고(주) |
| `credit_balance` | int64 | `FHPST04760000` `J`의 `whol_loan_rmnd_stcn` | 신용(융자)잔고(주) |

`toss.parquet`

| 열 | 타입 | 토스 필드 | 설명 |
|---|---|---|---|
| `date` | date32 | `date` | 그날 |
| `short_volume` | int64 | `short-selling`의 `shortSellingVolume` | 공매도 거래량(주) |
| `individual_net`, `institution_net` | int64 | `investor-trading`의 `individual.netBuyVolume`, `institution.netBuyVolume` | 개인·기관 순매수량(주) |
| `foreign_holding` | int64 | `investor-trading`의 `foreignerHolding.holdingQuantity` | 외국인 보유수량(주) |
| `foreign_ratio` | double | `foreignerHolding.holdingRate` × 100 | 외국인 지분율(상장주식수 대비 %) |
| `lending_balance` | int64 | `securities-lending`의 `balanceQuantity` | 대차잔고(주) |
| `credit_balance` | int64 | `credit-trades`의 `marginLoan.balanceQuantity` | 신용(융자)잔고(주) |

빈 칸(그 출처가 그날 주지 않은 값)은 null이다.

## 실데이터 관찰 (2026-10-08, 삼성전자, 13시 전후 90초)

- 체결 1,714건, 호가 409건을 받았다. 필드 수는 모든 메시지에서 문서와 같았다(체결 47개, 호가 66개).
- `MARKET_CLS_CODE`는 모든 체결에서 `2`였다. 통합 피드만으로는 체결별 거래소를 알 수 없다. 거래소별 체결이 필요하면 KRX 전용(`H0STCNT0`)과 NXT 전용(`H0NXCNT0`) 피드를 따로 구독해야 한다.
- 체결의 약 14%가 호가단위의 절반 가격(예: 263,250원, 호가단위 500원)에서 났다. 중간가 주문 체결로 보인다. 가격을 호가단위로 검증하는 코드는 이 경우를 허용해야 한다.
- 통합 호가의 약 29%가 최우선 매도호가와 매수호가가 같았다(locked). KRX와 NXT의 최우선 호가가 맞물린 상태로 보인다. 뒤집힌(crossed) 경우는 없었다. 통합 중간가 계산과 스프레드 피처는 스프레드 0을 정상 값으로 다뤄야 한다.

  > **바뀜** (2026-10-15, #213): "뒤집힌(crossed) 경우는 없었다"(삼성전자 90초) → 통합 호가는 뒤집힌다. 종목에 따라 연속매매 시간의 몇 %다. 이유: 서버 녹화 3거래일(2026-10-12 ~ 14, 연속매매 09:00–15:20)을 읽기만 하는 스크립트로 잰 결과가 아래와 같았다. 90초 관찰은 뒤집힘이 드문 005930 하나였다.
  >
  > | 종목 | 통합 호가(`H0UNASP0`) 중 뒤집힘 | 뒤집힌 시간 비율 | 통합 체결(`H0UNCNT0`)에 실린 1호가 중 뒤집힘 | 한 번 뒤집혀 있는 시간 p50 / p90 / 최대 | 깊이 |
  > |---|---|---|---|---|---|
  > | 005930 | 0.0~0.1% | 0.0~0.1% | 0.0~0.6% | 2~5초 / 11초 / 11초 | 1틱 |
  > | 000660 | 2.7~5.6% | 2.7~5.5% | 6.0~11.4% | 0.9~1.3초 / 5~6초 / 43초 | 보통 1틱, 최대 4틱 |
  > | 009150 | 6.1~8.5% | 6.0~8.0% | 10.7~13.1% | 1.0~1.9초 / 5~10초 / 98초 | 보통 1틱, 최대 4틱 |
  >
  > - 범위는 날마다의 값이다. 장 시작 때만이 아니라 하루 내내 나왔다(09:00, 09:30, 10:00, 11:00, 14:00 칸 등).
  > - 봉 마감 직전의 마지막 호가가 뒤집혀 있던 봉 마감은 000660 하루 9~18개, 009150 20~29개였다(376개 중). 그때 기준가가 쓴 유효 호가의 나이는 중앙 1.7~5.5초, 최대 59초였다.
  > - 원인(어느 거래소의 호가가 겹쳤는지, 실제로 거래할 수 있던 가격인지, 단일가였는지)과 처리 결정은 #187에서 본다. `krx-quant diagnose`의 9절이 같은 값을 잰다([operations.md](operations.md#진단-82)). 지금의 처리는 [engine-api.md](engine-api.md#기준가-통합-중간가)의 "기준가"에 있다.

  > **바뀜** (2026-10-17, #260): "원인(어느 거래소의 호가가 겹쳤는지, 실제로 거래할 수 있던 가격인지, 단일가였는지)과 처리 결정은 #187에서 본다" → 원인을 쟀고 처리를 정했다. 뒤집힘은 모두 두 거래소의 호가가 겹친 것이었고, 그 가격에 실제로 체결이 났다. 그래서 닿음(도달 정답, 장벽 정답)은 뒤집힌 호가의 매수·매도 1호가를 각각 실제 거래 가격으로 센다. 기준가, 호가 피처, 변동폭 정답은 지금처럼 버린다(결정 (b), [engine-api.md](engine-api.md#기준가-통합-중간가)). 이유: 예약 작업 `diagnose-203-187`이 2026-10-16 밤에 `krx-quant diagnose` 9절로 녹화 5거래일(2026-10-12 ~ 16, 연속매매)을 재 #187에 남긴 값이 아래와 같았고, 사용자가 그것을 보고 (b)를 골랐다(2026-10-17).
  >
  > | 종목 | 통합 호가 중 뒤집힘 | 뒤집힌 시간 비율 | 통합 체결에 실린 1호가 중 뒤집힘 | 뒤집힘 수 | 한 번 뒤집혀 있는 시간 p50 / p90 / p99 / 최대 | 깊이 p50 / p99 / 최대 |
  > |---|---|---|---|---|---|---|
  > | 005930 | 0.0~0.1% | 0.0~0.1% | 0.0~0.6% | 8 | 1.9 / 7.1 / 7.1 / 10.6초 | 1 / 1 / 1틱 |
  > | 000660 | 2.6~5.6% | 2.6~5.5% | 5.5~11.4% | 2,235 | 0.9 / 5.0 / 14.4 / 43.5초 | 1 / 2 / 11틱 |
  > | 009150 | 4.4~8.5% | 4.3~8.0% | 8.6~13.1% | 2,484 | 1.1 / 6.3 / 20.9 / 98.1초 | 1 / 2 / 4틱 |
  >
  > - 거래소 귀속(뒤집힘 수의 비율, 괄호는 뒤집힌 시간의 비율): 000660은 KRX 매수 > NXT 매도 44.4%(37.4%), NXT 매수 > KRX 매도 47.0%(55.5%), 009150은 34.7%(31.8%)와 53.6%(59.3%), 005930은 8번 모두 NXT 매수 > KRX 매도였다. 한 거래소 안의 뒤집힘(KRX 자체, NXT 자체)은 세 종목 모두 0%였다. 나머지는 섞임(000660 6.4%, 009150 7.7%), 통합과 맞지 않음(1.7%, 1.5%), 거래소별 체결 없음(0.4%, 2.5%)이다.
  > - 뒤집힌 가격에 체결: 높은 매수호가 쪽 거래소에서 그 매수호가에 체결이 난 비율은 79.0~93.3%, 낮은 매도호가 쪽 거래소에서 그 매도호가에 난 비율은 76.5~96.6%였다. 000660은 KRX 매수 > NXT 매도 993번 중 93.3%·88.4%(둘 다 83.1%), NXT 매수 > KRX 매도 1,050번 중 87.2%·96.6%(84.6%). 009150은 861번 중 91.3%·76.5%(69.2%), 1,332번 중 79.0%·93.4%(73.8%). 005930은 8번 모두 100%다.
  > - VI와 겹친 뒤집힘은 없었다(그 날들에 VI가 없었다). 통합 호가의 시간 구분 코드는 뒤집힌 호가의 99.8~100%가 `0`이었고, 거래소별 체결의 장운영/시간 구분 코드는 뒤집힌 동안에도 100% `20/0`이었다(그 밖과 같다). 단일가였던 흔적은 없다.
  > - 60초 넘게 뒤집힌 것은 009150의 2026-10-14 13:41:00부터 98.1초 한 번이었다. 봉 마감 직전 호가가 뒤집힌 봉 마감은 000660이 1,880개 중 56개(날마다 5~19), 009150이 103개(15~28), 005930이 0개였다. 그때 기준가가 쓴 유효 호가의 나이는 p50 2.1~2.8초, 최대 59.3초였다.
- 체결 시각 대비 수신 지연은 중앙값 약 0.7초였다. 체결 시각이 초 단위라서 최대 1초의 절삭이 포함된 값이다.

KIS 필드 순서는 [koreainvestment/open-trading-api](https://github.com/koreainvestment/open-trading-api)의 `examples_llm/domestic_stock/ccnl_total`, `asking_price_total` 예제를 따른다.

## 경제 캘린더 (#150)

수집기가 TradingView 경제 캘린더를 읽을 때마다(기본 15분) 새로 나타나거나 바뀐 일정만 쓴다(`econ_calendar.py`, [design.md](design.md#45-매크로-지표)의 "경제 캘린더"). 바뀌었다는 것은 발표 시각, 이름, 기간, 실제·예상·이전 값, 중요도 중 하나가 달라졌다는 뜻이다. 그래서 한 일정의 줄들을 `recorded_at` 순으로 보면 그때그때 알던 값이 나온다(실제 값은 발표 뒤 처음 읽을 때 생긴다). 수집기가 다시 시작하면 `calendar/latest.json`의 마지막 답에서 이어서 비교한다. 바뀐 일정을 이 계층에 먼저 쓰고 `latest.json`을 나중에 바꾼다. 그래서 그 사이에 멈추면 다시 시작한 뒤 같은 변화가 한 번 더 기록될 수는 있어도(적어도 한 번), 빠지지는 않는다.

**`calendar/date=<읽은 날, KST>/feed=tradingview/symbol=<국가>/`**: 일정 하나의 새 값마다 한 줄.

| 열 | 형식 | 내용 |
|---|---|---|
| `recorded_at` | timestamp (UTC) | 읽은 시각 |
| `id` | string | 출처의 일정 id |
| `at` | timestamp (UTC) | 발표 시각 |
| `country` | string | 두 글자 국가(`US`, `KR`, `EU` 등) |
| `title`, `indicator`, `category`, `period` | string | 일정 이름, 지표, 출처의 분류 코드, 기준 기간(`Sep` 등). 없으면 null |
| `actual`, `forecast`, `previous` | float64 | 실제(발표 전 null), 예상, 이전 값. `unit` 단위 |
| `unit`, `scale`, `currency` | string | 단위(`%`, `$`, `CNY` 등), 배율(`K`, `M`, `B`, `T`: 값은 `<숫자><배율>`), 통화. 없으면 null |
| `importance` | string | `low`, `medium`, `high` |
| `source`, `source_url` | string | 발표 기관과 그 주소 |

`calendar/latest.json`은 마지막 답 전체다(`fetched_at`, `source`, `countries`, `events`). 계층이 아니어서 보관(S3)은 올리지 않는다.

## 매크로 지표와 일일 국면 (#8)

Macro Collector가 거래일 07:30에 쓴다(`macro.py`, [design.md](design.md#45-매크로-지표)). `macro` 계층이다. 같은 날 다시 읽으면(재시도, 재시작, `krx-quant macro`) 줄이 더해진다. `recorded_at`이 가장 늦은 줄이 최신이다.

**`macro/date=<날짜>/feed=readings/symbol=<지표>/`**: 지표 하나에 한 줄.

| 열 | 형식 | 내용 |
|---|---|---|
| `day` | date | 그 국면을 쓰는 KRX 거래일 |
| `recorded_at` | timestamp (UTC) | 기록한 시각 |
| `indicator`, `source`, `series` | string | 지표 이름(`usdkrw` 등), 출처(`ecos`, `fred`, `yahoo`, `kis`), 출처의 코드 |
| `as_of` | date | 값의 날짜(출처의 달력). 없으면 null |
| `value` | float64 | 그날 값. 없으면(7일 넘게 묵었거나 출처에 없음) null |
| `change_pct` | float64 | KRX 전 거래일 아침에 알던 값 대비 %. 둘 중 하나가 없으면 null |

**`macro/date=<날짜>/feed=regime/symbol=market/`**: 한 번 읽을 때마다 한 줄.

| 열 | 형식 | 내용 |
|---|---|---|
| `day`, `recorded_at` | date, timestamp (UTC) | 위와 같다 |
| `regime` | string | `shock`, `risk_off`, `risk_on`, `neutral` |
| `reasons` | string (JSON 목록) | 맞은 조건, 한국어. 예: `["SOX -1.5% (-1% 이하)"]` |
| `failed` | string (JSON 객체) | 이번에 실패한 출처와 이유 |
| `skipped` | string (JSON 목록) | 키가 없어 건너뛴 출처 |
