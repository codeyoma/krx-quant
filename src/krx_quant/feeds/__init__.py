"""Feed definitions: KIS layouts and the normalized event schemas recorded for them.

Event schemas are documented in docs/events.md; keep the two in sync.
"""

from krx_quant.feeds.base import KST, RAW_SCHEMA, Feed, RestFeed
from krx_quant.feeds.market import (
    INDEX,
    INDEX_REST,
    KOSPI200_FUTURES,
    OPTION,
    US_TRADE,
    USD_FUTURES,
    us_daytime_key,
)
from krx_quant.feeds.stock import (
    BOOK,
    BOOK_SNAPSHOT,
    FOREIGN_ESTIMATE,
    KRX_TRADE,
    MEMBER,
    MEMBER_REST,
    NXT_TRADE,
    PROGRAM,
    QUOTE,
    TRADE,
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
}

__all__ = [
    "BOOK",
    "BOOK_SNAPSHOT",
    "FALLBACKS",
    "FOREIGN_ESTIMATE",
    "INDEX",
    "INDEX_REST",
    "KOSPI200_FUTURES",
    "KRX_TRADE",
    "KST",
    "MEMBER",
    "MEMBER_REST",
    "NXT_TRADE",
    "OPTION",
    "PROGRAM",
    "QUOTE",
    "RAW_SCHEMA",
    "TRADE",
    "US_TRADE",
    "USD_FUTURES",
    "WS_FEEDS",
    "Feed",
    "RestFeed",
    "us_daytime_key",
]
