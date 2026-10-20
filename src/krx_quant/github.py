"""Comments and labels on the repository's GitHub issues, where the scheduled jobs leave
their results (#215), and the issues a label asks the server's summary on (#322).

The REST API with a fine-grained token (``KRX_GITHUB_TOKEN``: this repository only, Issues
read and write). A comment is the job's heading line, its notes, its output in a code block
and any attachments folded in ``<details>``; one longer than GitHub's limit keeps the head
and the tail of the longest texts and says what was cut. What is posted is masked first
(``redaction.py``); this module does not look at the text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

from krx_quant.config import DEFAULT_GITHUB_API_URL, DEFAULT_GITHUB_REPO

API_VERSION = "2022-11-28"
COMMENT_LIMIT = 65_536  # GitHub's longest comment body, in characters
_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Room left for the notes saying what was cut, which are written after the cut.
_NOTE_ROOM = 300
# An attachment keeps at most this share of the limit once the comment is too long.
_DETAILS_SHARE = 8


class GitHubError(Exception):
    """GitHub did not take a comment or a label change: not set up, not reachable, or refused."""


class GitHub:
    """One repository's issues, as the token allows."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str | None,
        repo: str = DEFAULT_GITHUB_REPO,
        api_url: str = DEFAULT_GITHUB_API_URL,
    ) -> None:
        self._session = session
        self._token = token
        self._repo = repo
        self._api_url = api_url.rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self._token)

    async def comment(self, issue: int, body: str) -> str:
        """Post ``body`` on ``issue``; the comment's address."""
        data = await self._call("POST", f"/issues/{issue}/comments", {"body": body})
        return str(data["html_url"])

    async def relabel(self, issue: int, add: Sequence[str], remove: Sequence[str]) -> None:
        """Give ``issue`` the labels ``add`` and take ``remove`` off; one it lacks is no error."""
        if add:
            await self._call("POST", f"/issues/{issue}/labels", {"labels": list(add)})
        for name in remove:
            await self._call("DELETE", f"/issues/{issue}/labels/{quote(name, safe='')}", missing_ok=True)

    async def labelled(self, label: str, limit: int = 20) -> list[int]:
        """The numbers of the open issues (pull requests left out) that carry ``label``, oldest
        first, at most ``limit``. Nothing else of an issue is read: its text is anyone's."""
        query = urlencode(
            {"labels": label, "state": "open", "sort": "created", "direction": "asc", "per_page": limit}
        )
        data = await self._call("GET", f"/issues?{query}")
        if not isinstance(data, list):
            raise GitHubError(f"GET /issues: not a list of issues ({type(data).__name__})")
        found = []
        for item in data:
            if (
                not isinstance(item, dict)
                or "pull_request" in item
                or not isinstance(item.get("number"), int)
            ):
                continue
            names = {tag.get("name") for tag in item.get("labels") or () if isinstance(tag, dict)}
            if label in names:  # as asked: the filter is GitHub's, the check is ours
                found.append(item["number"])
        return found

    async def ensure_label(self, name: str, color: str, description: str) -> bool:
        """Make the repository's label ``name`` unless it is there; True if it was made."""
        if await self._call("GET", f"/labels/{quote(name, safe='')}", missing_ok=True) is not None:
            return False
        await self._call("POST", "/labels", {"name": name, "color": color, "description": description})
        return True

    async def _call(
        self, method: str, path: str, payload: dict[str, Any] | None = None, *, missing_ok: bool = False
    ) -> Any:
        if not self._token:
            raise GitHubError("KRX_GITHUB_TOKEN is not set")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        url = f"{self._api_url}/repos/{self._repo}{path}"
        try:
            async with self._session.request(
                method, url, json=payload, headers=headers, timeout=_TIMEOUT
            ) as resp:
                if resp.status == 404 and missing_ok:
                    return None
                if not 200 <= resp.status < 300:
                    detail = " ".join((await resp.text()).split())[:200]
                    raise GitHubError(f"{method} {path}: HTTP {resp.status} {detail}".rstrip())
                if resp.content_type != "application/json":
                    return None
                return await resp.json()
        except (aiohttp.ClientError, OSError, TimeoutError) as e:
            raise GitHubError(f"{method} {path}: GitHub not reachable ({type(e).__name__}: {e})") from e


def comment_body(
    heading: str,
    output: str,
    *,
    notes: Sequence[str] = (),
    details: Sequence[tuple[str, str]] = (),
    limit: int = COMMENT_LIMIT,
) -> str:
    """A comment: ``heading``, ``notes`` (a line each), ``output`` in a code block, and each
    of ``details`` (a title and a text) folded in ``<details>``.

    Longer than ``limit`` characters, each attachment keeps at most an eighth of it and the
    output what is left; a text cut keeps its head and tail (on line ends) and a line in its
    middle, and a note says how much of it was cut.
    """
    texts = [output, *(text for _, text in details)]
    titles = ["출력", *(title for title, _ in details)]
    body = _render(heading, notes, texts, details)
    if len(body) <= limit:
        return body
    room = limit - len(_render(heading, notes, ["" for _ in texts], details)) - _NOTE_ROOM * len(texts)
    sizes = [min(len(text), room // _DETAILS_SHARE) for text in texts[1:]]
    sizes.insert(0, max(0, room - sum(sizes)))
    cut_notes = list(notes)
    for i, size in enumerate(sizes):
        if len(texts[i]) > size:
            texts[i], chars, lines = _cut(texts[i], size)
            cut_notes.append(
                f"GitHub 댓글 한도({limit:,}자)를 넘어 {titles[i]}의 가운데 {chars:,}자({lines:,}줄)를 "
                "줄였다. 전체는 서버에서 같은 명령을 다시 돌려 본다."
            )
    return _render(heading, cut_notes, texts, details)[:limit]


def _render(
    heading: str, notes: Sequence[str], texts: Sequence[str], details: Sequence[tuple[str, str]]
) -> str:
    parts = [heading]
    if notes:
        parts.append("\n".join(notes))
    parts.append(_fenced(texts[0]))
    for (title, _), text in zip(details, texts[1:], strict=True):
        parts.append(f"<details><summary>{title}</summary>\n\n{_fenced(text)}\n\n</details>")
    return "\n\n".join(parts) + "\n"


def _fenced(text: str) -> str:
    """``text`` in a code block whose fence no run of backticks in it closes."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text.rstrip(chr(10))}\n{fence}"


def _cut(text: str, size: int) -> tuple[str, int, int]:
    """``text`` in at most ``size`` characters: its head and tail and a line between saying
    what was cut; with how many characters and lines were cut."""
    marker = "\n… (가운데를 줄였다) …\n"
    keep = max(0, size - len(marker))
    head, tail = text[: keep - keep // 2], text[len(text) - keep // 2 :] if keep // 2 else ""
    if "\n" in head:
        head = head[: head.rfind("\n") + 1]
    if "\n" in tail:
        tail = tail[tail.find("\n") + 1 :]
    middle = text[len(head) : len(text) - len(tail)]
    return head.rstrip("\n") + marker + tail, len(middle), middle.count("\n")
