"""The nightly training's outcomes, kept in the state folder across restarts and deploys (#355).

``state/training_nights.json`` holds each night's outcome (``Outcome``: whether its training
finished, when it ended, the exit code, the last step it started, why not), the latest
``NIGHTS_KEPT``, and the latest night the nights in a row were alerted for (``alerted_for``).
The training's output itself is the training process's log (#356).

The nights not applied (``TrainingNights.not_applied``) are the trading nights after the last
one whose promotion list the morning swap applied (``state/models/applied.json``): each whose
training failed, and each whose swap passed without its list (it failed, was final too late, or
never ran). They are counted from these files only, so a restart does not reset them.
"""

from __future__ import annotations

import json
import logging
import re
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar, next_on_trading_day

log = logging.getLogger(__name__)

FILE = "training_nights.json"
APPLIED_FILE = Path("models") / "applied.json"  # model_shelf's
NIGHTS_KEPT = 30
# The line each step of the training starts with (``nightly.STARTED_STEP``).
_STARTED = re.compile(r"started step (\S+)")


@dataclass(frozen=True)
class Failure:
    """Why a night's training did not finish: for the alert (``reason``), the exit code
    (None: stopped at the time limit, or not run here: a Spot night) and the last step it
    started (None: none, or not known)."""

    reason: str
    exit_code: int | None = None
    step: str | None = None


@dataclass(frozen=True)
class Outcome:
    day: date
    ok: bool
    ended_at: datetime
    exit_code: int | None = None
    step: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "ok": self.ok,
            "ended_at": self.ended_at.astimezone(KST).isoformat(),
            "exit_code": self.exit_code,
            "step": self.step,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Outcome:
        return cls(
            date.fromisoformat(data["day"]),
            bool(data["ok"]),
            datetime.fromisoformat(data["ended_at"]),
            data.get("exit_code"),
            data.get("step"),
            data.get("reason"),
        )


def last_step(text: str) -> str | None:
    """The last step a night's output says it started."""
    found = _STARTED.findall(text)
    return found[-1] if found else None


def exit_text(code: int) -> str:
    """``exit -9 (SIGKILL)``: a negative code is the signal that ended the process."""
    if code < 0:
        try:
            return f"exit {code} ({signal.Signals(-code).name})"
        except ValueError:
            pass
    return f"exit {code}"


class TrainingNights:
    """``state/training_nights.json`` in ``state_dir``, and the last night applied."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / FILE

    def record(self, outcome: Outcome) -> None:
        """Keep a night's outcome (the latest ``NIGHTS_KEPT``)."""
        data = self._read()
        nights = {o.day: o for o in self._outcomes(data)}
        nights[outcome.day] = outcome
        kept = [nights[d].as_dict() for d in sorted(nights)[-NIGHTS_KEPT:]]
        self._write({**data, "nights": kept})

    def outcomes(self) -> dict[date, Outcome]:
        return {o.day: o for o in self._outcomes(self._read())}

    def alerted_for(self) -> date | None:
        """The latest night the nights in a row were alerted for."""
        try:
            return date.fromisoformat(self._read()["alerted_for"])
        except (KeyError, TypeError, ValueError):
            return None

    def mark_alerted(self, day: date) -> None:
        self._write({**self._read(), "alerted_for": day.isoformat()})

    def last_applied(self) -> date | None:
        """The last night whose promotion list the morning swap applied (``Shelf.applied``)."""
        try:
            return date.fromisoformat(json.loads((self.state_dir / APPLIED_FILE).read_text())["night"])
        except (OSError, KeyError, TypeError, ValueError):
            return None

    def not_applied(self, calendar: MarketCalendar, swap_at: time, now: datetime) -> list[date]:
        """The trading nights after the last applied one whose list does not run, oldest first:
        each whose training failed, and each whose morning swap (``swap_at`` the next trading
        day) has passed. Before any night was applied, from the first outcome kept."""
        outcomes = self.outcomes()
        start = self.last_applied()
        if start is None:
            if not outcomes:
                return []
            start = min(outcomes) - timedelta(days=1)
        today = now.astimezone(KST).date()
        out = []
        day = start + timedelta(days=1)
        while day <= today:
            if calendar.is_trading_day(day):
                found = outcomes.get(day)
                swap = next_on_trading_day(calendar, swap_at, datetime.combine(day, time.max, KST))
                if (found is not None and not found.ok) or now >= swap:
                    out.append(day)
            day += timedelta(days=1)
        return out

    def fields(self, calendar: MarketCalendar, swap_at: time, now: datetime) -> dict[str, Any]:
        """``/v1/status``'s ``training`` fields of these files: the last night applied, the nights
        not applied since, and the failures among them (latest last)."""
        missed = self.not_applied(calendar, swap_at, now)
        outcomes = self.outcomes()
        applied = self.last_applied()
        return {
            "last_applied": None if applied is None else applied.isoformat(),
            "nights_not_applied": len(missed),
            "not_applied": [d.isoformat() for d in missed],
            "failures": [
                {k: v for k, v in outcomes[d].as_dict().items() if k != "ok"}
                for d in missed
                if d in outcomes and not outcomes[d].ok
            ],
        }

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.error("cannot read %s (%s): the nightly training's outcomes start afresh", FILE, e)
            return {}
        return data if isinstance(data, dict) else {}

    def _outcomes(self, data: Mapping[str, Any]) -> list[Outcome]:
        out = []
        for row in data.get("nights") or ():
            try:
                out.append(Outcome.from_dict(row))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def _write(self, data: Mapping[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)
