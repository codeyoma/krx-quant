# The trader's nightly report

The auto trader (#393) reports every trading night on issue #417, which stays open. The scheduled job `trader-report` runs `krx-quant refit-policy --report` after the nightly training: it judges the challengers, fits the night's policy (#399, #400), and posts one comment. The day's report is on top, the re-fit's own output below it. Full description: `docs/operations.md`, "예약 작업 (#215)" and "자동 매매 (#394)"; the rules: `docs/design.md` chapter 14.

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

## Read

Read every comment since your last turn, not only the labelled one: the routine nights before it are part of the week.

```bash
gh api --paginate --slurp repos/codeyoma/krx-quant/issues/417/comments \
  | jq -r 'add // [] | .[-6:][] | .created_at + "\n" + .body + "\n"'
```

Each report holds, in order:

1. The day's ledger and the champion's trades: times, cell, probability and θ, p* and the hit rate's lower bound, shares, how each ended (`take_profit`, `stop_loss`, `time`, `pause`, `daily_limit`, `cumulative_limit`), the ticks between its fills, the costs and the net P&L in won. Then what the champion's book did with the predictions it read: bought, tried to buy (`target_reached`, `no_quote`, …), skipped (`not_up`, `below_threshold`, `holding`, `special`, …).
   On a mock day (#405), then the KIS mock trades against the champion's paper shadow: each side's trades and net, the difference, and per prediction the entry and exit fill differences in ticks (KIS − shadow) and the net difference. The policies' records (3 and 5 below) are paper's in every mode: the shadow's for the champion in mock mode. A growing gap is execution, not the policy: it matters for the live mode (#408), not for a policy PR.
2. The day's pick from the cost map. When no cell traded, every cell considered and its verdict (`fail`, `few`, `no row`, …).
3. Each policy's paper record to that day and, for a challenger, where it stands against the champion: common days, mean difference, lower bound, verdict.
4. What the night changed: the champion, challengers dropped and admitted, the policies fitted and their cells, the policies waiting, a re-fit that kept nothing or failed.
5. The live gate (#408): each checkpoint judged that night (`실전 관문 판정 k번째 …`: its day, the champion then, its record's days, mean and lower bound at α_k, and the same with one extra adverse tick a leg), then the champion's standing (`실전 관문: …`: its record, the next checkpoint and its α, or the checkpoint it passed at).
   Then `실전 준비 (#408)`: whether the next pick would order on the live account, the gate and each execution check (✓ or ✗) on the mock period, on a mock or live day that day's orders compared with KIS's (`같다`, `다르다`, not yet), and on a live day the first period (cap, live days and round trips, its checks).
6. Limits, the halts that day, the failed KIS orders that day (mock, #405), a cumulative halt still holding, the pause in force, the pauses, resumes and re-arms asked for.
7. The last five trading days' ledger.

The comment shows no prices, account numbers, order numbers or keys. For more, see [server-logs.md](server-logs.md): the trader's lines are `{service="krx-quant", process="trader"}`, the night job's `{service="krx-quant", process="job"} |= "job=trader-report "`. For the server's state now, see [server-snapshot.md](server-snapshot.md).

## Do one of these

- **Nothing to change.** Comment on #417 in Korean with a short summary: the week's net P&L, the gate's progress, what you checked and why no change is warranted. Then remove the label.
- **Propose an improvement.** Open an issue that cites the report comments it rests on, then a PR that closes it ([pr-checklist.md](pr-checklist.md)). Examples: a new feature or algorithm for a kind whose cell keeps failing the cost map; an entry or exit rule the evidence calls for (many take-profits that net a loss, many `target_reached`, …); a change to the nightly fit's grid. An improvement goes through the same paper gate as everything else:
  - A new feature or algorithm passes the engine's own gates first (pre-validation, probation). The trader trades it only once the cost map passes its cell and a challenger fitted with that cell beats the champion in paper (#400).
  - A trading rule must be a value a policy carries, so that the champion trades as before and a challenger with the rule has to win in paper first. A PR must not change how the current champion trades. If the rule cannot be expressed as a policy value, ask the user before building it.
  - Comment on #417 with the issue and PR links, then remove the label.
- **Ask the user.** Comment on #417 in Korean with the question and the evidence, add `ready-for-human`, and remove `ready-for-agent`. Ask when:
  - the cumulative limit holds: re-arming is the user's call (`krx-quant trade rearm` on the server);
  - the live gate passed, or every execution check passes: going live is the user's call and their steps (`docs/operations.md` "실전 (#408)"). Never treat a passed gate as permission;
  - in live mode, the first period breached (a pause of source `ramp`) or a day's orders differ from KIS's (`그날 주문의 KIS 대조: 다르다`): only the user can check the live account and resume. Read the live day's report against `docs/kis-feeds.md` "실전 서버 (#408)": the first live days measure the live server's answers, and a field that differs from the mock's is a bug to fix in a PR (the trader stops new entries meanwhile);
  - a change would touch the limits, the capital, the modes, the live gate or the promotion rule;
  - losses or halts recur and the evidence does not point to one fix;
  - a KIS order's outcome is not known (`응답 없음(결과 모름)`) and was not settled: the trader looks up a buy whose answer was lost and adopts it when exactly one KIS order can be it (#406), but a sell's is not looked up. If the report shows a pause of source `계좌 맞추기` (a mock start found the account unlike the ledger, #406), only the user can check the account and resume, which accepts what is outside the ledger. A refused order with a code the trader should not have caused (off the tick, more than held) is a bug: open an issue with the code and the failure's line.

  > **바뀜** (2026-10-31, #424): "결과를 모르는 주문은 계좌 맞추기(#406) 전까지 사용자만 보고 정리한다" → trader가 mock 시작에서 계좌를 맞추고 응답이 사라진 매수를 장부에 넣는다. 계좌 맞추기의 정지는 여전히 사용자가 확인하고 재개한다. 이유: #406.

## Never

- Change the limits, the capital or the mode, or widen any of them, nor the live gate's checkpoints, the execution checks or the first period's cap (#408). They come from the code and the server's `.env` (#396). No policy or PR makes the trader trade more than they allow.
- Add order code outside `src/krx_quant/kis_orders.py`, or place, resume or re-arm anything. Agents cannot reach the server, and live orders are the user's.
- Put prices, account numbers, order numbers or keys in an issue, PR or doc. Both repositories are public.
