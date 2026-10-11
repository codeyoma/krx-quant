"""Cost Guard: the month's AWS spend kept under its budget, from the always-on server (#23).

The server reads the budget (AWS Budgets, ``KRX_COST_BUDGET``) every hour or so:

- at half and at four fifths of it, it says so on Telegram, once each a month;
- at the budget, it turns the nightly training's EC2 Spot schedules off
  (``KRX_TRAINING_SCHEDULES``, EventBridge Scheduler) for the rest of the month and says
  so; live predictions go on;
- in a new month, it turns them on again and says so.

AWS refreshes a budget's spend a few times a day, so the guard may act hours late. What it
told and did this month is kept in the state directory, so a restart does not tell again.
The checks run on the server because the Telegram token stays in its ``.env``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3

from krx_quant.config import Settings

log = logging.getLogger(__name__)

# The shares of the budget told on Telegram, in percent.
WARN_AT = (50, 80)
STATE_FILE = "cost_guard.json"
# What update_schedule takes back from get_schedule, besides the state.
_SCHEDULE_FIELDS = (
    "Name",
    "GroupName",
    "ScheduleExpression",
    "ScheduleExpressionTimezone",
    "StartDate",
    "EndDate",
    "Description",
    "FlexibleTimeWindow",
    "Target",
    "KmsKeyArn",
    "ActionAfterCompletion",
)


@dataclass
class _Month:
    """What the guard told and did in a month (UTC, as AWS bills)."""

    month: str
    warned: list[int] = field(default_factory=list)  # the shares told, in percent
    stopped: bool = False  # the Spot schedules are off for the rest of it


class CostGuard:
    def __init__(
        self,
        budgets: Any,
        scheduler: Any,
        account: str,
        budget: str,
        schedules: Sequence[str],
        state_dir: Path,
        notify: Callable[[str], Awaitable[bool]],
        now: Callable[[], datetime],
    ) -> None:
        """``budgets`` and ``scheduler`` are boto3 clients (Budgets answers in us-east-1)."""
        self._budgets = budgets
        self._scheduler = scheduler
        self._account = account
        self._budget = budget
        self._schedules = tuple(schedules)
        self._path = state_dir / STATE_FILE
        self._notify = notify
        self._now = now

    async def check(self) -> None:
        """Read the month's spend once and act on it."""
        month = self._now().astimezone(UTC).strftime("%Y-%m")
        state = self._load()
        if state.month != month:
            if state.stopped:
                await asyncio.to_thread(self._set_schedules, "ENABLED")
                await self._notify(f"새 달({month})이라 밤 학습(Spot) 스케줄을 다시 켰습니다.")
            state = _Month(month)
            self._save(state)
        spent, limit = await asyncio.to_thread(self._spend)
        for share in WARN_AT:
            if share not in state.warned and spent >= limit * share / 100:
                text = f"AWS 비용이 이달 ${spent:.2f}로 상한 ${limit:.0f}의 {share}%를 넘었습니다."
                if await self._notify(text):
                    state.warned.append(share)
                    self._save(state)
        if not state.stopped and spent >= limit:
            await asyncio.to_thread(self._set_schedules, "DISABLED")
            state.stopped = True
            self._save(state)
            await self._notify(
                f"AWS 비용이 이달 ${spent:.2f}로 상한 ${limit:.0f}에 닿았습니다. "
                "이달 남은 기간 밤 학습(Spot)을 끕니다. 실시간 예측은 계속합니다."
            )

    def _spend(self) -> tuple[float, float]:
        """The month's actual spend and the budget, in dollars."""
        budget = self._budgets.describe_budget(AccountId=self._account, BudgetName=self._budget)["Budget"]
        spent = budget.get("CalculatedSpend", {}).get("ActualSpend", {}).get("Amount", "0")
        return float(spent), float(budget["BudgetLimit"]["Amount"])

    def _set_schedules(self, state: str) -> None:
        for name in self._schedules:
            schedule = self._scheduler.get_schedule(Name=name)
            kept = {k: schedule[k] for k in _SCHEDULE_FIELDS if k in schedule}
            self._scheduler.update_schedule(**kept, State=state)
            log.info("schedule %s: %s", name, state)

    def _load(self) -> _Month:
        try:
            data = json.loads(self._path.read_text())
            return _Month(data["month"], [int(s) for s in data["warned"]], bool(data["stopped"]))
        except (OSError, ValueError, KeyError, TypeError):
            return _Month("")

    def _save(self, state: _Month) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"month": state.month, "warned": state.warned, "stopped": state.stopped}))
        tmp.replace(self._path)


def open_guard(
    settings: Settings, notify: Callable[[str], Awaitable[bool]], now: Callable[[], datetime]
) -> CostGuard:
    """The guard of ``settings.cost_budget``, on the server's AWS keys (asks AWS who it is)."""
    assert settings.cost_budget is not None
    session = boto3.session.Session(region_name=settings.aws_region)
    account = session.client("sts").get_caller_identity()["Account"]
    return CostGuard(
        session.client("budgets", region_name="us-east-1"),
        session.client("scheduler"),
        account,
        settings.cost_budget,
        settings.training_schedules,
        settings.state_dir,
        notify,
        now,
    )


async def keep_costs(
    settings: Settings, notify: Callable[[str], Awaitable[bool]], now: Callable[[], datetime]
) -> None:
    """Check the costs every ``settings.cost_check_seconds`` until cancelled; a failure
    (AWS unreachable, a missing permission) is logged and tried again the next time."""
    guard: CostGuard | None = None
    while True:
        try:
            if guard is None:
                guard = await asyncio.to_thread(open_guard, settings, notify, now)
            await guard.check()
        except Exception:
            log.exception("could not check the AWS costs")
        await asyncio.sleep(settings.cost_check_seconds)
