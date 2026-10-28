"""The server watchdog (#385): reboot the Lightsail instance when its operating system has hung.

On 2026-10-28 the server stopped from about 00:30 to 12:13 KST. Lightsail's instance status
check (``StatusCheckFailed_Instance``) read 1 every minute from 00:33 while the system check
(``StatusCheckFailed_System``) read 0: the hardware was fine and the OS had stopped, as a kernel
that panicked and waits forever does. Lightsail has no automatic recovery, and the alert reached a
user who could not reboot for hours. ``aws lightsail reboot-instance`` fixed it at once: the check
passed from 12:16, two and a half minutes after the reboot.

The scheduled workflow ``.github/workflows/server-watchdog.yml`` runs this every 10 minutes, with
the access key of the IAM user ``krx-quant-watchdog`` from the repository's secrets
(``deploy/create-watchdog-user.sh``, #387). It reads:

- the instance's state (``get-instance-state``);
- both status checks, one point a minute, over the last 40 minutes (``get-instance-metric-data``,
  period 60, statistics Minimum and Maximum);
- the instance's operations, for its recent reboots and starts (``get-operations-for-resource``);
- the open ``server-watchdog`` issue this workflow opened, for what it already did there.

What it decides (``decide``, docs/operations.md "서버가 멈추면 (#385)"):

1. Only a ``running`` instance is touched.
2. No points, or a newest point older than 6 minutes: nothing.
3. The instance check has failed (Minimum 1) at every point of the last 15 minutes, with at least
   14 of the 15 points there, and the failure started 15 minutes or more before the newest point:
   failing. Fewer points, or one point that passed: nothing yet.
4. Failing while the system check's newest point fails too: the host is at fault and a reboot
   keeps the same host, so it does not reboot; it says so once (``hardware``).
5. Failing after it gave up: nothing, until the issue it gave up on is closed.
6. A reboot or start in the last 60 minutes, by anyone: nothing (the boot may still be on its way).
7. Two reboots or starts in the last 3 hours, and still failing: it gives up, and says so once.
8. Otherwise it reboots.
9. The instance check passes at the last 3 points while its issue is open: it says so and closes
   the issue.

Each reboot, give-up, hardware notice and recovery goes to GitHub: the first opens an issue
labelled ``server-watchdog``, the later ones of the same incident comment on it. With
TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (the engine's names, ``notify.py``) it also sends a
short Telegram message. What it wrote is marked in the issue (``marker``); only the workflow's
own issue and comments are read.

Probed read-only on 2026-10-28 (#385): a period of 60 seconds gives one point per minute, keyed by
the minute's start, with no gaps over the 13 hours around the incident; a point appears 40-85
seconds after its minute ends, so the newest one starts 1.5-2.5 minutes before now. The points
come in no particular order, carry no SampleCount (asked for, it is left out), and their
timestamps carry the caller's time zone (``+09:00`` on a Mac in Korea, UTC on the runner). A
period of 300 seconds counts its windows from the start time asked and returns the last one
while still partial, so it is not used. Operations come newest first, a reboot as
``operationType`` ``RebootInstance`` with ``createdAt``; a reboot from inside the OS (the kernel's
own, ``deploy/kernel-recovery.sh``) leaves none.

Usage: python3 scripts/server_watchdog.py [--dry-run]

Environment: REPO (owner/repo, else GITHUB_REPOSITORY), GH_TOKEN for ``gh``, the AWS credentials
for ``aws``, INSTANCE (krx-quant), AWS_REGION (ap-southeast-2), RUN_URL (the run, for the
report), TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (optional), DRY_RUN=1 (decide only),
GITHUB_STEP_SUMMARY (the run's summary page).

Standard library only, so the workflow runs it without installing the project.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal, Protocol

KST = timezone(timedelta(hours=9), "KST")
INSTANCE = "krx-quant"
REGION = "ap-southeast-2"  # the account's only Region (docs/operations.md 1장)

PERIOD = timedelta(minutes=1)
LOOKBACK = timedelta(minutes=40)  # the metric window read each run
FAILING_FOR = timedelta(minutes=15)
MIN_POINTS = 14  # of the 15 one-minute points in FAILING_FOR
MAX_STEP = 2 * PERIOD  # one missing point between two failing ones still counts as one failure
STALE_AFTER = timedelta(minutes=6)  # the newest point starts 1.5-2.5 minutes before now (probed)
PASSING_POINTS = 3
COOLDOWN = timedelta(minutes=60)
GIVE_UP_WINDOW = timedelta(hours=3)
GIVE_UP_AFTER = 2
BOOTS = frozenset({"RebootInstance", "StartInstance"})
OPERATION_PAGES = 10

INSTANCE_CHECK = "StatusCheckFailed_Instance"
SYSTEM_CHECK = "StatusCheckFailed_System"

LABEL = "server-watchdog"
LABEL_COLOR = "b60205"
LABEL_DESCRIPTION = "The server watchdog rebooted the server, gave up or saw it recover (#385)"
DOCS = 'docs/operations.md 6장 "서버가 멈추면 (#385)"'

Event = Literal["reboot", "give-up", "hardware", "recovered"]
Action = Literal["none", "reboot", "give-up", "hardware", "recovered"]
Status = Literal["failing", "passing", "unclear", "stale", "missing"]
MARKER = re.compile(r"<!-- krx-quant-watchdog: (reboot|give-up|hardware|recovered) -->")


def marker(event: Event) -> str:
    """What marks an event in the issue, so the next run knows it was done."""
    return f"<!-- krx-quant-watchdog: {event} -->"


@dataclass(frozen=True)
class Point:
    """One minute of a status check: 1 failed, 0 passed."""

    at: datetime  # the minute's start
    minimum: float
    maximum: float

    @property
    def failing(self) -> bool:
        """Failed for the whole minute."""
        return self.minimum >= 1

    @property
    def passing(self) -> bool:
        return self.maximum <= 0

    @property
    def kind(self) -> bool | None:
        """True failing, False passing, None both within the minute."""
        return True if self.failing else False if self.passing else None


@dataclass(frozen=True)
class Check:
    """A status check read over the last minutes."""

    status: Status
    newest: datetime | None = None
    since: datetime | None = None  # where the run of points like the newest one starts
    points: int = 0  # points in the last FAILING_FOR
    newest_failing: bool = False
    since_open: bool = False  # the run reaches back to the oldest point read: it began then or before


@dataclass(frozen=True)
class Incident:
    """The open issue this workflow opened, and what it wrote there, oldest first."""

    number: int
    url: str
    events: tuple[Event, ...]


@dataclass(frozen=True)
class Decision:
    action: Action
    why: str
    state: str
    instance: Check
    system: Check
    boots: tuple[datetime, ...]  # reboots and starts in the last GIVE_UP_WINDOW, oldest first
    last_boot: datetime | None
    incident: Incident | None


def parse_points(data: Mapping[str, Any]) -> list[Point]:
    """``get-instance-metric-data``'s points, oldest first. One without a time or both statistics
    is a missing point."""
    points: dict[datetime, Point] = {}
    for item in data.get("metricData") or []:
        try:
            at = datetime.fromisoformat(str(item["timestamp"]))
            low, high = float(item["minimum"]), float(item["maximum"])
        except (KeyError, TypeError, ValueError):
            continue
        if at.tzinfo is not None:
            points[at] = Point(at, low, high)
    return [points[at] for at in sorted(points)]


def parse_boots(operations: Sequence[Mapping[str, Any]]) -> list[datetime]:
    """When the instance was rebooted or started (``get-operations-for-resource``), oldest first. A
    failed one did not boot it."""
    boots = []
    for operation in operations:
        if operation.get("operationType") not in BOOTS or operation.get("status") == "Failed":
            continue
        try:
            at = datetime.fromisoformat(str(operation["createdAt"]))
        except (KeyError, ValueError):
            continue
        if at.tzinfo is not None:
            boots.append(at)
    return sorted(boots)


def assess(points: Sequence[Point], now: datetime) -> Check:
    """What the check says now: failing for FAILING_FOR, passing at its last points, or neither."""
    seen = [p for p in points if p.at <= now]
    if not seen:
        return Check("missing")
    newest = seen[-1]
    if now - newest.at > STALE_AFTER:
        return Check("stale", newest.at)
    window = [p for p in seen if p.at > newest.at - FAILING_FOR]
    since = newest.at
    for earlier, later in zip(reversed(seen[:-1]), reversed(seen[1:]), strict=True):
        if earlier.kind != newest.kind or later.at - earlier.at > MAX_STEP:
            break
        since = earlier.at
    if (
        newest.failing
        and len(window) >= MIN_POINTS
        and all(p.failing for p in window)
        and since <= newest.at - (FAILING_FOR - PERIOD)
    ):
        return Check("failing", newest.at, since, len(window), True, since == seen[0].at)
    last = seen[-PASSING_POINTS:]
    if (
        len(last) == PASSING_POINTS
        and all(p.passing for p in last)
        and newest.at - last[0].at <= (PASSING_POINTS - 1) * PERIOD
    ):
        return Check("passing", newest.at, since, len(window), False, since == seen[0].at)
    return Check("unclear", newest.at, since, len(window), newest.failing, since == seen[0].at)


def decide(
    state: str,
    instance: Sequence[Point],
    system: Sequence[Point],
    boots: Sequence[datetime],
    incident: Incident | None,
    now: datetime,
) -> Decision:
    """What to do now; the rules are the module's docstring."""
    checked, host = assess(instance, now), assess(system, now)
    recent = tuple(sorted(b for b in boots if b > now - GIVE_UP_WINDOW))
    last_boot = max(boots, default=None)
    done = set(incident.events) if incident else set()

    def decision(action: Action, why: str) -> Decision:
        return Decision(action, why, state, checked, host, recent, last_boot, incident)

    if state != "running":
        return decision("none", f"인스턴스가 {state}라 손대지 않는다")
    if checked.status == "missing":
        return decision("none", f"지난 {_span(LOOKBACK)} 인스턴스 상태 검사의 점이 없다")
    if checked.status == "stale":
        return decision("none", f"인스턴스 상태 검사의 가장 새 점이 {_kst(checked.newest)}로 늦다")
    if checked.status == "passing":
        if incident is not None:
            return decision("recovered", f"인스턴스 상태 검사가 {_since(checked)} 통과한다")
        return decision("none", "인스턴스 상태 검사가 통과한다")
    if checked.status == "unclear":
        return decision(
            "none",
            f"인스턴스 상태 검사가 {_span(FAILING_FOR)} 동안 이어서 실패하지 않았다"
            f"(지난 {_span(FAILING_FOR)}의 점 {checked.points}개)",
        )
    failing = f"인스턴스 상태 검사가 {_since(checked)} 실패한다"
    if host.newest_failing:
        if "hardware" in done:
            return decision("none", f"{failing}. 시스템 상태 검사도 실패한다(이미 알렸다)")
        return decision("hardware", f"{failing}. 시스템 상태 검사도 실패한다: 재부팅하지 않는다")
    if "give-up" in done:
        return decision("none", f"{failing}. 이미 포기했다(#{incident.number if incident else '?'})")
    if last_boot is not None and now - last_boot < COOLDOWN:
        return decision(
            "none", f"{failing}. 마지막 재부팅·시작이 {_kst(last_boot)}라 {_span(COOLDOWN)}을 기다린다"
        )
    if len(recent) >= GIVE_UP_AFTER:
        return decision(
            "give-up",
            f"{failing}. 지난 {_span(GIVE_UP_WINDOW)}에 재부팅·시작이 {len(recent)}번 있었는데 낫지 않았다",
        )
    return decision("reboot", f"{failing}. 재부팅한다")


