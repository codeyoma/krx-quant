"""``krx-quant probe-sample-keys``: what changes the key of a day a night stored (#565).

A normal night says "23 days read (2 made)": besides its new day it makes the trading day before
again, in 532–1,132 s, about 40% of the night (Loki's ``read <day>: … (made, stored)`` lines, every
night from 10-27 to 11-04). A day is read back from the sample store only under the key it was stored
with (``sample_store.day_key``): the settings, the input files by SHA-256 (the Object Store's, of
which the night's cache is a copy), what the samples ask of the calendar, and the code. So between
the night that stored that day and the next night one of those changed, and the code alone does not
say which. The issue's hypothesis was the archive's run at the 06:00 start, merging rows that came
after 21:00 into the day's files (it uploaded 13 files on 2026-11-05); but the server snapshot of
2026-11-06 00:27 on #565 shows no feed the samples read received after 20:05 of 11-05 (the night
futures were, which they do not read). This probe, run after a night's training, prints:

1. the sample store (``state/training-samples``): its days and files, and the days the night made
   (their file written after that night's ``KRX_TRAIN_AT``);
2. for the latest stored day and the stored day before it, each input of its key that the Object
   Store stored after the night of that day began, or whose SHA-256 there is no longer the key's: its
   copies' SHA-256, sizes and times, the Object Store's, the night's cache's (when the night fetched
   it) and the data directory's. The night made the day before again tonight with what is there now,
   so an input stored after that day's own night is one its old key did not hold;
3. the latest day's late rows: the partitions of the data directory with part files the archive has
   not merged yet (written after its last run), those the samples read apart from the rest;
4. the keys that changed since the probe's last run (``STATE``, which every run writes): each day's
   key parts that differ, its inputs changed, added and removed, the calendar's entries that differ.
   Run on two trading nights in a row, this names whatever changed the key between them, a file or
   not.

It prints paths, feed and symbol names, sizes, times, counts and SHA-256 prefixes: no market value.
It reads the sample store, the training cache, the data directory and the Object Store (listings and
digests), and writes only its own state file (``state/probe-sample-keys.json``).
"""

from __future__ import annotations

import hashlib
import json
import os
import zlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from krx_quant.archive import MERGED, RawMissing, guarded_files
from krx_quant.config import Settings
from krx_quant.features import Leaders
from krx_quant.feeds.base import KST
from krx_quant.macro import regime_prefix
from krx_quant.object_store import ObjectStore, Stored, open_store
from krx_quant.replay import recorded_partitions
from krx_quant.sample_store import CALENDAR_REACH, FORMAT, STORE_DIR

TITLE = "표본 키 프로브 (#565)"
STATE = "probe-sample-keys.json"  # under the state directory: the keys the last run saw
CACHE_DIR = "training-cache"  # under the state directory: the night's copies (``nightly.run_training``)
TRAIN_AT = time(21, 30)  # the night's start when KRX_TRAIN_AT is off (its default)
SHA = 12  # the SHA-256 prefix shown
SHOWN = 40  # lines of one list shown, the first; the rest are counted
_HEAD = 1 << 22  # bytes of a stored day's text read for its key at most (it comes second, after the format)
_ARROW_ERRORS = (OSError, ValueError)  # pyarrow's ArrowInvalid is a ValueError


@dataclass(frozen=True)
class KeyFile:
    """A file of the sample store: its day, where, how big, when written and the key it holds
    (None when it cannot be read: ``error`` says why)."""

    day: date
    path: Path
    size: int
    written: datetime
    parts: Mapping[str, Any] | None
    error: str | None = None


