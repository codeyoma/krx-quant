"""The single-stock leveraged and inverse ETFs on the predicted symbols (#305, #267).

On 2026-05-27 sixteen ±2x ETFs on 005930 and 000660 listed in Korea (머니투데이 2026-08-03, SBS
Biz; codes checked against KIS's stock master on 2026-10-16, issue #305). Every product fact
the engine uses lives here, in ``PRODUCTS``, and nowhere else:

- ``code``: the KRX short code; the name comes from KIS's stock master when shown (``names.py``),
  so none is written here;
- ``underlying``: the stock it tracks;
- ``leverage``: L, the daily return it tracks as a multiple of the stock's (2 or −2). The
  closing rebalancing an L-times ETF must trade is A·(L² − L)·r: 2·A·r at 2x, 6·A·r at −2x
  (#305);
- ``structure``: how it holds the exposure: the stock plus single-stock futures
  (현물+선물형), or the futures alone (선물형). The 2x come in both; the −2x are futures only;
- ``listed``: its first trading day.

The same day's two ETNs (Q520100, Q520101) and the Hong Kong products (7747.HK, 7709.HK,
7347.HK) are not here: the closing flow leaves them out (#305, "범위 밖"). 009150 has none.

``INVERSE_PRODUCTS`` is each symbol's inverse product, read from ``PRODUCTS``: a down call can be
traded through it (#267). An ETF's sale pays no securities transaction tax (the tax falls on
shares only, 증권거래세법 제2조), so its round trip costs the commissions alone, on its own
notional: the stock's ÷ its ``exposure``.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date


class Structure(enum.Enum):
    SPOT_FUTURES = "spot_futures"  # 현물+선물형: the stock and single-stock futures
    FUTURES = "futures"  # 선물형: single-stock futures alone


@dataclass(frozen=True)
class Product:
    """A single-stock leveraged or inverse ETF."""

    code: str
    underlying: str
    leverage: int  # L: 2, or −2 for an inverse product
    structure: Structure
    listed: date  # the first day it traded

    @property
    def exposure(self) -> int:
        """The stock's exposure per won of the product, either way: |L|."""
        return abs(self.leverage)

    @property
    def inverse(self) -> bool:
        return self.leverage < 0


LISTED = date(2026, 5, 27)
_S, _F = Structure.SPOT_FUTURES, Structure.FUTURES

PRODUCTS: tuple[Product, ...] = (
    *(Product(code, "005930", 2, _S, LISTED) for code in ("0193W0", "0195R0", "0194M0", "0192M0", "0193K0")),
    *(Product(code, "005930", 2, _F, LISTED) for code in ("0194N0", "0198B0")),
    Product("0193L0", "005930", -2, _F, LISTED),
    *(Product(code, "000660", 2, _S, LISTED) for code in ("0193T0", "0195S0", "0194T0", "0192L0", "0197W0")),
    *(Product(code, "000660", 2, _F, LISTED) for code in ("0194R0", "0198D0")),
    Product("0197X0", "000660", -2, _F, LISTED),
)


def products_of(symbol: str) -> tuple[Product, ...]:
    """``symbol``'s products, in ``PRODUCTS``' order; none for a symbol without any."""
    return tuple(p for p in PRODUCTS if p.underlying == symbol)


def _inverse_products() -> dict[str, Product]:
    inverse: dict[str, Product] = {}
    for product in PRODUCTS:
        if product.inverse:
            if product.underlying in inverse:
                raise ValueError(f"two inverse products on {product.underlying}: one is traded (#267)")
            inverse[product.underlying] = product
    return inverse


# Each symbol's inverse product (#267): 005930's 0193L0 and 000660's 0197X0.
INVERSE_PRODUCTS: Mapping[str, Product] = _inverse_products()
