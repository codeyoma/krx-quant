# 공시·기업정보 기반 단기 신호 (노트 14)

*기준일 2026-10-17. 조사 자료이며 투자 조언이 아니다. 앞 보고서(`research/reports/단기 매매 퀀트 기법 조사.md`)가 이미 다룬 것(대형주 PEAD 소멸 [Martineau 2022], 지수 편입 효과 축소, LLM 뉴스 감성의 한계 [Lopez-Lira & Tang], FINSABER)은 반복하지 않는다.*

*조사 방법 메모: WebSearch 16회를 썼고 끝까지 작동했다. 나머지는 WebFetch, `gh api`(KIS 공식 예제 저장소), `curl`(DART·KIND RSS)로 확인했다. 한국 논문 PDF(koreascience, 증권사 PDF, KRX 밸류업 백서)는 이 환경에서 본문을 추출하지 못했다. 그래서 초록·서지 정보만 썼다. "(검색 요약)"은 검색 결과 요약에서 나온 것으로, 원문을 직접 열어 보지 못한 항목이다.*

## 1. 한국 공시·기업정보 출처: 무엇을, 얼마나 빨리, 어떤 조건으로 받나

### Takeaway
실시간에 가까운 경로는 KIS `news_title` API(공시 제목과 작성 시각을 줌)와 KIND·DART RSS 폴링이다. OpenDART는 구조화 상세(자사주 결정, 유상증자, 지분 보고)와 과거 이력용이다. 응답에 시각이 없어서 장중 타이밍에는 쓸 수 없다. 이 노트에서 초 단위 지연을 직접 잰 공식 수치는 찾지 못했다.

