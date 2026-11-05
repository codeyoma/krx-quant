"""The server watchdog (``scripts/server_watchdog.py``, #385): reboot a hung server, but not often.

The series are shaped as Lightsail answered on 2026-10-28 (probed read-only, #385): one point a
minute keyed by the minute's start, ``minimum`` and ``maximum`` 0 or 1 and no SampleCount, in no
particular order, in the caller's time zone; operations newest first. During the incident the
instance check read 1 from 00:33 through 12:15 KST and 0 again from 12:16, after the user's reboot
at 12:13:34; the system check read 0 throughout. Only times and 0/1 values are kept here.

The CPU burst capacity (#547) is shaped as Lightsail answered on 2026-11-05 (probed read-only): a
point every 5 minutes keyed :00, :05, ..., about a minute and a half after its time, ``minimum`` and
``maximum`` alike, unit ``Percent``. A night replay on one of the two vCPUs took it down by about
5.7 points an hour, idle it came back by about 3.6.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "server_watchdog.py"
REPO = "codeyoma/krx-quant"


def load_watchdog() -> ModuleType:
    spec = importlib.util.spec_from_file_location("server_watchdog", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


watchdog = load_watchdog()

KST = timezone(timedelta(hours=9))
FAIL_FROM = datetime(2026, 10, 28, 0, 33, tzinfo=KST)
FAIL_UNTIL = datetime(2026, 10, 28, 12, 15, tzinfo=KST)  # the last minute that failed
USER_REBOOT = datetime(2026, 10, 28, 12, 13, 34, 807000, tzinfo=KST)
# A minute's point appeared 40-85 seconds after the minute ended (probed).
VISIBLE_AFTER = timedelta(seconds=105)
# A burst capacity point appeared about a minute and a half after its time (probed, #547).
BURST_VISIBLE_AFTER = timedelta(seconds=90)
# The instance's operations before the incident, as probed (types and times only).
EARLIER_OPERATIONS = [
    ("AttachStaticIp", datetime(2026, 10, 15, 22, 43, 33, tzinfo=KST)),
    ("PutInstancePublicPorts", datetime(2026, 10, 8, 20, 7, 18, tzinfo=KST)),
    ("CreateInstance", datetime(2026, 10, 8, 19, 38, 30, tzinfo=KST)),
]
# The bot as gh shows it: an issue's author, and a comment's (seen 2026-10-28).
BOT = {"is_bot": True, "login": "app/github-actions"}
BOT_COMMENT = {"login": "github-actions"}
HUMAN = {"is_bot": False, "login": "codeyoma"}


def incident_instance(at: datetime) -> int:
    return 1 if FAIL_FROM <= at <= FAIL_UNTIL else 0


def metric_answer(
    name: str, points: Sequence[tuple[datetime, float]], tz: timezone = UTC, unit: str = "Count"
) -> dict[str, Any]:
    """``get-instance-metric-data``'s answer for Minimum and Maximum, in no particular order."""
    data = [
        {
            "maximum": float(v),
            "minimum": float(v),
            "timestamp": at.astimezone(tz).isoformat(),
            "unit": unit,
        }
        for at, v in points
    ]
    return {"metricName": name, "metricData": data[1::2] + data[::2]}


def operation(kind: str, at: datetime, status: str = "Succeeded") -> dict[str, Any]:
    return {
        "createdAt": at.isoformat(),
        "isTerminal": True,
        "operationType": kind,
        "resourceType": "Instance",
        "status": status,
        "statusChangedAt": at.isoformat(),
    }


def minutes(pattern: str, newest: datetime) -> list[Any]:
    """Points ending at ``newest``, one character a minute: ``1`` failed, ``0`` passed, ``.`` missing."""
    points = []
    for i, c in enumerate(reversed(pattern)):
        if c != ".":
            at = newest - timedelta(minutes=i)
            points.append(watchdog.Point(at, float(c), float(c)))
    return sorted(points, key=lambda p: p.at)


