"""A ranking stored before an engine starts (#697): the verdict (version 2) pools only its members."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import date, datetime
from pathlib import Path

from krx_quant.feeds.base import KST
from krx_quant.predictions import PredictionStore
from krx_quant.ranking import LIVE, Placed, Snapshot

NIGHT = date(2026, 10, 9)  # the trading day before the replayed mornings (2026-10-12)


def member(name: str, rank: int) -> Placed:
    return Placed(
        name,
        "probation",
        LIVE,
        12,
        1200,
        0.02,
        0.01,
        0.03,
        120,
        0.6,
        0.1,
        0,
        None,
        None,
        True,
        True,
        3,
        0,
        rank,
    )


def seed_ranking(db: Path, members: Mapping[str, Collection[str]], night: date = NIGHT) -> None:
    """Keep in the engine database at ``db`` a ranking of ``night`` making ``members`` (by kind) the
    members."""
    store = PredictionStore(db)
    made = datetime.combine(night, datetime.min.time(), KST)
    snapshots = [
        Snapshot(kind, night, made, night, tuple(member(n, i + 1) for i, n in enumerate(sorted(names))))
        for kind, names in members.items()
    ]
    store.add_rankings([(s.night, s.kind, s.version, s.made_at, s.through, s.as_json()) for s in snapshots])
    store.close()
