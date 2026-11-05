# The trader's nightly report

The auto trader (#393) reports every trading night on issue #417, which stays open. The scheduled job `trader-report` runs `krx-quant refit-policy --report` after the nightly training: it judges the challengers' cohort on a review night (#473), fits the night's policy (#399, #400), forms the next cycle's cohort on a review night, and posts one comment. The day's report is on top, the re-fit's own output below it. Full description: `docs/operations.md`, "예약 작업 (#215)" and "자동 매매 (#394)"; the rules: `docs/design.md` chapter 14.

## When it is your turn

The issue gets `ready-for-agent` only on a night that calls an agent. A report's second line says why: `에이전트 차례: <reasons>`, or `에이전트 차례: 아님(…)` on a night that is only a record. The reasons:

| Reason | Means |
|---|---|
| `이번 주 마지막 거래일(<day>요일)` | The weekly review: Friday, or the last trading day before a Friday the market is closed. |
| `하루 손실 한도에 닿음`, `누적 손실 한도에 닿음` | A loss limit stopped the trader that day. |
| `첫 챔피언`, `챔피언 교체` | The policy the trader trades changed that night. |
| `실전 관문을 넘음` | The champion passed the live gate at a checkpoint judged that night (#408: every 20th day of the trader's paper history, at α_k = 0.025 × 6 ÷ (π² k²)). |
| `실전 운용 상한을 올림(첫 단계를 마침)`, `실전 첫 단계 점검을 벗어남(새 진입 정지)` | In live mode (#408), the first period at the 250,000-won cap ended that day: lifted to the capital, or a check breached and new entries paused until the user resumes. |
| `KIS 주문 실패 N건(첫 실패 <time>)` | In mock mode (#405), a KIS order failed that day: refused, without an answer (its outcome unknown), or not sent. New entries stopped for the rest of the day. The report's limits section lists each one with KIS's code and message. |
| `밤 정책 맞추기 실패`, or a non-zero exit code in the comment's heading | The night's work did not finish. |

The job never takes a label off #417. Once you are done, remove `ready-for-agent` yourself.

#417 also gets the live account's read (#408, the scheduled job `probe-live-account-408`, `KIS 실전 계좌 읽기 프로브 (#408)`): once the live key is set, it reads the live account by inquiry calls alone and checks each field the trader's broker reads. `결과: 같다` passes the live mode's `live_probe` check: note it in `docs/kis-feeds.md` "실전 서버 (#408)" (the ⚠ it measured). `결과: 다르다` names the fields missing or not as read: open an issue and a PR fixing the reads in `src/krx_quant/kis_orders.py` (with fake answers in the measured shape), as for the mock probe (#403).

## Read

Read every comment since your last turn, not only the labelled one: the routine nights before it are part of the week.

```bash
gh api --paginate --slurp repos/codeyoma/krx-quant/issues/417/comments \
  | jq -r 'add // [] | .[-6:][] | .created_at + "\n" + .body + "\n"'
```

Each report holds, in order:

1. The day's ledger and the champion's trades: times, cell, probability and θ, p* and the hit rate's lower bound, shares, how each ended (`take_profit`, `stop_loss`, `time`, `pause`, `daily_limit`, `cumulative_limit`), the ticks between its fills, the costs and the net P&L in won. Under each trade its execution cost (#475, `실행: …`): each leg's adverse ticks from the mid-point the book knew when it decided it (positive: bought above it, sold below it; a take-profit's from its target, the limit resting from the buy) and the seconds from that decision to its order and to its fill; then the day's means (`실행 비용 (#475): …`), whose line says whose clock the times are on (paper: the quote the fill was made on; mock and live: when the trader read the fill off KIS's order inquiry, never KIS's own fill time, so a KIS leg's delay holds the inquiry's interval). Then what the champion's book did with the predictions it read: bought, tried to buy (`target_reached`, `no_quote`, `bad_stop`: the ask already under the stop, #471, …), skipped (`not_up`, `below_threshold`, `holding`, `special`, and the risk rules' `cooldown` after three net losses in a row and `buy_limit` at the day's 20 buy orders, #471, …).
   On a mock day (#405), then the KIS mock trades against the champion's paper shadow: each side's trades and net, the difference, and per prediction the entry and exit fill differences in ticks (KIS − shadow) and the net difference. The policies' records (3 and 5 below) are paper's in every mode: the shadow's for the champion in mock mode. A growing gap is execution, not the policy: it matters for the live mode (#408), not for a policy PR. With the mock practice on (#427, `KRX_TRADER_MOCK_PRACTICE=on`), a day no champion cell traded shows the practice apart: `연습 매매 N건(KIS 모의, …)` (one share of the practice cell a time, at its θ: since #474 the cell and θ frozen for the review cycle, `연습 칸 (#474): 주기 N에 고정 · …` under the pick with its replayed figures and, if its predictor went away, `gone` for the rest of the cycle; before #474 the cost map's greatest lower bound less p* at each pick), the predictions read on the practice pick, and the day's `연습 칸(KIS 모의, …)` under the pick. Since #548 the practice has no paper shadow and is left out of the KIS-against-shadow pairs (a day of practice alone has no such section): its legs are measured in the execution checks' `gap` below. Before #548 its pairs were marked `(연습)`, against the practice shadow. Practice trades count in the ledger, the limits and the live mode's execution checks, never in a policy's record: they are no evidence for a policy PR.
2. The day's pick from the cost map. When no cell traded, every cell considered and its verdict (`fail`, `few`, `no row`, …).
3. The review cycle (#469, `검토 주기 (#469): …`: the evaluation version, the cycle, its first day and review day, the day's place in it), then each policy's paper record to that day (on the current evaluation version's days alone) and, for a challenger, where it stands against its cohort's champion on the days of its cycle so far: common days, mean difference, lower bound (Bonferroni within the cohort), and where that stands now (`지금은 …`). That is a figure, not a verdict: a cohort is judged once, on its cycle's review night (every 20th trading day, after its close, #473), whose report gives the verdict (`그 밤 판정: …`). Nothing changes between reviews, whatever the figures say.
4. What the night changed: the champion, the cohort judged and each member's verdict (`- 묶음 판정 (#473): …`: promoted, passed, not above, undecided), the policies dropped, the cohort formed for the next cycle (`- 새 묶음 (#473): …`), the policies fitted and their cells, the policies waiting for the next cohort, a re-fit that kept nothing or failed.
5. The live gate (#408): each checkpoint judged that night (`실전 관문 판정 k번째 …`: its day, the champion then, its days as the champion (never its days as a challenger), their mean and lower bound at α_k, and the same with one extra adverse tick a leg), then the champion's standing (`실전 관문: …`: its days as the champion, the next checkpoint and its α, or the checkpoint it passed at).
   Then `실전 준비 (#408)`: whether the next pick would order on the live account, the gate and each execution check (✓ or ✗) on the mock period (since #548 the gap check measures every KIS leg against the paper rules' price at the quote it was decided on, and says how many of the legs it measured: under 90% it fails), on a mock or live day that day's orders compared with KIS's (`같다`, `다르다`, not yet), and on a live day the first period (cap, live days and round trips, its checks).
6. Limits, the risk rules that day (#475, `- 위험 규칙 (#471): …`: the loss budget in won, the day's buy orders against their limit, the cooldowns three net losses in a row began and the losses in a row at the day's last exit), the halts that day, the failed KIS orders that day (mock, #405), a cumulative halt still holding, the pause in force, the pauses, resumes and re-arms asked for.
7. The last five trading days' ledger.

Below the report, the re-fit's own output (#399): the cost map (every cell it names is tried, whatever its verdict, #472), the entry rule (`진입 (#472): …`: the grid, the evidence a θ needs, the Bonferroni level of the night's lower bounds), then the recorded days it replayed by the paper trader's own rules (#470, `재생 (#470): …`: the days, those without a recording and those with a gap in the session, the passes). Each cell kept with its θ (#471: no Kelly fraction λ; a trade's loss budget sizes every buy) and what it entered on (its signals and buys at that θ, the net per signal and its lower bound), or left out and why (`few_days`, `few_trades`, `no_edge`), and under it the cell's signals measured alone a share at each θ (`1주씩 따로 잰 신호: θ … 신호 N, 체결 M, 신호당 …틱(날 단위 95% 하한 …)`), then its calibration (#475, `보정 (#475, …)`: its rises in five bins of fixed edges, the grid's θ, each with its answers' classes `상승`, `보합`, `하락`, the class right, the upper target reached first and the share of those bought alone that netted above 0; the direction's `up` is a touch at any time, so its `목표 먼저` can be below its `클래스 맞음`). The calibration is a description: the replayed net enters a cell, and nothing changes a θ by the table. Then the whole policy's replay, its daily net and that mean's 95% lower bound: a policy whose bound is not above 0 is not kept (`새 정책 없음 · … (#472, policy_not_above)`). A signal of a day without its recording or in a recorded gap is left out and counted (`데이터가 빠져 뺀 신호 …`), never as a trade of no gain. Days without a recording or with gaps are the collector's problem, not the policy's: open an issue when they recur. With the mock practice on, the night's practice cell follows (#474, `연습 칸 (#474): …`): chosen for the review cycle in force when it has none (on a review night the next cycle's), kept as it is otherwise (`… 그대로다`), or not chosen and why (`다음 밤에 다시`: no practice until a night chooses one). It is mock execution practice, no evidence: never a reason for a policy PR.

  > **바뀜** (2026-11-03, #480): 비용 지도가 통과시킨 칸들을, 재생한 운용 상한이 가장 크게 자라는 격자 점에서 넣었다(`few_trades`, `no_gain`) → 이름이 있는 칸 모두를, 1주씩 재생한 신호당 순손익의 날 단위 하한이 0을 넘는 가장 낮은 θ로 넣는다. 이유: spec #468 3절 "진입".

The comment shows no prices, account numbers, order numbers or keys. For more, see [server-logs.md](server-logs.md): the trader's lines are `{service="krx-quant", process="trader"}`, the night job's `{service="krx-quant", process="job"} |= "job=trader-report "`. For the server's state now, see [server-snapshot.md](server-snapshot.md).

## Do one of these

- **Nothing to change.** Comment on #417 in Korean with a short summary: the week's net P&L, the gate's progress, what you checked and why no change is warranted. Then remove the label.
- **Propose an improvement.** Open an issue that cites the report comments it rests on, then a PR that closes it ([pr-checklist.md](pr-checklist.md)). Examples: a new feature or algorithm for a kind whose cell keeps being left out (`no_edge`); an entry or exit rule the evidence calls for (many take-profits that net a loss, many `target_reached`, …); a change to the nightly fit's grid; a cell whose calibration does not rise first more often in its higher bins (the score does not rank), or execution costs that eat a cell's edge (#475). An improvement goes through the same paper gate as everything else:
  - A new feature or algorithm passes the engine's own gates first (pre-validation, probation). The trader trades it only once the nightly fit enters its cell (the cost map names it, and its replayed one-share net has a lower bound above 0, #472) and a challenger fitted with that cell, entered in a cohort at a review cycle's start, beats the champion in paper on that cycle's review night (#400, #469, #473).

    > **바뀜** (2026-11-03, #480): "once the cost map passes its cell" → 밤 맞추기가 그 칸을 넣으면. 이유: 비용 지도 통과가 더 이상 조건이 아니다(spec #468 3절).
  - A trading rule must be a value a policy carries, so that the champion trades as before and a challenger with the rule has to win in paper first. A PR must not change how the current champion trades. If the rule cannot be expressed as a policy value, ask the user before building it.
  - Comment on #417 with the issue and PR links, then remove the label.
- **Ask the user.** Comment on #417 in Korean with the question and the evidence, add `ready-for-human`, and remove `ready-for-agent`. Ask when:
  - the cumulative limit holds: re-arming is the user's call (`krx-quant trade rearm` on the server);
  - the live gate passed, or every execution check passes: going live is the user's call and their steps (`docs/operations.md` "실전 (#408)"). Never treat a passed gate as permission;
  - in live mode, the first period breached (a pause of source `ramp`) or a day's orders differ from KIS's (`그날 주문의 KIS 대조: 다르다`): only the user can check the live account and resume. Read the live day's report against `docs/kis-feeds.md` "실전 서버 (#408)": the first live days measure the live server's answers, and a field that differs from the mock's is a bug to fix in a PR (the trader stops new entries meanwhile);
  - a change would touch the limits, the capital, the modes, the live gate or the promotion rule;
  - losses or halts recur and the evidence does not point to one fix;
  - a KIS order's outcome is not known (`응답 없음(결과 모름)`) and was not settled: the trader looks up a buy or a sell whose answer was lost and adopts it when exactly one KIS order can be it (#406, #460); a sell no KIS order can be is concluded `absent` once the inquiry and a balance read show it did not go in (#464: on the mock account about two and a half minutes after it was sent, on the live one once its day is over). A sell left unsettled is one that more than one KIS order could be, or one the account lacks the shares for: ask the user to check the account. If the report shows a pause of source `계좌 맞추기` (a mock start found the account unlike the ledger, #406), only the user can check the account and resume, which accepts what is outside the ledger. A refused order with a code the trader should not have caused (off the tick, more than held) is a bug: open an issue with the code and the failure's line.

  > **바뀜** (2026-10-31, #424): "결과를 모르는 주문은 계좌 맞추기(#406) 전까지 사용자만 보고 정리한다" → trader가 mock 시작에서 계좌를 맞추고 응답이 사라진 매수를 장부에 넣는다. 계좌 맞추기의 정지는 여전히 사용자가 확인하고 재개한다. 이유: #406.

  > **바뀜** (2026-11-02, #465): "but a sell's is not looked up" → 응답이 사라진 매도도 찾아 장부에 넣고(#460, PR #463), 아무 KIS 주문도 아니면 `absent`로 정한다. 이유: 매도를 다시 보내지 않고 KIS에서 찾게 됐고(#460), KIS에 닿지 않은 매도는 조회에 끝내 보이지 않아 그 매매가 팔리지 않았다(#464).

## Never

- Change the limits, the capital or the mode, or widen any of them, nor the live gate's checkpoints, the execution checks or the first period's cap (#408). They come from the code and the server's `.env` (#396). No policy or PR makes the trader trade more than they allow.
- Add order code outside `src/krx_quant/kis_orders.py`, or place, resume or re-arm anything. Agents cannot reach the server, and live orders are the user's.
- Put prices, account numbers, order numbers or keys in an issue, PR or doc. Both repositories are public.
