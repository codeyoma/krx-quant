# Fincept 표시 환율 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** KRW를 고르면 출처 통화가 확인된 금액을 실제 원화 금액으로 환산한다.

**Architecture:** 원본 시세·주문·저장 값은 유지하고 화면용 값만 환산한다. Qt Core의 순수 환율 모델을 기존 CurrencyManager가 소유하며, 별도 인증 없는 HTTPS 연결로 ECB 일일 자료를 읽고 SQLite settings에 검증된 사본을 저장한다. 각 화면은 원본에서 다시 그려 중복 환산을 피한다.

**Tech Stack:** C++20, Qt 6.8.3 Core/Network/Widgets, Qt Test, 기존 Python yfinance 어댑터.

**Spec:** https://github.com/codeyoma/krx-quant/issues/142

## Global Constraints

- PR은 사용자 검토를 위해 열린 상태로 둔다. Merge·release는 하지 않는다.
- ECB EUR 기준환율의 교차 비율을 쓴다. 기준일, 출처, 갱신 상태를 설정에 표시한다.
- 7일을 넘은 자료, 미래 기준일, 손상 자료, 미지원 통화는 환산하지 않는다. 원 통화·금액을 유지한다.
- GBp/GBX는 GBP의 1/100이다. USDT/USDC를 USD로 가정하지 않는다.
- 지수·환율쌍·비율·수량은 환산하지 않는다. 주문 입력과 분석 계산의 단위는 바꾸지 않는다.
- 공개 fork에는 화면 코드와 공개 환율 처리만 둔다. 부모 저장소에는 설계 문서와 submodule 포인터를 둔다.

## Review Focus

- 오래된 시세 캐시에 통화 메타데이터가 없어도 다시 조회하고 화면을 갱신해야 한다.
- 혼합 통화 포트폴리오 합계를 단일 통화로 간주하지 않아야 한다.
- 재시작·통화 전환·환율 응답 순서가 달라도 원본에 한 번만 적용해야 한다.
- 과거 스냅샷에 통화 증거가 없으므로 원 차트를 유지하고 이를 표시한다. 역사적 수익률을 다시 계산하지 않는다.
- 오류/만료가 새 유효 캐시를 덮어쓰거나 잘못된 원화 기호를 만들지 않아야 한다.

## Task 1: 환율 모델과 설정

**Files:** fork의 `fincept-qt/src/core/currency/fx/FxRates.{h,cpp}`, `CurrencyManager.{h,cpp}`, `Currency.h`, `screens/settings/GeneralSection.{h,cpp}`, `tests/tst_fx_rates.cpp`.

**Interfaces:** `currency::DisplayAmount { value, currency, available, converted, rate_date }`; `CurrencyManager::display_amount(double,const QString&) const`; `cur::display_money(double,const QString&,bool compact=false,int precision=-1)`; `CurrencyManager::display_changed()`.

- [x] XML/캐시 검증, USD 100 × 1500/1.2 = KRW 125000, 음수·역변환·GBp·미지원·만료·미래·잘못된 값 테스트를 먼저 실행한다.
- [x] 순수 모델을 구현하고 해당 테스트를 통과시킨다.
- [x] 전용 QNetworkAccessManager로 20초 timeout, 최대 256 KiB, HTTPS ECB 한 곳에서만 조회한다. 시작과 6시간마다 갱신하고 수동 갱신을 제공한다. 1분마다 만료를 확인한다.
- [x] `general.fx_rates_v1` JSON 캐시와 `general.currency`를 읽고 저장한다. 실패 시 유효한 기존 캐시를 유지한다.
- [x] 설정의 환산 설명·출처·날짜·오류 상태와 한국어 번역을 갱신한다. 독립된 임시 DB로 재시작·전환을 검증한다.

## Task 2: 시장 화면

**Files:** `services/markets/MarketDataService.*`, `scripts/yfinance_data.py`, `screens/markets/`, `screens/watchlist/`, `screens/dashboard/` (포트폴리오 위젯 제외).

