"""The models installed on the server, and their history.

The engine runs ``state/models/<algorithm>.json`` (the defaults without one). The Shelf
installs models there, keeps each one installed as ``state/models/versions/<algorithm>/
<version>.json``, and lists in ``state/models/history.json`` what was installed when and
why: a fit, a promotion, or a rollback. A rollback takes the algorithm back one model (to
the defaults past the first), and it stays back: a promotion list is applied once.
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
        save_model(self.state_dir, name, model)
        kept = self._root / "versions" / name / f"{model.version}.json"
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_text(model.to_json())
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
            model = Model.from_json((self._root / "versions" / name / f"{previous}.json").read_text())
            save_model(self.state_dir, name, model)
        self._record(name, previous, f"rollback from {current}", at)
        return model

    def history(self, name: str | None = None) -> list[dict[str, Any]]:
        path = self._root / "history.json"
        entries: list[dict[str, Any]] = json.loads(path.read_text()) if path.exists() else []
        return [e for e in entries if name is None or e["algorithm"] == name]

    def applied(self, night: date) -> bool:
        """Whether a promotion list of this night or a later one was applied."""
        why = [e["why"] for e in self.history() if e["why"].startswith("promotion ")]
        return any(date.fromisoformat(w.removeprefix("promotion ")) >= night for w in why)

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

    def _record(self, name: str, version: str | None, why: str, at: datetime) -> None:
        entries = self.history()
        entries.append({"at": at.isoformat(), "algorithm": name, "version": version, "why": why})
        path = self._root / "history.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entries, indent=1))
        tmp.replace(path)
