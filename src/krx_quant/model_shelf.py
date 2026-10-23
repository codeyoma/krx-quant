"""The models installed on the server, and their history.

The engine runs ``state/models/<algorithm>.json`` (the defaults without one). The Shelf
installs models there, keeps each one installed as ``state/models/versions/<algorithm>/
<version>.json``, and lists in ``state/models/history.json`` what was installed when and
why: a fit, a promotion, or a rollback. A rollback takes the algorithm back one step (to
the defaults past the first), and it stays back: a promotion list is applied once
(``state/models/applied.json`` names the last night applied).

Only two models of an algorithm are kept in ``versions/`` (#361, ``kept``): the one installed
now and the one a rollback goes to, the latest other one installed before it. Every install (a
promotion, a fit, a rollback) deletes the algorithm's other files there (``prune``), and the
collector does so for every algorithm when it starts (``prune_all``: what had piled up before).
A rollback therefore goes back one step only: rolled back once, the model before that one is
deleted already as a rule, and a second rollback goes to the defaults, or is refused where the
defaults learned another target. ``history.json`` keeps every entry, deleted models' too.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from krx_quant.algorithms import Model, model_path, save_model

log = logging.getLogger(__name__)

_MB = 1024 * 1024


@dataclass(frozen=True)
class Kept:
    """The versions of an algorithm a prune keeps (#361)."""

    required: frozenset[str]  # must be there, else nothing of the algorithm is deleted
    optional: frozenset[str] = frozenset()  # kept where they are

    @property
    def versions(self) -> frozenset[str]:
        return self.required | self.optional


@dataclass(frozen=True)
class Pruned:
    """What a prune deleted (#361): files and their bytes, and the algorithms it left whole
    (a file to keep missing, or a history the running file disagrees with)."""

    files: int = 0
    size: int = 0
    skipped: tuple[str, ...] = ()

    def __add__(self, other: Pruned) -> Pruned:
        return Pruned(self.files + other.files, self.size + other.size, self.skipped + other.skipped)


def size_text(size: int) -> str:
    """Bytes as the logs and ``doctor`` show them."""
    return f"{size / _MB:,.1f} MB" if size >= _MB else f"{size / 1024:,.0f} KB"


class Shelf:
    """The models installed on the server, and their history."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self._root = state_dir / "models"

    def install(self, name: str, model: Model, why: str, at: datetime) -> None:
        """Run ``model`` from now on; the one it replaces stays one rollback away, even if
        it was installed before there was a history. The others are deleted (``prune``).
        """
        current = model_path(self.state_dir, name)
        if current.exists() and not self.history(name):
            earlier = Model.from_json(current.read_text())
            self._keep(name, earlier)
            self._record(name, earlier.version, "installed before", at)
        save_model(self.state_dir, name, model)
        self._keep(name, model)
        self._record(name, model.version, why, at)
        self._prune_after(name)

    def rollback(self, name: str, at: datetime, target_version: int = 1) -> Model | None:
        """Go back one step: to the model installed before the current one, while it is kept
        (``kept``: only that one is), else to the defaults (None). On the touch target
        (``target_version`` 3, #170, #187) only to a model trained on it."""
        chain = self._chain(name)
        if not chain:
            raise ValueError(f"{name} runs its defaults: there is nothing to roll back")
        current = chain.pop()
        previous = chain[-1] if chain else None
        # Deleted when a later one was installed (#361): only one step back is kept.
        gone = previous is not None and not self._version(name, previous).exists()
        before = None if gone else previous
        if target_version >= 2:
            earlier = Model.from_json(self._version(name, before).read_text()) if before else None
            if earlier is None or (earlier.target_version or 1) != target_version:
                what = (
                    f"{previous} is no longer kept (a rollback goes back one step only), and its"
                    " defaults were"
                    if gone
                    else f"{previous or 'its defaults'} was"
                )
                raise ValueError(
                    f"{name}: {what} not trained on target {target_version}; "
                    f"rolling back would run a model of another target. Fit one instead "
                    f"(krx-quant fit {name}) or wait for the night's training"
                )
        if gone:
            log.warning(
                "%s: %s is no longer kept; rolled back from %s to the defaults", name, previous, current
            )
        if before is None:
            model_path(self.state_dir, name).unlink(missing_ok=True)
            model = None
        else:
            model = Model.from_json(self._version(name, before).read_text())
            save_model(self.state_dir, name, model)
        self._record(name, before, f"rollback from {current}", at)
        self._prune_after(name)
        return model

    def history(self, name: str | None = None) -> list[dict[str, Any]]:
        path = self._root / _HISTORY
        entries: list[dict[str, Any]] = json.loads(path.read_text()) if path.exists() else []
        return [e for e in entries if name is None or e["algorithm"] == name]

    def current_version(self, name: str) -> str | None:
        """The version the engine runs (or will at once); None for the defaults."""
        path = model_path(self.state_dir, name)
        return Model.from_json(path.read_text()).version if path.exists() else None

    def mark_applied(self, night: date) -> None:
        """A night's promotion list is applied (whatever it promoted): never again."""
        _write(self._root / _APPLIED, {"night": night.isoformat()})

    def applied(self, night: date) -> bool:
        """Whether the promotion list of this night, or of a later one, was applied."""
        path = self._root / _APPLIED
        return path.exists() and date.fromisoformat(json.loads(path.read_text())["night"]) >= night

    def store_refused(self) -> date | None:
        """The latest night the store refused to delete its models (#361), warned of then."""
        try:
            return date.fromisoformat(json.loads((self._root / _REFUSED).read_text())["night"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def mark_store_refused(self, night: date | None) -> None:
        """Keep the night the store refused (None: it deletes again, forgotten)."""
        if night is None:
            (self._root / _REFUSED).unlink(missing_ok=True)
        else:
            _write(self._root / _REFUSED, {"night": night.isoformat()})

    def kept(self, name: str) -> Kept | None:
        """The versions of ``name`` kept (#361): the one installed now and the one a rollback
        goes to, the latest other one installed before it. Both must be there, but for the one
        before after a rollback: that one may have been deleted already. A model installed
        before there was a history is the one installed now. None when the running file is not
        the model the history installed last: nothing of it is deleted then."""
        chain = self._chain(name)
        try:
            running = self.current_version(name)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not chain:  # the defaults, or a model installed before the history
            return Kept(frozenset() if running is None else frozenset({running}))
        current = chain[-1]
        if running != current:
            return None
        before = next((v for v in reversed(chain[:-1]) if v != current), None)
        if before is None:
            return Kept(frozenset({current}))
        if self.history(name)[-1]["why"].startswith("rollback"):
            return Kept(frozenset({current}), frozenset({before}))
        return Kept(frozenset({current, before}))

    def prune(self, name: str) -> Pruned:
        """Delete ``name``'s files in ``versions/`` but the ones kept (``kept``); none when one
        to keep is missing or the history disagrees with the running file (warned)."""
        files = self._files(name)
        if not files:
            return Pruned()
        kept = self.kept(name)
        if kept is None:
            log.warning(
                "%s: the running model is not the one its history installed last; no model file deleted", name
            )
            return Pruned(skipped=(name,))
        missing = sorted(kept.required - files.keys())
        if missing:
            log.warning(
                "%s: %s to keep is not in state/models/versions; no model file deleted",
                name,
                ", ".join(missing),
            )
            return Pruned(skipped=(name,))
        gone = [path for version, path in sorted(files.items()) if version not in kept.versions]
        size = 0
        for path in gone:
            size += path.stat().st_size
            path.unlink()
        if gone:
            log.info(
                "%s: deleted %d model files (%s) from state/models/versions, kept %s",
                name,
                len(gone),
                size_text(size),
                " and ".join(sorted(kept.versions & files.keys())) or "none (the defaults run)",
            )
        return Pruned(len(gone), size)

    def prune_all(self) -> Pruned:
        """``prune`` every algorithm with files in ``versions/``: what piled up before #361."""
        folder = self._root / "versions"
        names = sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []
        pruned = Pruned()
        for name in names:
            pruned += self.prune(name)
        log.info(
            "state/models/versions: deleted %d model files (%s) of %d algorithms; %d left whole",
            pruned.files,
            size_text(pruned.size),
            len(names),
            len(pruned.skipped),
        )
        return pruned

    def _prune_after(self, name: str) -> None:
        """An install's prune: a file it cannot delete leaves the install as it is."""
        try:
            self.prune(name)
        except OSError:
            log.exception("%s: could not delete the models no longer kept", name)

    def _files(self, name: str) -> dict[str, Path]:
        """``name``'s model files in ``versions/``, by version: its ``.json`` files only."""
        folder = self._root / "versions" / name
        if not folder.is_dir():
            return {}
        return {p.name.removesuffix(".json"): p for p in folder.glob("*.json") if p.is_file()}

    def _chain(self, name: str) -> list[str]:
        """The versions installed and not rolled back, oldest first. A rollback to the defaults
        empties it: one with a model before (deleted, #361) leaves nothing to go back to."""
        chain: list[str] = []
        for entry in self.history(name):
            if entry["why"].startswith("rollback"):
                if chain:
                    chain.pop()
                if entry["version"] is None:
                    chain.clear()
            else:
                chain.append(entry["version"])
        return chain

    def _version(self, name: str, version: str) -> Path:
        return self._root / "versions" / name / f"{version}.json"

    def _keep(self, name: str, model: Model) -> None:
        path = self._version(name, model.version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(model.to_json())

    def _record(self, name: str, version: str | None, why: str, at: datetime) -> None:
        entries = self.history()
        entries.append({"at": at.isoformat(), "algorithm": name, "version": version, "why": why})
        _write(self._root / _HISTORY, entries)


_HISTORY = "history.json"
_APPLIED = "applied.json"
_REFUSED = "store_refused.json"  # the night the store last refused a model's delete (#361)


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)