def events_in(texts: Sequence[str]) -> tuple[Event, ...]:
    found: list[Event] = []
    for text in texts:
        for match in MARKER.finditer(text):
            found.append(_event(match.group(1)))
    return tuple(found)


EVENTS: dict[str, Event] = {
    "reboot": "reboot",
    "give-up": "give-up",
    "hardware": "hardware",
    "recovered": "recovered",
}


def _event(name: str) -> Event:
    return EVENTS[name]


def _bot(author: Any) -> bool:
    """Written by this workflow (GITHUB_TOKEN): GitHub Actions' bot. Anyone may comment on a
    public issue, so nothing else is read. ``gh`` shows the bot as ``app/github-actions`` for an
    issue's author and ``github-actions`` for a comment's (seen 2026-10-28); the REST API says
    ``github-actions[bot]``."""
    login = str(author.get("login", "")) if isinstance(author, Mapping) else ""
    return login.removeprefix("app/").removesuffix("[bot]") == "github-actions"


# ----- reports


def title(decision: Decision, now: datetime) -> str:
    what = {
        "reboot": "멈춰 재부팅했다",
        "give-up": "멈췄고 재부팅을 포기했다",
        "hardware": "멈췄고 하드웨어 문제로 보인다",
        "recovered": "다시 응답한다",
        "none": "",
    }[decision.action]
    return f"서버 watchdog: krx-quant가 {what} ({now.astimezone(KST):%Y-%m-%d %H:%M} KST)"


