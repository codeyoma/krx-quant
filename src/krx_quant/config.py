"""Runtime settings, read from environment variables and an optional `.env` file."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_REST_URL = "https://openapi.koreainvestment.com:9443"
DEFAULT_WS_URL = "ws://ops.koreainvestment.com:21000"
# KIS publishes daily master files of listed contracts here.
DEFAULT_MASTER_URL = "https://new.real.download.dws.co.kr/common/master"
# KIS allows 20 REST calls per second on a real account.
MAX_REST_PER_SECOND = 20


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
    # KRX-listed US index ETFs (TIGER 미국나스닥100, TIGER 미국필라델피아반도체나스닥), recorded like stocks.
    etf_symbols: tuple[str, ...] = ("133690", "381180")
    # KIS allows 20 REST calls per second on a real account.
    rest_max_per_second: int = 20
    # WebSocket registrations to attempt per session (None: until KIS refuses); the rest are polled.
    ws_max_subscriptions: int | None = None
    # Poll interval for feeds that fell back from WebSocket to REST.
    fallback_poll_seconds: float = 5.0
    data_dir: Path = Path("data")
    # The recorder writes a Parquet part file per partition when either limit is reached.
    flush_rows: int = 5000
    flush_seconds: float = 30.0

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, dotenv_path: Path | None = Path(".env")
    ) -> Settings:
        """Build settings from `env` (default: os.environ) layered over `dotenv_path`."""
        values: dict[str, str] = {}
        if dotenv_path is not None and dotenv_path.exists():
            values.update({k: v for k, v in dotenv_values(dotenv_path).items() if v is not None})
        values.update(os.environ if env is None else env)

        def required(name: str) -> str:
            value = values.get(name, "").strip()
            if not value:
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
                raise SettingsError(f"{name} must be at least {minimum or 'above 0'}, got {raw!r}")
            if maximum is not None and value > maximum:
                raise SettingsError(f"{name} must be at most {maximum}, got {raw!r}")
            return value

        def codes(name: str, default: str) -> tuple[str, ...]:
            return tuple(c.strip() for c in values.get(name, default).split(",") if c.strip())

        symbols = codes("KRX_SYMBOLS", "005930")
        if not symbols:
            raise SettingsError("KRX_SYMBOLS lists no symbols")
        return cls(
            kis_app_key=required("KIS_APP_KEY"),
            kis_app_secret=required("KIS_APP_SECRET"),
            kis_rest_url=values.get("KIS_REST_URL", DEFAULT_REST_URL),
            kis_ws_url=values.get("KIS_WS_URL", DEFAULT_WS_URL),
            symbols=symbols,
            index_codes=codes("KRX_INDEX_CODES", "0001,2001,0013"),
            us_symbols=codes("KRX_US_SYMBOLS", "NVDA,MU,AMD"),
            venue_trades=values.get("KRX_VENUE_TRADES", "true").strip().lower() in ("1", "true", "yes"),
            master_url=values.get("KIS_MASTER_URL", DEFAULT_MASTER_URL),
            kospi200_futures_code=values.get("KRX_KOSPI200_FUTURES_CODE") or None,
            usd_futures_code=values.get("KRX_USD_FUTURES_CODE") or None,
            option_strikes_each_side=int(number("KRX_OPTION_STRIKES_EACH_SIDE", "2", int, minimum=0)),
            foreign_estimate_poll_seconds=float(number("KRX_FOREIGN_ESTIMATE_POLL_SECONDS", "300", float)),
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
            data_dir=Path(values.get("KRX_DATA_DIR", "data")),
            flush_rows=int(number("KRX_FLUSH_ROWS", "5000", int)),
            flush_seconds=float(number("KRX_FLUSH_SECONDS", "30", float)),
        )