NOW = datetime(2026, 10, 28, 1, 0, 0, tzinfo=KST)
NEWEST = NOW - timedelta(minutes=2)
FAILING = minutes("0" * 10 + "1" * 20, NEWEST)
PASSING = minutes("0" * 30, NEWEST)


def decide(
    instance: Sequence[Any] = FAILING,
    system: Sequence[Any] = PASSING,
    boots: Sequence[datetime] = (),
    incident: Any = None,
    state: str = "running",
    now: datetime = NOW,
) -> Any:
    return watchdog.decide(state, instance, system, list(boots), incident, now)


def incident(*events: str, number: int = 401) -> Any:
    return watchdog.Incident(number, f"https://github.com/{REPO}/issues/{number}", tuple(events))


# ----- the real incident, run by run


@dataclass
class World:
    """Lightsail and GitHub as the workflow's commands see them, at ``now``."""

    now: datetime
    instance: Callable[[datetime], int] = incident_instance
    system: Callable[[datetime], int] = lambda at: 0
    burst: Callable[[datetime], float] = lambda at: 100.0  # the CPU burst capacity, percent (#547)
    state: str = "running"
    operations: list[dict[str, Any]] = field(
        default_factory=lambda: [operation(kind, at) for kind, at in EARLIER_OPERATIONS]
    )
    issues: dict[int, dict[str, Any]] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)
    telegram: list[str] = field(default_factory=list)
    fail: str = ""  # a command that fails
    hang: str = ""  # a command that runs into ``run_command``'s time limit

    def run(self, args: Sequence[str], stdin: str | None = None) -> str:
        args = list(args)
        self.calls.append(args)
        if self.fail and self.fail in " ".join(args):
            raise RuntimeError(f"{self.fail} failed")
        if self.hang and self.hang in " ".join(args):
            raise subprocess.TimeoutExpired(args, 120)
        option = dict(zip(args[3::1], args[4::1], strict=False))
        if args[:2] == ["aws", "lightsail"]:
            return json.dumps(self.lightsail(args[2], option))
        if args[:2] == ["gh", "label"]:
            return ""
        if args[:2] == ["gh", "issue"]:
            return self.github(args[2], args, option, stdin)
        raise AssertionError(f"unexpected command: {args}")

    def lightsail(self, command: str, option: dict[str, str]) -> Any:
        if command == "get-instance-state":
            return {"state": {"code": 16, "name": self.state}}
        if command == "get-instance-metric-data" and option["--metric-name"] == watchdog.BURST:
            start = datetime.fromisoformat(option["--start-time"])
            end = datetime.fromisoformat(option["--end-time"])
            assert option["--period"] == "60" and option["--unit"] == "Percent"
            at = start.replace(second=0, microsecond=0)
            at -= timedelta(minutes=at.minute % 5)
            points = []
            while at <= end:
                if at >= start and at + BURST_VISIBLE_AFTER <= self.now:
                    points.append((at, round(self.burst(at), 6)))
                at += timedelta(minutes=5)
            return metric_answer(watchdog.BURST, points, unit="Percent")
        if command == "get-instance-metric-data":
            start = datetime.fromisoformat(option["--start-time"])
            end = datetime.fromisoformat(option["--end-time"])
            series = self.instance if option["--metric-name"] == "StatusCheckFailed_Instance" else self.system
            points = []
            at = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
            while at <= end and at + VISIBLE_AFTER <= self.now:
                points.append((at, series(at)))
                at += timedelta(minutes=1)
            return metric_answer(option["--metric-name"], points)
        if command == "get-operations-for-resource":
            happened = [o for o in self.operations if datetime.fromisoformat(o["createdAt"]) <= self.now]
            return {
                "operations": sorted(
                    happened, key=lambda o: datetime.fromisoformat(o["createdAt"]), reverse=True
                )
            }
        if command == "reboot-instance":
            done = operation("RebootInstance", self.now + timedelta(seconds=15))
            self.operations.append(done)
            return {"operations": [done]}
        raise AssertionError(command)

    def github(self, command: str, args: list[str], option: dict[str, str], stdin: str | None) -> str:
        if command == "list":
            found = [
                {"number": n, "url": i["url"], "author": i["author"], "body": i["body"]}
                for n, i in self.issues.items()
                if i["state"] == "open" and option["--label"] in i["labels"]
            ]
            return json.dumps(found)
        if command == "view":
            return json.dumps({"comments": self.issues[int(args[3])]["comments"]})
        if command == "create":
            number = 401 + len(self.issues)
            url = f"https://github.com/{REPO}/issues/{number}"
            self.issues[number] = {
                "url": url,
                "author": BOT,
                "title": option["--title"],
                "body": stdin,
                "labels": [option["--label"]],
                "state": "open",
                "comments": [],
            }
            return url + "\n"
        if command == "comment":
            self.issues[int(args[3])]["comments"].append({"author": BOT_COMMENT, "body": stdin})
            return f"{self.issues[int(args[3])]['url']}#issuecomment-1\n"
        if command == "close":
            self.issues[int(args[3])]["state"] = "closed"
            return ""
        raise AssertionError(command)

    def send(self, token: str, chat: str, text: str) -> None:
        self.telegram.append(text)

    def tick(self, env: dict[str, str] | None = None) -> int:
        code: int = watchdog.main([], env or ENV, self.run, self.send, self.now)
        return code

    def reboots(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["aws", "lightsail", "reboot-instance"]]