def report(decision: Decision, now: datetime, run_url: str, instance: str) -> str:
    """The issue body or comment for an event (Markdown, Korean like the docs)."""
    head = {
        "reboot": "**재부팅했다.**",
        "give-up": "**재부팅을 포기했다.** 사람이 봐야 한다.",
        "hardware": "**재부팅하지 않았다.** 시스템 상태 검사도 실패한다.",
        "recovered": "**다시 응답한다.** 이 이슈를 닫는다.",
        "none": "",
    }[decision.action]
    run = f"[실행]({run_url})" if run_url else "실행"
    lines = [
        marker(_event(decision.action)) if decision.action != "none" else "",
        f"{head} {now.astimezone(KST):%Y-%m-%d %H:%M:%S} KST, {run}, `{instance}`",
        "",
        f"- 판단: {decision.why}.",
        f"- 인스턴스 상태 검사(`{INSTANCE_CHECK}`): {_check(decision.instance)}",
        f"- 시스템 상태 검사(`{SYSTEM_CHECK}`): {_check(decision.system)}",
        f"- 지난 {_span(GIVE_UP_WINDOW)}의 재부팅·시작: {_times(decision.boots)}",
    ]
    lines += ["", _next(decision.action)]
    return "\n".join(lines) + "\n"


def _next(action: Action) -> str:
    if action == "reboot":
        return (
            f"재부팅 뒤 {_span(COOLDOWN)} 동안은 다시 재부팅하지 않는다. {_span(GIVE_UP_WINDOW)} 안에"
            f" {GIVE_UP_AFTER}번 재부팅해도 낫지 않으면 포기하고 여기에 남긴다. 서버가 다시 뜨면 이"
            " 이슈에 `needs-server-snapshot` 라벨을 붙여 요약을 받고, 사용자에게 서버에서"
            " `sudo journalctl -b -1 -k -n 200`(멈추기 전 부팅의 커널 로그)을 부탁한다."
            f" {DOCS}."
        )
    if action == "give-up":
        return (
            "이 이슈가 열려 있는 동안은 재부팅하지 않고 다시 알리지도 않는다. 맥에서"
            " `aws lightsail get-instance-metric-data`로 상태를 보고, 손으로 재부팅하거나"
            " Lightsail 콘솔에서 멈춘 뒤 다시 시작한다(다른 하드웨어로 옮겨진다). 고친 뒤 이 이슈를"
            f" 닫으면 watchdog이 다시 지킨다. {DOCS}."
        )
    if action == "hardware":
        return (
            "AWS 쪽 하드웨어나 네트워크의 문제로 보인다. 재부팅은 같은 하드웨어에서 다시 뜨므로 하지"
            " 않는다. AWS가 고치기를 기다리거나, Lightsail 콘솔에서 인스턴스를 멈춘 뒤 다시 시작한다"
            " (다른 하드웨어로 옮겨진다. 고정 IP와 Tailscale 이름은 그대로다). 시스템 상태 검사가 통과하고"
            f" 인스턴스 상태 검사만 실패하면 보통 규칙대로 재부팅한다. {DOCS}."
        )
    if action == "recovered":
        return (
            "멈춘 원인은 서버의 journal에 남는다: 이 이슈를 다시 열거나 새 이슈에서"
            " `needs-server-snapshot`으로 요약을 받고, 사용자에게"
            f" `sudo journalctl -b -1 -k -n 200`을 부탁한다. {DOCS}."
        )
    return ""


