# 녹화 데이터 형식

수집기는 KIS에서 받은 메시지를 두 계층으로 녹화한다.

| 계층 | 내용 | 용도 |
|---|---|---|
| `raw` | 받은 메시지 원문 그대로 | 정규화 로직이 바뀌어도 이벤트를 다시 만들 수 있게 한다 |
| `events` | 피드별로 정규화한 이벤트 | 피처 계산, 재생, 학습의 입력 |

## 저장 위치

```
<KRX_DATA_DIR>/<계층>/date=<YYYY-MM-DD>/feed=<피드>/symbol=<종목>/part-<HHMMSS>-<id>.parquet
```

- `date`는 메시지를 받은 시각의 한국 날짜다.
- `feed`는 KIS TR ID다(예: `H0UNCNT0`). 피드 목록과 키는 [kis-feeds.md](kis-feeds.md)에 있다. KIS 제어 메시지(구독 응답, PINGPONG)는 `raw` 계층의 `feed=_system`에 남는다. 형식을 알아볼 수 없는 메시지는 `raw` 계층의 `feed=_unparsed`에 남는다.
- 메시지는 항상 `raw`에 먼저 기록된다. 정규화에 실패한 메시지는 `raw`에만 남고, 수집은 멈추지 않는다.
- 한 파티션은 행이 `KRX_FLUSH_ROWS`만큼 쌓이거나 `KRX_FLUSH_SECONDS`가 지나면 새 part 파일로 쓰인다. 종료할 때도 남은 행을 쓴다.
- part 파일은 임시 이름으로 쓴 뒤 이름을 바꾼다. 그래서 프로세스가 중간에 죽어도 반쯤 쓰인 part 파일은 남지 않는다. fsync는 하지 않으므로 전원이 꺼지는 경우까지 보장하지는 않는다.
- 장중에는 파티션마다 part 파일이 여러 개 생긴다. 하루치를 파티션당 파일 하나로 합치는 작업은 S3 업로드(#6)에서 한다.
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
| `venue` | string | | 피드별로 `UN`(통합, 체결별 거래소 정보 없음), `KRX`, `NXT` |
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

`symbol`은 업종코드다. REST 형식은 시각을 주지 않아 `event_time`에 수신 시각을 쓴다.

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
| `qty`, `cum_volume` | int64 | `last_cnqn`, `acml_vol` | 체결량, 누적 거래량 |
| `open_interest` | int64 | `hts_otst_stpl_qty` | 미결제약정 |
| `theory_price`, `market_basis` | float64 | `hts_thpr`, `mrkt_basis` | 이론가, 시장 베이시스 |
| `ask1`, `bid1`, `ask_qty1`, `bid_qty1` | | `futs_askp1` … | 최우선 호가와 잔량 |
| `total_ask_qty`, `total_bid_qty` | int64 | | 총 호가 잔량 |

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

## events: KOSPI200 옵션 (`H0IOCNT0`)

| 열 | 타입 | KIS 필드 | 설명 |
|---|---|---|---|
| `symbol` | string | `optn_shrn_iscd` | 옵션 코드 |
| `event_time`, `received_at` | | `bsop_hour` | 시각, 수신 시각 |
| `price`, `theory_price`, `ask1`, `bid1` | float64 | `optn_prpr`, `hts_thpr`, … | 체결가, 이론가, 최우선 호가 |
| `qty`, `cum_volume`, `open_interest` | int64 | `last_cnqn`, `acml_vol`, `hts_otst_stpl_qty` | 체결량, 누적 거래량, 미결제약정 |
| `implied_vol` | float64 | `hts_ints_vltl` | 내재변동성(%) |
| `delta`, `gamma`, `vega`, `theta` | float64 | `delta`, `gama`, `vega`, `theta` | 그릭스 |

## events: REST 스냅샷

| 피드 | 열 |
|---|---|
| `FHKST01010100` 시세(체결 대체) | `symbol`, `received_at`, `price`, `cum_volume`, `change_pct` |
| `FHKST01010200` 호가 스냅샷(호가 대체) | `symbol`, `event_time`(`aspr_acpt_hour`), `received_at`, `price`, `ask_px_1..10`, `bid_px_1..10`, `ask_qty_1..10`, `bid_qty_1..10`, `total_ask_qty`, `total_bid_qty`, `vi_cls_code` |
| `HHPTJ04160200` 추정 가집계 | `symbol`, `received_at`, `slot`(`bsop_hour_gb`), `foreign_net_qty`, `institution_net_qty`, `total_net_qty` |

REST 응답 원문은 raw 계층에 `{"tr_id", "params", "body"}` JSON으로 남는다.

## 실데이터 관찰 (2026-10-08, 삼성전자, 13시 전후 90초)

- 체결 1,714건, 호가 409건을 받았다. 필드 수는 모든 메시지에서 문서와 같았다(체결 47개, 호가 66개).
- `MARKET_CLS_CODE`는 모든 체결에서 `2`였다. 통합 피드만으로는 체결별 거래소를 알 수 없다. 거래소별 체결이 필요하면 KRX 전용(`H0STCNT0`)과 NXT 전용(`H0NXCNT0`) 피드를 따로 구독해야 한다.
- 체결의 약 14%가 호가단위의 절반 가격(예: 263,250원, 호가단위 500원)에서 났다. 중간가 주문 체결로 보인다. 가격을 호가단위로 검증하는 코드는 이 경우를 허용해야 한다.
- 통합 호가의 약 29%가 최우선 매도호가와 매수호가가 같았다(locked). KRX와 NXT의 최우선 호가가 맞물린 상태로 보인다. 뒤집힌(crossed) 경우는 없었다. 통합 중간가 계산과 스프레드 피처는 스프레드 0을 정상 값으로 다뤄야 한다.
- 체결 시각 대비 수신 지연은 중앙값 약 0.7초였다. 체결 시각이 초 단위라서 최대 1초의 절삭이 포함된 값이다.

KIS 필드 순서는 [koreainvestment/open-trading-api](https://github.com/koreainvestment/open-trading-api)의 `examples_llm/domestic_stock/ccnl_total`, `asking_price_total` 예제를 따른다.
