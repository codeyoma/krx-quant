"""Runtime settings, read from environment variables and an optional `.env` file."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from pathlib import Path

from dotenv import dotenv_values

from krx_quant.regime import RegimeRules
from krx_quant.signals import SignalRule
from krx_quant.stats import DEFAULT_MIN_SAMPLE

DEFAULT_REST_URL = "https://openapi.koreainvestment.com:9443"
DEFAULT_WS_URL = "ws://ops.koreainvestment.com:21000"
# KIS publishes daily master files of listed contracts here.
DEFAULT_MASTER_URL = "https://new.real.download.dws.co.kr/common/master"
# KIS allows 20 REST calls per second on a real account.
MAX_REST_PER_SECOND = 20
DEFAULT_TELEGRAM_API_URL = "https://api.telegram.org"
# Macro sources (macro.py).
DEFAULT_ECOS_URL = "https://ecos.bok.or.kr/api"
DEFAULT_FRED_URL = "https://api.stlouisfed.org"
DEFAULT_YAHOO_URL = "https://query1.finance.yahoo.com"
RULES = RegimeRules()  # the regime's default thresholds
SIGNALS = SignalRule()  # the signal alerts' defaults


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    kis_app_key: str
    kis_app_secret: str
    kis_rest_url: str = DEFAULT_REST_URL
    kis_ws_url: str = DEFAULT_WS_URL
    symbols: tuple[str, ...] = ("005930",)
    # Sector / market indices: 0001 KOSPI, 2001 KOSPI200, 0013 (electrical & electronics).
    index_codes: tuple[str, ...] = ("0001", "2001", "0013")
    # NASDAQ tickers recorded in the US daytime session.
    us_symbols: tuple[str, ...] = ("NVDA", "MU", "AMD")
    # Also register the KRX-only and NXT-only trade feeds, to tell venues apart.
    venue_trades: bool = True
    # Derivative codes come from the KIS master files unless set here.
    master_url: str = DEFAULT_MASTER_URL
    kospi200_futures_code: str | None = None
    usd_futures_code: str | None = None
    # KOSPI200 options recorded: the ATM strike and this many strikes on each side, calls and puts.
    option_strikes_each_side: int = 2
    foreign_estimate_poll_seconds: float = 300.0
    # How often each symbol's VIs are polled while the market is open.
    vi_poll_seconds: float = 5.0
    # KRX-listed US index ETFs (TIGER 미국나스닥100, TIGER 미국필라델피아반도체나스닥), recorded from
    # the KRX-only trade feed: ETFs do not trade on NXT.
    etf_symbols: tuple[str, ...] = ("133690", "381180")
    # KIS allows 20 REST calls per second on a real account.
    rest_max_per_second: int = 20
    # WebSocket registrations to attempt per session (None: until KIS refuses); the rest are polled.
    ws_max_subscriptions: int | None = None
    # Local state such as the cached KIS access token (keep it out of git; mode 600).
    state_dir: Path = Path("state")
    # Refresh the access token this long before it expires; never issue twice within the interval.
    token_refresh_margin_seconds: float = 3600.0
    token_min_interval_seconds: float = 61.0
    # Operational alerts go to this Telegram bot and chat; unset means log only.
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_api_url: str = DEFAULT_TELEGRAM_API_URL
    # How often feed health (stale feeds) is checked.
    health_check_seconds: float = 5.0
    # A reconnect counts as recovered (and is announced) only after staying up this long,
    # so a flapping connection does not send an alert pair on every cycle.
    recovery_hold_seconds: float = 60.0
    # WebSocket reconnect backoff: starts here, doubles per failed attempt, capped at the max.
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 60.0
    # Poll interval for feeds that fell back from WebSocket to REST.
    fallback_poll_seconds: float = 5.0
    # One-minute bars are kept for trading days this far back (KIS keeps about a year).
    backfill_days: int = 365
    # Backfill queries are spaced evenly at this rate, inside the shared REST limit.
    backfill_max_per_second: float = 8.0
    data_dir: Path = Path("data")
    # Recorded days are archived here after the close: ``s3://bucket/prefix`` or a local
    # directory. Unset: no archiving.
    object_store: str | None = None
    # S3 access. Unset keys fall back to boto3's own lookup; the endpoint is for
    # S3-compatible stores such as MinIO.
    aws_region: str = "ap-southeast-2"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    s3_endpoint_url: str | None = None
    # The recorder writes a Parquet part file per partition when either limit is reached.
    flush_rows: int = 5000
    flush_seconds: float = 30.0
    # Commission per side, as a fraction (0.015% is 0.00015). With the sell tax it sets the
    # flat band: moves smaller than the round-trip cost count as flat.
    fee_rate: float = 0.00015
    # The Engine API listens here (KRX_API_HOST, KRX_API_PORT, default 127.0.0.1:8080).
    # Inside Docker the host is 0.0.0.0 and compose publishes the port on the Tailscale
    # address only. None (tests only) runs the engine without the API.
    api_host: str = "127.0.0.1"
    api_port: int | None = None
    # One algorithm may take this long for a bar close (all symbols); the algorithms' expected
    # work per bar close must fit in the CPU budget, or the last registered are skipped.
    algorithm_seconds: float = 30.0
    cpu_budget_seconds: float = 30.0
    # An algorithm needs this many scored predictions in a leaderboard to be ranked.
    min_sample: int = DEFAULT_MIN_SAMPLE
    # The collector starts the nightly training at this time (KST) on trading days, once the
    # day's bars are archived (KRX_TRAIN_AT: 21:30 unless set; "off": trained elsewhere).
    # None here, so a collector built in a test never trains.
    train_at: time | None = None
    # Where that night's training runs (KRX_TRAIN_ON): "server", in its own process here,
    # or "spot", on EC2 Spot started by AWS at the same time (#23): then the collector only
    # publishes what the run needs and waits for its result.
    train_on: str = "server"
    # Cost Guard (#23): the AWS Budgets budget the server watches (KRX_COST_BUDGET; unset:
    # none), the EventBridge Scheduler schedules of the Spot training it turns off at the
    # budget and on again in a new month (KRX_TRAINING_SCHEDULES), and how often it looks.
    cost_budget: str | None = None
    training_schedules: tuple[str, ...] = ()
    cost_check_seconds: float = 3600.0
    # The collector reads the macro indicators and decides the day's regime at this time
    # (KST) on trading days, after the US close (KRX_MACRO_AT: 07:30 unless set; "off").
    # None here, so a collector built in a test never does.
    macro_at: time | None = None
    # Free API keys of the Bank of Korea (ECOS) and FRED; a source without its key is skipped.
    ecos_api_key: str | None = None
    fred_api_key: str | None = None
    ecos_url: str = DEFAULT_ECOS_URL
    fred_url: str = DEFAULT_FRED_URL
    yahoo_url: str = DEFAULT_YAHOO_URL
    regime_rules: RegimeRules = RegimeRules()
    signal_rule: SignalRule | None = None  # Telegram signal alerts; None: off

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        dotenv_path: Path | None = Path(".env"),
        *,
        require_kis: bool = True,
    ) -> Settings:
        """Build settings from `env` (default: os.environ) layered over `dotenv_path`.

        Without ``require_kis`` the KIS keys may be missing, for commands that never call
        KIS (replay).
        """
        values: dict[str, str] = {}
        if dotenv_path is not None and dotenv_path.exists():
            values.update({k: v for k, v in dotenv_values(dotenv_path).items() if v is not None})
        values.update(os.environ if env is None else env)

        def required(name: str) -> str:
            value = values.get(name, "").strip()
            if not value and require_kis:
                raise SettingsError(f"{name} is not set")
            return value

        def number(
            name: str,
            default: str,
            kind: type[int] | type[float],
            minimum: float | None = None,
            maximum: float | None = None,
        ) -> int | float:
            """Parse a number; by default it must be positive."""
            raw = values.get(name, default)
            try:
                value = kind(raw)
            except ValueError as e:
                raise SettingsError(f"{name} must be a number, got {raw!r}") from e
            if (value < minimum) if minimum is not None else (value <= 0):
                floor = "above 0" if minimum is None else minimum
                raise SettingsError(f"{name} must be at least {floor}, got {raw!r}")
            if maximum is not None and value > maximum:
                raise SettingsError(f"{name} must be at most {maximum}, got {raw!r}")
            return value

        def codes(name: str, default: str) -> tuple[str, ...]:
            return tuple(c.strip() for c in values.get(name, default).split(",") if c.strip())

        symbols = codes("KRX_SYMBOLS", "005930")
        if not symbols:
            raise SettingsError("KRX_SYMBOLS lists no symbols")
        index_codes = codes("KRX_INDEX_CODES", "0001,2001,0013")
        if "0001" not in index_codes:
            # The engine calls KOSPI circuit breakers from it (special.py).
            raise SettingsError(
                "KRX_INDEX_CODES must include 0001 (KOSPI): circuit breakers are called from it"
            )
        # Checked even while the alerts are off, so a wrong value shows before they are turned on.
        signal_rule = SignalRule(
            min_probability=float(
                number("KRX_SIGNAL_MIN_PROBABILITY", str(SIGNALS.min_probability), float, 0.5, 1)
            ),
            min_agree=int(number("KRX_SIGNAL_MIN_AGREE", str(SIGNALS.min_agree), int, 1)),
        )
        return cls(
            kis_app_key=required("KIS_APP_KEY"),
            kis_app_secret=required("KIS_APP_SECRET"),
            kis_rest_url=values.get("KIS_REST_URL", DEFAULT_REST_URL),
            kis_ws_url=values.get("KIS_WS_URL", DEFAULT_WS_URL),
            symbols=symbols,
            index_codes=index_codes,
            us_symbols=codes("KRX_US_SYMBOLS", "NVDA,MU,AMD"),
            venue_trades=values.get("KRX_VENUE_TRADES", "true").strip().lower() in ("1", "true", "yes"),
            master_url=values.get("KIS_MASTER_URL", DEFAULT_MASTER_URL),
            kospi200_futures_code=values.get("KRX_KOSPI200_FUTURES_CODE") or None,
            usd_futures_code=values.get("KRX_USD_FUTURES_CODE") or None,
            option_strikes_each_side=int(number("KRX_OPTION_STRIKES_EACH_SIDE", "2", int, minimum=0)),
            foreign_estimate_poll_seconds=float(number("KRX_FOREIGN_ESTIMATE_POLL_SECONDS", "300", float)),
            vi_poll_seconds=float(number("KRX_VI_POLL_SECONDS", "5", float)),
            etf_symbols=codes("KRX_ETF_SYMBOLS", "133690,381180"),
            rest_max_per_second=int(
                number("KIS_REST_MAX_PER_SECOND", "20", int, maximum=MAX_REST_PER_SECOND)
            ),
            ws_max_subscriptions=(
                int(number("KIS_WS_MAX_SUBSCRIPTIONS", "1", int))
                if values.get("KIS_WS_MAX_SUBSCRIPTIONS")
                else None
            ),
            fallback_poll_seconds=float(number("KRX_FALLBACK_POLL_SECONDS", "5", float)),
            state_dir=Path(values.get("KRX_STATE_DIR", "state")),
            telegram_bot_token=values.get("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=values.get("TELEGRAM_CHAT_ID") or None,
            telegram_api_url=values.get("TELEGRAM_API_URL", DEFAULT_TELEGRAM_API_URL),
            health_check_seconds=float(number("KRX_HEALTH_CHECK_SECONDS", "5", float)),
            recovery_hold_seconds=float(number("KRX_RECOVERY_HOLD_SECONDS", "60", float)),
            reconnect_initial_seconds=float(number("KRX_RECONNECT_INITIAL_SECONDS", "1", float)),
            reconnect_max_seconds=float(number("KRX_RECONNECT_MAX_SECONDS", "60", float)),
            backfill_days=int(number("KRX_BACKFILL_DAYS", "365", int)),
            backfill_max_per_second=float(
                number("KRX_BACKFILL_MAX_PER_SECOND", "8", float, maximum=MAX_REST_PER_SECOND)
            ),
            data_dir=Path(values.get("KRX_DATA_DIR", "data")),
            object_store=values.get("KRX_OBJECT_STORE") or None,
            aws_region=values.get("AWS_REGION") or values.get("AWS_DEFAULT_REGION") or "ap-southeast-2",
            aws_access_key_id=values.get("AWS_ACCESS_KEY_ID") or None,
            aws_secret_access_key=values.get("AWS_SECRET_ACCESS_KEY") or None,
            s3_endpoint_url=values.get("KRX_S3_ENDPOINT_URL") or None,
            flush_rows=int(number("KRX_FLUSH_ROWS", "5000", int)),
            flush_seconds=float(number("KRX_FLUSH_SECONDS", "30", float)),
            fee_rate=float(number("KRX_FEE_PERCENT", "0.015", float, minimum=0, maximum=1)) / 100,
            api_host=values.get("KRX_API_HOST", "127.0.0.1"),
            api_port=int(number("KRX_API_PORT", "8080", int, maximum=65535)),
            algorithm_seconds=float(number("KRX_ALGORITHM_SECONDS", "30", float, maximum=55)),
            cpu_budget_seconds=float(number("KRX_CPU_BUDGET_SECONDS", "30", float)),
            min_sample=int(number("KRX_MIN_SAMPLE", str(DEFAULT_MIN_SAMPLE), int)),
            train_at=_time_or_off("KRX_TRAIN_AT", values.get("KRX_TRAIN_AT", "21:30")),
            train_on=_one_of("KRX_TRAIN_ON", values.get("KRX_TRAIN_ON", "server"), TRAIN_ON),
            cost_budget=values.get("KRX_COST_BUDGET", "").strip() or None,
            training_schedules=codes("KRX_TRAINING_SCHEDULES", ""),
            cost_check_seconds=float(number("KRX_COST_CHECK_SECONDS", "3600", float, minimum=60)),
            macro_at=_time_or_off("KRX_MACRO_AT", values.get("KRX_MACRO_AT", "07:30")),
            ecos_api_key=values.get("KRX_ECOS_API_KEY") or None,
            fred_api_key=values.get("KRX_FRED_API_KEY") or None,
            ecos_url=values.get("KRX_ECOS_URL", DEFAULT_ECOS_URL),
            fred_url=values.get("KRX_FRED_URL", DEFAULT_FRED_URL),
            yahoo_url=values.get("KRX_YAHOO_URL", DEFAULT_YAHOO_URL),
            regime_rules=RegimeRules(
                shock_sox_pct=float(number("KRX_REGIME_SHOCK_SOX_PERCENT", str(RULES.shock_sox_pct), float)),
                shock_vix=float(number("KRX_REGIME_SHOCK_VIX", str(RULES.shock_vix), float)),
                shock_usdkrw_pct=float(
                    number("KRX_REGIME_SHOCK_USDKRW_PERCENT", str(RULES.shock_usdkrw_pct), float)
                ),
                sox_move_pct=float(number("KRX_REGIME_SOX_MOVE_PERCENT", str(RULES.sox_move_pct), float)),
                weak_won_pct=float(number("KRX_REGIME_WEAK_WON_PERCENT", str(RULES.weak_won_pct), float)),
            ),
            signal_rule=signal_rule
            if _on_or_off("KRX_SIGNAL_ALERT", values.get("KRX_SIGNAL_ALERT", "off"))
            else None,
        )


def _on_or_off(name: str, raw: str) -> bool:
    text = raw.strip().lower()
    if text in ("on", "true", "1", "yes"):
        return True
    if text in ("", "off", "false", "0", "no"):
        return False
    raise SettingsError(f"{name} must be on or off, got {raw!r}")


# Where the nightly training runs: on this server, or on EC2 Spot (#23).
TRAIN_ON = ("server", "spot")


def _one_of(name: str, raw: str, choices: tuple[str, ...]) -> str:
    text = raw.strip().lower()
    if text not in choices:
        raise SettingsError(f"{name} must be one of {', '.join(choices)}, got {raw!r}")
    return text


def _time_or_off(name: str, raw: str) -> time | None:
    text = raw.strip().lower()
    if text in ("", "off"):
        return None
    try:
        return time.fromisoformat(text)
    except ValueError as e:
        raise SettingsError(f"{name} must be HH:MM or off, got {raw!r}") from e