ENV = {
    "REPO": REPO,
    "RUN_URL": f"https://github.com/{REPO}/actions/runs/1",
    "TELEGRAM_BOT_TOKEN": "bot-token",
    "TELEGRAM_CHAT_ID": "42",
}


def runs(world: World, first: datetime, last: datetime) -> list[tuple[str, list[str]]]:
    """The workflow every 10 minutes (minute 4, 14, ...) from ``first`` to ``last``: what each
    run did to Lightsail and GitHub."""
    done = []
    world.now = first
    while world.now <= last:
        before = len(world.calls)
        assert world.tick() == 0
        writes = [
            " ".join(c[:3])
            for c in world.calls[before:]
            if c[:3] == ["aws", "lightsail", "reboot-instance"]
            or c[:2] == ["gh", "issue"]
            and c[2] in {"create", "comment", "close"}
        ]
        if writes:
            done.append((f"{world.now.astimezone(KST):%H:%M}", writes))
        world.now += timedelta(minutes=10)
    return done


def test_the_night_of_2026_10_28_reboots_twice_gives_up_once_and_closes_the_issue_on_recovery() -> None:
    """The real series, with the watchdog in place. Its reboots do not fix it here (the series
    stays as it was until the user's reboot), so it must stop after two and stay quiet."""
    world = World(datetime(2026, 10, 28, 0, 4, tzinfo=KST))
    world.operations.append(operation("RebootInstance", USER_REBOOT))  # listed from 12:13:34 on

    done = runs(world, datetime(2026, 10, 28, 0, 4, tzinfo=KST), datetime(2026, 10, 28, 12, 44, tzinfo=KST))

    assert done == [
        # Failing since 00:33; the newest point at 00:54 is 00:52, 15 minutes of failure from 00:38.
        ("00:54", ["aws lightsail reboot-instance", "gh issue create"]),
        # 01:54 is 59 minutes and 45 seconds after the reboot: still the cooldown.
        ("02:04", ["aws lightsail reboot-instance", "gh issue comment"]),
        # Two reboots in three hours did not fix it.
        ("03:14", ["gh issue comment"]),
        # Quiet, the user's reboot at 12:13 included, until the check passes again from 12:16.
        ("12:24", ["gh issue comment", "gh issue close"]),
    ]
    [issue] = world.issues.values()
    assert issue["state"] == "closed" and issue["labels"] == ["server-watchdog"]
    assert issue["title"] == "서버 watchdog: krx-quant가 멈춰 재부팅했다 (2026-10-28 00:54 KST)"
    events = watchdog.events_in([issue["body"]] + [c["body"] for c in issue["comments"]])
    assert events == ("reboot", "reboot", "give-up", "recovered")
    assert "인스턴스 상태 검사가 10-28 00:33부터 실패한다" in issue["body"]
    assert "**재부팅을 포기했다.**" in issue["comments"][1]["body"]
    assert len(world.reboots()) == 2
    assert all("--instance-name" in c and "krx-quant" in c for c in world.reboots())
    assert [t.split(".")[0] for t in world.telegram] == [
        "[watchdog] krx-quant 서버가 응답하지 않아 재부팅했습니다",
        "[watchdog] krx-quant 서버가 응답하지 않아 재부팅했습니다",
        "[watchdog] krx-quant 서버가 재부팅 뒤에도 응답하지 않아 재부팅을 멈춥니다",
        "[watchdog] krx-quant 서버가 다시 응답합니다",
    ]
    assert all(t.endswith("/issues/401") for t in world.telegram)


