"""What ``krx-quant probe-supply`` says about an answer's fields without showing a value (#226).

The probe's report goes on a public issue, so a field is described by its look only: empty,
zero, whole numbers (counts, amounts), decimals (ratios), a date or text, and whether some
values are negative. Two sources are compared by how many days have equal values.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

EMPTY, ZERO, MISSING = "empty", "zero", "missing"
FLAG_ORDER = ("int", "int±", "dec", "dec±", "date", "text", ZERO, EMPTY)
_NUMBER = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)")


def present(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def flag(name: str, values: Sequence[Any]) -> str:
    """What a field's values look like, without them: ``empty``, ``zero``, ``date``, ``int``
    (whole numbers: counts, amounts), ``dec`` (decimals: ratios) or ``text``; a number field
    with a negative value gets ``±``."""
    shown = [str(v).strip() for v in values if present(v)]
    if not shown:
        return EMPTY
    if "date" in name:
        return "date"
    if not all(_NUMBER.fullmatch(v) for v in shown):
        return "text"
    if all(float(v) == 0 for v in shown):
        return ZERO
    kind = "dec" if any("." in v for v in shown) else "int"
    return kind + ("±" if any(v.startswith("-") for v in shown) else "")


def flags(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Each field of the rows (any row having it), flagged over all of them."""
    names = dict.fromkeys(name for row in rows for name in row)
    return {name: flag(name, [row.get(name) for row in rows]) for name in names}


def describe(fields: Mapping[str, str]) -> str:
    """The fields grouped by flag: ``int: a, b · dec: c · empty: d``."""
    groups = {f: sorted(n for n, g in fields.items() if g == f) for f in FLAG_ORDER}
    return " · ".join(f"{f}: {', '.join(names)}" for f, names in groups.items() if names) or "none"


def day_of(row: Mapping[str, Any], name: str, form: str = "%Y%m%d") -> date | None:
    try:
        return datetime.strptime(str(row.get(name) or ""), form).date()
    except ValueError:
        return None


def order_of(days: Sequence[date]) -> str:
    if len(set(days)) < 2:
        return "one day" if days else "no day"
    if list(days) == sorted(days, reverse=True):
        return "newest first"
    if list(days) == sorted(days):
        return "oldest first"
    return "unordered"


def same(a: Any, b: Any, tolerance: float = 0.0) -> bool:
    """Equal numbers (within ``tolerance``), or equal text; never when one is missing."""
    if not (present(a) and present(b)):
        return False
    try:
        return abs(float(a) - float(b)) <= tolerance
    except ValueError:
        return str(a).strip() == str(b).strip()
