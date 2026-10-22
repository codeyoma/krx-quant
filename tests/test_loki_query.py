"""``scripts/loki-query.sh`` (#358): an agent's query of the server's lines in Loki, the read token
taken from the Mac's file without ever being printed or put on a command line.

The fake Loki answers ``query_range`` on the test's event loop; the script runs as a process
beside it, with a home of its own (its ``~/.config/krx-quant/grafana.env``) and a Keychain
service no one has, so the user's real one is never read.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "loki-query.sh"
USER = "123456"
TOKEN = "glc_" + "R" * 40  # a made-up read token

pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="the script calls curl")


class FakeLoki:
    def __init__(self) -> None:
        self.url = ""
        self.asked: list[dict[str, str]] = []
        self.auth: list[str | None] = []
        self.answer: dict[str, Any] = {"status": "success", "data": {"resultType": "streams", "result": []}}
        self.status = 200

    async def query_range(self, request: web.Request) -> web.Response:
        self.asked.append(dict(request.query))
        self.auth.append(request.headers.get("Authorization"))
        if self.status != 200:
            return web.Response(status=self.status, text="parse error at line 1, col 2")
        return web.json_response(self.answer)


@pytest.fixture
async def fake() -> AsyncIterator[FakeLoki]:
    found = FakeLoki()
    app = web.Application()
    app.router.add_get("/loki/api/v1/query_range", found.query_range)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server(access_log=None)
    found.url = str(server.make_url("")).rstrip("/")
    yield found
    await server.close()


def home_with(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    config = tmp_path / ".config" / "krx-quant" / "grafana.env"
    config.parent.mkdir(parents=True)
    config.write_text(text)
    config.chmod(mode)
    return tmp_path


async def run(home: Path, *args: str, **env: str) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        "bash",
        str(SCRIPT),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "LOKI_KEYCHAIN_SERVICE": "krx-quant-loki-read-test-none",
            **env,
        },
    )
    out, err = await process.communicate()
    assert process.returncode is not None
    return process.returncode, out.decode(), err.decode()


async def test_it_asks_with_the_file_s_token_and_prints_the_lines_oldest_first(
    fake: FakeLoki, tmp_path: Path
) -> None:
    home = home_with(tmp_path, f'LOKI_URL="{fake.url}/"\nLOKI_USER={USER}\nexport LOKI_READ_TOKEN={TOKEN}\n')
    fake.answer["data"]["result"] = [
        {
            "stream": {"service": "krx-quant", "process": "training", "level": "error"},
            "values": [
                ["1792674000000000000", "night=2026-10-22 b"],
                ["1792673999000000000", "night=2026-10-22 a"],
            ],
        }
    ]
    code, out, err = await run(
        home, '{service="krx-quant", process="training"}', "--since", "2d", "--limit", "50"
    )
    assert code == 0
    assert out.splitlines() == [
        "2026-10-22 21:59:59.000 [training/error] night=2026-10-22 a",
        "2026-10-22 22:00:00.000 [training/error] night=2026-10-22 b",
    ]
    assert fake.asked == [
        {
            "query": '{service="krx-quant", process="training"}',
            "limit": "50",
            "direction": "backward",
            "since": "48h",
        }
    ]
    assert fake.auth == ["Basic " + base64.b64encode(f"{USER}:{TOKEN}".encode()).decode()]
    assert TOKEN not in out and TOKEN not in err


async def test_a_window_is_asked_in_kst_and_the_environment_s_token_comes_before_the_file(
    fake: FakeLoki, tmp_path: Path
) -> None:
    home = home_with(tmp_path, f"LOKI_URL={fake.url}\nLOKI_USER={USER}\nLOKI_READ_TOKEN=file-token-0000\n")
    code, _, err = await run(
        home,
        '{service="krx-quant", process="engine"} |= "WebSocket"',
        "--from",
        "2026-10-22T15:20",
        "--to",
        "2026-10-22T15:31:30",
        "--forward",
        LOKI_READ_TOKEN=TOKEN,
    )
    assert code == 0 and "-- 0 lines" in err
    (asked,) = fake.asked
    assert (asked["start"], asked["end"], asked["direction"]) == (
        "2026-10-22T15:20:00+09:00",
        "2026-10-22T15:31:30+09:00",
        "forward",
    )
    assert "since" not in asked
    assert fake.auth == ["Basic " + base64.b64encode(f"{USER}:{TOKEN}".encode()).decode()]


async def test_a_metric_query_prints_its_series(fake: FakeLoki, tmp_path: Path) -> None:
    home = home_with(tmp_path, f"LOKI_URL={fake.url}\nLOKI_USER={USER}\nLOKI_READ_TOKEN={TOKEN}\n")
    fake.answer["data"] = {
        "resultType": "matrix",
        "result": [{"metric": {"level": "error"}, "values": [[1792673999, "3"]]}],
    }
    code, out, _ = await run(home, 'sum by (level) (count_over_time({service="krx-quant"}[1h]))')
    assert code == 0 and out == '2026-10-22 21:59:59.000 {level="error"} 3\n'


async def test_what_is_missing_or_refused_is_said_without_the_token(fake: FakeLoki, tmp_path: Path) -> None:
    home = home_with(tmp_path, f"LOKI_URL={fake.url}\nLOKI_USER={USER}\n", mode=0o644)
    code, out, err = await run(home, '{service="krx-quant"}')
    assert code == 3 and out == ""
    assert "not set: LOKI_READ_TOKEN" in err and "readable by others (mode 644)" in err
    assert fake.asked == []
    fake.status = 400
    code, out, err = await run(home, "{bad", LOKI_READ_TOKEN=TOKEN)
    assert code == 4 and "Loki answered HTTP 400: parse error at line 1, col 2" in err
    assert TOKEN not in out + err
    code, _, err = await run(home, '{service="krx-quant"}', "--since", "soon", LOKI_READ_TOKEN=TOKEN)
    assert code == 2 and "--since must be" in err