def test_a_reboot_that_fixes_it_is_followed_by_one_recovery_comment() -> None:
    world = World(datetime(2026, 10, 28, 0, 54, tzinfo=KST))
    assert world.tick() == 0
    assert len(world.reboots()) == 1
    # The reboot worked: the check passes from three minutes later.
    world.instance = lambda at: 1 if FAIL_FROM <= at <= datetime(2026, 10, 28, 0, 57, tzinfo=KST) else 0
    world.now = datetime(2026, 10, 28, 1, 4, tzinfo=KST)
    assert world.tick() == 0
    [issue] = world.issues.values()
    assert issue["state"] == "closed"
    assert "인스턴스 상태 검사가 10-28 00:58부터 통과한다" in issue["comments"][0]["body"]
    world.now += timedelta(minutes=10)
    assert world.tick() == 0
    assert len(world.reboots()) == 1 and len(issue["comments"]) == 1


# ----- reading Lightsail


def test_points_come_in_any_order_and_time_zone_and_a_point_without_both_statistics_is_missing() -> None:
    at = datetime(2026, 10, 28, 0, 33, tzinfo=KST)
    answer = metric_answer("StatusCheckFailed_Instance", [(at, 1), (at + timedelta(minutes=1), 0)], KST)
    answer["metricData"].append({"maximum": 1.0, "timestamp": "2026-10-27T15:35:00Z", "unit": "Count"})
    answer["metricData"].append({"maximum": 1.0, "minimum": 1.0, "timestamp": "2026-10-27T15:36:00Z"})
    answer["metricData"].append(
        {"timestamp": "2026-10-28T00:37:00+09:00", "unit": "Count"}
    )  # SampleCount alone

    points = watchdog.parse_points(answer)

    assert [(p.at.astimezone(KST).strftime("%H:%M"), p.failing, p.passing) for p in points] == [
        ("00:33", True, False),
        ("00:34", False, True),
        ("00:36", True, False),
    ]


def test_only_reboots_and_starts_that_did_not_fail_count_as_boots() -> None:
    boots = watchdog.parse_boots(
        [
            operation("RebootInstance", USER_REBOOT),
            operation("RebootInstance", USER_REBOOT - timedelta(hours=1), status="Failed"),
            operation("StartInstance", USER_REBOOT - timedelta(hours=2)),
            operation("StopInstance", USER_REBOOT - timedelta(hours=2, minutes=1)),
            *(operation(kind, at) for kind, at in EARLIER_OPERATIONS),
        ]
    )
    assert boots == [USER_REBOOT - timedelta(hours=2), USER_REBOOT]


# ----- the rules


def test_fifteen_minutes_of_failure_reboots_and_fourteen_do_not() -> None:
    assert decide(minutes("0" * 15 + "1" * 15, NEWEST)).action == "reboot"
    assert decide(minutes("0" * 16 + "1" * 14, NEWEST)).action == "none"


def test_one_passing_minute_in_the_last_fifteen_is_no_reboot() -> None:
    assert decide(minutes("1" * 20 + "0" + "1" * 9, NEWEST)).action == "none"


