"""The trader's store as its modules read it (#509): the times it keeps, and a read-only open of it.

- ``iso``: a time as the store keeps it (``store.py``), an ISO 8601 string in KST, which sorts in
  time order.
- ``reading``: the trader's store (``state/trader.sqlite3``) opened read-only (``mode=ro``: a write
  through it fails), its rows ``sqlite3.Row``, closed once the read is done; None if there is no
  store. The trader keeps it in write-ahead-log mode, so a reader sees each commit as it is made.
  The Engine API's reads open it so (``store.read_trader``, ``read_trades``, ``read_days``,
  ``marks.read_marks``, ``policy.read_policies``, ``decisions.read_decisions``), and so does the mock
  check (``mock_check.py``).

The other read-only opens are not this one and keep their own: the engine's database
(``cells.open_engine_db``, whose caller closes the connection; ``predictions.read_only`` and
``EngineReader``, plain rows, ``immutable`` while the engine is not running), the night report's
(``report.py``, an error without the store, which its caller checks first), the fill notices'
records (``fill_notices.py``, plain rows) and the notice layout's ledger (``notice_layout.py``,
opened by the URI its caller makes). Both helpers live here, below ``store.py``, because
``policy.py`` and ``cohort.py``, which ``store.py`` imports, use them too.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from krx_quant.feeds.base import KST


def iso(at: datetime) -> str:
    return at.astimezone(KST).isoformat()


@contextmanager
def reading(path: Path) -> Iterator[sqlite3.Connection | None]:
    """The trader's store, read-only; None if there is none."""
    if not path.exists():
        yield None
        return
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()