def read_key(path: Path) -> dict[str, Any]:
    """The key a stored day was made under (``sample_store.encode``'s ``key``), from the head of its
    file: the samples after it are not decompressed. ``ValueError`` for a file of another layout."""
    inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)  # gzip
    decoder = json.JSONDecoder()
    head = b""
    with path.open("rb") as f:
        while True:
            chunk = f.read(1 << 16)
            head += inflate.decompress(chunk) if chunk else inflate.flush()
            text = head.decode("ascii")  # json.dumps escapes every other character
            if not text.startswith('{"format":'):
                if len(text) >= len('{"format":') or not chunk:
                    raise ValueError("not a stored day")
                continue
            try:
                layout, end = decoder.raw_decode(text, len('{"format":'))
                if layout != FORMAT:
                    raise ValueError(f"format {layout}, not {FORMAT}")
                if not text.startswith(',"key":', end):
                    raise ValueError("no key after the format")
                parts, _ = decoder.raw_decode(text, end + len(',"key":'))
            except json.JSONDecodeError:
                if not chunk or len(head) > _HEAD:
                    raise ValueError("the key is cut short") from None
                continue  # the head so far ends inside the key
            if not isinstance(parts, dict):
                raise ValueError("the key is not an object")
            return parts


def key_files(root: Path) -> dict[date, list[KeyFile]]:
    """The sample store's files by day, oldest day first, each day's oldest file first."""
    found: dict[date, list[KeyFile]] = {}
    for folder in sorted(root.glob("date=*")):
        try:
            day = date.fromisoformat(folder.name.removeprefix("date="))
        except ValueError:
            continue
        files = []
        for path in folder.glob("*.json.gz"):  # one left half-written ends in .tmp
            stat = path.stat()
            parts: dict[str, Any] | None
            try:
                parts, error = read_key(path), None
            except (OSError, ValueError, zlib.error, UnicodeDecodeError) as e:
                parts, error = None, f"{type(e).__name__}: {e}"
            files.append(KeyFile(day, path, stat.st_size, _at(stat.st_mtime), parts, error))
        if files:
            found[day] = sorted(files, key=lambda f: (f.written, f.path.name))
    return found


def probe_sample_keys(settings: Settings, now: datetime, *, day: date | None = None) -> tuple[str, int]:
    """``krx-quant probe-sample-keys``: the report (the module doc) and the exit code, 0 when it ran,
    1 when the sample store holds no day (or not ``day``) or the Object Store is not set."""
    train_at = settings.train_at or TRAIN_AT
    night = (now.astimezone(KST) - timedelta(hours=12)).date()  # 22:30–05:30: the evening's day
    head = [f"{TITLE}: {now.astimezone(KST):%Y-%m-%d %H:%M} KST, {night}의 밤"]
    keys = key_files(settings.state_dir / STORE_DIR)
    store = open_store(settings)
    days = [d for d in keys if day is None or d <= day]
    if not days or (day is not None and day not in keys) or store is None:
        why = "KRX_OBJECT_STORE가 없다" if store is None else f"표본 저장소에 {day or '날'}이 없다"
        return "\n".join([*head, f"  {why}: 볼 것이 없다"]), 1
    latest = days[-1]
    before = days[-2] if len(days) > 1 else None
    head.append(
        f"  마지막 날 {latest}, 그 앞의 날 {before or '없음'}. 밤 학습은 그날 {train_at:%H:%M}에 시작한다"
        f"({'KRX_TRAIN_AT' if settings.train_at else 'KRX_TRAIN_AT 꺼짐: 기본값으로 본다'})."
        " 시각은 모두 KST다. 시세 값은 없다(경로, 크기, 시각, 개수, SHA-256 앞 12자만)."
    )
    sections = [
        "\n".join(head),
        _store_section(keys, _start(night, train_at)),
        *(
            _inputs_section(settings, store, _chosen(keys[d]), _start(d, train_at))
            for d in (before, latest)
            if d
        ),
        _late_section(settings, latest, _chosen(keys[latest])),
        _changes_section(settings.state_dir / STATE, keys, now),
    ]
    return "\n\n".join(sections), 0


def _chosen(found: Sequence[KeyFile]) -> KeyFile:
    """A day's newest file that holds a key it can read, else its newest (normally its only one)."""
    return next((f for f in reversed(found) if f.parts is not None), found[-1])


def _start(day: date, train_at: time) -> datetime:
    return datetime.combine(day, train_at, KST)


# --- 1. the store ------------------------------------------------------------------------------


