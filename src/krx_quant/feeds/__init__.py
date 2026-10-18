"""Feed definitions: KIS layouts and the normalized event schemas recorded for them.

Event schemas are documented in docs/events.md; keep the two in sync.
"""

from krx_quant.feeds.base import KST, RAW_SCHEMA, Feed, RestFeed
from krx_quant.feeds.market import (
    INDEX,
    INDEX_REST,
    KOSPI200_FUTURES,
    KOSPI200_FUTURES_REST,
    NIGHT_FUTURES,
    NIGHT_FUTURES_BOOK,
    OPTION,
    OPTION_REST,
    US_TRADE,
    US_TRADE_REST,
    USD_FUTURES,
    USD_FUTURES_REST,
    us_daytime_key,
)
from krx_quant.feeds.stock import (
    BOOK,
    BOOK_SNAPSHOT,
    FOREIGN_ESTIMATE,
    KRX_QUOTE,
    KRX_TRADE,
    MEMBER,
    MEMBER_REST,
    NXT_TRADE,
    PROGRAM,
    PROGRAM_REST,
    QUOTE,
    TRADE,
    VI_STATUS,
)

WS_FEEDS: dict[str, Feed] = {
    f.tr_id: f
    for f in (
        TRADE,
        BOOK,
        PROGRAM,
        MEMBER,
        KRX_TRADE,
        NXT_TRADE,
        INDEX,
        KOSPI200_FUTURES,
        USD_FUTURES,
        NIGHT_FUTURES,
        NIGHT_FUTURES_BOOK,
        US_TRADE,
        OPTION,
    )
}

# When a WebSocket registration is refused for the session limit, poll this REST feed instead.
FALLBACKS: dict[str, RestFeed] = {
    TRADE.tr_id: QUOTE,
    BOOK.tr_id: BOOK_SNAPSHOT,
    MEMBER.tr_id: MEMBER_REST,
    INDEX.tr_id: INDEX_REST,
    PROGRAM.tr_id: PROGRAM_REST,
    KOSPI200_FUTURES.tr_id: KOSPI200_FUTURES_REST,
    USD_FUTURES.tr_id: USD_FUTURES_REST,
    OPTION.tr_id: OPTION_REST,
    US_TRADE.tr_id: US_TRADE_REST,
}

# ETFs are registered on the KRX-only trade feed and fall back to the KRX snapshot. Stocks'
# KRX-only trades have no fallback: a KRX snapshot would share the integrated one's partition.
ETF_FALLBACKS: dict[str, RestFeed] = {KRX_TRADE.tr_id: KRX_QUOTE}

__all__ = [
    "BOOK",
    "BOOK_SNAPSHOT",
    "ETF_FALLBACKS",
    "FALLBACKS",
    "FOREIGN_ESTIMATE",
    "INDEX",
    "INDEX_REST",
    "KOSPI200_FUTURES",
    "KOSPI200_FUTURES_REST",
    "KRX_QUOTE",
    "KRX_TRADE",
    "KST",
    "MEMBER",
    "MEMBER_REST",
    "NIGHT_FUTURES",
    "NIGHT_FUTURES_BOOK",
    "NXT_TRADE",
    "OPTION",
    "OPTION_REST",
    "PROGRAM",
    "PROGRAM_REST",
    "QUOTE",
    "RAW_SCHEMA",
    "TRADE",
    "US_TRADE",
    "US_TRADE_REST",
    "USD_FUTURES",
    "USD_FUTURES_REST",
    "VI_STATUS",
    "WS_FEEDS",
    "Feed",
    "RestFeed",
    "us_daytime_key",
]
