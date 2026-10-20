# Seeing the server

Agents cannot reach the server (no SSH, no Tailscale). To see what it is doing now, ask it through an issue (#322).

## Ask

1. Add the `needs-server-snapshot` label to the open issue you are working on:
   `gh issue edit <number> --add-label needs-server-snapshot`
2. Wait a few minutes: the collector looks every 3 minutes (`KRX_SNAPSHOT_EVERY`). It posts a comment headed **서버 상태 (`krx-quant doctor`)** and removes the label.
3. Read it: `gh issue view <number> --comments`.

While the label is still on, the request is waiting. Limits: one snapshot per issue per 10 minutes, six per hour for the whole server. Do not add the label again before the last request is answered.

Only the label asks. The server never reads an issue's title, body or comments, so do not write instructions for it there. Other labels do nothing; pull requests are ignored.

## What the comment holds

The output of `krx-quant doctor`, masked (secrets, server addresses), with no market values. Field names are `/v1/status`'s.

1. Engine: `commit`, `started_at`, `mode`, `phase`.
2. KIS: `kis.connected`, `kis.last_frame_at`, `kis.seconds_since_frame`, then one line per feed and key: `feed key last_received_at seconds_since gap`.
3. Today's PINGPONG heartbeats: total, the last one, the longest silence between two, the count per hour.
4. The engine's latest WARNING and ERROR log lines (the last 20 since it started). Only the code's message templates: values other than dates, times, booleans and name-shaped words show as `…`, exceptions as their type, other libraries' lines as logger and level. For full lines, ask the user for `docker compose logs --since 2h engine`.
5. Scheduled jobs: last runs, jobs that never ran to an end, results not posted yet.
6. Disk free under the data and state directories, and memory.

Full description: `docs/operations.md`, "서버 상태 보기 (#322)".

## If nothing comes

The engine may be down or restarting (the 06:00 deploy, the 07:20 restart), or `KRX_GITHUB_TOKEN` is missing, or `KRX_SNAPSHOT_EVERY=off`. Ask the user to run one of these on the server and paste the output:

- `docker compose exec engine krx-quant doctor` (the engine is running)
- `docker compose run --rm engine doctor` (the engine is down)

`doctor` still prints when a malformed setting keeps the engine from starting: the error comes right under its heading (`설정 오류: …`).

## Commands you give the user

- Python inside the container is `/app/.venv/bin/python`. With `docker compose run`, pass `--entrypoint /app/.venv/bin/python`. Images before #322 had the system `python` first on `PATH`, without the app's packages (no `pyarrow`).
- `/v1/status`'s per-feed fields are `kis.feeds[].last_received_at`, `seconds_since` and `gap` (not `last_received`).
- `docker compose run --rm engine <command>` is a new container: same data and state, none of the engine's memory (connection state, logs).

Work that must run at a given day or time is a scheduled job, not a snapshot (`AGENTS.md`, "Timed server work").