def _store_section(keys: Mapping[date, Sequence[KeyFile]], tonight: datetime) -> str:
    files = [f for found in keys.values() for f in found]
    made = [f for f in files if f.written >= tonight]
    lines = [
        f"1. 표본 저장소 (state/{STORE_DIR})",
        f"  날 {len(keys)}개 ({min(keys)}–{max(keys)}) · 파일 {len(files)}개"
        f" · {_size(sum(f.size for f in files))}",
        f"  {tonight:%m-%d %H:%M} 뒤에 쓴 파일(그 밤이 만든 날): "
        + (", ".join(f"{f.day} ({f.written:%H:%M:%S}, {_size(f.size)})" for f in made) or "없다"),
    ]
    several = {d: found for d, found in keys.items() if len(found) > 1}
    lines += [
        f"  파일이 {len(found)}개인 날 {d}: " + ", ".join(f.path.name for f in found)
        for d, found in several.items()
    ]
    lines += [f"  읽지 못한 파일 {f.day}/{f.path.name}: {f.error}" for f in files if f.parts is None]
    return "\n".join(lines)


# --- 2. a day's inputs -------------------------------------------------------------------------


def _inputs_section(settings: Settings, store: ObjectStore, key: KeyFile, night: datetime) -> str:
    title = f"2. {key.day}의 키 입력 (파일 {key.path.name[:SHA]}, {key.written:%m-%d %H:%M:%S}에 씀)"
    if key.parts is None:
        return f"{title}\n  키를 읽지 못했다: {key.error}"
    inputs = [(str(p), str(sha)) for p, sha in key.parts.get("inputs", [])]
    listed: dict[str, Stored] = {}
    for prefix in sorted({"/".join(p.split("/")[:2]) + "/" for p, _ in inputs}):  # layer/date=…/
        listed.update({o.key: o for o in store.objects(prefix)})
    after, differ, flagged = 0, 0, []
    for path, sha in inputs:
        stored = listed.get(path)
        now_sha = store.digest(path) if stored is not None else None
        late = stored is not None and stored.modified > night
        changed = now_sha != sha
        after += late
        differ += changed
        if late or changed:
            flagged.append(_input_line(settings, path, sha, stored, now_sha))
    lines = [
        title,
        f"  입력 {len(inputs)}개: 그 날의 밤({night:%m-%d %H:%M}) 뒤에 Object Store에 올라간 것 {after}개,"
        f" 지금 Object Store의 SHA-256이 키와 다르거나 없는 것 {differ}개",
    ]
    if flagged:
        lines.append(
            "  경로 · 키의 SHA-256 · Object Store(SHA-256, 크기, 올린 시각) · 밤의 캐시(크기, 받은 시각)"
        )
        lines.append("    · 데이터 폴더(크기, 수정 시각, 합치지 않은 part 수)")
        lines += _first(flagged)
    else:
        newest = max((listed[p] for p, _ in inputs if p in listed), key=lambda o: o.modified, default=None)
        lines.append(
            "  그 밤 뒤에 바뀐 입력은 없다"
            + ("" if newest is None else f"(가장 늦게 올라간 입력: {newest.key} {_when(newest.modified)})")
        )
    return "\n".join(lines)


def _input_line(settings: Settings, path: str, sha: str, stored: Stored | None, now_sha: str | None) -> str:
    there = (
        "없음"
        if stored is None
        else f"{(now_sha or '-')[:SHA]} {_size(stored.size)} {_when(stored.modified)}"
    )
    cached = settings.state_dir / CACHE_DIR / path
    local = settings.data_dir / path
    return (
        f"  {path} · {sha[:SHA]} · {there} · {_file(cached)}"
        f" · {_file(local)}{_unmerged(local) if local.name == MERGED else ''}"
    )


def _file(path: Path) -> str:
    if not path.is_file():
        return "없음"
    stat = path.stat()
    return f"{_size(stat.st_size)} {_when(_at(stat.st_mtime))}"


def _unmerged(merged: Path) -> str:
    if not merged.is_file():
        return ""
    try:
        parts = [f for f in guarded_files(merged.parent) if f.name != MERGED]
    except RawMissing:
        return ", 서버에서 지웠다(#607)"
    except _ARROW_ERRORS as e:
        return f", part를 셀 수 없다({type(e).__name__})"
    return f", part {len(parts)}개"


# --- 3. the latest day's late rows -------------------------------------------------------------