### Cited Findings
- **OpenDART 공시검색** `list.json`은 GET 방식이다. 인자는 `corp_code`, `bgn_de`/`end_de`(YYYYMMDD), `pblntf_ty`(A~J), `pblntf_detail_ty`, `last_reprt_at`, `page_count`(최대 100)다. `corp_code`를 빼면 검색 기간이 3개월로 제한된다. **응답에는 시각이 없고 날짜만 있다**(`rcept_dt`). 오류 `020`(요청 제한 초과)은 "일반적으로 20,000건 이상"에서 나고, 키마다 다를 수 있다 — [OpenDART 공시검색 가이드](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001)
- 상세 유형 코드: D001 대량보유상황보고, D002 임원·주요주주 소유보고, **D005 임원·주요주주 거래계획보고서**, E001 자기주식 취득·처분, I001 수시공시, I002 공정공시, I003 시장조치·안내 — [같은 페이지](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001)
- OpenDART API 그룹은 6개다(공시정보, 정기보고서 주요정보, 정기보고서 재무정보, 지분공시 종합정보, 주요사항보고서 주요정보, 증권신고서 주요정보). 이 소개 페이지에는 호출 한도가 적혀 있지 않다 — [OpenDART 소개](https://opendart.fss.or.kr/intro/main.do)
- 지분공시(DS004) API는 2개다(대량보유 상황보고, 임원·주요주주 소유보고) — [DS004](https://opendart.fss.or.kr/guide/main.do?apiGrpCd=DS004). 임원·주요주주 소유보고 응답에는 **거래일도 변동 사유도 없다**. 접수일(`rcept_dt`)과 변동 주식 수·비율(`sp_stock_lmp_irds_cnt`, `sp_stock_lmp_irds_rate`)만 있다 — [DS004 상세](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS004&apiId=2019022)
- 주요사항보고서(DS005) API는 36개다. 이 노트와 관련된 것은 유상증자 결정(2020023), 전환사채권 발행결정(2020033), 자기주식 취득 결정(2020038), 처분 결정(2020039), 자기주식취득 신탁계약 체결(2020040), 회사합병 결정(2020050)이다 — [DS005](https://opendart.fss.or.kr/guide/main.do?apiGrpCd=DS005)
- **OpenDART 약관**: 서비스는 "원칙적으로 무료"이고, 일부는 사전 공지 뒤 유료화될 수 있다(제11조). 1인 1ID·1키이고, 키를 남에게 쓰게 할 수 없다(제19조). 호출 허용량은 홈페이지에 게시한다(제10조). 금감원은 정확성·완전성을 보장하지 않는다(제23조). 상업적 이용과 재배포는 약관에 명시되어 있지 않다 — [OpenDART 이용약관](https://opendart.fss.or.kr/intro/terms.do)
- **DART RSS** `https://dart.fss.or.kr/api/todayRSS.xml`가 있다(채널명 "DART : 최근공시"). 2026-10-17(토)에 받아 보니 항목이 비어 있었다. 그래서 항목에 시각이 붙는지는 확인하지 못했다 — [DART RSS](https://dart.fss.or.kr/api/todayRSS.xml)
- **KIND 오늘의공시**: 시장별 필터, EXCEL, RSS를 제공한다. RSS 주소 형식은 `rsstodaydistribute.do?method=searchRssTodayDistribute&repIsuSrtCd=<종목>&mktTpCd=<시장>`이다. 토요일이라 비어 있었다 — [KIND 오늘의공시](https://kind.krx.co.kr/disclosure/todaydisclosure.do?method=searchTodayDisclosureMain), [KIND RSS](https://kind.krx.co.kr/disclosure/rsstodaydistribute.do?method=searchRssTodayDistribute&repIsuSrtCd=&mktTpCd=0&searchCorpName=&currentPageSize=15)
- **KIS Open API `news_title`** "종합 시황/공시(제목) [국내주식-141]": `GET /uapi/domestic-stock/v1/quotations/news-title`, tr_id `FHKST01011800`. 종목코드로 거를 수 있다. 응답 필드는 `data_dt`(작성일자), `data_tm`(작성시간), `hts_pbnt_titl_cntt`(HTS 공시 제목), `dorg`(자료원), `iscd1~5`(관련 종목)다 — [KIS 예제 news_title.py](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/news_title/news_title.py), [chk_news_title.py](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/news_title/chk_news_title.py)
- KIS 공식 예제에는 이 밖에도 `invest_opinion`(종목투자의견 [국내주식-188]), `estimate_perform`(종목추정실적 [국내주식-187]), `ksdinfo_*`(예탁원 배당, 유상증자, 무상증자, 감자, 합병·분할, 주주총회 일정 등), `daily_short_sale`, `daily_loan_trans`(대차), `credit_balance`, `inquire_vi_status`(VI 발동 현황)가 있다 — [KIS 예제 목록](https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock)
- **KRX 공시 운영시간은 07:30~18:00**이다. 2026-09-03 KRX는 애프터마켓(16:00~20:00) 개장을 앞두고 시행세칙 개정을 예고했다. 운영시간 뒤 부도, 감사의견 거절, 횡령·배임 같은 중대 사실이 보도되면 애프터마켓 매매를 정지할 수 있게 하는 내용이다 — [SBS Biz 2026-09-04](https://biz.sbs.co.kr/amp/article/20000332802)
- **장중 중요 공시는 매매거래정지로 이어질 수 있다**(유가증권시장 공시규정 제40조). 신세계푸드는 "주식소각 10% 이상" 공시로 2026-07-16 14:28~14:58, **30분** 정지됐다. 해당 종목의 선물·옵션도 함께 정지된다. 다만 기사는 30분을 모든 정지의 고정 기간으로 제시하지 않는다 — [IB토마토 2026-07-16](https://newstomato.com/ReadNews.aspx?no=1307421)
- **KRX Data Marketplace Open API**는 지수, 주식, 증권상품, 채권, 파생, 일반상품, ESG 7개 영역을 다룬다. 메인 페이지에는 갱신 주기와 호출 한도가 없다. "Open API 미제공 데이터 안내" 공지가 따로 있다 — [KRX Open API](https://openapi.krx.co.kr/contents/OPP/MAIN/main/index.cmd)
- **공매도 정보**: KRX는 "공매도 데일리 브리프"를 매일 낸다. 정보데이터시스템은 종목별 공매도 현황, 거래, 순보유잔고, 과열종목을 제공하지만 **실시간 반영이 아니다**. 2024년 12월부터 발행량의 0.01% 또는 10억원 이상의 잔고는 공시 의무가 있다. 공매도는 2025-03-31 전면 재개됐다 — [아주경제 2025-04-01](https://www.ajunews.com/view/20250331163839226). 그 전의 공시 문턱이 0.5%인지 5%인지는 검색 결과의 기사들끼리 표기가 엇갈렸다(검색 요약, 미확인).

### Inferences
- 장중 "공시가 떴다"를 가장 싸게 아는 길은 이미 쓰는 KIS 인증으로 `news_title`을 3종목 코드로 폴링하는 것이다. 공시 제목과 `data_tm`이 같이 오기 때문이다. KIND RSS는 백업이고, OpenDART는 이벤트 뒤 상세(금액, 기간, 비율)와 과거 이벤트 표를 만드는 데 쓴다.
- OpenDART `rcept_no` 앞 8자리는 날짜일 뿐이다. 과거 공시의 **장중 시각**을 복원하려면 KIS `news_title`의 과거 조회 범위(미확인)나 KIND 화면 기록이 필요하다. 이벤트 스터디로 "장중 vs 장후"를 가르려면 이 시각 데이터를 오늘부터 쌓아야 한다.
- 키(OpenDART `crtfc_key`, KIS app key)는 서버 `.env`에만 둔다. 저장소가 공개이고, OpenDART 약관은 키 공유도 금지한다.

### Gaps
- KIND RSS와 DART RSS의 실제 지연(공시 공개 → RSS 반영)과 항목의 시각 필드는 주말이라 확인하지 못했다. 평일에 `curl`로 한 번 받아 보면 된다.
- KIS `news_title`의 갱신 지연, 과거 조회 가능 기간, 호출 한도는 공식 문서로 확인하지 못했다.
- KRX 정보데이터시스템(data.krx.co.kr)과 공매도 종합 포털의 자동 수집 허용 여부(약관)와 잔고 공시 시차(T+2 등)는 1차 출처로 확인하지 못했다.
- 기업 IR 페이지(삼성전자, SK하이닉스, 삼성전기)의 실적 발표 일정 표는 열어 보지 못했다.

## 2. 단기 가격 효과가 있는 이벤트와 근거 (한국 먼저)

### Takeaway
세 종목에서 가격을 크게 움직인 공시성 이벤트는 실적(잠정실적·확정실적)과 대규모 자사주 매입·소각이다. 한국 학술 근거는 대부분 2000년대 소형주 포함 표본의 초록 수준이다. 이 대형 3종목에 맞는 정량 근거는 사례(뉴스)뿐이다. 내부자, 5% 보고, 공매도 잔고, 밸류업은 근거가 약하거나 효과가 주·월 단위다.

### Cited Findings

| 이벤트 | 한국 근거 | 반응 속도·크기 | 출처 |
|---|---|---|---|
| **잠정실적**(삼성전자는 분기 종료 다음 달 초, 개장 전) | 2024 3Q 잠정은 2024-10-08 **개장 전** 발표. 영업이익 9.1조원으로 기대 하회(회사도 인정). 종가 −1.15%(60,300원). 장 전 59,900원까지 내렸다가 회복 | 당일, 기대 하회인데도 낙폭 작음 | [뉴시스 2024-10-08](https://mobile.newsis.com/view/NISX20241008_0002913127) |
| 〃 | 2026 2Q 잠정은 2026-07-07 예정. 컨센서스 영업이익 84조5,807억원(연합인포맥스). 전날 +2.75%(318,000원) | 발표 전 기대 선반영 | [세계일보 2026-07-06](https://www.segye.com/newsView/20260706513870) |
| 〃 (학술) | 김형순(2018): 시계열 기준 서프라이즈와 주총소집공고일을 쓰면 PEAD가 있다. **애널리스트 예측 기준 서프라이즈로는 어느 공시일에서도 PEAD가 없다** | 다일 drift는 측정 방식에 좌우됨 | [koreascience](https://koreascience.or.kr/article/JAKO201823965854148.page) |
| **자사주 매입·소각** | 삼성전자 10조원 매입(그중 3조원은 3개월 안에 매입 후 소각)을 2024-11-15 **장 마감 뒤** 이사회 의결·공시. 다음 거래일 11-18 종가 **+5.98%**(56,700원), 장 초반 +7.48%(57,500원). 이틀 연속 상승 | 다음 날 시가 갭 + 장중 | [메트로신문 2024-11-18](https://www.metroseoul.co.kr/article/20241118500665) |
| 〃 (학술) | 김수정·이윤구·설원식(2005, 2001.4~2003.12): 이익소각 공시와 일반 자사주 매입 공시 모두 공시일 전후로 유의한 양의 초과수익. 소각 쪽 CAR이 더 높았지만 유의하지 않았고, 2003년만 10% 수준에서 유의 | 공시일 전후 | [숙명 ScholarWorks](https://scholarworks.sookmyung.ac.kr/handle/2020.sw.sookmyung/9267?mode=full) |
| 〃 | 설원식·김수정(2005): 주가안정·소각 목적 취득은 양(+), 임직원 인센티브 목적은 음(−)의 초과수익(검색 요약) | — | [숙명 ScholarWorks](https://scholarworks.sookmyung.ac.kr/handle/2020.sw.sookmyung/9218?mode=full) |
| **임원·주요주주 매매** | 고혁진(2009): 매매 뒤 20일 초과수익은 **임원과 기관**이 다른 집단보다 컸다. 초록에 수치 없음 | 20일 | [earticle](https://m.earticle.net/Article/A158680) |
| **밸류업 공시** | 2024-05~2025-04 기간 147개사 215건(예고공시 제외). 공시 뒤 **1~2개월** 업종 대비 초과수익이 났지만 6개월이면 일부는 업종보다 못했다. 정량 목표(TSR, 총주주환원율)와 시점을 밝힌 기업, 후속 이행공시를 낸 기업은 초과성과가 이어졌다(대신경제연구소 우동조) | 주~월 | [이코노미스트](https://economist.co.kr/article/view/ecn202505270043) |
| 〃 | KRX 백서: 2024-05~2025-03 기간 125사, 코스피 시총의 약 46%(검색 요약, PDF 미열람) | — | [KRX 밸류업 백서](https://kind.krx.co.kr/external/dst/valueupReference/11359/%ED%95%9C%EA%B5%AD%EA%B1%B0%EB%9E%98%EC%86%8C%20%EA%B8%B0%EC%97%85%20%EB%B0%B8%EB%A5%98%EC%97%85%20%ED%94%84%EB%A1%9C%EA%B7%B8%EB%9E%A8%20%EB%B0%B1%EC%84%9C.pdf) |
| **유상증자** | 유상증자설이 기관 메신저로 돈 뒤 금호산업이 3거래일 −16.9%(2007, 검색 요약). 금융위는 2016년, 공시일부터 발행가 결정일 사이에 공매도한 자의 유상증자 참여를 제한했다(검색 요약) | 공시 **전** 선반영 | [한국경제 2007](https://www.hankyung.com/amp/2007120457961), [금융위](https://www.fsc.go.kr/no010101/72431?curPage=25) |
| **풍문 → 회사 해명** | 2026-06 삼성전자가 최대 90조원 자사주 매입을 검토한다는 국내 보도가 나왔다. 회사는 "시기와 규모는 결정되지 않았다"며 한 달 안에 다시 알리겠다고 했다 | 보도 시점에 반응 | [TheNextWeb 2026-06-25](https://thenextweb.com/news/south-korea-chip-shares-rally-micron-earnings) |
| **글로벌: 실적발표 premium** | 발표가 있는 달의 수익률이 높고, 거래량 급증·소액투자자 매수와 관련된다(Frazzini & Lamont). 20개국 중 9개국에서 유의한 양의 premium이 있었다(Barber et al. 2013). 한국 포함 여부는 미확인 | 발표 주변 수일 | [NBER w13090](https://www.nber.org/papers/w13090), [Barber et al. 2013](https://ideas.repec.org/a/eee/jfinec/v108y2013i1p118-138.html) |

### Inferences
- 세 종목은 시총이 커서 임원 매매나 5% 보고 한 건이 수급에 미치는 비중이 작다. 소형주 표본에서 나온 내부자·유상증자 효과를 그대로 옮기기 어렵다. 효과가 큰 쪽은 **실적 이벤트**와 **대규모 주주환원**(10조원 매입, 소각 결정)이다.
- 삼성전자 잠정실적 사례(기대 하회인데 −1.15%)가 보여 주듯, 방향은 "발표값 − 기대"로 정해지고 기대는 발표 전에 이미 가격에 들어간다(2026-07-06 +2.75%). 그래서 잠정실적은 방향 피처보다 **변동성·국면 플래그**로 쓰는 편이 안전하다. 서프라이즈 방향 피처를 쓰려면 컨센서스가 필요하다. KIS `estimate_perform`이 그 대체재가 될 수 있는지는 미확인이다.
- 자사주 매입을 결정하면 매입 기간(예: 3조원을 3개월에) 동안 회사가 매일 사는 수급이 생긴다. 이 "매입 진행 중" 상태는 일·장중 피처 후보다(효과는 추론, 미검증).

### Gaps
- **블록딜**: 한국 대형주 블록딜의 단기 반응을 다룬 실증 논문을 찾지 못했다. 블록딜은 대개 장 마감 뒤 수요예측과 다음 날 시간외 대량매매로 진행된다는 것이 통념이지만, 1차 출처로 확인하지 못했다.
- **공매도 잔고**: KAIST 학위논문들이 공매도 잔고·잔고 서프라이즈의 횡단면 음(−)의 예측력을 보고한다는 검색 요약이 있었다. 다른 논문은 시장 수준 예측력이 표본 밖에서 사라진다고 했다고 한다. 하지만 페이지가 로딩되지 않아 원문과 링크의 대응을 확인하지 못했다 — [KAIST koasas](https://koasas.kaist.ac.kr/handle/10203/321904?mode=full)
- **조회공시, 투자주의·경고, 배당기준일**: 세 대형주에 해당하는 단기 반응 근거를 찾지 못했다. 시장경보는 대개 이상 급등 소형주 대상이라 세 종목에서는 드물 것으로 보이지만, 이는 추론이다.
- **5% 대량보유 보고, D005 거래계획보고**의 단기 효과에 대한 한국 근거를 찾지 못했다. 법정 보고 기한(통상 5영업일로 알려짐)도 1차 법령으로 확인하지 못했다.
- 2000년대 한국 사건연구들의 CAR 수치는 초록에 없어서 쓰지 못했다.

## 3. 한국의 반응 속도: 장중 vs 장후 공시, 사전 유출, 개인 지연으로 가능한가

### Takeaway
삼성전자의 큰 공시(잠정실적, 자사주)는 개장 전이나 장 마감 뒤에 나와서 **다음 시가 갭**에 반영된다. 장중 중요 공시는 매매가 정지(예: 30분)되어 사람 속도로는 첫 반응을 잡을 수 없다. 해외 근거는 "첫 반응은 빠르지만 그 뒤 수일 drift가 남는다"이다. 그래서 개인 지연 시스템이 가져갈 수 있는 것은 (1) 갭 예측, (2) 이벤트 국면 플래그, (3) 일 단위 drift다. 초~분 단위 선점은 아니다.

### Cited Findings
- 2024-05-03(연휴 전 거래일) 공시 539건 중 292건(**54.2%**)이 정규장 마감(15:30) 뒤에 나왔다. 최대주주 변경, 횡령·배임 고소 같은 악재성 공시가 섞여 있었다. 거래소는 장 마감 전 공시를 "강제할 수 없다"고 했다 — [아주경제 2024-05-08](https://www.ajunews.com/view/20240507160117451)
- 3일 이상 휴장 전 마지막 거래일의 장후 공시를 거래소가 연휴 뒤 첫 거래일에 다시 공지한다(검색 요약) — [세계일보 2025-10-03](https://www.segye.com/newsView/20251003507384)
- 2012년 기준 거래소 공시는 접수 뒤 공시팀 약 20명이 검토하고 부서장이 승인해 공개까지 약 **10분**이 걸렸다. 코스닥 직원이 이 "10분간의 사각지대"에 미공개 공시로 매매한 혐의로 수사받았다. 개선안은 공시의 약 85%를 사전 검토 없이 바로 공개하는 것이었다 — [서울신문 2012-08-22](https://www.seoul.co.kr/news/society/2012/08/22/20120822010014)
- 삼성전자 2024 3Q 잠정실적은 개장 전 발표 → [뉴시스](https://mobile.newsis.com/view/NISX20241008_0002913127). 10조원 자사주는 장 마감 뒤 공시 → 다음 날 +5.98% → [메트로신문](https://www.metroseoul.co.kr/article/20241118500665)
- 장중 중요 공시는 매매정지(신세계푸드 30분, 해당 선물·옵션 포함) — [IB토마토](https://newstomato.com/ReadNews.aspx?no=1307421)
- 해외: 금요일 실적 발표는 즉시 반응이 **15% 낮고** 지연 반응이 **70% 높다**. 거래량은 약 8% 적다(DellaVigna & Pollet 2009) — [IDEAS](https://ideas.repec.org/a/bla/jfinan/v64y2009i2p709-749.html)
- 해외: 고빈도 데이터로 일 수익률을 뉴스 부분과 비뉴스 부분으로 나누면 기업 뉴스에 **광범위한 과소반응**이 보인다. 첫 반응 뒤 수일 같은 방향으로 drift하고 반전이 없다. 비용 뒤에도 수익이 나고, 투자자 주의가 분산될 때 더 강하다(Jiang, Li, Wang 2021 JFE) — [IDEAS](https://ideas.repec.org/a/eee/jfinec/v141y2021i2p573-599.html)
- 해외: 텍스트로 식별한 뉴스가 있는 날의 변동성은 다른 날의 2배가 넘는다. 수익률 분산 비율은 비뉴스일보다 120% 높다(식별 안 된 뉴스는 20%). 시장모형 R²는 "16% versus 33%"(뉴스일이 16%로 읽히지만 페이지가 대응을 명시하지 않음)(Boudoukh, Feldman, Kogan, Richardson) — [NBER w18725](https://www.nber.org/papers/w18725)

### Inferences
- 세 종목의 큰 공시 이벤트는 대부분 장외 시간에 나온다. 그러니 이 정보를 쓰는 자연스러운 자리는 #264의 **`gap` 종류**(다음 날 시가 방향)다. 2026-09-14 개장한 애프터마켓(16:00~20:00) 때문에 장후 공시의 첫 반응 일부가 애프터마켓 가격에 들어갈 것이다. 그러면 갭 피처에 애프터마켓 마지막 가격이 들어가야 한다(추론).
- 4분 예측에서 공시는 "맞힐 대상"이라기보다 **분포를 깨는 사건**이다. 공시 직후 N분은 abstain하거나 따로 채점하는 편이 적중률 지표를 정직하게 만든다(추론).
- Jiang-Li-Wang식 drift는 미국 횡단면 결과다. 한국 대형 3종목의 장중 drift는 검증되지 않았다.

### Gaps
- 한국에서 공시의 **장중 가격 반영 속도**(분 단위)를 잰 학술 연구를 찾지 못했다. 장중 vs 장후 공시를 직접 비교한 한국 논문도 찾지 못했다.
- Busse & Green(2002) "Market efficiency in real time"(CNBC 보도에 대한 초 단위 반응)은 서지만 확인했고 수치는 확인하지 못했다 — [IDEAS](https://ideas.repec.org/a/eee/jfinec/v65y2002i3p415-437.html). Patell & Wolfson(1984)의 장중 조정 속도 논문도 열지 못했다.
- 증권사 분석의 "최근 3년 삼성전자 잠정실적 당일 등락률: 컨센 상회 +0.16%, 하회 −0.04%" 류의 수치가 검색 요약에 나왔다. 하지만 원 PDF를 추출하지 못해 출처와 정확성을 확인하지 못했으므로 쓰지 않는다.

## 4. 글로벌 유사 신호와 근거 (EDGAR, 내부자, 실적, 뉴스, 경제적 연결)

### Takeaway
미국은 EDGAR가 1초 미만 지연의 무료 JSON API를 준다. 근거가 가장 단단한 것은 기회주의적 내부자 매수(월 82bp)와 고객사→공급사 momentum(월 150bp 이상)이다. 다만 후자는 발견 뒤 줄었다. 둘 다 월 단위 횡단면 전략이라 세 종목의 분·일 예측에 그대로 쓸 수는 없다. 대신 "고객사 뉴스가 공급사에 늦게 반영된다"는 메커니즘이 5절의 직접 근거다.

### Cited Findings
- EDGAR는 평일 06:00~22:00 ET에 접수한다. 17:30 ET 이후 접수분(Form 3/4/5는 22:00 이후)은 다음 영업일에 배포된다. 최대 **10 requests/second**이고, User-Agent에 회사명과 연락 이메일을 적어야 한다 — [SEC Accessing EDGAR Data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
- `data.sec.gov` submissions API는 "real time", 처리 지연 "less than a second"다. XBRL API는 "under a minute"이다. 인증·키가 필요 없다. bulk ZIP은 매일 03:00 ET에 다시 만든다 — [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- 내부자 거래는 대부분 Form 3/4/5로 "within two business days" 안에 보고한다 — [SEC](https://www.sec.gov/resources-small-businesses/going-public/officers-directors-10-shareholders)
- **Cohen, Malloy, Pomorski "Decoding Inside Information"**(JF 2012): 예측 가능한 routine 거래(절반 이상)는 정보가 없다. opportunistic 거래 포트폴리오는 가치가중 **월 82bp** 초과수익을 낸다. routine은 사실상 0이다 — [NBER w16454](https://www.nber.org/papers/w16454)
- **Cohen & Frazzini "Economic Links and Predictable Returns"**(JF 2008): 고객사 충격 뒤 공급사를 사고파는 long-short 전략이 "monthly alphas of over 150 basis points"를 낸다 — [IDEAS](https://ideas.repec.org/a/bla/jfinan/v63y2008i4p1977-2011.html). 재현(Pinchuk 2023): 동일가중 월 122bp, 가치가중 106bp. **발견 뒤 표본에서는 크기가 줄고 유의성을 잃었다** — [arXiv 2301.11394](https://arxiv.org/abs/2301.11394)
- **Thomas & Zhang "Overreaction to Intra-industry Information Transfers?"**(JAR 2008): 먼저 발표한 기업의 실적에 늦게 발표하는 동종 기업 주가가 반응한다. 그 반응은 늦은 기업의 자기 발표 반응과 **음(−)의 관계**다. 시장이 산업 내 함의를 과대평가하고, 늦은 기업이 발표할 때 바로잡힌다 — [IDEAS](https://ideas.repec.org/a/bla/joares/v46y2008i4p909-940.html)
- 실적발표 premium(Frazzini & Lamont, Barber et al.)과 금요일 효과(DellaVigna & Pollet)는 2·3절 참조. 뉴스 과소반응(Jiang, Li, Wang)과 뉴스일 변동성(Boudoukh et al.)도 3절 참조.

### Inferences
- Form 4 신호는 미국 종목 대상이다. 세 종목에 직접 쓸 길은 없다. 한국판(D002 임원 매매)은 거래일·사유 필드가 없고, 대형주에서 거래 규모가 작아 우선순위가 낮다.
- Thomas & Zhang의 결과는 5절의 Micron→SK하이닉스 spillover에 대한 **경고**다. 앞선 기업 발표에 대한 늦은 기업의 반응이 과도할 수 있고, 늦은 기업이 실적을 낼 때 되돌려진다. 그러니 spillover는 갭 방향 피처로 쓰되, 장중 지속을 가정하지 않는다.

### Gaps
- **애널리스트 수정 drift**(Gleason & Lee 2003 등)와 **컨퍼런스콜 텍스트·음성 신호**(Mayew & Venkatachalam 2012, Price et al. 2012)는 이번에 1차 출처를 열지 못했다(IDEAS 404, SSRN·ScienceDirect 403). 한국에서 쓸 수 있는 데이터는 KIS `invest_opinion`과 `estimate_perform`인데, 갱신 주기는 미확인이다.
- RavenPack, Dow Jones 뉴스 analytics의 장중 연구는 열지 못했다. 앞 보고서의 LLM 뉴스 결론(초기 반응에 몰리고 줄어듦)을 따른다.
- 8-K의 4영업일 기한은 1차 출처로 확인하지 못했다.

## 5. 동종·고객사 실적 spillover: Micron, TSMC, NVDA → SK하이닉스·삼성전자

### Takeaway
Micron 실적(미국 장 마감 뒤 발표)은 다음 날 한국 개장에서 SK하이닉스와 삼성전자를 같은 방향으로 크게 움직인 사례가 있다. 가이던스 실망일 때도 마찬가지였다. 학술 연구는 찾지 못했지만, 메커니즘(Cohen-Frazzini, intra-industry transfer)과 사례가 `gap` 종류의 이벤트 피처로 쓸 근거가 된다. TSMC 월매출은 월 1회 공개되어 빈도는 낮다.

### Cited Findings
- **2024-12-19**: Micron이 다음 분기 매출 가이던스를 $7.9B로 내놨다(컨센서스 $8.9B). 시간외에서 −16%. 다음 날 한국에서 **삼성전자 −3.1%**(53,200원), **SK하이닉스 −4.6%**(175,000원) — [Korea JoongAng Daily 2024-12-19](https://www.koreajoongangdaily.com/business/samsung-electronics-and-sk-hynix-face-uncertainty-after-micron-forecast-disappoints/12145236)
- **2026-06-25**: Micron의 이익과 매출 가이던스가 기대를 크게 웃돌았다(시간외 +12%). 한국 개장 초 SK하이닉스는 최대 **+11.6%**, 삼성전자는 최대 **+6.2%**였다. 00:19 GMT 기준 코스피는 +5.3%였다. 두 종목은 코스피 시총의 55%가 넘는다 — [The Star (Reuters) 2026-06-25](https://www.thestar.com.my/business/business-news/2026/06/25/south-korean-chip-shares-surge-after-micron-flags-strong-ai-related-demand)
  - 같은 날 다른 보도는 삼성전자 +5.3%(358,500원), SK하이닉스 +9.2%(2,818,000원), 코스피 약 +4.1%였다. 그날 삼성전자 90조원 자사주 검토 보도와 SK하이닉스 Nasdaq ADR 상장 계획(최대 45.45조원, 7월 10일 잠정)도 함께 있었다 — [TheNextWeb](https://thenextweb.com/news/south-korea-chip-shares-rally-micron-earnings). 코스피 종가 +5.42%, 반도체 +8.46%라는 보도도 있다(검색 요약) — [FN News](https://en.fnnews.com/news/202606251603381637). **보도마다 수치가 다르다**(장중 vs 종가 시점 차이로 보임).
- **TSMC 월매출**(백만 대만달러, 2026년 미감사): 9월 511,857(+54.6% YoY), 8월 514,806(+53.3%), 7월 467,580(+44.7%) — [TSMC IR Monthly Revenue](https://investor.tsmc.com/english/monthly-revenue)
- 고객사→공급사 momentum은 월 150bp 이상이었지만 발견 뒤 약해졌다 — [Cohen & Frazzini](https://ideas.repec.org/a/bla/jfinan/v63y2008i4p1977-2011.html), [Pinchuk](https://arxiv.org/abs/2301.11394)
- 동종 선행 발표에 대한 반응은 과대반응일 수 있다 — [Thomas & Zhang](https://ideas.repec.org/a/bla/joares/v46y2008i4p909-940.html)

### Inferences
- Micron 실적은 미국 장 마감 뒤라 한국 시간으로는 새벽에 나온다. 그 정보는 **한국 시가 갭**에 들어간다. 엔진은 이미 MU·NVDA 일별 데이터를 받는다. 더할 것은 (a) "어젯밤 Micron/NVDA 실적" 이벤트 플래그와 (b) 시간외 수익률(가능하면)이다. 두 사례 모두 방향이 같았지만 사례 2개는 통계가 아니다.
- 2026-06-25처럼 같은 날 다른 큰 뉴스(자사주 보도, ADR)가 겹치면 효과를 분리할 수 없다. 이벤트 플래그는 "원인"이 아니라 "고변동 국면 표시"로 해석해야 한다.
- SK하이닉스 ADR이 실제로 Nasdaq에서 거래된다면, 한국 장 마감 뒤 미국 시간의 SK하이닉스 가격이 생긴다. 그러면 다음 날 갭의 가장 직접적인 피처가 된다(상장 여부는 미확인, 추론).
- TSMC 월매출과 한국 수출(관세청 10일 단위 반도체 수출) 같은 월·순 단위 공개치는 4분이나 일 예측보다 5거래일 종류에 어울린다(추론).

### Gaps
- Micron, NVDA, TSMC 실적·월매출이 SK하이닉스·삼성전자 수익률을 예측하는지 다룬 **학술 연구는 찾지 못했다**. 사례 기사뿐이다.
- TSMC 월매출의 공개일(대만 규정상 매월 10일 전후로 알려짐)은 1차 출처로 확인하지 못했다.
- SK하이닉스 ADR이 2026-07에 실제로 상장했는지, SK하이닉스·삼성전기의 실적 발표 일정(잠정실적 공시 여부)도 확인하지 못했다.
- 관세청 10일 단위 수출 통계의 공개 시각과 효과도 이 노트 범위 밖이라 확인하지 못했다.

## krx-quant 적용 메모

전제: 엔진은 주문하지 않는다. 사람이 신호를 보고 손으로 매매하므로 지연은 초~분이다. 장중 공시는 매매정지와 HFT 때문에 첫 반응을 잡을 수 없다(3절). 그래서 공시 정보는 주로 세 가지로 쓴다. ① **special event 플래그**(abstain, 별도 채점, 국면), ② **`gap`·`open_to_close` 종류의 피처**(#264), ③ 일·5일 피처.

| 순위 | 이벤트 | 쓰는 방식 | 데이터원·API | 예상 지연 | 근거 강도 |
|---|---|---|---|---|---|
| 1 | **3종목 실적 이벤트**(삼성전자 잠정실적, 3사 확정실적·IR) | 이벤트 당일 플래그. 4분 예측은 발표 직후 N분 abstain 또는 별도 채점. `gap`에 "오늘 장전/어제 장후 실적" 피처 | 일정은 KIND 공정공시(IR 개최 예고)와 회사 IR. 발생은 KIS `news_title` 폴링(005930·000660·009150). 상세는 OpenDART | 일정은 수일 전. 발생은 폴링 주기 + 벤더 지연(초~분, 미측정) | 사례 B(삼성 잠정 −1.15%), 학술 C |
| 2 | **Micron·NVDA 실적 다음 날** | `gap` 종류의 이벤트 플래그 + 시간외 수익률 피처. 4분 모델에는 개장 첫 30분 국면 플래그 | 이미 받는 MU·NVDA 일별 + 실적 일정(TradingView 수집기가 실적 일정을 주는지는 미확인). 시간외 가격 출처는 미정 | 한국 개장 전에 확정(수 시간 여유) | 사례 B(2건, 같은 방향), 메커니즘 A(Cohen-Frazzini), 과대반응 경고(Thomas-Zhang) |
| 3 | **자사주 취득·소각 결정과 매입 진행 기간** | 공시 플래그(대개 장후 → `gap`). 매입 기간 중 "회사 매수 진행" 일 피처 | OpenDART DS005 `자기주식 취득 결정`(2020038), 신탁(2020040), 소각. 상세 유형 E001. 발생 감지는 KIS `news_title` | 공시 시각 기준 초~분, OpenDART 상세는 당일 | 사례 B(+5.98%), 한국 학술 B−(2005, 수치 미확인) |
| 4 | **장중 공시 일반**(조회공시 답변, 풍문 해명, 단일판매·공급계약, 매매정지·VI) | "공시 직후 N분" 플래그. 매매정지나 VI가 걸리면 그 구간 예측과 채점 제외 | KIS `news_title`, `inquire_vi_status`. KIND RSS는 백업 | 초~분(미측정) | 메커니즘(매매정지 규정 B). 방향 근거 없음 |
| 5 | **공매도·대차 잔고** | 일 피처(`gap`, 5일 종류). 4분에는 쓰지 않음 | KIS `daily_short_sale`, `daily_loan_trans`. KRX 정보데이터시스템(실시간 아님) | 일 단위 지연(정확한 T+n 미확인) | C(한국 결과 엇갈림, 원문 미확인) |
| 6 | **애널리스트 의견·추정실적 변화** | 실적 서프라이즈의 기대치 대용. 일 피처 | KIS `invest_opinion`, `estimate_perform` | 일 단위(미확인) | C(한국에서 애널리스트 기준 PEAD 없음, 김형순 2018) |
| 7 | **TSMC 월매출, 밸류업 공시** | 5거래일 종류나 장기 국면 피처. 밸류업은 플래그만 | TSMC IR. KIND 밸류업 공시 | 월 1회 | 밸류업 1~2개월 초과수익(C), TSMC 근거 없음 |
| 보류 | 임원·주요주주 매매, 5% 보고, 블록딜, 투자경고 | 세 대형주에서는 규모가 작거나 드물다. 블록딜은 실시간 데이터원과 근거가 모두 없다 | OpenDART D001·D002·D005 | 보고 기한만큼 늦음 | 대형주 근거 없음 |

구현 메모:
- 가장 먼저 할 일은 **이벤트 원장**이다. 오늘부터 `news_title` 결과(종목, `data_dt`, `data_tm`, 제목)와 OpenDART 접수번호를 서버에 쌓는다. 장중 시각이 있는 과거 이벤트 표가 없으면 3절의 "장중 vs 장후" 효과를 엔진 데이터로 검증할 수 없다.
- 이벤트일과 비이벤트일의 적중률을 **따로 보고**한다. 이벤트일 몇 번이 1년 적중률을 좌우할 수 있다(앞 보고서: 일 단위 표본 약 245개, 표준오차 약 3.2%p).
- 키는 서버 `.env`에만 둔다. OpenDART 키는 1인 1개이고 공유가 금지되어 있다. 공개 저장소에는 키도, 수집한 공시 원문도 커밋하지 않는다.
- 문서 갱신 대상: 새 수집기, 저장 파일·키, 일정, 플래그 정의(`docs/`, `.env.example`).