def test_sparse_data_is_no_reboot_but_one_missing_point_is_allowed() -> None:
    assert decide(minutes("1" * 20 + "." + "1" * 9, NEWEST)).action == "reboot"
    assert decide(minutes("1" * 15 + "." + "1" * 5 + "." + "1" * 8, NEWEST)).action == "none"
    assert decide(minutes("1" * 20 + ".." + "1" * 8, NEWEST)).action == "none"
    # The missing point is the oldest of the fifteen: the failure must still reach back to it.
    assert decide(minutes("0" + "." + "1" * 14, NEWEST)).action == "none"
    assert decide(minutes("01" + "." + "1" * 14, NEWEST)).action == "reboot"


def test_no_data_or_late_data_is_no_reboot() -> None:
    assert decide([]).action == "none"
    late = minutes("1" * 30, NOW - timedelta(minutes=7))
    assert decide(late).action == "none"
    assert decide(late).instance.status == "stale"
    assert decide(minutes("1" * 30, NOW - timedelta(minutes=5))).action == "reboot"


@pytest.mark.parametrize("state", ["stopped", "stopping", "pending", "rebooting"])
def test_an_instance_that_is_not_running_is_left_alone(state: str) -> None:
    assert decide(state=state).action == "none"
    assert decide(PASSING, state=state, incident=incident("reboot")).action == "none"


def test_no_reboot_within_an_hour_of_the_last_one_whoever_made_it() -> None:
    assert decide(boots=[NOW - timedelta(minutes=59)]).action == "none"
    assert decide(boots=[NOW - timedelta(minutes=61)]).action == "reboot"


def test_two_boots_in_three_hours_that_did_not_fix_it_give_up() -> None:
    two = [NOW - timedelta(minutes=150), NOW - timedelta(minutes=70)]
    assert decide(boots=two).action == "give-up"
    older = [NOW - timedelta(minutes=181), NOW - timedelta(minutes=70)]
    assert decide(boots=older).action == "reboot"


def test_after_giving_up_it_stays_quiet_until_the_issue_is_closed() -> None:
    gave_up = incident("reboot", "reboot", "give-up")
    assert decide(incident=gave_up, boots=[NOW - timedelta(hours=5)]).action == "none"
    assert decide(incident=None, boots=[NOW - timedelta(hours=5)]).action == "reboot"


def test_a_failing_system_check_is_the_host_said_once_and_not_rebooted() -> None:
    host = minutes("1" * 30, NEWEST)
    assert decide(system=host).action == "hardware"
    assert decide(system=host, incident=incident("hardware")).action == "none"
    # The host is fine again and the OS is still hung: the usual rules.
    assert decide(system=PASSING, incident=incident("hardware")).action == "reboot"
    # The system check's own failure needs no fifteen minutes: one failing newest point holds a reboot.
    assert decide(system=minutes("0" * 28 + "11", NEWEST)).action == "hardware"


def test_a_passing_check_closes_an_open_incident_and_needs_three_points() -> None:
    assert decide(PASSING, incident=incident("reboot")).action == "recovered"
    assert decide(PASSING, incident=incident("give-up")).action == "recovered"
    assert decide(PASSING).action == "none"
    assert decide(minutes("1" * 28 + "00", NEWEST), incident=incident("reboot")).action == "none"
    assert decide(minutes("1" * 26 + "0.00", NEWEST), incident=incident("reboot")).action == "none"


def test_a_failure_older_than_the_window_read_says_it_began_then_or_before() -> None:
    decision = decide(minutes("1" * 40, NEWEST))
    assert decision.action == "reboot"
    assert "00:19 또는 그 전부터 실패한다" in decision.why


# ----- GitHub


def test_only_the_workflow_s_own_issue_and_comments_are_read() -> None:
    world = World(NOW)
    world.issues = {
        400: {
            "url": f"https://github.com/{REPO}/issues/400",
            "author": HUMAN,
            "body": watchdog.marker("give-up"),
            "labels": ["server-watchdog"],
            "state": "open",
            "comments": [],
        },
        401: {
            "url": f"https://github.com/{REPO}/issues/401",
            "author": BOT,
            "body": watchdog.marker("reboot"),
            "labels": ["server-watchdog"],
            "state": "open",
            "comments": [
                {"author": HUMAN, "body": watchdog.marker("give-up")},
                {"author": {"login": "github-actions[bot]"}, "body": watchdog.marker("reboot")},
            ],
        },
    }

    found = watchdog.find_incident(world.run, REPO)

    assert found == watchdog.Incident(401, f"https://github.com/{REPO}/issues/401", ("reboot", "reboot"))


