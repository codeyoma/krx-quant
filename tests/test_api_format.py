"""Engine API responses read well as they are: Korean as is, indented JSON."""

from __future__ import annotations

import json
from pathlib import Path

from tests.replay_events import api, at, run


async def test_responses_are_indented_utf8_json_with_korean_as_is(tmp_path: Path) -> None:
    engine = await run(tmp_path / "engine.db", [], until=at("09:00:00"))

    async with api(engine) as client:
        response = await client.get("/v1/algorithms")
        raw = await response.read()

    assert response.headers["Content-Type"] == "application/json; charset=utf-8"
    text = raw.decode("utf-8")
    assert "추세 추종" in text  # not 추세 ...
    assert text.startswith('{\n  "algorithms": [\n')
    assert json.loads(text)["algorithms"][0]["card"]["character"] == "추세 추종"


async def test_errors_are_utf8_json_too(tmp_path: Path) -> None:
    engine = await run(tmp_path / "engine.db", [], until=at("09:00:00"))

    async with api(engine) as client:
        response = await client.get("/v1/leaderboard?period=week")
        body = await response.json()

    assert response.status == 400
    assert response.headers["Content-Type"] == "application/json; charset=utf-8"
    assert "period" in body["error"]
