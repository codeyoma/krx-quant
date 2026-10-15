"""The scheduled jobs' comments and labels on GitHub issues (#214), against a local stand-in."""

from __future__ import annotations

import aiohttp
import pytest

from krx_quant.github import API_VERSION, COMMENT_LIMIT, GitHub, GitHubError, comment_body
from tests.fake_github import REPO, TOKEN, fake_github


def test_a_comment_is_the_heading_the_notes_the_output_and_the_details() -> None:
    body = comment_body(
        "**예약 작업 `x`** · 2026-10-16 밤",
        "line 1\nline 2\n",
        notes=["다시 돌리기: `docker compose run --rm engine x`", "둘째 줄"],
        details=[("stderr", "a warning")],
    )
    assert body == (
        "**예약 작업 `x`** · 2026-10-16 밤\n\n"
        "다시 돌리기: `docker compose run --rm engine x`\n둘째 줄\n\n"
        "```\nline 1\nline 2\n```\n\n"
        "<details><summary>stderr</summary>\n\n```\na warning\n```\n\n</details>\n"
    )


def test_backticks_in_the_output_do_not_close_its_block() -> None:
    body = comment_body("h", "a ``` fence\nand ```` four")
    assert "`````\na ``` fence\nand ```` four\n`````" in body


def test_a_long_output_keeps_its_head_and_tail_and_says_what_was_cut() -> None:
    output = "\n".join(f"line {i:05d}" for i in range(20_000))  # 220,000 characters
    body = comment_body("h", output, details=[("stderr", "short")])
    assert len(body) <= COMMENT_LIMIT
    assert "line 00000" in body and "line 19999" in body
    assert "… (가운데를 줄였다) …" in body
    assert "GitHub 댓글 한도(65,536자)를 넘어 출력의 가운데" in body
    assert "short" in body  # an attachment within its share is whole
    kept = [line for line in body.splitlines() if line.startswith("line ")]
    assert kept == sorted(kept) and all(len(line) == 10 for line in kept)  # cut on line ends
    assert len(body) > COMMENT_LIMIT - 1_000  # the room is used


def test_a_long_attachment_keeps_an_eighth_of_the_limit() -> None:
    stderr = "\n".join(f"log {i}" for i in range(50_000))
    body = comment_body("h", "the output", details=[("stderr", stderr)])
    assert len(body) <= COMMENT_LIMIT
    assert "the output" in body
    assert "stderr의 가운데" in body
    folded = body[body.index("<details>") :]
    assert len(folded) < COMMENT_LIMIT // 8 + 1_000


def test_a_comment_within_the_limit_is_not_cut() -> None:
    output = "x" * (COMMENT_LIMIT - 100)
    assert "줄였다" not in comment_body("h", output)
    assert len(comment_body("h", output + "x" * 200)) <= COMMENT_LIMIT


async def test_a_comment_goes_on_the_issue_with_the_token_and_the_api_version() -> None:
    async with fake_github() as fake, aiohttp.ClientSession() as session:
        github = GitHub(session, TOKEN, REPO, fake.url)
        url = await github.comment(203, "hello")
        assert url == f"https://github.com/{REPO}/issues/203#issuecomment-1"
        assert fake.comments == [(203, "hello")]
        method, path, headers = fake.requests[0]
        assert (method, path) == ("POST", f"/repos/{REPO}/issues/203/comments")
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert headers["Accept"] == "application/vnd.github+json"
        assert headers["X-GitHub-Api-Version"] == API_VERSION


async def test_relabelling_adds_and_removes_and_a_missing_label_is_no_error() -> None:
    async with fake_github() as fake, aiohttp.ClientSession() as session:
        fake.labels[187] = {"ready-for-human", "bug"}
        await GitHub(session, TOKEN, REPO, fake.url).relabel(
            187, ["ready-for-agent"], ["ready-for-human", "needs-triage"]
        )
        assert fake.labels[187] == {"ready-for-agent", "bug"}
        assert [(m, p) for m, p, _ in fake.requests][-1] == (
            "DELETE",
            f"/repos/{REPO}/issues/187/labels/needs-triage",
        )


async def test_a_refusal_or_no_token_is_a_github_error() -> None:
    async with fake_github() as fake, aiohttp.ClientSession() as session:
        fake.fail_comments = 1
        with pytest.raises(GitHubError, match="HTTP 502"):
            await GitHub(session, TOKEN, REPO, fake.url).comment(1, "x")
        with pytest.raises(GitHubError, match="HTTP 401"):
            await GitHub(session, "wrong", REPO, fake.url).comment(1, "x")
        unset = GitHub(session, None, REPO, fake.url)
        assert not unset.configured
        with pytest.raises(GitHubError, match="KRX_GITHUB_TOKEN"):
            await unset.comment(1, "x")
        assert fake.comments == []


async def test_github_out_of_reach_is_a_github_error() -> None:
    async with aiohttp.ClientSession() as session:
        with pytest.raises(GitHubError, match="not reachable"):
            await GitHub(session, TOKEN, REPO, "http://127.0.0.1:9").comment(1, "x")
