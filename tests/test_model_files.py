"""Model files kept to two (#361): the one installed now and the one a rollback goes to.

Input: installs, rollbacks and nightly promotion lists with their models in a local Object
Store (and one on S3, moto), as the swap and a night of training leave them; a state folder
with what piled up before. Output: the files left in ``state/models/versions`` and in the
store's ``models/``, the history, what a rollback does, and what the prune logs.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from krx_quant.algorithms import Model
from krx_quant.cli import main
from krx_quant.clock import FakeClock
from krx_quant.engine import PredictionEngine
from krx_quant.feeds import KST
from krx_quant.feeds.base import kst_day
from krx_quant.market_calendar import MarketCalendar
from krx_quant.model_shelf import Kept, Pruned, Shelf
from krx_quant.nightly import LIVE
from krx_quant.object_store import LocalObjectStore, ObjectStore, S3ObjectStore, put_json
from krx_quant.predictions import PredictionStore
from krx_quant.swap import morning_swap, prune_store, publish_live, tidy_models
from krx_quant.touch import TARGET_VERSION

DAYS = [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)]
# A direction algorithm, a kind's (#179), a daily one (#264) and the tables (#169, #180).
NAMES = ("newton", "mandelbrot", "cox", "baseline-climate", "range-climate")


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


def model(name: str, day: date, tag: str = "a1b2c3", target: int | None = None) -> Model:
    return Model(f"{name}-{day:%Y%m%d}-{tag}", {"x": 1.0}, f"trained to {day}", target_version=target)


def versions(state: Path, name: str) -> set[str]:
    return {p.name.removesuffix(".json") for p in (state / "models" / "versions" / name).glob("*.json")}


def in_store(root: Path, name: str) -> set[str]:
    return {p.name.removesuffix(".json") for p in (root / "models" / name).glob("*.json")}


def store_models(root: Path, models: Mapping[str, Model], stored: datetime) -> None:
    """The models as a night's training stores them, at ``stored``."""
    for name, m in models.items():
        path = root / "models" / name / f"{m.version}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(m.to_json())
        os.utime(path, (stored.timestamp(), stored.timestamp()))


def night(
    root: Path,
    day: date,
    *,
    promoted: Mapping[str, Model],
    beaten: Mapping[str, Model] = {},
    final: str = "21:40",
) -> None:
    """A night's challengers, stored half an hour before its list, final at ``final``."""
    finalized = at(day, final) if final > "12:00" else at(day + timedelta(days=1), final)
    store_models(root, {**promoted, **beaten}, finalized - timedelta(minutes=30))
    reports = [{"algorithm": n, "promoted": True, "reason": "won"} for n in promoted]
    reports += [{"algorithm": n, "promoted": False, "reason": "lost"} for n in beaten]
    data = {
        "day": str(day),
        "finalized_at": finalized.isoformat(),
        "promoted": {n: m.version for n, m in promoted.items()},
        "reports": reports,
    }
    path = root / "promotions" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture
def engine(tmp_path: Path) -> PredictionEngine:
    calendar = MarketCalendar(dict.fromkeys(DAYS, True))
    clock = FakeClock(at(DAYS[1], "07:00"))
    return PredictionEngine(PredictionStore(tmp_path / "engine.db"), calendar, ("005930",), 0.00015, clock)


async def morning(store: ObjectStore, shelf: Shelf, engine: PredictionEngine, day: date) -> Pruned:
    """The morning swap as the collector runs it: the list, what runs published, the prune."""
    await morning_swap(store, shelf, engine, today=day, at=at(day, "08:00"))
    publish_live(store, shelf, engine.algorithms)
    return prune_store(store, shelf, day)


