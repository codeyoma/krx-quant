# 마이크로구조 · order flow · LOB 신호 (수초~수분 horizon)

> 기준일 2026-10-17. 연구 정리이며 투자 조언이 아니다.
> 증거 등급: **A** = 여러 시장·여러 연구에서 out-of-sample로 반복 / **B** = 탄탄한 단일 연구 또는 한 시장 OOS / **C** = 엇갈림·비용 무시·단일 시장 / **D** = 반박되었거나 예측력 증거 없음.
> "설명(contemporaneous)"과 "예측(forward)"을 구분한다. 이 분야에서 R²가 높게 보고되는 수치는 대부분 같은 구간의 가격 변화를 **설명**한 값이다.
> (미검증) 표시는 이번 조사에서 원문이나 출판 정보를 직접 확인하지 못한 항목이다.

## 1. 신호 카탈로그 (증거 등급 요약)

### Takeaway
같은 구간을 설명하는 힘은 OFI 계열이 압도적이다(R² 65~88%). 하지만 1분 앞을 예측할 때는 미국 대형주에서 out-of-sample R²가 0 아래로 떨어진다. 다음 mid 변화 하나를 맞히는 데는 large-tick 종목의 queue imbalance와 microprice가 가장 확실한 신호다. 비용을 넣은 뒤 단독 전략으로 남는다는 증거는 거의 없다.

### Cited Findings

