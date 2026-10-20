"""The single-stock leveraged and inverse ETFs (#305): one list of product facts, and the inverse
products the research P&L trades a down call through (#267) read from it."""

from __future__ import annotations

from collections import Counter
from datetime import date

from krx_quant import structure_check
from krx_quant.products import INVERSE_PRODUCTS, LISTED, PRODUCTS, Structure, products_of


def test_the_list_holds_the_sixteen_products_of_the_issue_s_table() -> None:
    table = {
        ("005930", Structure.SPOT_FUTURES, 2): {"0193W0", "0195R0", "0194M0", "0192M0", "0193K0"},
        ("005930", Structure.FUTURES, 2): {"0194N0", "0198B0"},
        ("005930", Structure.FUTURES, -2): {"0193L0"},
        ("000660", Structure.SPOT_FUTURES, 2): {"0193T0", "0195S0", "0194T0", "0192L0", "0197W0"},
        ("000660", Structure.FUTURES, 2): {"0194R0", "0198D0"},
        ("000660", Structure.FUTURES, -2): {"0197X0"},
    }
    found: dict[tuple[str, Structure, int], set[str]] = {}
    for p in PRODUCTS:
        found.setdefault((p.underlying, p.structure, p.leverage), set()).add(p.code)
    assert found == table
    codes = [p.code for p in PRODUCTS]
    assert len(codes) == len(set(codes)) == 16
    assert all(len(code) == 6 for code in codes), "KRX short codes"
    assert {p.listed for p in PRODUCTS} == {date(2026, 5, 27)} == {LISTED}
    assert Counter(p.underlying for p in PRODUCTS) == {"005930": 8, "000660": 8}
    assert products_of("009150") == (), "009150 has none: the closing flow's control"
    assert [p.code for p in products_of("000660")][-1] == "0197X0"


def test_a_product_s_exposure_is_its_leverage_either_way() -> None:
    by_code = {p.code: p for p in PRODUCTS}
    two, inverse = by_code["0193W0"], by_code["0193L0"]
    assert (two.leverage, two.exposure, two.inverse) == (2, 2, False)
    assert (inverse.leverage, inverse.exposure, inverse.inverse) == (-2, 2, True)
    # The closing rebalancing's multiple of A·r (#305): L² − L is 2 at 2x and 6 at −2x.
    assert {p.leverage**2 - p.leverage for p in PRODUCTS} == {2, 6}


def test_the_inverse_products_come_from_the_list() -> None:
    """#267's inverse products, now read from the one list: each symbol's −2x product, with the
    listing day and the exposure (2) the research P&L used before."""
    assert {s: p.code for s, p in INVERSE_PRODUCTS.items()} == {"005930": "0193L0", "000660": "0197X0"}
    assert all(p in PRODUCTS and p.inverse for p in INVERSE_PRODUCTS.values())
    assert {(p.listed, p.exposure) for p in INVERSE_PRODUCTS.values()} == {(date(2026, 5, 27), 2)}
    assert "009150" not in INVERSE_PRODUCTS


def test_the_structure_check_s_listing_period_starts_on_the_products_listing() -> None:
    assert structure_check.LISTING == LISTED