@pytest.mark.parametrize("name", NAMES)
def test_each_install_keeps_the_model_installed_and_the_one_before(tmp_path: Path, name: str) -> None:
    state = tmp_path / "state"
    shelf = Shelf(state)
    first, second, third = (model(name, d) for d in DAYS[:3])

    shelf.install(name, first, "fit", at(DAYS[0], "20:00"))
    shelf.install(name, second, f"promotion {DAYS[0]}", at(DAYS[1], "08:00"))
    assert versions(state, name) == {first.version, second.version}
    shelf.install(name, third, f"promotion {DAYS[1]}", at(DAYS[2], "08:00"))

    assert versions(state, name) == {second.version, third.version}
    assert shelf.kept(name) == Kept(frozenset({second.version, third.version}))
    # The history keeps every entry, the deleted model's too.
    assert [h["version"] for h in shelf.history(name)] == [first.version, second.version, third.version]


def test_a_model_installed_twice_keeps_the_one_before_it(tmp_path: Path) -> None:
    """A night that learns nothing new promotes the same version again (a daily kind's
    refit): the model before it is the one kept, not a second copy of itself."""
    state = tmp_path / "state"
    shelf = Shelf(state)
    first, second = model("cox", DAYS[0]), model("cox", DAYS[1])
    shelf.install("cox", first, "fit", at(DAYS[0], "20:00"))
    shelf.install("cox", second, f"promotion {DAYS[0]}", at(DAYS[1], "08:00"))

    shelf.install("cox", second, f"promotion {DAYS[1]}", at(DAYS[2], "08:00"))

    assert versions(state, "cox") == {first.version, second.version}


