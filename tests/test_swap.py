"""The morning swap and the rollback on the always-on server, through a local Object Store.

Input: promotion lists and models in the store (as a night of training leaves them), the
server's state directory, and the time. Output: the models the engine runs, what the
server publishes as live, and the training status it shows.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.algorithms import Model, save_model
from krx_quant.api import create_app
from krx_quant.cli import main
from krx_quant.clock import FakeClock
from krx_quant.engine import PredictionEngine
from krx_quant.feeds import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.model_shelf import Shelf
from krx_quant.object_store import LocalObjectStore, ObjectStore
from krx_quant.predictions import PredictionStore
from krx_quant.swap import morning_swap, night_status, night_to_train, publish_live, reload_models
from tests.api_schema import check
from tests.replay_events import api, get_json

NIGHT = date(2026, 10, 12)  # a Monday's night
MORNING = date(2026, 10, 13)
TRAINED = Model("newton-20261012-abc123", {"damping": 0.25, "threshold": 0.5}, "2026-09-14..2026-10-07")


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


def night_list(
    store_root: Path, *, finalized: datetime, promoted: dict[str, Model], day: date = NIGHT
) -> None:
    for name, model in promoted.items():
        path = store_root / "models" / name / f"{model.version}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(model.to_json())
    reports = [{"algorithm": name, "promoted": True, "reason": "0.6050 < 0.6102"} for name in promoted]
    data = {
        "day": str(day),
        "finalized_at": finalized.isoformat(),
        "promoted": {name: m.version for name, m in promoted.items()},
        "reports": reports,
    }
    path = store_root / "promotions" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture
def store(tmp_path: Path) -> ObjectStore:
    return LocalObjectStore(tmp_path / "store")


@pytest.fixture
def engine(tmp_path: Path) -> PredictionEngine:
    calendar = MarketCalendar({NIGHT: True, MORNING: True, date(2026, 10, 14): True})
    clock = FakeClock(at(MORNING, "07:00"))
    return PredictionEngine(PredictionStore(tmp_path / "engine.db"), calendar, ("005930",), 0.00015, clock)


async def morning(store: ObjectStore, tmp_path: Path, engine: PredictionEngine, day: date = MORNING) -> Any:
    shelf = Shelf(tmp_path / "state")
    return await morning_swap(store, shelf, engine, today=day, at=at(day, "08:00"))


async def test_the_night_s_promoted_models_run_from_08_00(
    store: ObjectStore, tmp_path: Path, engine: PredictionEngine
) -> None:
    night_list(tmp_path / "store", finalized=at(NIGHT, "21:40"), promoted={"newton": TRAINED})

    await morning(store, tmp_path, engine)

    assert engine.models["newton"] == TRAINED
    assert engine.models["gauss"].version == "gauss-default", "not promoted: unchanged"
    async with api(engine) as client:
        training = (await get_json(client, "/v1/status"))["training"]
    assert training["day"] == str(NIGHT) and training["applied"] is True
    assert training["promoted"] == {"newton": TRAINED.version}
    assert training["reports"] == [{"algorithm": "newton", "promoted": True, "reason": "0.6050 < 0.6102"}]
    publish_live(store, Shelf(tmp_path / "state"), engine.algorithms)
    live = json.loads((tmp_path / "store" / "live.json").read_text())
    assert live["newton"] == TRAINED.version and live["gauss"] == "gauss-default"


async def test_a_list_not_final_by_07_30_is_left_for_yesterday_s_models(
    store: ObjectStore, tmp_path: Path, engine: PredictionEngine
) -> None:
    night_list(tmp_path / "store", finalized=at(MORNING, "07:45"), promoted={"newton": TRAINED})

    applied = await morning(store, tmp_path, engine)

    assert engine.models["newton"].version == "newton-default"
    assert applied.as_dict()["applied"] is False
    assert "07:30" in applied.as_dict()["reason"]
    # The next morning it runs, if no later night replaced it.
    later = await morning(store, tmp_path, engine, day=date(2026, 10, 14))
    assert engine.models["newton"] == TRAINED
    assert later.as_dict()["applied"] is True


async def test_without_a_list_nothing_changes(
    store: ObjectStore, tmp_path: Path, engine: PredictionEngine
) -> None:
    assert await morning(store, tmp_path, engine) is None
    assert engine.models["newton"].version == "newton-default"


async def test_a_rollback_goes_back_one_model_and_stays_back(
    store: ObjectStore, tmp_path: Path, engine: PredictionEngine
) -> None:
    shelf = Shelf(tmp_path / "state")
    fitted = Model("newton-20261009-0f0f0f", {"damping": 0.5, "threshold": 1.0})
    shelf.install("newton", fitted, "fit", at(NIGHT, "20:00"))
    night_list(tmp_path / "store", finalized=at(NIGHT, "21:40"), promoted={"newton": TRAINED})
    await morning(store, tmp_path, engine)
    assert engine.models["newton"] == TRAINED

    assert shelf.rollback("newton", at(MORNING, "09:30")) == fitted
    reload_models(engine, shelf)
    assert engine.models["newton"] == fitted
    status = night_status(store, shelf, MORNING)
    assert status is not None and status.applied is False
    assert status.reason == f"applied, then replaced: newton runs {fitted.version}"

    # The next morning has no newer list: the rolled-back promotion is not applied again.
    await morning(store, tmp_path, engine, day=date(2026, 10, 14))
    assert engine.models["newton"] == fitted
    # Back once more: the defaults.
    assert shelf.rollback("newton", at(MORNING, "10:00")) is None
    reload_models(engine, shelf)
    assert engine.models["newton"].version == "newton-default"
    assert [h["why"] for h in shelf.history("newton")] == [
        "fit",
        f"promotion {NIGHT}",
        f"rollback from {TRAINED.version}",
        f"rollback from {fitted.version}",
    ]


def test_the_rollback_command_names_what_runs_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    shelf = Shelf(tmp_path / "state")
    on_touch = replace(TRAINED, version="newton-20261009-0a0b0c", target_version=2)
    shelf.install("newton", on_touch, "fit", at(NIGHT, "20:00"))

    # On the touch target (#170) the defaults were not trained on it: not rolled back to.
    assert main(["rollback", "newton"]) == 1
    assert "was not trained on target 2" in capsys.readouterr().err
    shelf.install("newton", replace(on_touch, version="newton-20261010-0d0e0f"), "fit", at(NIGHT, "20:30"))
    assert main(["rollback", "newton"]) == 0
    assert "newton now runs newton-20261009-0a0b0c" in capsys.readouterr().out

    monkeypatch.setenv("KRX_TARGET_VERSION", "1")  # before the switch any model goes
    assert main(["rollback", "newton"]) == 0
    assert "newton now runs its defaults" in capsys.readouterr().out
    assert main(["rollback", "newton"]) == 1
    assert "nothing to roll back" in capsys.readouterr().err
    assert main(["rollback", "einstein"]) == 1


async def test_a_night_that_promoted_nothing_is_taken_all_the_same(
    store: ObjectStore, tmp_path: Path, engine: PredictionEngine
) -> None:
    night_list(tmp_path / "store", finalized=at(NIGHT, "21:40"), promoted={})

    applied = await morning(store, tmp_path, engine)

    assert applied.as_dict()["applied"] is True
    status = night_status(store, Shelf(tmp_path / "state"), MORNING)
    assert status is not None and status.as_dict()["reason"] == "applied"


def test_a_model_installed_before_the_history_is_one_rollback_away(tmp_path: Path) -> None:
    earlier = Model("newton-20261008-14a50c", {"damping": 0.5, "threshold": 1.0}, "fit by hand")
    save_model(tmp_path / "state", "newton", earlier)
    shelf = Shelf(tmp_path / "state")

    shelf.install("newton", TRAINED, f"promotion {NIGHT}", at(MORNING, "08:00"))

    assert shelf.rollback("newton", at(MORNING, "09:00")) == earlier


async def test_a_list_naming_a_model_the_store_lacks_is_not_applied_at_all(
    store: ObjectStore, tmp_path: Path, engine: PredictionEngine
) -> None:
    gauss = Model("gauss-20261012-def456", {"speed": 0.1, "weight": 0.5, "threshold": 1.0})
    night_list(tmp_path / "store", finalized=at(NIGHT, "21:40"), promoted={"newton": TRAINED, "gauss": gauss})
    (tmp_path / "store" / "models" / "gauss" / f"{gauss.version}.json").unlink()

    status = await morning(store, tmp_path, engine)

    assert status.as_dict()["applied"] is False and gauss.version in status.as_dict()["reason"]
    assert engine.models["newton"].version == "newton-default", "all or nothing"
    assert not Shelf(tmp_path / "state").applied(NIGHT), "tried again the next morning"


def test_a_training_before_the_night_s_archive_trains_the_day_before() -> None:
    calendar = MarketCalendar({NIGHT: True, MORNING: True})

    assert night_to_train(calendar, at(MORNING, "21:30")) == MORNING
    assert night_to_train(calendar, at(MORNING, "16:00")) == NIGHT, "today's bars are archived at 21:00"
    assert night_to_train(calendar, at(date(2026, 10, 19), "16:00")) == date(2026, 10, 16), "over a weekend"


def test_the_train_command_needs_the_object_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KRX_OBJECT_STORE", raising=False)

    assert main(["train"]) == 2
    assert "KRX_OBJECT_STORE is not set" in capsys.readouterr().err


async def test_an_algorithm_s_detail_shows_the_models_installed_and_rolled_back(
    store: ObjectStore, tmp_path: Path, engine: PredictionEngine
) -> None:
    night_list(tmp_path / "store", finalized=at(NIGHT, "21:40"), promoted={"newton": TRAINED})
    await morning(store, tmp_path, engine)
    shelf = Shelf(tmp_path / "state")
    shelf.rollback("newton", at(MORNING, "09:10"))

    async with TestClient(TestServer(create_app(engine, shelf=shelf))) as client:
        newton = await get_json(client, "/v1/algorithms/newton")
        gauss = await get_json(client, "/v1/algorithms/gauss")
        unknown = await client.get("/v1/algorithms/nobody")

    assert (newton["name"], newton["card"]["scientist"]) == ("newton", "Isaac Newton")
    assert newton["status"]["status"] == "probation"
    assert newton["model_history"] == [
        {"at": "2026-10-13T08:00:00+09:00", "version": TRAINED.version, "why": f"promotion {NIGHT}"},
        {"at": "2026-10-13T09:10:00+09:00", "version": None, "why": f"rollback from {TRAINED.version}"},
    ]
    assert gauss["model_history"] == [], "never installed: its defaults"
    check("algorithm_detail", newton)
    assert unknown.status == 404
