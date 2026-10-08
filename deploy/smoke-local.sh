#!/usr/bin/env bash
# Run the engine in local Docker against the fake KIS server: record for a while, check
# that the Engine API answers, then archive twice and check that the second run changes
# nothing.
#
# Usage: deploy/smoke-local.sh   (KEEP=1 keeps the containers and volumes afterwards)
set -euo pipefail

cd "$(dirname "$0")/.."
compose=(docker compose -p krx-quant-smoke --env-file deploy/smoke.env
  -f compose.yaml -f deploy/compose.smoke.yaml)
cleanup() {
  if [[ -z "${KEEP:-}" ]]; then
    "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

listing() {
  # Every archived object with its digest, for comparing runs.
  "${compose[@]}" run --rm -T --no-deps --entrypoint /app/.venv/bin/python engine -c '
from pathlib import Path
from krx_quant.object_store import LocalObjectStore
store = LocalObjectStore(Path("/archive"))
for key in store.keys():
    print(store.digest(key), key)'
}

"${compose[@]}" build -q
"${compose[@]}" up -d fake-kis engine
echo "recording for ${SECONDS_TO_RECORD:=15}s"
sleep "$SECONDS_TO_RECORD"
port=$(grep '^KRX_ENGINE_PORT=' deploy/smoke.env | cut -d= -f2)
status=$(curl -fsS "http://127.0.0.1:$port/v1/status") ||
  { echo "smoke: the Engine API did not answer" >&2; exit 1; }
echo "engine status: $status"
"${compose[@]}" stop engine # flushes buffered rows

"${compose[@]}" run --rm -T engine archive
first=$(listing)
"${compose[@]}" run --rm -T engine archive
second=$(listing)

echo "$first"
grep -q "events/date=.*/feed=H0UNCNT0/symbol=005930/data.parquet" <<<"$first" ||
  { echo "smoke: no archived trades" >&2; exit 1; }
[[ "$first" == "$second" ]] || { echo "smoke: the second archive run changed objects" >&2; exit 1; }
"${compose[@]}" run --rm -T --no-deps --entrypoint /app/.venv/bin/python engine -c '
import glob, pyarrow.parquet as pq
[path] = glob.glob("/archive/events/date=*/feed=H0UNCNT0/symbol=005930/data.parquet")
print(f"archived trades: {pq.read_metadata(path).num_rows}")'
echo "smoke: recorded, archived, and archiving again changed nothing"