def _late_section(settings: Settings, day: date, key: KeyFile) -> str:
    """The partitions of ``day`` in the data directory with part files not merged yet."""
    data = settings.data_dir
    read: set[Path] = {data / regime_prefix(day) / "symbol=market"}
    if key.parts is not None:
        read |= {(data / str(p)).parent for p, _ in key.parts.get("inputs", [])}
    try:
        engine = recorded_partitions(data, day, settings.symbols, Leaders.of(settings))
    except _ARROW_ERRORS:
        engine = []
    read |= {files[0].parent for _, files in engine if files}
    late: list[tuple[Path, list[Path]]] = []
    unreadable = pruned = 0
    for partition in sorted(data.glob(f"*/date={day.isoformat()}/feed=*/symbol=*")):
        if not partition.is_dir():
            continue
        try:
            parts = [f for f in guarded_files(partition) if f.name != MERGED]
        except RawMissing:  # a raw partition the archive pruned (#607): no part is left in it
            pruned += 1
            continue
        except _ARROW_ERRORS:
            unreadable += 1
            continue
        if parts:
            late.append((partition, parts))
    lines = [
        f"3. {day}의 합치지 않은 part 파일 (데이터 폴더: 아카이브의 마지막 실행 뒤에 쓴 행)",
        f"  part가 남은 파티션 {len(late)}개"
        + (f", 읽지 못한 파티션 {unreadable}개" if unreadable else "")
        + (f", 서버에서 지운 raw 파티션 {pruned}개(#607, part 없음)" if pruned else "")
        + ". 이 뒤의 행은 다음 아카이브 실행(06:00 시작, 장 마감 15분 뒤, 21:00)이 합친다",
    ]
    mine = [(p, parts) for p, parts in late if p in read]
    lines.append(f"  표본이 읽는 파티션 {len(mine)}개" + (":" if mine else ""))
    lines += _first(
        f"    {p.relative_to(data).as_posix()} · part {len(parts)}개 {_size(_bytes(parts))} · {_span(parts)}"
        for p, parts in mine
    )
    others: dict[tuple[str, str], list[list[Path]]] = defaultdict(list)
    for p, parts in late:
        if p not in read:
            others[(p.parts[-4], p.parts[-2].removeprefix("feed="))].append(parts)
    lines.append(
        f"  표본이 읽지 않는 파티션 {sum(len(v) for v in others.values())}개 (층, 피드별)"
        + (":" if others else "")
    )
    lines += _first(
        f"    {layer} {feed} · 파티션 {len(groups)}개 · part {sum(len(g) for g in groups)}개"
        f" {_size(sum(_bytes(g) for g in groups))} · {_span([f for g in groups for f in g])}"
        for (layer, feed), groups in sorted(others.items())
    )
    return "\n".join(lines)


def _bytes(files: Iterable[Path]) -> int:
    return sum(f.stat().st_size for f in files)


def _span(files: Sequence[Path]) -> str:
    times = [f.stat().st_mtime for f in files]
    first, last = _when(_at(min(times))), _when(_at(max(times)))
    return first if first == last else f"{first}–{last}"


# --- 4. the keys since the last run ------------------------------------------------------------