def test_a_dry_run_decides_and_writes_the_summary_but_reboots_and_posts_nothing(tmp_path: Path) -> None:
    world = World(datetime(2026, 10, 28, 0, 54, tzinfo=KST))
    page = tmp_path / "summary.md"

    assert world.tick({**ENV, "DRY_RUN": "1", "GITHUB_STEP_SUMMARY": str(page)}) == 0

    assert world.reboots() == [] and world.issues == {} and world.telegram == []
    assert page.read_text().startswith("### server watchdog: reboot (dry run)")
    reads = {tuple(c[:3]) for c in world.calls}
    assert reads == {
        ("aws", "lightsail", "get-instance-state"),
        ("aws", "lightsail", "get-instance-metric-data"),
        ("aws", "lightsail", "get-operations-for-resource"),
        ("gh", "issue", "list"),
    }


def test_telegram_is_sent_only_with_both_secrets_and_its_failure_does_not_stop_the_issue() -> None:
    world = World(datetime(2026, 10, 28, 0, 54, tzinfo=KST))
    assert world.tick({**ENV, "TELEGRAM_CHAT_ID": ""}) == 0
    assert world.telegram == [] and len(world.issues) == 1

    def refuse(token: str, chat: str, text: str) -> None:
        raise RuntimeError("Telegram refused it: HTTP 401")

    world = World(datetime(2026, 10, 28, 0, 54, tzinfo=KST))
    assert watchdog.main([], ENV, world.run, refuse, world.now) == 1
    assert len(world.reboots()) == 1 and len(world.issues) == 1


def test_nothing_is_rebooted_when_lightsail_or_github_cannot_be_read() -> None:
    for failing in ("get-operations-for-resource", "issue list"):
        world = World(datetime(2026, 10, 28, 0, 54, tzinfo=KST), fail=failing)
        assert world.tick() == 2
        assert world.reboots() == []


def test_a_reboot_that_fails_is_not_reported_as_done() -> None:
    world = World(datetime(2026, 10, 28, 0, 54, tzinfo=KST), fail="reboot-instance")
    assert world.tick() == 1
    assert world.issues == {} and world.telegram == []


def test_the_workflow_runs_the_script_every_ten_minutes_with_the_watchdog_users_key() -> None:
    workflow = (ROOT / ".github" / "workflows" / "server-watchdog.yml").read_text()
    assert '- cron: "4-59/10 * * * *"' in workflow
    assert "python3 scripts/server_watchdog.py" in workflow
    assert "aws-access-key-id: ${{ secrets.AWS_WATCHDOG_ACCESS_KEY_ID }}" in workflow
    assert "aws-secret-access-key: ${{ secrets.AWS_WATCHDOG_SECRET_ACCESS_KEY }}" in workflow
    assert "aws-region: ap-southeast-2" in workflow and "mask-aws-account-id: true" in workflow
    assert "issues: write" in workflow
    # The account's service control policy denies GitHub's OIDC provider (#387).
    assert "id-token" not in workflow and "role-to-assume" not in workflow
    # Without both secrets the run ends at once, and says which script sets them.
    check = workflow.split("- name: Is the AWS key set?", 1)[1].split("- uses:", 1)[0]
    assert '[[ -z "$KEY_ID" || -z "$SECRET_KEY" ]]' in check and "deploy/create-watchdog-user.sh" in check
    assert workflow.count("if: steps.key.outputs.set == 'true'") == 3


# ----- the CPU burst capacity (#547)


def line(start: datetime, value: float, per_hour: float) -> Callable[[datetime], float]:
    """A burst capacity of ``value`` at ``start``, moving by ``per_hour`` points an hour, within
    0-100."""
    return lambda at: min(100.0, max(0.0, value + per_hour * (at - start).total_seconds() / 3600))