def telegram_text(decision: Decision, issue_url: str) -> str:
    what = {
        "reboot": "서버가 응답하지 않아 재부팅했습니다",
        "give-up": "서버가 재부팅 뒤에도 응답하지 않아 재부팅을 멈춥니다. 손으로 봐야 합니다",
        "hardware": "서버가 응답하지 않고 시스템 상태 검사도 실패합니다. 재부팅하지 않습니다",
        "recovered": "서버가 다시 응답합니다",
        "none": "",
    }[decision.action]
    return f"[watchdog] krx-quant {what}. {decision.why.split('. ')[0]}. {issue_url}".strip()


def _check(check: Check) -> str:
    if check.status == "missing":
        return "점 없음"
    if check.status == "stale":
        return f"가장 새 점 {_kst(check.newest)}(늦음)"
    word = {"failing": "실패", "passing": "통과", "unclear": "섞임"}[check.status]
    if check.status == "unclear" and check.newest_failing:
        word = "실패(아직 판단 전)"
    since = f", {_since(check)}" if check.since else ""
    return (
        f"{word}{since} · 가장 새 점 {_kst(check.newest)} · 지난 {_span(FAILING_FOR)}의 점 {check.points}개"
    )


def _since(check: Check) -> str:
    return f"{_kst(check.since)}{' 또는 그 전' if check.since_open else ''}부터"


