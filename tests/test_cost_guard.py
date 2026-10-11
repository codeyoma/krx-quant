"""The Cost Guard on the always-on server (#23), against AWS Budgets and EventBridge
Scheduler as moto fakes them.

Input: the month's spend the budget shows, the time, and the server's state directory.
Output: the Telegram notices sent, and whether the nightly training's Spot schedules run.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from krx_quant.cost_guard import CostGuard

BUDGET = "krx-quant-monthly"
SCHEDULES = ("krx-quant-training", "krx-quant-training-retry")
REGION = "ap-southeast-2"


class Aws:
    """The faked account: its budget's spend, and the training schedules."""

    def __init__(self) -> None:
        self.budgets = boto3.client("budgets", region_name="us-east-1")
        self.scheduler = boto3.client("scheduler", region_name=REGION)
        self.account = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
        self.budgets.create_budget(AccountId=self.account, Budget=self._budget(0.0))
        for name in SCHEDULES:
            self.scheduler.create_schedule(
                Name=name,
                ScheduleExpression="cron(30 21 ? * MON-FRI *)",
                ScheduleExpressionTimezone="Asia/Seoul",
                FlexibleTimeWindow={"Mode": "OFF"},
                Target={
                    "Arn": "arn:aws:scheduler:::aws-sdk:ec2:runInstances",
                    "RoleArn": f"arn:aws:iam::{self.account}:role/krx-quant-training-scheduler",
                    "Input": "{}",
                },
                State="ENABLED",
            )

    def _budget(self, spent: float) -> dict[str, Any]:
        return {
            "BudgetName": BUDGET,
            "BudgetLimit": {"Amount": "100", "Unit": "USD"},
            "TimeUnit": "MONTHLY",
            "BudgetType": "COST",
            "CalculatedSpend": {"ActualSpend": {"Amount": str(spent), "Unit": "USD"}},
        }

    def spend(self, dollars: float) -> None:
        # moto has no update_budget: the budget is made again with the spend AWS would show.
        self.budgets.delete_budget(AccountId=self.account, BudgetName=BUDGET)
        self.budgets.create_budget(AccountId=self.account, Budget=self._budget(dollars))

    def states(self) -> set[str]:
        return {self.scheduler.get_schedule(Name=name)["State"] for name in SCHEDULES}


@pytest.fixture
def aws() -> Iterator[Aws]:
    with mock_aws():
        yield Aws()


class Guarded:
    """A Cost Guard on the faked account, its clock, and the notices it sent."""

    def __init__(self, aws: Aws, state: Path) -> None:
        # Now, as moto stamps the budget's spend when it is made: fresh this month.
        self.at = datetime.now(UTC)
        self.sent: list[str] = []
        self.telegram_down = False
        self.guard = CostGuard(
            aws.budgets, aws.scheduler, aws.account, BUDGET, SCHEDULES, state, self._notify, self.now
        )

    def now(self) -> datetime:
        return self.at

    def next_month(self) -> None:
        """Just after the 1st of the next month."""
        first = (self.at.replace(day=1) + timedelta(days=32)).replace(day=1)
        self.at = first.replace(hour=0, minute=5, second=0, microsecond=0)

    async def _notify(self, text: str) -> bool:
        if self.telegram_down:
            return False
        self.sent.append(text)
        return True


async def test_half_and_four_fifths_of_the_budget_are_told_once_each(aws: Aws, tmp_path: Path) -> None:
    g = Guarded(aws, tmp_path / "state")

    aws.spend(57.3)
    await g.guard.check()
    await g.guard.check()
    aws.spend(83.0)
    await g.guard.check()

    assert len(g.sent) == 2, g.sent
    assert "$57.30" in g.sent[0] and "50%" in g.sent[0]
    assert "$83.00" in g.sent[1] and "80%" in g.sent[1]
    assert aws.states() == {"ENABLED"}


async def test_at_the_budget_the_spot_training_stops_for_the_month_and_starts_again_on_the_1st(
    aws: Aws, tmp_path: Path
) -> None:
    g = Guarded(aws, tmp_path / "state")

    aws.spend(100.4)
    await g.guard.check()

    assert aws.states() == {"DISABLED"}
    stopped = g.sent[-1]
    assert "$100.40" in stopped and "실시간 예측은 계속" in stopped, stopped
    assert len(g.sent) == 3, "50% and 80% told on the way"

    # A new month, the spend still last month's for a few hours (AWS refreshes it a few
    # times a day): not this month's, so nothing is stopped again.
    g.next_month()
    await g.guard.check()

    assert aws.states() == {"ENABLED"}
    assert "다시 켰습니다" in g.sent[-1], g.sent[-1]
    assert len(g.sent) == 4


async def test_schedules_a_redeploy_turned_on_while_capped_are_turned_off_again(
    aws: Aws, tmp_path: Path
) -> None:
    g = Guarded(aws, tmp_path / "state")
    aws.spend(101.0)
    await g.guard.check()

    # `aws cloudformation deploy` puts the template's ENABLED back.
    for name in SCHEDULES:
        schedule = aws.scheduler.get_schedule(Name=name)
        kept = {k: schedule[k] for k in ("Name", "ScheduleExpression", "FlexibleTimeWindow", "Target")}
        aws.scheduler.update_schedule(**kept, State="ENABLED")
    await g.guard.check()

    assert aws.states() == {"DISABLED"}


async def test_the_stop_is_told_once_telegram_answers_again(aws: Aws, tmp_path: Path) -> None:
    g = Guarded(aws, tmp_path / "state")
    g.telegram_down = True
    aws.spend(100.4)
    await g.guard.check()
    assert aws.states() == {"DISABLED"}, "stopped whether told or not"

    g.telegram_down = False
    await g.guard.check()

    assert [t for t in g.sent if "닿았습니다" in t], g.sent


async def test_schedules_left_off_without_the_guard_s_record_are_turned_on_in_a_new_month(
    aws: Aws, tmp_path: Path
) -> None:
    g = Guarded(aws, tmp_path / "state")
    aws.spend(101.0)
    await g.guard.check()
    (tmp_path / "state" / "cost_guard.json").unlink()  # the state directory was lost

    again = Guarded(aws, tmp_path / "state")
    again.next_month()
    await again.guard.check()

    assert aws.states() == {"ENABLED"}


async def test_a_restarted_server_does_not_tell_again(aws: Aws, tmp_path: Path) -> None:
    aws.spend(85.0)
    first = Guarded(aws, tmp_path / "state")
    await first.guard.check()

    again = Guarded(aws, tmp_path / "state")
    await again.guard.check()

    assert len(first.sent) == 2 and again.sent == []
