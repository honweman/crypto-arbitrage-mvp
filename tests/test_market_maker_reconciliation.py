from arbitrage_bot.web.loops import _market_order_reconciliation_is_clear


EXCHANGE = "workspace:connection-gate:spot"
SYMBOL = "ACS/USDT"


def test_market_reconciliation_clears_when_no_intents_are_pending() -> None:
    assert _market_order_reconciliation_is_clear(
        {"enabled": True, "pending_count": 0, "quarantined_resources": []},
        exchange=EXCHANGE,
        symbol=SYMBOL,
    )


def test_market_reconciliation_stays_blocked_for_matching_resource() -> None:
    assert not _market_order_reconciliation_is_clear(
        {
            "enabled": True,
            "pending_count": 1,
            "quarantined_resources": [
                {"exchange": EXCHANGE, "symbol": SYMBOL, "count": 1}
            ],
        },
        exchange=EXCHANGE,
        symbol=SYMBOL,
    )


def test_market_reconciliation_ignores_other_quarantined_markets() -> None:
    assert _market_order_reconciliation_is_clear(
        {
            "enabled": True,
            "pending_count": 1,
            "quarantined_resources": [
                {"exchange": EXCHANGE, "symbol": "BTC/USDT", "count": 1}
            ],
        },
        exchange=EXCHANGE,
        symbol=SYMBOL,
    )


def test_market_reconciliation_fails_closed_for_inconsistent_summary() -> None:
    assert not _market_order_reconciliation_is_clear(
        {"enabled": True, "pending_count": 1, "quarantined_resources": []},
        exchange=EXCHANGE,
        symbol=SYMBOL,
    )
    assert not _market_order_reconciliation_is_clear(
        {"enabled": False, "pending_count": 0, "quarantined_resources": []},
        exchange=EXCHANGE,
        symbol=SYMBOL,
    )
