"""Runtime settings, read from environment variables and an optional `.env` file."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_REST_URL = "https://openapi.koreainvestment.com:9443"
DEFAULT_WS_URL = "ws://ops.koreainvestment.com:21000"


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    kis_app_key: str
    kis_app_secret: str
    kis_rest_url: str = DEFAULT_REST_URL
    kis_ws_url: str = DEFAULT_WS_URL
    symbols: tuple[str, ...] = ("005930",)
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

        def number(name: str, default: str, kind: type[int] | type[float]) -> int | float:
            raw = values.get(name, default)
            try:
                value = kind(raw)
            except ValueError as e:
                raise SettingsError(f"{name} must be a number, got {raw!r}") from e
            if value <= 0:
                raise SettingsError(f"{name} must be positive, got {raw!r}")
            return value

        symbols = tuple(s.strip() for s in values.get("KRX_SYMBOLS", "005930").split(",") if s.strip())
        if not symbols:
            raise SettingsError("KRX_SYMBOLS lists no symbols")
        return cls(
            kis_app_key=required("KIS_APP_KEY"),
            kis_app_secret=required("KIS_APP_SECRET"),
            kis_rest_url=values.get("KIS_REST_URL", DEFAULT_REST_URL),
            kis_ws_url=values.get("KIS_WS_URL", DEFAULT_WS_URL),
            symbols=symbols,
            data_dir=Path(values.get("KRX_DATA_DIR", "data")),
            flush_rows=int(number("KRX_FLUSH_ROWS", "5000", int)),
            flush_seconds=float(number("KRX_FLUSH_SECONDS", "30", float)),
        )
