# The nightly recording-quality report

Every trading night the scheduled job `recording-quality-570` runs `krx-quant recording-quality` right after the trader's report (about 22:31 KST) and posts one comment on issue #571, which stays open. The report covers that trading day: the WebSocket disconnects in the session by feed, the recorded one-minute bars against KIS's backfilled ones, the raw frames that came short of fields or could not be parsed, the REST polls' failures, and the night futures' recording of the night that ended that morning against the nights before. Full description, sections and thresholds with their reasons: `docs/operations.md`, "기록 품질 리포트 (#570)"; the code: `src/krx_quant/recording_quality.py`.

## When it is your turn

The issue gets `ready-for-agent` only on a night past a threshold, or when the job failed. The report's second line says why: `에이전트 차례: <reason>; <reason>; …`, or `에이전트 차례: 아님(…)` on a night that is only a record. Each reason states its figure and its threshold, for example `장중 끊김 4번(기준 3번 이상)` or `005930 결측 구간 밖의 빠진 분 2개(기준 1개 이상)`. A non-zero exit code or `시간 초과` in the comment's heading means the report itself did not finish: read the job's lines in Loki (`{service="krx-quant", process="job"} |= "job=recording-quality-570 "`, [server-logs.md](server-logs.md)).

The job never takes a label off #571. Once you are done, remove `ready-for-agent` yourself.

## Read

```bash
gh api --paginate --slurp repos/codeyoma/krx-quant/issues/571/comments \
  | jq -r 'add // [] | .[-5:][] | .created_at + "\n" + .body + "\n"'
```

Read the routine nights before the labelled one too: a reason that recurs over several nights is a trend, one night alone may be the market or KIS.

## Do one of these

- **Find the cause and open an issue for it.** One issue per cause, not per night: if an open issue already covers it, comment there with the night and the figures instead. Use the evidence the report points at:
  - Disconnects: the collector's `gap` and `KIS WebSocket` lines in Loki around each start the report lists ([server-logs.md](server-logs.md), "The WebSocket around the close" shows the query).
  - Missing or differing minutes outside a gap: the report counts them but does not list them. Look first for a feed that fell back to REST that day (the report's "스키마와 다른 파일" lists event files without trade columns), for a restart in the session (Loki, the engine's start line), and at the same night's disconnect section. If that does not explain them, the issue you open asks for a probe that lists the minutes (a scheduled job, AGENTS.md "Timed server work").
  - Rows possibly recorded twice: a reconnect that replayed trades, or a partition read twice (`archive.partition_files`).
  - Frames short of fields or not parsed: the collector's WARNING and ERROR lines of that day (`could not normalize`, `unparseable message kept raw`, `frame short of fields kept raw only`).
  - REST failures: the `poll <TR> <key> failed` and `poll outage of` lines (server-logs.md, "REST poll outages").
  - The night: whether KRX held a night session (it skips the evening before a long holiday without saying so through KIS's holiday API, `docs/kis-feeds.md` "야간선물 (#264)"), the contract the collector subscribed (`contracts.night_front_month`; the first roll is 2026-12-10's evening), and the subscription answers in the report's first night line.
  - Memory over its bound: the report's last section; compare with the nights before.
- **Leave a note when there is nothing to fix.** A night KRX did not trade, a known KIS outage, a threshold that turned out too tight: say so on #571 in one comment. If a threshold is wrong for the data, open an issue to change its constant in `recording_quality.py` with the nights that show it; the thresholds without a baseline yet (the bars' 1%, the night's quarter) are the first to look at.

The comment shows no prices or volumes: counts, seconds, rates, times, feed names, keys and contract codes only. Keep it that way in what you write.