def _incident(incident: Incident | None) -> str:
    if incident is None:
        return "없음"
    return f"#{incident.number} ({', '.join(incident.events) or '표시 없음'})"


def _times(times: Sequence[datetime]) -> str:
    return ", ".join(_kst(t) for t in times) or "없음"


def _kst(at: datetime | None) -> str:
    return "?" if at is None else f"{at.astimezone(KST):%m-%d %H:%M}"


def _span(span: timedelta) -> str:
    minutes = int(span.total_seconds() // 60)
    return f"{minutes // 60}시간" if minutes % 60 == 0 and minutes >= 60 else f"{minutes}분"


# ----- reading and acting


class Run(Protocol):
    """Runs a command and returns its standard output; raises RuntimeError when it fails."""

    def __call__(self, args: Sequence[str], stdin: str | None = None) -> str: ...


Send = Callable[[str, str, str], None]  # (bot token, chat id, text)


def run_command(args: Sequence[str], stdin: str | None = None) -> str:
    done = subprocess.run(list(args), input=stdin, capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args[:3])} failed (exit {done.returncode}): {done.stderr.strip()[-600:]}"
        )
    return done.stdout


def send_telegram(token: str, chat_id: str, text: str) -> None:
    """Send ``text``; raise RuntimeError without the token (it is part of the URL)."""
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError(f"Telegram refused it: HTTP {response.status}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram refused it: HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"Telegram not reachable: {type(e).__name__}") from None


@dataclass(frozen=True)
class Inputs:
    state: str
    instance: list[Point]
    system: list[Point]
    boots: list[datetime]


def gather(run: Run, instance: str, region: str, now: datetime) -> Inputs:
    """What Lightsail says, read-only."""

    def lightsail(*args: str) -> Any:
        return json.loads(run(["aws", "lightsail", *args, "--region", region, "--output", "json"]))

    state = lightsail("get-instance-state", "--instance-name", instance).get("state", {})
    checks: dict[str, list[Point]] = {}
    for metric in (INSTANCE_CHECK, SYSTEM_CHECK):
        checks[metric] = parse_points(
            lightsail(
                "get-instance-metric-data",
                *("--instance-name", instance, "--metric-name", metric, "--period", "60"),
                *("--start-time", _iso(now - LOOKBACK), "--end-time", _iso(now)),
                *("--unit", "Count", "--statistics", "Minimum", "Maximum"),
            )
        )
    operations: list[Mapping[str, Any]] = []
    token = None
    for _ in range(OPERATION_PAGES):
        page = lightsail(
            "get-operations-for-resource",
            "--resource-name",
            instance,
            *(("--page-token", token) if token else ()),
        )
        operations += page.get("operations") or []
        token = page.get("nextPageToken")
        if not token:
            break
    return Inputs(
        str(state.get("name", "?")), checks[INSTANCE_CHECK], checks[SYSTEM_CHECK], parse_boots(operations)
    )


def find_incident(run: Run, repo: str) -> Incident | None:
    """The newest open ``server-watchdog`` issue this workflow opened, with what it wrote there."""
    issues = json.loads(
        run(
            ["gh", "issue", "list", "--repo", repo, "--label", LABEL, "--state", "open"]
            + ["--json", "number,url,author,body", "--limit", "50"]
        )
    )
    ours = [issue for issue in issues if _bot(issue.get("author"))]
    if not ours:
        return None
    issue = max(ours, key=lambda i: int(i["number"]))
    view = json.loads(
        run(["gh", "issue", "view", str(issue["number"]), "--repo", repo, "--json", "comments"])
    )
    texts = [str(issue.get("body") or "")]
    texts += [str(c.get("body") or "") for c in view.get("comments") or [] if _bot(c.get("author"))]
    return Incident(int(issue["number"]), str(issue["url"]), events_in(texts))


def act(
    run: Run, send: Send, decision: Decision, now: datetime, env: Mapping[str, str], repo: str, instance: str
) -> list[str]:
    """Reboot when decided, then report: the problems in reporting (a failed reboot raises)."""
    region = env.get("AWS_REGION") or REGION
    if decision.action == "reboot":
        run(["aws", "lightsail", "reboot-instance", "--instance-name", instance, "--region", region])
        print(f"rebooted {instance}")
    problems: list[str] = []
    body = report(decision, now, env.get("RUN_URL", ""), instance)
    url = ""
    incident = decision.incident
    try:
        if incident is None:
            run(
                ["gh", "label", "create", LABEL, "--repo", repo, "--force"]
                + ["--color", LABEL_COLOR, "--description", LABEL_DESCRIPTION]
            )
            url = run(
                ["gh", "issue", "create", "--repo", repo, "--title", title(decision, now)]
                + ["--label", LABEL, "--body-file", "-"],
                stdin=body,
            ).strip()
        else:
            url = incident.url
            if not (decision.action == "recovered" and incident.events[-1:] == ("recovered",)):
                run(
                    ["gh", "issue", "comment", str(incident.number), "--repo", repo, "--body-file", "-"],
                    stdin=body,
                )
            if decision.action == "recovered":
                run(["gh", "issue", "close", str(incident.number), "--repo", repo, "--reason", "completed"])
        print(f"reported: {url}")
    except RuntimeError as e:
        problems.append(f"GitHub: {e}")
    token, chat = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
    if token and chat:
        try:
            send(token, chat, telegram_text(decision, url))
            print("telegram sent")
        except RuntimeError as e:
            problems.append(f"Telegram: {e}")
    return problems


def summary(decision: Decision, now: datetime, instance: str, dry_run: bool) -> str:
    lines = [
        f"### server watchdog: {decision.action}{' (dry run)' if dry_run else ''}",
        "",
        f"- {now.astimezone(KST):%Y-%m-%d %H:%M:%S} KST, `{instance}` {decision.state}",
        f"- {decision.why}",
        f"- {INSTANCE_CHECK}: {_check(decision.instance)}",
        f"- {SYSTEM_CHECK}: {_check(decision.system)}",
        f"- 지난 {_span(GIVE_UP_WINDOW)}의 재부팅·시작: {_times(decision.boots)}"
        f" (마지막 {_kst(decision.last_boot)})",
        f"- 열린 이슈: {_incident(decision.incident)}",
    ]
    return "\n".join(lines) + "\n"


def main(
    argv: Sequence[str],
    env: Mapping[str, str] = os.environ,
    run: Run = run_command,
    send: Send = send_telegram,
    now: datetime | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Reboot the Lightsail instance when its OS has hung (#385).")
    parser.add_argument("--dry-run", action="store_true", help="decide and print only")
    args = parser.parse_args(argv)
    dry_run = args.dry_run or env.get("DRY_RUN", "") in {"1", "true"}
    repo = env.get("REPO") or env.get("GITHUB_REPOSITORY") or ""
    if not repo:
        print("::error::REPO (owner/repo) is not set")
        return 2
    instance = env.get("INSTANCE") or INSTANCE
    region = env.get("AWS_REGION") or REGION
    now = now or datetime.now(UTC)
    try:
        inputs = gather(run, instance, region, now)
        incident = find_incident(run, repo)
    except (RuntimeError, ValueError, KeyError, TypeError) as e:
        # Without what Lightsail or the issue says, nothing is decided.
        print(f"::error::could not read the server's state: {e}")
        return 2
    decision = decide(inputs.state, inputs.instance, inputs.system, inputs.boots, incident, now)
    text = summary(decision, now, instance, dry_run)
    print(text)
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as page:
            page.write(text)
    if dry_run or decision.action == "none":
        return 0
    try:
        problems = act(run, send, decision, now, env, repo, instance)
    except RuntimeError as e:
        print(f"::error::could not reboot {instance}: {e}")
        return 1
    for problem in problems:
        print(f"::error::could not report the {decision.action}: {problem}")
    return 1 if problems else 0


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