def test_a_rollback_goes_back_one_step_only(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    state = tmp_path / "state"
    shelf = Shelf(state)
    first, second, third = (model("newton", d) for d in DAYS[:3])
    for day, m in zip(DAYS, (first, second, third), strict=False):
        shelf.install("newton", m, f"promotion {day}", at(day, "08:00"))

    assert shelf.rollback("newton", at(DAYS[2], "09:00")) == second
    assert versions(state, "newton") == {second.version}, "the one rolled back from goes too"
    assert shelf.kept("newton") == Kept(frozenset({second.version}), frozenset({first.version}))

    # The model before was deleted when the third was installed: back to the defaults.
    with caplog.at_level(logging.WARNING, logger="krx_quant.model_shelf"):
        assert shelf.rollback("newton", at(DAYS[2], "09:10")) is None
    assert f"{first.version} is no longer kept" in caplog.text
    assert shelf.current_version("newton") is None
    assert shelf.history("newton")[-1] == {
        "at": at(DAYS[2], "09:10").isoformat(),
        "algorithm": "newton",
        "version": None,
        "why": f"rollback from {second.version}",
    }
    assert versions(state, "newton") == set(), "the defaults run: nothing to go back to"
    with pytest.raises(ValueError, match="nothing to roll back"):
        shelf.rollback("newton", at(DAYS[2], "09:20"))


def test_on_the_touch_target_a_second_rollback_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    shelf = Shelf(tmp_path / "state")
    on_touch = [model("newton", d, target=TARGET_VERSION) for d in DAYS[:3]]
    for day, m in zip(DAYS, on_touch, strict=False):
        shelf.install("newton", m, f"promotion {day}", at(day, "08:00"))

    assert main(["rollback", "newton"]) == 0
    assert f"newton now runs {on_touch[1].version}" in capsys.readouterr().out
    assert main(["rollback", "newton"]) == 1

    err = capsys.readouterr().err
    assert f"{on_touch[0].version} is no longer kept (a rollback goes back one step only)" in err
    assert f"its defaults were not trained on target {TARGET_VERSION}" in err
    assert shelf.current_version("newton") == on_touch[1].version, "refused: it runs on"


def test_a_promotion_after_a_rollback_keeps_the_model_rolled_back_to(tmp_path: Path) -> None:
    state = tmp_path / "state"
    shelf = Shelf(state)
    first, second, third, fourth = (model("mandelbrot", d) for d in DAYS[:4])
    for day, m in zip(DAYS, (first, second, third), strict=False):
        shelf.install("mandelbrot", m, f"promotion {day}", at(day, "08:00"))
    shelf.rollback("mandelbrot", at(DAYS[2], "09:00"))

    shelf.install("mandelbrot", fourth, f"promotion {DAYS[3]}", at(DAYS[4], "08:00"))

    assert versions(state, "mandelbrot") == {second.version, fourth.version}
    assert shelf.rollback("mandelbrot", at(DAYS[4], "09:00")) == second
    assert versions(state, "mandelbrot") == {second.version}


async def test_the_store_keeps_what_runs_the_one_before_and_drops_the_challengers(
    tmp_path: Path, engine: PredictionEngine, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "store"
    store, shelf = LocalObjectStore(root), Shelf(tmp_path / "state")
    one = {n: model(n, DAYS[0]) for n in NAMES}
    two = {n: model(n, DAYS[1]) for n in NAMES}
    three = {n: model(n, DAYS[2]) for n in NAMES}
    gauss = [model("gauss", d) for d in DAYS[:3]]

    night(root, DAYS[0], promoted=one, beaten={"gauss": gauss[0]})
    await morning(store, shelf, engine, DAYS[1])
    assert in_store(root, "gauss") == set(), "its challenger lost: gone once the night is applied"
    assert in_store(root, "newton") == {one["newton"].version}

    # The night after: the kind's challenger loses, the others win.
    beaten = {"gauss": gauss[1], "mandelbrot": two["mandelbrot"]}
    night(root, DAYS[1], promoted={n: m for n, m in two.items() if n != "mandelbrot"}, beaten=beaten)
    await morning(store, shelf, engine, DAYS[2])
    assert in_store(root, "mandelbrot") == {one["mandelbrot"].version}
    for name in ("newton", "cox", "baseline-climate", "range-climate"):
        assert in_store(root, name) == {one[name].version, two[name].version}

    # A third night, and a fourth one training now: its model is stored after the list is final.
    night(root, DAYS[2], promoted=three, beaten={"gauss": gauss[2]})
    training_now = model("newton", DAYS[3], "f0f0f0")
    store_models(root, {"newton": training_now}, at(DAYS[3], "07:50"))
    with caplog.at_level(logging.INFO, logger="krx_quant.swap"):
        pruned = await morning(store, shelf, engine, DAYS[3])

    assert in_store(root, "newton") == {two["newton"].version, three["newton"].version, training_now.version}
    assert in_store(root, "mandelbrot") == {one["mandelbrot"].version, three["mandelbrot"].version}
    for name in ("cox", "baseline-climate", "range-climate"):
        assert in_store(root, name) == {two[name].version, three[name].version}
    assert in_store(root, "gauss") == set()
    assert pruned.files == 5 and pruned.skipped == (), "four of the first night, gauss's third"
    assert "the store's models: deleted 5 files" in caplog.text
    assert json.loads((root / LIVE).read_text())["newton"] == three["newton"].version


async def test_a_night_left_waiting_keeps_its_promoted_models(
    tmp_path: Path, engine: PredictionEngine
) -> None:
    root = tmp_path / "store"
    store, shelf = LocalObjectStore(root), Shelf(tmp_path / "state")
    late, lost = model("newton", DAYS[0]), model("gauss", DAYS[0])
    night(root, DAYS[0], promoted={"newton": late}, beaten={"gauss": lost}, final="07:45")

    await morning(store, shelf, engine, DAYS[1])

    assert engine.models["newton"].version == "newton-default", "final after 07:30: not applied"
    assert in_store(root, "newton") == {late.version}, "it may still be applied"
    assert in_store(root, "gauss") == set(), "decided: not applied today"
    await morning(store, shelf, engine, DAYS[2])
    assert engine.models["newton"] == late


def piled_up(state: Path, root: Path, name: str, count: int) -> list[Model]:
    """What a server before #361 holds of ``name``: ``count`` promotions, each kept in
    ``versions/`` and in the store, the last one running, the night applied."""
    models = [model(name, DAYS[0] - timedelta(days=count - i)) for i in range(count)]
    folder = state / "models" / "versions" / name
    folder.mkdir(parents=True, exist_ok=True)
    history_path = state / "models" / "history.json"
    history = json.loads(history_path.read_text()) if history_path.exists() else []
    for m in models:
        (folder / f"{m.version}.json").write_text(m.to_json())
        history.append(
            {"at": at(DAYS[0], "08:00").isoformat(), "algorithm": name, "version": m.version, "why": "fit"}
        )
    history_path.write_text(json.dumps(history))
    (state / "models" / f"{name}.json").write_text(models[-1].to_json())
    for m in models:
        store_models(root, {name: m}, at(DAYS[0], "21:00"))
    return models


def test_the_first_start_deletes_what_piled_up(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    state, root = tmp_path / "state", tmp_path / "store"
    store, shelf = LocalObjectStore(root), Shelf(state)
    newton = piled_up(state, root, "newton", 6)
    cox = piled_up(state, root, "cox", 4)
    lost = model("gauss", DAYS[0], "0d0d0d")
    night(root, DAYS[0], promoted={}, beaten={"gauss": lost})
    shelf.mark_applied(DAYS[0])
    put_json(store, LIVE, {"newton": newton[-1].version, "gauss": "gauss-default", "cox": cox[-1].version})
    going = [
        *(state / "models" / "versions" / "newton" / f"{m.version}.json" for m in newton[:-2]),
        *(state / "models" / "versions" / "cox" / f"{m.version}.json" for m in cox[:-2]),
        *(root / "models" / "newton" / f"{m.version}.json" for m in newton[:-2]),
        *(root / "models" / "cox" / f"{m.version}.json" for m in cox[:-2]),
        root / "models" / "gauss" / f"{lost.version}.json",
    ]
    freed = sum(p.stat().st_size for p in going)

    with caplog.at_level(logging.INFO, logger="krx_quant"):
        pruned = tidy_models(store, shelf, DAYS[1])

    assert pruned == Pruned(4 + 2 + 4 + 2 + 1, freed)
    assert versions(state, "newton") == in_store(root, "newton") == {m.version for m in newton[-2:]}
    assert versions(state, "cox") == in_store(root, "cox") == {m.version for m in cox[-2:]}
    assert in_store(root, "gauss") == set()
    assert "state/models/versions: deleted 6 model files" in caplog.text
    assert "the store's models: deleted 7 files" in caplog.text
    assert tidy_models(store, shelf, DAYS[1]) == Pruned(), "the next start finds nothing to delete"


def test_nothing_of_an_algorithm_is_deleted_while_a_model_to_keep_is_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state, root = tmp_path / "state", tmp_path / "store"
    store, shelf = LocalObjectStore(root), Shelf(state)
    newton = piled_up(state, root, "newton", 4)
    cox = piled_up(state, root, "cox", 4)
    night(root, DAYS[0], promoted={})
    shelf.mark_applied(DAYS[0])
    # The one a rollback goes to is gone from the server, the one running from the store.
    (state / "models" / "versions" / "newton" / f"{newton[-2].version}.json").unlink()
    (root / "models" / "cox" / f"{cox[-1].version}.json").unlink()

    with caplog.at_level(logging.WARNING, logger="krx_quant"):
        pruned = tidy_models(store, shelf, DAYS[1])

    assert versions(state, "newton") == {m.version for m in [*newton[:-2], newton[-1]]}, "left whole"
    assert in_store(root, "cox") == {m.version for m in cox[:-1]}, "left whole"
    assert versions(state, "cox") == {m.version for m in cox[-2:]}, "the other place is pruned"
    assert in_store(root, "newton") == {m.version for m in newton[-2:]}
    assert pruned.skipped == ("newton", "cox")
    assert f"newton: {newton[-2].version} to keep is not in state/models/versions" in caplog.text
    assert f"cox: {cox[-1].version} to keep is not in the store" in caplog.text


def test_a_history_the_running_file_disagrees_with_deletes_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state = tmp_path / "state"
    newton = piled_up(state, tmp_path / "store", "newton", 3)
    (state / "models" / "newton.json").write_text(model("newton", DAYS[3], "9e9e9e").to_json())  # by hand

    with caplog.at_level(logging.WARNING, logger="krx_quant.model_shelf"):
        assert Shelf(state).prune("newton") == Pruned(skipped=("newton",))

    assert versions(state, "newton") == {m.version for m in newton}
    assert "the running model is not the one its history installed last" in caplog.text


class Refusing(LocalObjectStore):
    """A store whose user may not delete (the IAM policy before #361)."""

    def delete(self, key: str) -> None:
        raise PermissionError("access denied")


def test_a_store_that_refuses_to_delete_is_warned_of_and_left(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state, root = tmp_path / "state", tmp_path / "store"
    shelf = Shelf(state)
    newton = piled_up(state, root, "newton", 4)
    night(root, DAYS[0], promoted={})
    shelf.mark_applied(DAYS[0])

    with caplog.at_level(logging.WARNING, logger="krx_quant.swap"):
        pruned = prune_store(Refusing(root), shelf, DAYS[1])

    assert pruned.files == 0
    assert in_store(root, "newton") == {m.version for m in newton}
    assert "the store refused to delete" in caplog.text and "(PermissionError)" in caplog.text


def test_without_a_night_s_list_the_store_is_left_as_it_is(tmp_path: Path) -> None:
    state, root = tmp_path / "state", tmp_path / "store"
    newton = piled_up(state, root, "newton", 4)

    assert prune_store(LocalObjectStore(root), Shelf(state), DAYS[1]) == Pruned()
    assert in_store(root, "newton") == {m.version for m in newton}


@pytest.fixture
def s3() -> Iterator[S3ObjectStore]:
    with mock_aws():
        client = boto3.client("s3", region_name="ap-southeast-2")
        client.create_bucket(
            Bucket="krx-quant-test", CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"}
        )
        yield S3ObjectStore(client, "krx-quant-test", "engine")


def test_on_s3_the_challengers_and_older_models_go(tmp_path: Path, s3: S3ObjectStore) -> None:
    """S3 dates its objects by the real clock: the night's list here is final a minute after
    they were stored."""
    shelf = Shelf(tmp_path / "state")
    older, before, now_running = (model("newton", d) for d in DAYS[:3])
    for day, m in zip(DAYS, (older, before, now_running), strict=False):
        shelf.install("newton", m, f"promotion {day}", at(day, "08:00"))
    lost = model("turing", DAYS[2])
    for name, m in (("newton", older), ("newton", before), ("newton", now_running), ("turing", lost)):
        put_json(s3, f"models/{name}/{m.version}.json", m.as_dict(blob=True))
    final = datetime.now(UTC) + timedelta(minutes=1)
    day = kst_day(final)
    put_json(
        s3,
        f"promotions/{day}.json",
        {"day": str(day), "finalized_at": final.isoformat(), "promoted": {}, "reports": []},
    )
    shelf.mark_applied(day)

    pruned = prune_store(s3, shelf, day)

    assert s3.keys("models/") == [
        f"models/newton/{before.version}.json",
        f"models/newton/{now_running.version}.json",
    ]
    assert pruned.files == 2


def test_the_kept_model_of_one_installed_before_the_history_is_the_running_one(tmp_path: Path) -> None:
    state = tmp_path / "state"
    earlier = replace(model("newton", DAYS[0]), trained="fit by hand")
    (state / "models").mkdir(parents=True)
    (state / "models" / "newton.json").write_text(earlier.to_json())

    assert Shelf(state).kept("newton") == Kept(frozenset({earlier.version}))
    assert Shelf(state).kept("gauss") == Kept(frozenset()), "the defaults: nothing kept"