def replay_night(at: datetime) -> float:
    """The night of 2026-11-04 as probed: full, then a replay on one vCPU from 21:00 for 4 h 40 min
    (5.7 points an hour), then idle (3.6 points an hour back)."""
    start, stop = datetime(2026, 11, 4, 21, 0, tzinfo=KST), datetime(2026, 11, 5, 1, 40, tzinfo=KST)
    if at <= start:
        return 100.0
    if at <= stop:
        return line(start, 100.0, -5.7)(at)
    return line(stop, line(start, 100.0, -5.7)(stop), 3.6)(at)


def test_a_night_replay_as_on_2026_11_05_is_written_on_the_summary_and_not_alerted(tmp_path: Path) -> None:
    world = World(datetime(2026, 11, 4, 20, 4, tzinfo=KST), instance=lambda at: 0, burst=replay_night)

    done = runs(world, datetime(2026, 11, 4, 20, 4, tzinfo=KST), datetime(2026, 11, 5, 9, 4, tzinfo=KST))

    assert done == [] and world.telegram == []
    assert replay_night(datetime(2026, 11, 5, 1, 40, tzinfo=KST)) == pytest.approx(73.4)
    page = tmp_path / "summary.md"
    world.now = datetime(2026, 11, 5, 1, 44, tzinfo=KST)
    assert world.tick({**ENV, "GITHUB_STEP_SUMMARY": str(page)}) == 0
    assert (
        "- BurstCapacityPercentage: 73.4% · 가장 새 점 11-05 01:40 · 지난 40분의 가장 낮은 점 73.4%"
        in page.read_text()
    )
    assert "- 버스트 판단: none. CPU 버스트 여유가 73.4%다(11-05 01:40) (열린 이슈: 없음)" in page.read_text()


def test_below_half_it_alerts_once_and_closes_the_issue_back_at_sixty() -> None:
    """Both vCPUs busy from midnight (15 points an hour) until 03:00, then idle."""
    midnight = datetime(2026, 11, 6, 0, 0, tzinfo=KST)
    three = midnight + timedelta(hours=3)

    def busy(at: datetime) -> float:
        return line(midnight, 80.0, -15.0)(at) if at <= three else line(three, 35.0, 3.6)(at)

    world = World(midnight, instance=lambda at: 0, burst=busy)

    done = runs(world, midnight + timedelta(minutes=4), midnight + timedelta(hours=10, minutes=24))

    assert done == [
        # 02:04 reads the 02:00 point, 50.0%: not below it. 02:14 reads 02:10's, 47.5%.
        ("02:14", ["gh issue create"]),
        # Back at 60% from the 10:00 point (60.2%); quiet while it climbs.
        ("10:04", ["gh issue comment", "gh issue close"]),
    ]
    [issue] = world.issues.values()
    assert issue["labels"] == ["server-burst"] and issue["state"] == "closed"
    assert (
        issue["title"]
        == "서버 watchdog: krx-quant CPU 버스트 여유가 50% 아래다 (47.5%, 2026-11-06 02:14 KST)"
    )
    assert issue["body"].startswith(
        "<!-- krx-quant-watchdog: burst-low -->\n**CPU 버스트 여유가 50% 아래다.**"
    )
    assert "- CPU 버스트 여유(`BurstCapacityPercentage`): 47.5% · 가장 새 점 11-06 02:10" in issue["body"]
    assert issue["comments"][0]["body"].startswith("<!-- krx-quant-watchdog: burst-back -->")
    assert "CPU 버스트 여유가 60.2%다(11-06 10:00). 60% 이상으로 돌아왔다" in issue["comments"][0]["body"]
    assert world.telegram == [
        "[watchdog] krx-quant CPU 버스트 여유가 47.5%로 50% 아래입니다. 바닥나면 CPU가 기준선(20%)으로"
        f" 느려집니다. https://github.com/{REPO}/issues/401",
        f"[watchdog] krx-quant CPU 버스트 여유가 60.2%로 돌아왔습니다. https://github.com/{REPO}/issues/401",
    ]
    assert world.reboots() == []