def _changes_section(state: Path, keys: Mapping[date, Sequence[KeyFile]], now: datetime) -> str:
    """The keys that changed since the last run's (``state``), which this run's then replace."""
    seen = {d.isoformat(): _seen(_chosen(found)) for d, found in keys.items()}
    title = "4. 지난 실행 뒤 키가 바뀐 날"
    try:
        last = json.loads(state.read_text())
        before: dict[str, Any] = last["days"]
        lines = [f"{title} (지난 실행 {last['ran_at']}, state/{STATE})", *_changes(before, seen)]
    except FileNotFoundError:
        lines = [f"{title}: 지난 실행이 없다. 이번에 본 키를 state/{STATE}에 남겨 다음 실행이 견준다"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        lines = [f"{title}: state/{STATE}를 읽지 못했다({type(e).__name__}). 이번에 본 키로 바꾼다"]
    _write(state, {"ran_at": f"{now.astimezone(KST):%Y-%m-%d %H:%M} KST", "days": seen})
    return "\n".join(lines)


def _seen(key: KeyFile) -> dict[str, Any]:
    return {"file": key.path.name, "written": _when(key.written), "parts": key.parts}


def _changes(before: Mapping[str, Any], now: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    same = 0
    for day in sorted(set(before) & set(now)):
        old, new = before[day], now[day]
        if old["file"] == new["file"]:
            same += 1
            continue
        lines.append(f"  {day}: 파일 {old['file'][:SHA]} → {new['file'][:SHA]} ({new['written']}에 씀)")
        lines += _part_changes(date.fromisoformat(day), old.get("parts") or {}, new.get("parts") or {})
    added = sorted(set(now) - set(before))
    gone = sorted(set(before) - set(now))
    lines.append(f"  키가 그대로인 날 {same}개")
    lines.append(f"  새 날 {len(added)}개" + (f": {', '.join(added)}" if added else ""))
    lines.append(f"  없어진 날 {len(gone)}개" + (f": {', '.join(gone)}" if gone else ""))
    return lines


def _part_changes(day: date, old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    lines = []
    kept = []
    for name in sorted(set(old) | set(new)):
        a, b = old.get(name), new.get(name)
        if _canonical(a) == _canonical(b):
            kept.append(name)
        elif name == "inputs":
            lines += _input_changes(a or [], b or [])
        elif name == "calendar":
            lines += _calendar_changes(day, a or {}, b or {})
        else:
            lines.append(f"    {name}: {_digest(a)} → {_digest(b)}")
    return [*lines, f"    그대로인 부분: {', '.join(kept) or '없음'}"]


def _input_changes(old: Sequence[Sequence[str]], new: Sequence[Sequence[str]]) -> list[str]:
    a = {str(p): str(sha) for p, sha in old}
    b = {str(p): str(sha) for p, sha in new}
    changed = [f"      {p} · {a[p][:SHA]} → {b[p][:SHA]}" for p in a if p in b and a[p] != b[p]]
    added = [f"      {p} · {b[p][:SHA]}" for p in b if p not in a]
    gone = [f"      {p} · {a[p][:SHA]}" for p in a if p not in b]
    lines = [
        f"    inputs: {len(a)}개 → {len(b)}개"
        f" (바뀐 것 {len(changed)}, 더한 것 {len(added)}, 뺀 것 {len(gone)})"
    ]
    for label, found in (("바뀐 입력", changed), ("더한 입력", added), ("뺀 입력", gone)):
        if found:
            lines += [f"     {label}:", *_first(found)]
    if not (changed or added or gone):
        lines.append("     같은 파일, 같은 SHA-256이고 순서만 다르다")
    return lines


def _calendar_changes(day: date, old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    lines = []
    sessions = zip(old.get("sessions") or [], new.get("sessions") or [], strict=False)
    reach = [day - CALENDAR_REACH + timedelta(days=i) for i in range(len(old.get("sessions") or []))]
    lines += [
        f"      {d}: 장 {_session(a)} → {_session(b)}"
        for d, (a, b) in zip(reach, sessions, strict=False)
        if a != b
    ]
    if len(old.get("sessions") or []) != len(new.get("sessions") or []):
        lines.append(f"      장의 날 수 {len(old.get('sessions') or [])} → {len(new.get('sessions') or [])}")
    for name in ("expiry", "first_known"):
        if old.get(name) != new.get(name):
            lines.append(f"      {name}: {old.get(name)} → {new.get(name)}")
    return ["    calendar:", *lines]


def _session(value: Any) -> str:
    """A day's session as the key holds it (``[open, close]`` in ISO, or None): its hours."""
    if not value:
        return "쉬는 날"
    return "–".join(datetime.fromisoformat(str(t)).astimezone(KST).strftime("%H:%M") for t in value)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()[:SHA]


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(path)


# --- the output's pieces -----------------------------------------------------------------------


def _first(lines: Iterable[str]) -> list[str]:
    found = list(lines)
    return found[:SHOWN] + ([f"    … 그 밖에 {len(found) - SHOWN}개"] if len(found) > SHOWN else [])


def _at(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, KST)


def _when(at: datetime) -> str:
    return f"{at.astimezone(KST):%m-%d %H:%M:%S}"


def _size(size: int) -> str:
    return f"{size / 1e6:,.1f} MB" if size >= 1e5 else f"{size / 1e3:,.1f} KB"