**Interfaces:** `currency_code(symbol)`, `is_monetary(symbol)`, `display_price(symbol,amount,precision=-1,compact=false)`, `metadata_changed()`, `display_changed()`; QuoteData의 `currency`와 `quote_type`.

- [x] 어댑터에서 통화·유형을 받아 모든 캐시 경로에 보존한다. 기존 이름만 있는 캐시도 메타데이터를 다시 읽는다.
- [x] 원본 캐시로 시세와 대시보드를 다시 그린다. 숫자 정렬은 표시 단위에 맞추고 수량·등락률은 유지한다.
- [x] INDEX/CURRENCY/YIELD와 미지원 통화가 잘못 환산되지 않는 어댑터 테스트를 실행한다.

## Task 3: 포트폴리오와 주식 분석

**Files:** `screens/portfolio/`, `dashboard/widgets/PortfolioSummaryWidget.*`, `screens/equity_research/`, `services/equity/`, `EquityChartPanel.*`, `EquityTradingScreen_Streams.cpp`.

- [x] 확인된 통화만 화면용 사본으로 환산한다. 혼합/미확인 합계는 환산 불가를 드러낸다.
- [x] 주요 카드·표·차트의 금액 좌표와 라벨을 일치시킨다. 입력 단위가 필요한 고급 분석/주문 화면은 명시한 원 통화를 유지한다.
- [x] 원본 불변과 동일 통화/혼합 통화 포트폴리오를 순수 테스트로 검증한다. 표의 최신 시세도 원본 캐시에 반영한다.
- [x] 주식 분석은 가격 통화와 재무제표 보고 통화를 구별한다. 원화 환산이 불가능하면 원 단위를 유지한다.

## Task 4: 통합 검증과 PR

- [x] USD 결제 기록은 출처를 명시해 환산하고 실제 청구 통화 USD를 함께 보인다.
- [x] 전체 앱 빌드, CTest, 격리 프로필 selftest/smoke, 실제 ECB 조회를 실행한다.
- [x] 변경 파일과 오류 경계를 독립 검토하고 발견된 결함을 수정한다.
- [x] fork 운영 문서와 부모 설계에 적용 범위·저장 키·바뀜 기록을 남긴다.
- [x] fork topic을 push하고 열린 PR을 만든다. 부모 포인터는 원격에 있는 해당 commit으로 갱신하여 종속 PR을 만든다. 두 PR 모두 첨부하고 merge하지 않는다.

## 검증 기록 (2026-10-13)

- macOS arm64 Qt 6.8.3 전체 앱 빌드, CTest 14개, Python 시세 메타데이터 4개 통과.
- 격리된 한국어/KRW 프로필에서 앱 selftest 11개와 화면 생성 53개 통과.
- 독립된 SQLite 설정 harness에서 선택·재시작·캐시·만료·네트워크 실패·실제 ECB 조회 통과. 실제 응답 기준일은 2026-10-12이며 100 USD를 약 133,309 KRW로 표시했다.
- 실제 BaseWidget을 쓰는 재현 가능한 `custom/tools/fx-ui-check`의 15개 검사 통과. 이전 callback을 복원한 음성 대조에서는 대기 중인 시세 손실이 재현됐다.
- 독립 리뷰의 5개 지적(브로커 통화 추정, 대기 중인 시세 손실, 미확인 종목 유형, 선물 포인트 환산, 혼합 통화 합계)을 수정하고 재검토했다.
- clang-format 18 변경 줄 검사와 버전 일치 검사 통과. Architecture ratchet는 기준 브랜치부터 5,224/5,200 stylesheet 호출로 실패하며, 이번 변경 뒤 세 카운터 모두 기준 브랜치와 같다(5,224 / 460 / 120). 임계값을 올리지 않았다.
- fork의 자동 PR 빌드·lint·architecture workflow는 이미 disabled_manually 상태다. 이 작업에서 켜거나 release/tag를 만들지 않았다. 로컬 성공은 Windows/Linux의 hosted CI 성공을 뜻하지 않는다.

공개 구현 PR: https://github.com/codeyoma/FinceptTerminal/pull/26 (열린 상태). 부모 PR은 이 commit을 가리키며 공개 PR 검토 뒤 반영한다.
