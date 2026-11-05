"""The auto trader's commands (#578): the trader itself and its controls, its policy's re-fit and
report, and its checks and probes on KIS's mock and live servers."""

from __future__ import annotations

import asyncio
import signal
import time
from datetime import date, datetime

from krx_quant.cli.core import Commands
from krx_quant.config import Settings
from krx_quant.feeds.base import KST


async def _trade() -> int:
    from krx_quant.trader.run import run_trader
    from krx_quant.trader.settings import TraderSettings

    # The KIS keys are the collector's, which the trader never uses (#393): paper needs none, mock
    # its own (KRX_TRADER_KIS_*, #405) and live the live account's (KRX_TRADER_LIVE_KIS_*, #408), which
    # the trader checks as it starts. A trader that stops by itself (#408: the live mode's answer
    # changed at a pick) exits 0, and Docker starts it again (``restart: unless-stopped``).
    settings = Settings.from_env(require_kis=False)
    trader = TraderSettings.from_env()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass
    await run_trader(settings, trader, stop=stop)
    return 0


def _trade_control(action: str) -> int:
    """``krx-quant trade resume|rearm`` (#396): the trader's pause lifted, or its cumulative loss
    limit re-armed, written into its store for the running trader to read; whether it did."""
    from krx_quant.trader import LIVE, limits
    from krx_quant.trader.promotion import mode_at
    from krx_quant.trader.settings import TraderSettings
    from krx_quant.trader.store import DB_NAME as TRADER_DB
    from krx_quant.trader.store import TraderStore

    settings = Settings.from_env(require_kis=False)
    mode = TraderSettings.from_env().mode  # a re-arm is of the mode the trader runs in
    store = TraderStore(settings.state_dir / TRADER_DB)
    try:
        if mode == LIVE:  # #408: a live trader runs as paper until the gate and the checks pass
            mode = mode_at(store.db)
        asked = limits.ask(store, action, mode, datetime.now(KST))
        if isinstance(asked, str):
            print(asked)
            return 1
        print(f"{action} asked for (control {asked.id}) at {asked.at.isoformat()}")
        deadline = time.monotonic() + 3 * limits.CONTROL_SECONDS + 1
        while time.monotonic() < deadline:
            control = store.control(asked.id)
            if control is not None and control.applied_at is not None:
                print(f"the trader read it at {control.applied_at.isoformat()}")
                return 0
            time.sleep(limits.CONTROL_SECONDS / 2)
    finally:
        store.close()
    print("the trader has not read it yet: it does when it runs (docker compose ps trader)")
    return 0


async def _refit_policy(engine_url: str | None, report: bool) -> int:
    """The trader's policy fitted again (#399); with ``report``, then the day's report above its
    output (#402): the scheduled job ``trader-report``'s, in the engine's container."""
    from krx_quant.trader.refit import run_refit
    from krx_quant.trader.report import run_night
    from krx_quant.trader.settings import TraderSettings

    settings = Settings.from_env(require_kis=False)
    trader = TraderSettings.from_env()
    if report:
        return await run_night(settings, trader, datetime.now(KST), engine_url)
    return await run_refit(settings, trader, datetime.now(KST), engine_url)


def _trader_report(day: date | None) -> int:
    """The trader's report of a trading day (#402), read-only."""
    from krx_quant.trader.report import run_report

    return run_report(Settings.from_env(require_kis=False), datetime.now(KST), day)


async def _check_mock_trades(day: date | None) -> int:
    """A mock trading day's orders in the ledger against KIS's order inquiry (#405), read-only."""
    from krx_quant.trader.mock_check import check_mock_trades

    return await check_mock_trades(Settings.from_env(require_kis=False), datetime.now(KST), day)


async def _fill_notice_layout() -> int:
    """The mock fill notices the trader recorded, measured against the day's orders (#484),
    read-only."""
    from krx_quant.trader.notice_layout import fill_notice_layout

    return await fill_notice_layout(Settings.from_env(require_kis=False), datetime.now(KST))


async def _probe_live_account() -> int:
    """The live account read by inquiry calls alone (#408): the trader's live key, no order."""
    from krx_quant.live_probe import probe_live_account

    return await probe_live_account(Settings.from_env(require_kis=False), datetime.now(KST))


async def _probe_kis_orders() -> int:
    """The trader's orders on KIS's mock server (#403): its own keys, not the collector's."""
    from krx_quant.order_probe import probe_kis_orders

    return await probe_kis_orders(Settings.from_env(require_kis=False), datetime.now(KST))


