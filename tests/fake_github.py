"""A stand-in for GitHub's issue comments and labels REST API (#215), and its issue list and
repository labels (#322), on a local port."""

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
    fail_unlabels: int = 0  # likewise taking a label off
    fail_lists: int = 0  # likewise the issue lists
    pulls: set[int] = field(default_factory=set)  # the numbers that are pull requests
    closed: set[int] = field(default_factory=set)
    repo_labels: dict[str, dict[str, str]] = field(default_factory=dict)  # name -> the label
    refuse_label_create: bool = False  # making a label is refused (HTTP 403)
    titles: dict[int, str] = field(default_factory=dict)  # an issue's title, anyone's text

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
        if self.fail_unlabels:
            self.fail_unlabels -= 1
            return web.Response(status=502, text="Bad Gateway")
        issue, name = int(request.match_info["issue"]), request.match_info["name"]
        if name not in self.labels.get(issue, set()):
            return web.json_response({"message": "Label does not exist"}, status=404)
        self.labels[issue].discard(name)
        return web.json_response([{"name": n} for n in sorted(self.labels[issue])])

    async def _list_issues(self, request: web.Request) -> web.Response:
        """Open issues and pull requests carrying every label of ``labels``, as GitHub lists them."""
        self._log(request)
        if not self._authorized(request):
            return web.json_response({"message": "Bad credentials"}, status=401)
        if self.fail_lists:
            self.fail_lists -= 1
            return web.Response(status=502, text="Bad Gateway")
        wanted = {n for n in request.query.get("labels", "").split(",") if n}
        state = request.query.get("state", "open")
        items = []
        for number in sorted(self.labels):
            if wanted - self.labels[number] or (state == "open" and number in self.closed):
                continue
            item: dict[str, Any] = {
                "number": number,
                "state": "closed" if number in self.closed else "open",
                "title": self.titles.get(number, f"issue {number}"),
                "body": "",
                "labels": [{"name": n} for n in sorted(self.labels[number])],
            }
            if number in self.pulls:
                item["pull_request"] = {"url": f"https://api.github.com/repos/{REPO}/pulls/{number}"}
            items.append(item)
        return web.json_response(items[: int(request.query.get("per_page", "30"))])

    async def _get_label(self, request: web.Request) -> web.Response:
        self._log(request)
        label = self.repo_labels.get(request.match_info["name"])
        if label is None:
            return web.json_response({"message": "Not Found"}, status=404)
        return web.json_response(label)

    async def _create_label(self, request: web.Request) -> web.Response:
        self._log(request)
        if self.refuse_label_create:
            return web.json_response(
                {"message": "Resource not accessible by personal access token"}, status=403
            )
        body: dict[str, Any] = await request.json()
        if body["name"] in self.repo_labels:
            return web.json_response({"message": "Validation Failed"}, status=422)
        self.repo_labels[body["name"]] = {k: str(v) for k, v in body.items()}
        return web.json_response(self.repo_labels[body["name"]], status=201)

    def _log(self, request: web.Request) -> None:
        self.requests.append((request.method, request.path, dict(request.headers)))

    def app(self) -> web.Application:
        app = web.Application()
        base = "/repos/{owner}/{repo}/issues/{issue}"
        app.router.add_post(f"{base}/comments", self._comment)
        app.router.add_post(f"{base}/labels", self._add_labels)
        app.router.add_delete(f"{base}/labels/{{name}}", self._remove_label)
        app.router.add_get("/repos/{owner}/{repo}/issues", self._list_issues)
        app.router.add_get("/repos/{owner}/{repo}/labels/{name}", self._get_label)
        app.router.add_post("/repos/{owner}/{repo}/labels", self._create_label)
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
