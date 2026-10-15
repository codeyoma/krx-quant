"""A stand-in for GitHub's issue comments and labels REST API (#214), on a local port."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

TOKEN = "test-token"
REPO = "codeyoma/krx-quant"


@dataclass
class FakeGitHub:
    url: str = ""
    comments: list[tuple[int, str]] = field(default_factory=list)  # (issue, body) in order
    labels: dict[int, set[str]] = field(default_factory=dict)
    requests: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)  # method, path, headers
    fail_comments: int = 0  # the next this many comment posts fail (HTTP 502)
    fail_labels: int = 0  # likewise the label changes

    def bodies(self, issue: int) -> list[str]:
        return [body for n, body in self.comments if n == issue]

    def _authorized(self, request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {TOKEN}"

    async def _comment(self, request: web.Request) -> web.Response:
        self._log(request)
        if not self._authorized(request):
            return web.json_response({"message": "Bad credentials"}, status=401)
        if self.fail_comments:
            self.fail_comments -= 1
            return web.Response(status=502, text="Bad Gateway")
        issue = int(request.match_info["issue"])
        body: dict[str, Any] = await request.json()
        self.comments.append((issue, body["body"]))
        url = f"https://github.com/{REPO}/issues/{issue}#issuecomment-{len(self.comments)}"
        return web.json_response({"id": len(self.comments), "html_url": url}, status=201)

    async def _add_labels(self, request: web.Request) -> web.Response:
        self._log(request)
        if self.fail_labels:
            self.fail_labels -= 1
            return web.Response(status=502, text="Bad Gateway")
        issue = int(request.match_info["issue"])
        body: dict[str, Any] = await request.json()
        self.labels.setdefault(issue, set()).update(body["labels"])
        return web.json_response([{"name": n} for n in sorted(self.labels[issue])])

    async def _remove_label(self, request: web.Request) -> web.Response:
        self._log(request)
        issue, name = int(request.match_info["issue"]), request.match_info["name"]
        if name not in self.labels.get(issue, set()):
            return web.json_response({"message": "Label does not exist"}, status=404)
        self.labels[issue].discard(name)
        return web.json_response([{"name": n} for n in sorted(self.labels[issue])])

    def _log(self, request: web.Request) -> None:
        self.requests.append((request.method, request.path, dict(request.headers)))

    def app(self) -> web.Application:
        app = web.Application()
        base = "/repos/{owner}/{repo}/issues/{issue}"
        app.router.add_post(f"{base}/comments", self._comment)
        app.router.add_post(f"{base}/labels", self._add_labels)
        app.router.add_delete(f"{base}/labels/{{name}}", self._remove_label)
        return app


@asynccontextmanager
async def fake_github() -> AsyncIterator[FakeGitHub]:
    fake = FakeGitHub()
    server = TestServer(fake.app())
    await server.start_server()
    fake.url = str(server.make_url("")).rstrip("/")
    try:
        yield fake
    finally:
        await server.close()
