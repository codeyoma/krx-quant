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
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3

from krx_quant.config import Settings
from krx_quant.notify import Alerts

log = logging.getLogger(__name__)

# The shares of the budget told on Telegram, in percent.
WARN_AT = (50, 80)
STATE_FILE = "cost_guard.json"
_ALERT = "cost_guard"
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
    stop_told: bool = False


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
        """Read the month's spend once and act on it.

        In a new month the schedules are turned on, whatever the record says (it may be
        lost). While the month is capped they are kept off (a stack deployed again turns
        them on). A spend AWS last worked out before this month is last month's: nothing
        is told or stopped on it.
        """
        now = self._now().astimezone(UTC)
        month = now.strftime("%Y-%m")
        state = self.load(self._path)
        if state.month != month:
            if await asyncio.to_thread(self._set_schedules, "ENABLED"):
                await self._notify(f"새 달({month})이라 밤 학습(Spot) 스케줄을 다시 켰습니다.")
            state = _Month(month)
            self._save(state)
        spent, limit = await asyncio.to_thread(self._spend, now.replace(day=1, hour=0, minute=0, second=0))
        if spent is not None:
            for share in WARN_AT:
                if share not in state.warned and spent >= limit * share / 100:
                    text = f"AWS 비용이 이달 ${spent:.2f}로 상한 ${limit:.0f}의 {share}%를 넘었습니다."
                    if await self._notify(text):
                        state.warned.append(share)
                        self._save(state)
            if not state.stopped and spent >= limit:
                state.stopped = True
                self._save(state)
        if state.stopped:
            await asyncio.to_thread(self._set_schedules, "DISABLED")
            if not state.stop_told:
                shown = f"${spent:.2f}" if spent is not None else "상한"
                state.stop_told = await self._notify(
                    f"AWS 비용이 이달 {shown}로 상한 ${limit:.0f}에 닿았습니다. "
                    "이달 남은 기간 밤 학습(Spot)을 끕니다. 실시간 예측은 계속합니다."
                )
                self._save(state)

    def _spend(self, month_start: datetime) -> tuple[float | None, float]:
        """The month's actual spend (None if AWS last worked it out before ``month_start``)
        and the budget, in dollars."""
        budget = self._budgets.describe_budget(AccountId=self._account, BudgetName=self._budget)["Budget"]
        limit = float(budget["BudgetLimit"]["Amount"])
        updated = budget.get("LastUpdatedTime")
        if updated is not None and updated < month_start:
            return None, limit
        spent = budget.get("CalculatedSpend", {}).get("ActualSpend", {}).get("Amount", "0")
        return float(spent), limit

    def _set_schedules(self, state: str) -> list[str]:
        """Put every schedule in ``state``; the ones that were not."""
        changed = []
        for name in self._schedules:
            schedule = self._scheduler.get_schedule(Name=name)
            if schedule["State"] == state:
                continue
            kept = {k: schedule[k] for k in _SCHEDULE_FIELDS if k in schedule}
            self._scheduler.update_schedule(**kept, State=state)
            changed.append(name)
            log.info("schedule %s: %s", name, state)
        return changed

    @staticmethod
    def load(path: Path) -> _Month:
        try:
            data = json.loads(path.read_text())
            warned = [int(s) for s in data["warned"]]
            return _Month(data["month"], warned, bool(data["stopped"]), bool(data.get("stop_told", True)))
        except (OSError, ValueError, KeyError, TypeError):
            return _Month("")

    def _save(self, state: _Month) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(state)))
        tmp.replace(self._path)


def training_stopped(state_dir: Path, now: datetime) -> bool:
    """Whether the guard turned the Spot training off for ``now``'s month."""
    state = CostGuard.load(state_dir / STATE_FILE)
    return state.stopped and state.month == now.astimezone(UTC).strftime("%Y-%m")


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


async def keep_costs(settings: Settings, alerts: Alerts, now: Callable[[], datetime]) -> None:
    """Check the costs every ``settings.cost_check_seconds`` until cancelled. A failure (AWS
    unreachable, a missing permission) raises an alert, cleared once a check works again."""
    guard: CostGuard | None = None
    while True:
        try:
            if guard is None:
                guard = await asyncio.to_thread(open_guard, settings, alerts.deliver, now)
            await guard.check()
        except Exception as e:
            log.exception("could not check the AWS costs")
            alerts.raise_(_ALERT, f"AWS 비용을 읽지 못해 상한을 지킬 수 없습니다: {e}")
        else:
            alerts.resolve(_ALERT, "AWS 비용을 다시 읽습니다.")
        await asyncio.sleep(settings.cost_check_seconds)