def test_a_burst_capacity_late_or_missing_neither_alerts_nor_closes() -> None:
    now = datetime(2026, 11, 6, 2, 14, tzinfo=KST)
    low = [watchdog.Point(now - timedelta(minutes=m), 40.0, 40.0) for m in (24, 19, 16)]
    assert watchdog.decide_burst(low, None, now).action == "none"
    assert watchdog.decide_burst(low, None, now).stale
    assert watchdog.decide_burst([], None, now).action == "none"
    back = [watchdog.Point(now - timedelta(minutes=16), 70.0, 70.0)]
    assert watchdog.decide_burst(back, incident(number=410), now).action == "none"
    assert (
        watchdog.decide_burst(
            low[2:] + [watchdog.Point(now - timedelta(minutes=4), 40.0, 40.0)], None, now
        ).action
        == "low"
    )


def test_between_the_two_marks_it_keeps_what_it_did() -> None:
    now = datetime(2026, 11, 6, 2, 14, tzinfo=KST)
    at = [watchdog.Point(now - timedelta(minutes=4), 55.0, 55.0)]
    assert watchdog.decide_burst(at, None, now).action == "none"
    assert watchdog.decide_burst(at, incident(number=410), now).action == "none"
    low = [watchdog.Point(now - timedelta(minutes=4), 49.9, 49.9)]
    assert watchdog.decide_burst(low, incident(number=410), now).action == "none", "already alerted"
    assert watchdog.decide_burst(low, None, now).action == "low", "its issue was closed by hand: again"


@pytest.mark.parametrize(
    ("fail", "hang"),
    [
        (watchdog.BURST, ""),
        ("", watchdog.BURST),  # Lightsail does not answer: ``run_command``'s time limit
        ("", "--label server-burst"),  # the burst issue's lookup
    ],
)
def test_the_burst_capacity_unread_holds_no_reboot_and_fails_the_run(fail: str, hang: str) -> None:
    world = World(datetime(2026, 10, 28, 0, 54, tzinfo=KST), fail=fail, hang=hang)
    assert world.tick() == 1
    assert len(world.reboots()) == 1 and len(world.issues) == 1
    [issue] = world.issues.values()
    assert issue["labels"] == ["server-watchdog"]
    # The burst capacity is read after the reboot and its report: a read that hangs delays neither.
    done = [
        i
        for i, c in enumerate(world.calls)
        if c[:3] in (["aws", "lightsail", "reboot-instance"], ["gh", "issue", "create"])
    ]
    burst = [i for i, c in enumerate(world.calls) if watchdog.BURST in c or "server-burst" in c]
    assert len(done) == 2 and burst and max(done) < min(burst)


def test_the_reboot_issue_and_the_burst_issue_are_apart() -> None:
    """An open burst issue is not the reboots' incident, and the other way round."""
    world = World(datetime(2026, 11, 6, 2, 14, tzinfo=KST), instance=lambda at: 0, burst=lambda at: 47.5)
    assert world.tick() == 0
    world.now += timedelta(minutes=10)
    assert world.tick() == 0
    assert [i["labels"] for i in world.issues.values()] == [["server-burst"]]
    assert world.issues[401]["state"] == "open" and world.issues[401]["comments"] == []


def test_without_its_issue_the_burst_alert_is_not_sent_but_tried_again_next_run() -> None:
    """The open issue keeps the next runs quiet: a Telegram line sent without it would come again
    every 10 minutes while GitHub refuses."""
    world = World(
        datetime(2026, 11, 6, 2, 14, tzinfo=KST),
        instance=lambda at: 0,
        burst=lambda at: 47.5,
        fail="issue create",
    )
    assert world.tick() == 1
    assert world.issues == {} and world.telegram == []
    world.fail = ""
    world.now += timedelta(minutes=10)
    assert world.tick() == 0
    assert len(world.issues) == 1 and len(world.telegram) == 1
