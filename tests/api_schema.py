"""The Engine API's /v1 contract, ``schemas/engine-api-v1.json``, for tests to check answers against."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "engine-api-v1.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text())

# Each GET route and the definition of its answer.
ROUTES = {
    "/v1/status": "status",
    "/v1/symbols": "symbols",
    "/v1/symbols/{symbol}/latest": "latest",
    "/v1/symbols/{symbol}/predictions": "predictions",
    "/v1/symbols/{symbol}/bars": "bars",
    "/v1/symbols/{symbol}/candles": "candles",
    "/v1/symbols/{symbol}/special": "special",  # #236
    "/v1/symbols/{symbol}/supply": "supply",  # #226
    "/v1/symbols/{symbol}/flow": "flow",  # #306
    "/v1/symbols/{symbol}/closing-flow": "closing_flow",  # #305
    "/v1/symbols/{symbol}/closing-flow/days": "closing_flow_days",
    "/v1/algorithms": "algorithms",
    "/v1/algorithms/{name}": "algorithm_detail",
    "/v1/algorithms/{name}/stats": "algorithm_stats",
    "/v1/leaderboard": "leaderboard",
    "/v1/cost-map": "cost_map",  # #307
    "/v1/consensus": "consensus",
    "/v1/macro": "macro",
    "/v1/calendar": "calendar",
    "/v1/briefing": "briefing",  # #304
    # The kinds besides the direction (#179).
    "/v1/kinds": "kinds",
    "/v1/symbols/{symbol}/kinds/{kind}/latest": "kind_latest",
    "/v1/symbols/{symbol}/kinds/{kind}/predictions": "kind_predictions",
    "/v1/trader/trades": "trader_trades",  # #394
    "/v1/trader/policies": "trader_policies",  # #399
}
# The routes of a kind besides the direction: answered only by an engine predicting one.
KIND_ROUTES = {"kind_latest", "kind_predictions"}
# The WebSocket route and the definition of each of its messages.
STREAM = {"/v1/stream": "event"}


def check(definition: str, body: Any) -> None:
    """``body`` keeps ``$defs/<definition>`` of the schema."""
    validator = Draft202012Validator({"$ref": f"#/$defs/{definition}", "$defs": SCHEMA["$defs"]})
    errors = [
        f"{'/'.join(map(str, e.absolute_path)) or '(top)'}: {e.message}" for e in validator.iter_errors(body)
    ]
    assert not errors, f"{definition}:\n" + "\n".join(errors[:20])