| 신호 | 1줄 공식 | 작동 horizon / 감쇠 | 핵심 논문 | OOS·해외 증거 | 비용 후(non-colo) | 등급 |
|---|---|---|---|---|---|---|
| OFI (best level) | 이벤트마다 e = 1{Pb↑or=}·qb_new − 1{Pb↓or=}·qb_old − 1{Pa↓or=}·qa_new + 1{Pa↑or=}·qa_old, OFI = Σe | 10초 구간 **설명** R² 평균 65%. 10초 단위 자기상관은 작고 금방 사라짐 | [Cont, Kukanov, Stoikov 2014](https://arxiv.org/abs/1011.6402) | 미국 50종목(2010), 1분 S&P100 재현([CCZ](https://arxiv.org/abs/2112.13213)) | 예측용 단독으로는 증거 없음 | 설명 A / 예측 C |
| Multi-level / integrated OFI | 1~M 호가 레벨별 OFI 벡터, 또는 그 PCA 제1성분 | 설명력 향상: OS RMSE 개선 large-tick 65~75%, small-tick 15~30% | [Xu, Gould, Howison 2019](https://arxiv.org/abs/1907.06230), [CCZ 2023](https://arxiv.org/abs/2112.13213) | Nasdaq 6종목(2016), S&P100(2017~19) | 1분 예측에서 best-level보다 나은 점 없음 | 설명 A / 예측 C |
| Cross-asset lagged OFI | 다른 종목들의 지난 OFI로 회귀(LASSO) | 1분 앞에서 가장 강하고, 1→30분으로 가며 빠르게 감쇠 | [CCZ 2023](https://arxiv.org/abs/2112.13213) | 미국만 | 논문은 거래비용을 무시함 | C |
| Queue imbalance | I = Qb / (Qb + Qa) (best level), logistic으로 다음 mid 방향 예측 | 다음 mid 변화 1회(event time) | [Gould & Bonart](https://arxiv.org/abs/1512.03492) | Nasdaq 10종목(2014), 80/20 OOS | 다음 1틱만 맞히고, spread를 건너면 남는 것이 적음 | large-tick B+ / small-tick C |
| Microprice | P_micro = mid + g(I, spread). g는 (I, S) Markov chain에서 추정한 미래 mid의 조건부 기대값의 극한 | 수초~수십초 | [Stoikov 2018](https://ideas.repec.org/a/taf/quantf/v18y2018i12p1959-1966.html) | 원 논문은 미국 | 예측이라기보다 "공정가" 추정치이고 호가 배치에 쓰는 신호 | B |
| 다층 depth imbalance | Σ_l w_l (Qb_l − Qa_l) / Σ_l w_l (Qb_l + Qa_l) | 수초 | MLOFI·deep-LOB 계열 간접 증거 | deep 모형은 OFI 입력이 raw 호가보다 우수([Kolm et al.](https://papers.ssrn.com/abstract=3900141)) | spoof에 취약함([Cartea et al.](https://ideas.repec.org/a/taf/apmtfi/v27y2020i1-2p67-98.html)) | C |
| Trade-sign long memory | sign ACF ~ τ^(−α), α≈0.6 | 다음 체결 **부호**는 수백 trade 앞까지 예측됨. 가격은 거의 white noise | [Lillo & Farmer](https://arxiv.org/abs/cond-mat/0311053), [Sato & Kanazawa 2023](https://link.aps.org/doi/10.1103/PhysRevLett.131.197401) | LSE, TSE 9년 이상 | 가격 방향 신호로는 약함 | 부호 A / 가격 D |
| Hawkes intensity | λ(t) = μ + Σ φ(t − t_i), 매수·매도·취소별 다변량 | 수초~수분(activity) | [Bacry, Mastromatteo, Muzy](https://arxiv.org/abs/1502.04592) | 리뷰 논문 | 방향 예측력의 정량 증거는 찾지 못함 | activity B / 방향 gap |
| Square-root impact | Δp ≈ Y·σ·√(Q/V) | 메타오더 단위 | [Tóth et al. 2011](https://arxiv.org/abs/1105.1694), [Sato & Kanazawa 2025](https://arxiv.org/abs/2411.13965) | TSE 전 종목 8년, δ = 1/2 | 신호가 아니라 비용·변동 모형 | A |
| Impact ∝ 1/depth | ΔP ≈ (c / depth) · OFI | 장 초반 depth는 평균의 절반 → impact가 큼 | [CKS 2014](https://arxiv.org/abs/1011.6402) | 미국 | 레짐 feature로 쓸 만함 | B |
| VPIN | 거래량 bucket마다 \|V_buy − V_sell\| / V의 이동평균(BVC 분류) | 단기 변동성(주장) | [Easley, López de Prado, O'Hara 2012](https://papers.ssrn.com/abstract=1695596) | 반박: [Andersen & Bondarenko](https://repec.econ.au.dk/repec/creates/rp/13/rp13_43.pdf) | 거래량·변동성을 통제하면 추가 예측력 없음 | D |
| Iceberg / hidden 탐지 | 체결 뒤 같은 가격 queue가 다시 차는 패턴 | 수초~수분 | [Zotikov & Antonov](https://arxiv.org/abs/1909.09495) | CME E-mini OOS, Xetra 패턴 | 주식·KRX 근거 없음 | C |
| 취소율·spoofing 패턴 | 취소량 / 신규량, 먼 호가 대량 주문의 빠른 취소 | — | [Cartea, Jaimungal, Wang 2020](https://ideas.repec.org/a/taf/apmtfi/v27y2020i1-2p67-98.html) | 탐지 연구만 있음 | 방향 예측 증거 없음 | D / gap |
| Deep LOB (DeepLOB 등) | CNN/LSTM에 raw LOB 또는 OFI 입력 | 평균 가격 변화 약 2번까지 | [DeepLOB](https://arxiv.org/abs/1808.03668), [Kolm et al.](https://papers.ssrn.com/abstract=3900141), [Lucchese et al.](https://arxiv.org/abs/2211.13777) | LSE, Nasdaq | "예측력은 흔하지만 단독 거래는 어렵다" | 예측 B / 비용 후 D |

### Inferences
- 신호 대부분의 반감기는 수초~1분이다. krx-quant의 4분 touch 목표에서는 이 신호들이 대부분 섞여 희석된다. 직접 쓸 곳은 k=1틱 touch와 같은 짧은 목표, 그리고 레짐 feature다.
- 등급 A인 항목(설명 OFI, sign memory, square-root)은 "방향 예측기"가 아니다. 구조를 이해하고 비용·변동성을 모델링하는 데 쓰는 도구다.

### Gaps
- 표의 horizon과 감쇠 값은 미국·유럽·일본 데이터에서 나왔다. KRX 개별 종목의 tick-level OFI 예측력을 다룬 논문은 찾지 못했다(7절).

## 2. OFI · Multi-level OFI · Cross-impact OFI: 예측하는가, 설명만 하는가

### Takeaway
OFI는 **같은 구간의 가격 변화를 설명하는 변수**다. 10초 구간에서 R² 65%, 1분 구간에서 integrated OFI의 OS R²는 약 84%다. **1분 앞 예측**에서는 자기 종목 OFI의 out-of-sample R²가 음수(−0.37)였다. 다른 종목의 lagged OFI가 조금 낫지만 빠르게 감쇠한다.

### Cited Findings
- CKS 2014: NYSE TAQ 50종목(2010년 4월 데이터. **오래된 결과**), Δt = 10초, 30분 창. OFI 회귀 평균 R² **65%**, trade imbalance(TI)는 **32%**. 이차항을 넣어도 65%→68%로 거의 늘지 않는다(선형). 가격을 바꾼 이벤트를 OFI에서 빼도 R²는 35~60%를 유지한다. β는 depth에 반비례하고, 10초 단위 이벤트 자기상관은 "small and quickly vanish". — [arXiv 1011.6402](https://arxiv.org/abs/1011.6402), 출판 [J. Financial Econometrics 12(1):47–88, DOI](https://doi.org/10.1093/jjfinec/nbt003). 데이터 시기는 [Xu et al.](https://arxiv.org/abs/1907.06230) 2절의 요약에서 확인했다.
- Xu, Gould, Howison: Nasdaq 6종목, LOBSTER 2016. 레벨 간 OFI가 강하게 상관돼 있어 OLS는 과적합하고, Ridge 회귀를 썼다. 레벨을 하나 더할 때마다 OS 적합도가 오른다. OS RMSE 개선은 **large-tick 약 65~75%, small-tick 약 15~30%**다. 역시 설명(contemporaneous) 모형이다. — [arXiv 1907.06230](https://arxiv.org/abs/1907.06230). 저널판(Market Microstructure and Liquidity로 알려짐)은 (미검증).
- Cont, Cucuringu, Zhang(CCZ): S&P500 상위 100종목, 2017~2019, 1분 단위, 30분 rolling 추정.
  - 설명 IS R²: best-level 자기 OFI 71.16%, cross 포함 73.87%, integrated 87.14%, integrated+cross 87.85%. OS R²: 64.64 / 66.03 / **83.83** / 83.62.
  - tick/price 비율이 큰 종목일수록 설명력이 높다. best-level OS R²는 최하위 사분위 44.38%, 최상위 70.70%이고, integrated는 68.14% → 89.86%다.
  - **1분 앞 예측** OS R²(%p): 자기 OFI −0.37, cross OFI −0.10, AR −0.36, cross-return(CAR) −0.10. cross 모형은 모든 종목에서 1% 수준으로 유의하게 우세했다.
  - forecast가 spread를 넘을 때만 매매하는 전략의 연환산 PnL은 자기 OFI 0.21, cross OFI 0.43이다. **논문 스스로 "ignores trading costs"라고 밝힌다**. 1→30분으로 horizon을 늘리면 PnL이 줄고, cross-asset 모형이 더 빨리 감쇠한다.
  - — [arXiv 2112.13213 v4 (2023-06)](https://arxiv.org/abs/2112.13213). Quantitative Finance 2023 게재로 알려져 있으나 권·호는 (미검증).
- Kolm, Turiel, Westray: Nasdaq 115종목. OFI처럼 stationary한 입력을 쓴 단순 신경망이 raw 호가 입력 모형을 이긴다. 종목별 유효 예측 horizon은 **평균 가격 변화 약 2번**이다. — [SSRN 3900141](https://papers.ssrn.com/abstract=3900141), [Mathematical Finance 33(4):1044–1081](https://ideas.repec.org/a/bla/mathfi/v33y2023i4p1044-1081.html)

### Inferences
- OFI를 예측에 쓰려면 (1) 아주 짧은 horizon(수초~1분), (2) depth로 정규화(β ∝ 1/depth), (3) 다른 자산에서 오는 lead가 필요하다. 4분 목표에서는 "현재 구간 OFI가 이미 반영한 움직임"이 대부분이라 직접 예측력이 작을 것이다.
- OS R²가 음수여도 PnL이 양수일 수 있다는 CCZ의 지적은 hit rate로 평가하는 krx-quant의 방식과 맞는다. 방향 분류 지표로 평가해야 한다.

### Gaps
- 비용을 넣은 OFI 단독 전략의 OOS 성과를 비-colocated 조건에서 보고한 논문은 찾지 못했다.
- MLOFI 논문의 구간 길이별 R² 표는 확인하지 못했다(본문 추출 범위 밖).

## 3. Microprice · Queue imbalance · Depth imbalance와 tick-size 레짐

### Takeaway
Best-level queue imbalance는 **large-tick 종목에서 다음 mid 방향을 AUC 0.75~0.81로** 맞힌다. small-tick 종목에서는 AUC 0.58~0.64에 그친다. Microprice는 이 정보를 "공정가"로 바꾼 것이다. tick이 클수록(스프레드가 거의 늘 1틱일수록) 호가 잔량 신호가 강하다.

### Cited Findings
- Gould & Bonart: Nasdaq 10종목, 2014년 252일, 하루 10:00~15:30. 종목마다 하루 mid 변화 100개 표본, 80/20 분할.
  - OOS ROC AUC는 large-tick(MSFT 0.762, INTC 0.798, MU 0.752, CSCO 0.805, ORCL 0.770), small-tick(GOOG 0.581, AMZN 0.642, TSLA 0.602, PCLN 0.583, NFLX 0.627)이다.
  - null 대비 MSE 감소는 large-tick 약 20~30%, small-tick 약 2~6%다. local logistic은 아주 조금 낫다.
  - **오래된 결과(2014 데이터)**. — [arXiv 1512.03492](https://arxiv.org/abs/1512.03492), [ar5iv 본문](https://ar5iv.arxiv.org/html/1512.03492). 저널판(Market Microstructure and Liquidity로 알려짐)은 (미검증).
- Stoikov microprice는 실증적으로 mid나 weighted-mid보다 단기 가격을 더 잘 예측한다. imbalance와 spread를 조건으로 한 미래 mid 기대값의 극한이며, 그래서 martingale이다. — [Quantitative Finance 18(12):1959–1966 (IDEAS)](https://ideas.repec.org/a/taf/quantf/v18y2018i12p1959-1966.html), DOI 10.1080/14697688.2018.1489139. 후속 논의는 [Boltzmann Price (arXiv 2507.09734, 2025)](https://arxiv.org/abs/2507.09734)에 있다(내용 미확인).
- Lehalle & Mounjid: limit order가 체결될지는 liquidity imbalance에 달려 있다. latency가 imbalance 정보의 가치를 깎는다. — [arXiv 1610.00261](https://arxiv.org/abs/1610.00261)
- CCZ: tick/price 비율이 큰 종목일수록 OFI 설명력이 높다(위 표의 사분위 결과). — [arXiv 2112.13213](https://arxiv.org/abs/2112.13213)
- Xu et al.: 깊은 레벨을 더했을 때의 이득도 large-tick에서 크다(65~75% 대 15~30%). — [arXiv 1907.06230](https://arxiv.org/abs/1907.06230)
- 일본: Nikkei 225 종목의 절반 이상에서 order-flow와 order-book imbalance가 단기 수익률을 예측했다. 그러나 imbalance 전략 중 buy-and-hold를 이긴 것은 없었다(대만 대학 학위논문으로, 근거 등급이 낮다). — [NCCU repository](https://nccur.lib.nccu.edu.tw/handle/140.119/67028)

### Inferences
- KRX는 가격대별로 호가단위가 고정돼 있다. 삼성전자처럼 가격 대비 tick이 크고 스프레드가 거의 늘 1틱인 종목은 large-tick 레짐에 가깝다고 **추정**한다. 그렇다면 queue imbalance와 microprice가 가장 효과적인 축이다. 종목마다 "스프레드 = 1틱인 시간 비율"과 tick/price를 krx-quant 데이터로 직접 재야 한다.
- AUC 0.8은 "다음 mid 변화 1회"에 대한 값이다. 4분 안의 ±k틱 touch에서는 k=1일 때 일부만 이어지고, k가 클수록 약해질 것이다.

### Gaps
- KRX 호가단위 표와 2023년 개편 내용은 이번 조사에서 1차 출처로 확인하지 못했다. 2010년 이후 12년 만의 개편이 있었다는 보도만 확인했다([다음 뉴스](https://v.daum.net/v/qGS7P0BEoG)).
- Microprice 원 논문의 실증 종목과 정확도 수치는 페이월이라 확인하지 못했다.

## 4. Trade-sign 장기기억 · Hawkes · Impact 법칙 (square-root, Kyle λ)

### Takeaway
체결 부호의 장기기억은 일본(TSE) 전 계좌 데이터로도 정량 검증됐다. 다만 유동성 반응이 상쇄해서 가격 예측력으로는 거의 이어지지 않는다. Square-root impact는 2025년 TSE 전 종목에서 δ = 1/2 "strict universality"가 보고됐다. 신호라기보다 비용과 변동폭 모형이다.

### Cited Findings
- Lillo & Farmer: LSE에서 order sign ACF는 약 τ^(−α), α ≈ 0.6(H ≈ 0.7)이다. 거래 크기와 유동성 변동이 이 예측가능성을 상쇄해 수익률은 white noise에 가깝다. **오래된 결과(2003)**. — [arXiv cond-mat/0311053](https://arxiv.org/abs/cond-mat/0311053). 저널판은 (미검증).
- Sato & Kanazawa (PRL 131, 197401, 2023): TSE 9년 이상, 모든 거래자를 order-splitting 또는 random으로 분류했다. Lillo-Mike-Farmer 모형의 예측 γ ≈ α − 1이 정량적으로 맞았다. ACF의 prefactor로 분할 주문 거래자 수를 추정할 수 있다고 논의한다. — [PRL DOI](https://link.aps.org/doi/10.1103/PhysRevLett.131.197401), [arXiv 2301.13505](https://arxiv.org/abs/2301.13505)
- Sato & Kanazawa (PRL, 2025-12): TSE의 모든 유동 종목을 8년간, 계좌 단위까지 분석했다. square-root 지수 δ는 종목 수준과 거래자 수준 모두에서 오차 범위 안에서 1/2이다. GGPS와 FGLW 비보편 모형은 기각됐다. Bouchaud의 Viewpoint는 "단일 데이터셋 연구"라는 단서를 달았다. — [PRL abstract](https://journals.aps.org/prl/abstract/10.1103/65jz-81kv), [arXiv 2411.13965](https://arxiv.org/abs/2411.13965), [Physics Viewpoint](https://physics.aps.org/articles/v18/196)
- Tóth et al.: latent liquidity의 V자 프로파일로 메타오더 분할, sign 장기기억, square-root impact를 함께 설명한다. — [PRX 1, 021006](https://doi.org/10.1103/PhysRevX.1.021006), [arXiv 1105.1694](https://arxiv.org/abs/1105.1694)
- Hawkes: 고빈도 금융에서 변동성 추정, 안정성, 최적 집행, 전체 order book 동역학에 쓰인다(리뷰). — [Bacry, Mastromatteo, Muzy, arXiv 1502.04592](https://arxiv.org/abs/1502.04592). 저널판은 (미검증).
- 가격 impact 계수는 depth에 반비례하고 depth에는 장중 계절성이 있다. 장 초반 depth는 평균의 약 절반이다. — [CKS, arXiv 1011.6402](https://arxiv.org/abs/1011.6402)

### Inferences
- 부호 장기기억은 "다음 체결이 매수일 확률"을 주지만, 가격은 이를 이미 반영한다. 방향 신호로 쓰려면 **예상 밖의(surprise) 부호 흐름**, 즉 sign − E[sign | 과거]를 써야 한다(추론).
- KRX의 회원사(broker)별 flow는 TSE 연구가 쓴 "거래자별 분할 주문" 정보의 대리변수가 될 수 있다(추론). 같은 증권사에서 같은 방향 흐름이 지속되는지를 보는 식이다.
- Square-root law는 Mandelbrot(HAR range)와 barrier 폭 설계의 사전 정보로 쓸 수 있다: 기대 변동 ∝ σ√(거래량 비중).

### Gaps
- Kyle(1985)의 λ는 이번 조사에서 원문 URL을 확인하지 않았다(미검증). 실증적으로는 CKS의 "impact ∝ 1/depth"가 같은 역할을 한다.
- Hawkes 강도의 비대칭(매수 MO 강도 − 매도 MO 강도)이 수분 앞 방향을 예측한다는 정량 연구는 찾지 못했다.
- Bouchaud et al. "Trades, Quotes and Prices"(2018)와 Cartea, Jaimungal, Penalva(2015) 교과서는 원문을 확인하지 않았다(URL 미검증).

## 5. VPIN과 그 비판

### Takeaway
VPIN은 단기 toxicity와 변동성 지표로 제안됐다. 하지만 거래량과 변동성을 통제하면 추가 예측력이 없고, flash crash도 사후에야 최고치를 찍었다는 반박이 있다. 방향 예측용으로는 근거가 없다(D).

### Cited Findings
- Easley, López de Prado, O'Hara: volume-time 기반 VPIN과 bulk volume classification(BVC)을 제안했다. VPIN은 toxicity가 유발한 단기 변동성의 유용한 지표라고 주장한다. — [SSRN 1695596](https://papers.ssrn.com/abstract=1695596), [RFS 25(5):1457–1493 (IDEAS)](https://ideas.repec.org/a/oup/rfinst/v25y2012i5p1457-1493.html), DOI 10.1093/rfs/hhs053
- Andersen & Bondarenko: 거래 강도와 변동성을 통제하면 BVC-VPIN의 미래 변동성 추가 예측력은 없다. VPIN은 flash crash **전이 아니라 후에** 최고치에 도달했다. BVC는 tick rule보다 못하고, VPIN의 예측력은 거래량·변동성과 상관된 체계적 분류 오류에서 나온다. ELO가 반박했고 A&B가 재반박해 논쟁은 남아 있다. — [Kellogg 요약 (JFM 2014)](https://www.kellogg.northwestern.edu/academics-research/research/detail/2014/vpin-and-the-flash-crash/), [CREATES WP rp13_43](https://repec.econ.au.dk/repec/creates/rp/13/rp13_43.pdf)

### Inferences
- krx-quant에 넣는다면 Mandelbrot의 range 예측 feature 후보 정도다. 이미 있는 거래량·실현변동성 feature를 넘는 이득이 있는지 ablation으로 확인한 뒤에만 남겨야 한다.

### Gaps
- 아시아 시장 VPIN 연구는 이번 조사에서 확인하지 못했다.

## 6. Trade-through · iceberg/hidden 유동성 · 취소율 · spoofing 패턴

### Takeaway
Iceberg 탐지는 선물(CME)에서 OOS로 검증된 방법이 있다. 주식(Xetra)에서도 체결 뒤 같은 가격 잔량이 다시 차는 패턴이 관찰된다. 취소율과 spoofing 패턴은 탐지·규제 연구가 대부분이고, **방향 예측 신호로서의 증거는 찾지 못했다**. Best-level imbalance 자체가 spoof 대상이라는 점이 imbalance 신호의 약점이다.

### Cited Findings
- Zotikov & Antonov: CME E-mini S&P500 전체 호가 로그로 iceberg 주문을 탐지하고 예측했다. 분류·회귀 모형으로 OOS 평가. — [arXiv 1909.09495](https://arxiv.org/abs/1909.09495)
- Xetra에서는 iceberg의 다음 peak가 자동으로 표시되는 방식 때문에, 호가를 지켜보는 누구나 알아볼 수 있는 독특한 패턴이 생긴다. — [CFR Cologne WP 09-06](https://www.cfr-cologne.de/download/workingpaper/cfr-09-06.pdf)(제목·저자는 미확인)
- Hidden 유동성이 있으면 limit·market 주문 제출 행태가 바뀐다. 일부 거래자는 이를 탐지할 수 있다는 뜻이다. — [Frey & Sandås, CFS WP](https://publikationen.ub.uni-frankfurt.de/frontdoor/index/index/year/2009/docId/6190)
- Cartea, Jaimungal, Wang: spoofer는 매수 쪽 허위 LO로 volume imbalance를 왜곡하고, 다른 참가자의 매수 MO 도착률과 mid drift를 끌어올린다. 벌금이 크면 spoof하지 않는 것이 최적이다. — [Applied Mathematical Finance 27(1-2):67–98](https://ideas.repec.org/a/taf/apmtfi/v27y2020i1-2p67-98.html)
- 탐지 연구: [On Detecting Spoofing Strategies in HFT (arXiv 2009.14818)](https://arxiv.org/abs/2009.14818), [Learning the Spoofability of LOBs (arXiv 2504.15908, 2025)](https://arxiv.org/abs/2504.15908). 후자의 검색 요약에 따르면 "spoofer는 best 가격에 비진성 주문을 거의 두지 않는다"는 지적이 있다(원문 미확인).

### Inferences
- KIS의 10호가 스냅샷과 체결 스트림만으로도 대략적인 분해가 가능하다. "체결로 설명되지 않는 잔량 감소 = 취소", "체결 직후 같은 가격 잔량 복원 = iceberg 의심"으로 근사할 수 있다(추론. order-by-order 메시지가 없어 정확도는 제한적).
- 깊은 호가(5~10레벨)의 imbalance는 spoof에 더 취약할 수 있다. 그 가중치는 OOS에서 검증한 경우에만 키워야 한다.

### Gaps
- "trade-through(여러 호가를 한 번에 쓸어가는 체결, sweep)"가 이후 수분 방향을 예측한다는 정량 논문은 이번 조사에서 찾지 못했다.
- KRX에서 iceberg·hidden 주문의 존재 여부와 표시 규칙은 확인하지 못했다.

## 7. 미국 밖 증거(유럽·아시아·한국)와 비용 후 생존

### Takeaway
유럽(LSE)과 일본(TSE)에서는 구조적 사실(sign 장기기억, square-root impact, deep-LOB 예측력)이 재현됐다. 중국은 엇갈린다. 한국은 **개별 종목 tick-level OFI 예측 연구를 찾지 못했다**. 다만 KOSPI200 옵션 order imbalance가 지수의 잔여 일중 수익률을 예측하고, 파생 → 현물 방향의 정보 흐름이 보고됐다. 비용 후 생존 증거는 거의 없다. 예외는 한국 HFT 개인투자자 연구로, 수수료와 세금을 넣어도 우수했다.

### Cited Findings
- 한국 ①: KOSPI200 옵션의 개장 첫 10분 order imbalance가 지수·선물의 당일 잔여 수익률을 예측한다. 불확실성이 클 때 가장 강하고, 단순 timing 전략이 거래비용 뒤에도 수익을 낸다. 효과를 만드는 것은 기관이다. — [Kang, Kang, Lee, J. Futures Markets 42(3):338–364 (2022)](https://ideas.repec.org/a/wly/jfutmk/v42y2022i3p338-364.html), 관련 [KAIST 학위논문](https://koasas.kaist.ac.kr/handle/10203/294444)
- 한국 ②: KOSPI200 현물·선물·옵션에서 과거 수익률이 과거 order imbalance보다 미래 가격을 더 잘 예측한다. 정보는 파생 → 현물 방향으로 더 강하게 흐른다. — [KAIST](https://koasas.kaist.ac.kr/handle/10203/91122)
- 한국 ③: 2018~2022년 KRX 전 종목에서 고빈도 알고리즘을 쓰는 개인투자자는 **수수료와 거래세를 고려해도** 일반 개인보다 통계적으로 우수했다. 주 전략은 짧은 데이트레이딩과 단기 추세 역전(역추세)이다. 공저자 한 명은 한국거래소 시장감시부 소속이다. — [박수철·우민철, 재무관리연구 40(5) (2023), PDF(블로그 호스팅, 출판사 링크 미확인)](https://smallake.kr/wp-content/uploads/2024/06/40-5-01_%eb%b0%95%ec%88%98%ec%b2%a0_%ec%9a%b0%eb%af%bc%ec%b2%a01-28.pdf)
- 일본: sign 장기기억과 square-root impact를 TSE 전 계좌 데이터로 검증했다(4절). imbalance는 Nikkei 225 절반 이상에서 예측력이 있었지만 전략 성과는 buy-and-hold 이하였다(3절, NCCU 학위논문).
- 유럽: DeepLOB은 LSE 1년 데이터에서 OOS로 안정적이고 학습하지 않은 종목에도 전이됐다. — [arXiv 1808.03668](https://arxiv.org/abs/1808.03668), IEEE TSP 67(11), 2019
- 중국: 일중 데이터에서는 order imbalance가 1분~90분 수익률을 예측한다는 연구가 있다([RMIT 등록 논문, 저자 매칭은 검색 요약 기준](https://research-repository.rmit.edu.au/articles/journal_contribution/Do_order_imbalances_predict_Chinese_stock_returns_New_evidence_from_intraday_data/27477654)). 반면 일별로는 동시점 관계만 있고 예측력은 없다는 결과도 있다([Shenoy & Zhang](https://bearworks.missouristate.edu/articles-cob/139)). A주는 T+1 규칙 때문에 일중 매매가 제약된다.
- 비용: CCZ는 비용을 무시했다. Lucchese et al.은 고빈도 예측력이 "ubiquitous"하지만 단독 전략으로는 바로 거래할 수 없을 수 있다고 썼다. Briola et al.은 예측력이 높아도 실행 가능한 거래 신호로 늘 이어지지는 않는다고 결론냈다. Lehalle & Mounjid는 latency가 imbalance 정보의 가치를 깎는다고 했다. — [CCZ](https://arxiv.org/abs/2112.13213), [Lucchese et al. IJF 40(4) 2024](https://ideas.repec.org/a/eee/intfor/v40y2024i4p1587-1621.html), [Briola et al. arXiv 2403.09267](https://arxiv.org/abs/2403.09267), [Lehalle & Mounjid](https://arxiv.org/abs/1610.00261)

### Inferences
- 비-colocated(수십~수백 ms) 환경에서 **스프레드를 건너는 taker 전략**으로 1틱 예측을 수익화하기는 어렵다. 1틱 이동을 맞혀도 스프레드 1틱과 세금·수수료를 빼면 남는 것이 거의 없다(추론, CCZ·Lucchese·Briola와 일관).
- krx-quant는 주문을 내지 않는 신호 엔진이고 평가 기준이 hit rate다. 그래서 "비용 후"라는 질문은 사용자가 인버스 ETF 등으로 실행할 때 생긴다. 이 경우 4분보다 긴 horizon이나 큰 k에서만 비용을 넘을 여지가 있다(추론).
- 한국 연구 ①과 ②는 Granger와 Kalman이 이미 쓰는 "선물·옵션 → 현물" 경로를 지지한다.

### Gaps
- KRX 개별 종목(삼성전자 등) tick 데이터로 OFI나 queue imbalance의 예측력을 측정한 공개 논문은 찾지 못했다.
- 2026년 현재 KOSPI 증권거래세율과 ETF 매매의 과세 여부는 이번 조사에서 확인하지 못했다(검색 실패). 실행 비용을 계산하려면 별도 확인이 필요하다.

## 8. 최근 연구(2023~2026)와 deep-LOB 없는 통계적 접근

### Takeaway
최근 흐름은 세 갈래다. (1) 입력 표현(OFI, volume representation)이 모형 깊이보다 중요하다. (2) 예측력은 흔하지만 거래 가능성은 별개다. (3) 일본 전 계좌 데이터로 impact와 sign memory의 이론을 정량 검증했다. deep 모형 없이도 OFI와 logistic/ridge 같은 통계 모형이 핵심 정보를 대부분 담는다는 증거가 쌓이고 있다.

### Cited Findings
- 2023: CCZ(cross-impact OFI, 1분 예측) — [arXiv 2112.13213](https://arxiv.org/abs/2112.13213). Kolm et al.(OFI 입력이 raw LOB보다 우수, 유효 horizon은 가격 변화 약 2번) — [Math. Finance](https://ideas.repec.org/a/bla/mathfi/v33y2023i4p1044-1081.html). Sato & Kanazawa(LMF 검증) — [PRL 131, 197401](https://link.aps.org/doi/10.1103/PhysRevLett.131.197401)
- 2024: Lucchese, Pakkanen, Veraart. 예측력은 어디에나 있지만 성능은 order book 표현 선택에 크게 좌우되고 volume representation이 유리하다. Nasdaq만 다뤘다. — [arXiv 2211.13777](https://arxiv.org/abs/2211.13777), [IJF 40(4):1587–1621](https://ideas.repec.org/a/eee/intfor/v40y2024i4p1587-1621.html). Briola, Bartolucci, Aste: 미시구조 특성이 모형 효능을 좌우하고, 완결된 거래 단위로 평가하는 틀과 LOBFrame을 공개했다. — [arXiv 2403.09267](https://arxiv.org/abs/2403.09267)(Quantitative Finance 2025 게재는 검색 요약 기준, 미검증)
- 2025: Square-root δ = 1/2 strict universality(TSE) — [arXiv 2411.13965](https://arxiv.org/abs/2411.13965). Spoofability 학습 — [arXiv 2504.15908](https://arxiv.org/abs/2504.15908). 암호화폐 LOB 연구가 제목부터 "Better Inputs Matter More Than Stacking Another Hidden Layer"라고 주장한다 — [arXiv 2506.05764](https://arxiv.org/abs/2506.05764)(본문 미확인). Microprice 후속 "Boltzmann Price" — [arXiv 2507.09734](https://arxiv.org/abs/2507.09734)(본문 미확인)

### Inferences
- krx-quant의 방향(Shannon의 multinomial logistic, Turing의 LightGBM)은 이 흐름과 맞는다. 모형을 키우기보다 **입력을 OFI 계열로 정규화하는 것**이 우선이다.

### Gaps
- 2026년에 나온 OFI·LOB 예측 논문은 이번 조사의 검색으로 특정하지 못했다.

## 9. krx-quant 적용 메모

### Takeaway
기존 알고리즘이 OFI, depth imbalance, Hawkes, 선물 lead-lag, microprice의 큰 축은 이미 다룬다. 빠진 것은 (1) **depth로 정규화한 OFI와 integrated(PCA) OFI**, (2) **best-level queue imbalance와 Stoikov식 microprice 조정값을 event-time 짧은 창에서** 계산하는 것, (3) **세 종목과 선물 사이의 cross-asset lagged OFI**, (4) **부호 surprise와 회원사 flow 기반 분할 주문 대리변수**, (5) square-root 기반 변동폭 사전값이다. VPIN, iceberg, spoofing은 증거가 약하거나 데이터가 부족해 우선순위가 낮다.

### Cited Findings
- 기존 커버리지 대조(근거는 위 절들):
  - **Shannon**(OFI 1m/5m, 10레벨 depth imbalance, trade-sign entropy) → [CKS](https://arxiv.org/abs/1011.6402), [MLOFI](https://arxiv.org/abs/1907.06230) 계열을 다룬다. 다만 OFI는 10초 단위 자기상관이 금방 사라지므로 1m/5m 창은 감쇠 구간보다 길다.
  - **Kalman**(선물 입력 state-space microprice) → microprice 개념을 다룬다. Stoikov식 g(I, S) 조정은 별도다([Stoikov](https://ideas.repec.org/a/taf/quantf/v18y2018i12p1959-1966.html)).
  - **Poisson**(Hawkes 체결 도착, 10s/60s 감쇠) → activity를 다룬다([Bacry et al.](https://arxiv.org/abs/1502.04592)). 매수·매도·취소로 나눈 다변량 강도 비대칭은 없다.
  - **Granger**(선물·지수·미국 lead-lag) → 파생 → 현물 경로를 다룬다([KAIST](https://koasas.kaist.ac.kr/handle/10203/91122), [Kang et al.](https://ideas.repec.org/a/wly/jfutmk/v42y2022i3p338-364.html)). 수익률 lead-lag이지 OFI cross-impact는 아니다([CCZ](https://arxiv.org/abs/2112.13213)).
  - **Mandelbrot / Breiman** → range와 barrier에 square-root impact 사전 정보를 쓸 수 있다([Sato & Kanazawa 2025](https://arxiv.org/abs/2411.13965)).

### Inferences
빠진 신호, 필요한 데이터, 우선순위(추론):

| 우선 | 빠진 신호 | 왜 | 필요한 데이터 | 넣을 곳 |
|---|---|---|---|---|
| 1 | Queue imbalance I = Qb/(Qb+Qa)와 microprice − mid (틱 단위) | large-tick에서 다음 mid 방향 AUC 0.75~0.8. k=1 touch에 직접 해당 | KIS 실시간 1호가 잔량과 스프레드(이미 있음). 종목별 "스프레드 = 1틱 비율" 측정 | Shannon, Turing feature. g(I,S)는 과거 호가에서 Markov chain으로 추정 |
| 2 | Depth 정규화 OFI(OFI / 평균 depth)와 10~30초 창, integrated OFI(10레벨 OFI의 PCA 1성분) | β ∝ 1/depth, 장 초반 depth 절반. integrated OFI의 OS 설명력 84% vs 65% | 10호가 스냅샷 시계열(이미 있음) | Shannon의 OFI 대체 또는 보강 |
| 3 | Cross-asset lagged OFI(005930↔000660↔009150, KOSPI200 선물 OFI) | 1분 앞 예측에서 자기 OFI보다 우세하지만 빠르게 감쇠 | 세 종목 호가(있음). **선물 실시간 호가**가 필요한데, 현재 선물 데이터가 체결가 위주인지는 미확인 | Granger 확장(수익률 대신 OFI lead) |
| 4 | 체결 부호 surprise: sign − EWMA(sign), 회원사별 동일 방향 지속도 | 부호 장기기억은 가격에 이미 반영돼 있어 surprise만 정보. TSE는 계좌 단위 분할 주문을 검증 | 체결 방향 필드(없으면 tick rule), 회원사 flow(있음) | Shannon, Turing |
| 5 | 다변량 Hawkes 강도 비대칭(매수 MO − 매도 MO 강도, 취소 강도) | activity → 방향으로의 확장. 정량 증거는 부족 | 체결 스트림과 호가 변화에서 추정한 취소(근사) | Poisson |
| 6 | 변동폭 사전값 σ√(누적 거래량 / 일평균 거래량) | square-root impact | 1분봉과 일거래량(있음) | Mandelbrot, Breiman barrier |
| 낮음 | VPIN | 거래량·변동성 통제 후 추가 예측력 없음 | 체결 | 필요하면 Mandelbrot ablation만 |
| 낮음 | Iceberg 복원 패턴, 취소율, spoof 의심 | 방향 증거 없음. order-by-order 없이 근사만 가능 | 체결과 호가 스냅샷 동기화 | 탐색 연구용 |

- 평가: 이 신호들은 수초~1분에서 강하고 4분에서 희석된다. touch target을 k별로 쪼개 "k=1에서만 이득"인지 확인해야 한다. hit rate(up/flat/down)와 함께 flat 비율의 변화도 같이 봐야 한다. CCZ처럼 R²가 음수여도 방향 분류는 쓸모 있을 수 있다.
- 실행: 사용자가 인버스 ETF로 down을 실행하면, 개별 종목 1~2틱 신호는 ETF의 스프레드와 비용을 넘기 어렵다. 엔진의 down 신호가 실제로 쓸모 있으려면 지수 수준의 움직임이나 더 큰 k와 연결돼야 한다.

### Gaps
- KIS 실시간 체결의 매수·매도 방향 필드와 KOSPI200 선물 실시간 10호가 제공 여부는 이번 조사에서 확인하지 않았다. 코드나 문서로 확인해야 한다.
- KRX 세 종목의 tick/price 비율과 스프레드 1틱 비율은 krx-quant 자체 데이터로 측정해야 한다. 공개 연구 값은 없다.