def add(sub: Commands) -> None:
    """The trader's commands, each with its handler (``cli._parser``)."""
    trade_cmd = sub.add_parser(
        "trade",
        help="trade 005930 on the cells the cost map passes, in paper (#394, #395), on KIS's mock server"
        " (KRX_TRADER_MODE=mock, #405) or on the live account once the live gate and the execution"
        " checks pass (KRX_TRADER_MODE=live, #408), until interrupted;"
        " resume: lift the screen's pause; rearm: re-arm the cumulative loss limit (#396)",
    )
    trade_cmd.add_argument(
        "control",
        nargs="?",
        choices=("resume", "rearm"),
        help="instead of trading, ask the running trader to resume after a pause, or to re-arm its"
        " cumulative loss limit (counted again from then)",
    )
    trade_cmd.set_defaults(
        run=lambda args: asyncio.run(_trade()) if args.control is None else _trade_control(args.control)
    )
    refit_cmd = sub.add_parser(
        "refit-policy",
        help="fit the trader's policy again (#399): tonight's cost map from the Engine API, each passing"
        " cell's threshold and Kelly fraction on a small grid, from the stored predictions and answers of"
        " its 20 days (read-only); keeps it in state/trader.sqlite3, the first as the champion",
    )
    refit_cmd.add_argument(
        "--engine-url",
        help="the Engine API the cost map is read from (default KRX_TRADER_ENGINE_URL, which compose sets"
        " to the engine service's, http://engine:8080; else http://127.0.0.1:8080)",
    )
    refit_cmd.add_argument(
        "--report",
        action="store_true",
        help="then print the day's trader report (#402) above the re-fit's output: the scheduled job"
        " trader-report's",
    )
    refit_cmd.set_defaults(run=lambda args: asyncio.run(_refit_policy(args.engine_url, args.report)))
    report_cmd = sub.add_parser(
        "trader-report",
        help="print the trader's report of a trading day (#402), read-only from state/trader.sqlite3: its"
        " trades, why no cell traded, each policy's paper record, what its night changed, the live gate,"
        " limits and pauses; no price",
    )
    report_cmd.add_argument(
        "--day",
        type=date.fromisoformat,
        help="the trading day, YYYY-MM-DD (default: the latest at or before tonight's evening)",
    )
    report_cmd.set_defaults(run=lambda args: _trader_report(args.day))
    mock_check_cmd = sub.add_parser(
        "check-mock-trades",
        help="compare a mock trading day's orders in the trader's ledger with KIS's order inquiry (#405),"
        " with KRX_TRADER_KIS_* on the mock server: sides, quantities, fills and fill prices in ticks;"
        " no order number, account or price",
    )
    mock_check_cmd.add_argument(
        "--day",
        type=date.fromisoformat,
        help="the trading day, YYYY-MM-DD (default: the latest at or before tonight's evening)",
    )
    mock_check_cmd.set_defaults(run=lambda args: asyncio.run(_check_mock_trades(args.day)))
    sub.add_parser(
        "fill-notice-layout",
        help="measure the mock fill notices the trader recorded (state/fill_notices.sqlite3, #484) column by"
        " column against the day's orders in its ledger and on KIS's order inquiry (KRX_TRADER_KIS_*,"
        " read-only): each column's shape, lengths, values' count and matches; no value, order number,"
        " account or price",
    ).set_defaults(run=lambda args: asyncio.run(_fill_notice_layout()))
    sub.add_parser(
        "probe-live-account",
        help="read the trader's live account with KRX_TRADER_LIVE_KIS_* by inquiry calls alone (#408): the"
        " price, the balance, the orderable cash and the day's order inquiry, each field the trader's broker"
        " reads checked against how it reads it; places no order; keeps the result for the live mode;"
        " prints codes and field names, no key, account, order number or value",
    ).set_defaults(run=lambda args: asyncio.run(_probe_live_account()))
    sub.add_parser(
        "probe-kis-orders",
        help="place the trader's probe orders on KIS's mock-trading server with KRX_TRADER_KIS_* (#403):"
        " a 1-share marketable buy and sell of 005930, a far limit buy and its cancel, three orders KIS"
        " should refuse; refuses any server but the mock one and runs only in the KRX session; prints"
        " the answers' codes and field shapes, no key, account, order number or market value",
    ).set_defaults(run=lambda args: asyncio.run(_probe_kis_orders()))
