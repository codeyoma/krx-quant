"""``krx-quant probe-member-frames`` (#381): the member frames whose field count is not 78, read
from raw rows the collector's own writer made, without printing a field's value."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant.cli import main
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds import MEMBER, RAW_SCHEMA, TRADE
from krx_quant.feeds.base import KST
from krx_quant.member_frames_probe import GROUPS, WIDTH, block_starts, grouped, report, shape
from krx_quant.recorder import ParquetRecorder
from tests.kis_frames import MEMBER_COLUMNS, book_frame, member_frame, trade_frame
from tests.test_collector import FIXED_NOW, collect, read, settings_for

QUIET, SHORT, NO_NEXT = date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26)
NIGHT = datetime.combine(NO_NEXT, time(22, 30), KST)
STOCKS = ("005930", "000660")
STRANGER = "77777777"  # a first field that is not a configured symbol

# Made-up values, each of a shape: none of them may appear in the output.
NAMES = ("가상증권", "모의증권", "예시증권", "시험증권", "보기증권")
ENGLISH = ("MADEUP SEC", "MOCK SEC", "SAMPLE SEC", "TRIAL SEC", "")  # the last member's empty
VALUES = {
    "MKSC_SHRN_ISCD": "005930",
    **{f"SELN2_MBCR_NAME{i}": n for i, n in enumerate(NAMES, 1)},
    **{f"BYOV_MBCR_NAME{i}": n for i, n in enumerate(NAMES, 1)},
    **{f"TOTAL_SELN_QTY{i}": f"98765432{i}" for i in range(1, 6)},
    **{f"TOTAL_SHNU_QTY{i}": f"87654321{i}" for i in range(1, 6)},
    **{f"SELN_MBCR_GLOB_YN_{i}": "Y" for i in range(1, 6)},
    **{f"SHNU_MBCR_GLOB_YN_{i}": "N" for i in range(1, 6)},
    **{f"SELN_MBCR_NO{i}": f"0077{i}" for i in range(1, 6)},
    **{f"SHNU_MBCR_NO{i}": f"0066{i}" for i in range(1, 6)},
    **{f"SELN_MBCR_RLIM{i}": f"12.3{i}" for i in range(1, 6)},
    **{f"SHNU_MBCR_RLIM{i}": f"23.4{i}" for i in range(1, 6)},
    **{f"SELN_QTY_ICDC{i}": f"-432{i}" for i in range(1, 6)},
    **{f"SHNU_QTY_ICDC{i}": f"+543{i}" for i in range(1, 6)},
    "GLOB_TOTAL_SELN_QTY": "765432109",
    "GLOB_TOTAL_SHNU_QTY": "654321098",
    "GLOB_TOTAL_SELN_QTY_ICDC": "-111222",
    "GLOB_TOTAL_SHNU_QTY_ICDC": "+222333",
    "GLOB_NTBY_QTY": "-333444",
    "GLOB_SELN_RLIM": "34.56",
    "GLOB_SHNU_RLIM": "45.67",
    **{f"SELN2_MBCR_ENG_NAME{i}": n for i, n in enumerate(ENGLISH, 1)},
    **{f"BYOV_MBCR_ENG_NAME{i}": n for i, n in enumerate(ENGLISH, 1)},
}
FULL_SHAPES = "9 AAAAA AAAAA 99999 99999 YYYYY YYYYY 99999 99999 +++++ +++++ +++++ +++++ 99+++++ AAAA_ AAAA_"
PRICE = "76543"


def full(symbol: str = "005930") -> str:
    return member_frame(VALUES | {"MKSC_SHRN_ISCD": symbol})


def dropping(frame: str, start: int, count: int) -> tuple[str, str]:
    """``frame`` with ``count`` of its fields from ``start`` (0-based) left out, and those fields."""
    head, payload = frame.rsplit("|", 1)
    fields = payload.split("^")
    kept = fields[:start] + fields[start + count :]
    return f"{head}|{'^'.join(kept)}", "^".join(fields[start : start + count])


SHORT_END, TAIL = dropping(full(), WIDTH - 6, 6)  # the last six fields: 72
SHORT_MIDDLE, _ = dropping(full("000660"), 41, 5)  # SELN_MBCR_RLIM1–5: 73


def at(day: date, hhmmss: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmmss), KST)


def record_raw(data: Path, rows: list[tuple[date, str, str, str, str]]) -> None:
    """Raw rows as the collector writes them: (day, feed, key, receipt time, message)."""
    recorder = ParquetRecorder(data, flush_rows=1_000, flush_seconds=3600)
    for day, feed, key, hhmmss, message in rows:
        row = {"received_at": at(day, hhmmss).astimezone(UTC), "message": message}
        recorder.record("raw", day, feed, key, row, RAW_SCHEMA)
    recorder.close()


def trade(price: str = PRICE) -> str:
    return trade_frame({"MKSC_SHRN_ISCD": "005930", "STCK_PRPR": price})


@pytest.fixture
def recorded(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    m = MEMBER.tr_id
    short_trade, _ = dropping(trade(), 40, 3)
    record_raw(
        data,
        [
            (QUIET, m, "005930", "09:00:00", full()),
            (QUIET, m, "005930", "09:00:01", member_frame(VALUES, VALUES)),  # two records: 156 fields
            (SHORT, m, "005930", "07:59:59.500", full()),
            (SHORT, m, "005930", "08:00:01.100", SHORT_END),
            (SHORT, "_unparsed", "_", "08:00:01.101", TAIL),  # as a split frame's tail would come
            (SHORT, m, "005930", "08:00:01.400", full()),
            (SHORT, m, "000660", "08:00:02.000", SHORT_MIDDLE),
            (SHORT, m, "000660", "08:00:03.250", full("000660")),
            (SHORT, m, "000660", "08:00:04.000", "1|H0UNMBC0|001|ENCRYPTEDTEXT"),
            (SHORT, m, "005930", "09:00:00", full()),
            (SHORT, TRADE.tr_id, "005930", "08:00:00.500", trade()),
            (SHORT, TRADE.tr_id, "005930", "08:00:01.200", short_trade),
            (SHORT, TRADE.tr_id, "005930", "10:00:00", short_trade),  # outside the window
            (SHORT, "_system", "_", "08:00:00.300", '{"header": {"tr_id": "PINGPONG"}}'),
            (SHORT, "H0UNASP0", "005930", "12:00:00", book_frame({"MKSC_SHRN_ISCD": "005930"})),
            (NO_NEXT, m, "005930", "07:50:00", full()),
            (NO_NEXT, m, STRANGER, "08:00:00", dropping(full(STRANGER), 70, 8)[0]),
        ],
    )
    return data


def test_a_field_s_shape_is_all_that_is_kept_of_it() -> None:
    assert [shape(v) for v in ("", "  ", "Y", "N", "00036", "-250000", "+3", "12.5", ".5")] == [
        "_",
        "_",
        "Y",
        "Y",
        "9",
        "+",
        "+",
        "+",
        "+",
    ]
    assert [shape(v) for v in ("모건스탠리", "MORGAN STANLEY", "1e5", "12-3", "YES")] == ["A"] * 5


def test_the_layout_is_grouped_as_kis_lays_it_out() -> None:
    # 1 symbol + 12 runs of the five members + 7 foreign totals + 2 runs of English names.
    assert [len(g) for g in GROUPS] == [1, *[5] * 12, 7, 5, 5]
    assert sum(len(g) for g in GROUPS) == WIDTH == len(MEMBER_COLUMNS) == 78
    assert GROUPS[13][0] == "GLOB_TOTAL_SELN_QTY" and GROUPS[13][-1] == "GLOB_SHNU_RLIM"
    assert grouped("9" * 80) == f"{grouped('9' * 78)} | 9 9", "a second record after |, grouped too"


def test_a_single_missing_block_fits_where_the_shapes_agree() -> None:
    assert list(block_starts(72, 72, 0)) == [72], "the end"
    assert list(block_starts(73, 56, 32)) == list(range(41, 57))
    assert not block_starts(61, 10, 20), "no single block"


def test_each_short_frame_is_shown_by_its_shapes_beside_the_next_full_one(recorded: Path) -> None:
    output = report(recorded, STOCKS, NIGHT)
    frames = output.split("3. 필드 수가")[1].split("4. 같은 때의")[0]

    assert (
        "  2026-10-23 08:00:01.100 005930 · 레코드 1 · 필드 72(78보다 6 적다) · "
        "그날 이 종목의 2번째 프레임(앞 프레임 07:59:59.500)\n"
        "    이 프레임: 9 AAAAA AAAAA 99999 99999 YYYYY YYYYY 99999 99999 +++++ +++++ +++++ +++++ "
        "99+++++ AAAA\n"
        f"    다음 78칸: {FULL_SHAPES} (08:00:01.400, 0.3초 뒤)\n"
        "    앞에서 같은 모양 72칸, 뒤에서 0칸 · 끝의 6칸이 빠진 모양이다(시작 칸 73(SELN2_MBCR_ENG_NAME5))"
    ) in frames
    assert (
        "  2026-10-23 08:00:02.000 000660 · 레코드 1 · 필드 73(78보다 5 적다) · "
        "그날 이 종목의 1번째 프레임(앞 프레임 없음)"
    ) in frames
    assert (
        "    앞에서 같은 모양 56칸, 뒤에서 32칸 · 빠진 5칸이 한 덩어리라면 시작 칸 후보 "
        "42(SELN_MBCR_RLIM1)–57(SHNU_QTY_ICDC1)"
    ) in frames
    # No next full frame that day; a key that is no configured symbol is shown by its shape.
    assert (
        "  2026-10-26 08:00:00.000 설정에 없는 키(모양 9) · 레코드 1 · 필드 70(78보다 8 적다)" in frames
        and "    다음 78칸 프레임: 그날 없음" in frames
    )


def test_the_short_frames_are_counted_by_day_and_hour(recorded: Path) -> None:
    output = report(recorded, STOCKS, NIGHT)

    assert output.startswith(
        "회원사 프레임의 필드 수 (#381): 2026-10-22 – 2026-10-26, H0UNMBC0를 녹화한 날 3일, "
        "프레임 11개 중 필드 수가 맞지 않는 것 3개"
    )
    days = output.split("1. 날마다")[1].split("2. 시")[0]
    assert "  2026-10-22: 프레임 2개 · 필드 수가 맞지 않는 것 없음" in days, "two records of 78 fit"
    assert (
        "  2026-10-23: 프레임 7개 · 필드 수가 맞지 않는 것 2개(필드 72 ×1, 73 ×1 · 08시 2) · 암호화 1개"
        in days
    )
    assert "  2026-10-26: 프레임 2개 · 필드 수가 맞지 않는 것 1개(필드 70 ×1 · 08시 1)" in days
    hours = output.split("2. 시(KST)마다")[1].split("3. 필드 수가")[0]
    assert "  07시: 프레임 2개 · 필드 수가 맞지 않는 것 없음" in hours
    assert "  08시: 프레임 6개 · 필드 수가 맞지 않는 것 3개(2일: 2026-10-23, 2026-10-26)" in hours
    assert "  09시: 프레임 3개 · 필드 수가 맞지 않는 것 없음" in hours


def test_the_other_feeds_are_read_around_the_short_frames(recorded: Path) -> None:
    output = report(recorded, STOCKS, NIGHT)
    around = output.split("4. 같은 때의 WebSocket 피드")[1]

    assert "  2026-10-23 07:59–08:02(3절의 08:00:01.100, 08:00:02.000)" in around
    assert "    H0UNCNT0 프레임 2개 · 필드 수가 맞지 않는 것 1개(44 ×1)" in around, "not the 10:00 one"
    assert "    H0UNMBC0 프레임 6개 · 필드 수가 맞지 않는 것 2개(72 ×1, 73 ×1) · 암호화 1개" in around
    assert "    _system 프레임 1개" in around
    assert "    _unparsed 프레임 1개: 08:00:01.101(필드 6, '|' 0개)" in around
    assert "    그날 녹화는 있는데 이 구간에 프레임이 없는 피드: H0UNASP0" in around
    assert "  2026-10-26 07:59–08:02(3절의 08:00:00.000)" in around


def test_no_field_value_is_printed(recorded: Path) -> None:
    output = report(recorded, STOCKS, NIGHT)

    shown = output.replace(",", "")
    values = {v for v in VALUES.values() if len(v) >= 4 and v not in STOCKS}
    assert values and all(v not in shown for v in values)
    assert PRICE not in shown and STRANGER not in shown and "ENCRYPTED" not in shown


def test_a_file_unlike_the_schema_is_counted_not_read(recorded: Path) -> None:
    partition = recorded / "raw" / f"date={SHORT}" / f"feed={MEMBER.tr_id}" / "symbol=005930"
    pq.write_table(
        pa.table({"received_at": ["08:00:00"], "message": [SHORT_END]}), partition / "part-x.parquet"
    )
    pq.write_table(
        pa.table({"received_at": pa.array([None], RAW_SCHEMA.field("received_at").type), "message": ["x"]}),
        partition / "part-y.parquet",
    )

    output = report(recorded, STOCKS, NIGHT)

    assert "스키마와 다른 파일, 빈 값의 행" in output
    assert "  raw H0UNMBC0 received_at 타입 string(읽지 않음): 파일 1개" in output
    assert "  raw H0UNMBC0 빈 값(그 행을 읽지 않음): 행 1개" in output
    assert "필드 수가 맞지 않는 것 3개" in output, "the rows of the files read"


def test_without_a_recording_the_probe_says_so(tmp_path: Path) -> None:
    assert (
        report(tmp_path, STOCKS, NIGHT)
        == "회원사 프레임의 필드 수 (#381): raw에 H0UNMBC0 프레임이 있는 날이 없다"
    )


def test_a_day_after_now_is_not_read(recorded: Path) -> None:
    output = report(recorded, STOCKS, at(SHORT, "23:00:00"))

    assert ": 2026-10-22 – 2026-10-23, H0UNMBC0를 녹화한 날 2일" in output


async def test_the_frame_the_collector_could_not_normalize_is_the_one_reported(
    kis: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The raw row as the collector itself records it: a short frame between two full ones, all
    # received at the same instant (a fixed clock), in the order received.
    kis.script[(MEMBER.tr_id, "005930")] = [full(), SHORT_END, full()]

    with caplog.at_level(logging.ERROR):
        await collect(kis, settings_for(kis, tmp_path))

    assert len(read(tmp_path, "events", MEMBER.tr_id)) == 2 and len(read(tmp_path, "raw", MEMBER.tr_id)) == 3
    assert "could not normalize H0UNMBC0 frame; kept raw only" in caplog.text
    output = report(tmp_path, ("005930",), FIXED_NOW + timedelta(hours=1))
    assert (
        "  2026-10-08 09:30:16.000 005930 · 레코드 1 · 필드 72(78보다 6 적다) · "
        "그날 이 종목의 2번째 프레임(앞 프레임 09:30:16.000)"
    ) in output
    assert f"    다음 78칸: {FULL_SHAPES} (09:30:16.000, 0.0초 뒤)" in output
    assert "_system 프레임" in output, "the subscription's reply"


def test_the_command_prints_the_report(
    recorded: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(recorded)  # no .env
    monkeypatch.setenv("KRX_DATA_DIR", str(recorded))
    monkeypatch.setenv("KRX_STATE_DIR", str(recorded / "state"))
    monkeypatch.setenv("KRX_SYMBOLS", ",".join(STOCKS))

    assert main(["probe-member-frames"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("회원사 프레임의 필드 수 (#381): 2026-10-22 – ")
    assert "2026-10-23 08:00:01.100 005930 · 레코드 1 · 필드 72" in out
