"""The models installed on the server, and their history.

The engine runs ``state/models/<algorithm>.json`` (the defaults without one). The Shelf
installs models there, keeps each one installed as ``state/models/versions/<algorithm>/
<version>.json``, and lists in ``state/models/history.json`` what was installed when and
why: a fit, a promotion, or a rollback. A rollback takes the algorithm back one model (to
the defaults past the first), and it stays back: a promotion list is applied once
(``state/models/applied.json`` names the last night applied).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from krx_quant.algorithms import Model, model_path, save_model


class Shelf:
    """The models installed on the server, and their history."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self._root = state_dir / "models"

    def install(self, name: str, model: Model, why: str, at: datetime) -> None:
        """Run ``model`` from now on; the one it replaces stays one rollback away, even if
        it was installed before there was a history.
        """
        current = model_path(self.state_dir, name)
        if current.exists() and not self.history(name):
            earlier = Model.from_json(current.read_text())
            self._keep(name, earlier)
            self._record(name, earlier.version, "installed before", at)
        save_model(self.state_dir, name, model)
        self._keep(name, model)
        self._record(name, model.version, why, at)

    def rollback(self, name: str, at: datetime) -> Model | None:
        """Go back to the model installed before the current one; None: the defaults."""
        chain = self._chain(name)
        if not chain:
            raise ValueError(f"{name} runs its defaults: there is nothing to roll back")
        current = chain.pop()
        previous = chain[-1] if chain else None
        if previous is None:
            model_path(self.state_dir, name).unlink(missing_ok=True)
            model = None
        else:
            model = Model.from_json(self._version(name, previous).read_text())
            save_model(self.state_dir, name, model)
        self._record(name, previous, f"rollback from {current}", at)
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

    def _chain(self, name: str) -> list[str]:
        """The versions installed and not rolled back, oldest first."""
        chain: list[str] = []
        for entry in self.history(name):
            if entry["why"].startswith("rollback"):
                if chain:
                    chain.pop()
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


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)
