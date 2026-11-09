"""Scheduled analysis refreshes valuations, never the user's stored positions."""
from datetime import datetime, timezone

import pandas as pd

from betafolio.models import PortfolioSnapshot, Position
from betafolio.providers import YFinanceMarketDataProvider


async def test_prices_refresh_without_changing_manual_quantities_or_cash(monkeypatch):
    import yfinance as yf
    prices = pd.DataFrame({"Close": [119.0, 120.0]}, index=pd.to_datetime(["2026-10-15", "2026-10-16"]))

    class Ticker:
        def history(self, **kwargs):
            return prices

    monkeypatch.setattr(yf, "Ticker", lambda symbol: Ticker())
    saved = PortfolioSnapshot(as_of=datetime(2026, 10, 1, tzinfo=timezone.utc), cash="1250.50", source="manual",
                              positions=[Position(symbol="ABC", quantity="7", price="50", previous_close="49")])
    updated, metadata = await YFinanceMarketDataProvider().enrich_with_metadata(saved)
    assert updated.positions[0].price == 120
    assert updated.positions[0].previous_close == 119
    assert updated.positions[0].quantity == saved.positions[0].quantity == 7
    assert updated.cash == saved.cash
    assert updated.as_of == saved.as_of
    assert saved.positions[0].price == 50
    assert metadata["ABC"]["trading_date"] == "2026-10-16"


async def test_missing_quote_does_not_invent_a_fresh_valuation(monkeypatch):
    import yfinance as yf

    class Ticker:
        def history(self, **kwargs):
            return pd.DataFrame()

    monkeypatch.setattr(yf, "Ticker", lambda symbol: Ticker())
    saved = PortfolioSnapshot(as_of=datetime.now(timezone.utc), positions=[Position(symbol="ABC", quantity=1, price=50)])
    updated, metadata = await YFinanceMarketDataProvider().enrich_with_metadata(saved)
    assert updated.positions[0].price == 50
    assert metadata == {}


async def test_newest_quote_across_sessions_and_weekend_intraday(monkeypatch):
    """Do not freeze live valuations at the regular close or on a US weekend."""
    import yfinance as yf
    now = datetime.now(timezone.utc)
    regular = now.timestamp() - 3600
    extended = now.timestamp() - 600
    bar_time = pd.Timestamp(now) - pd.Timedelta(minutes=1)

    class Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def get_info(self):
            return {"regularMarketPrice": 100, "regularMarketTime": regular,
                    "regularMarketPreviousClose": 99, "postMarketPrice": 102,
                    "postMarketTime": extended, "preMarketPrice": 999,
                    "preMarketTime": now.timestamp() + 3600,
                    "exchangeTimezoneName": "America/New_York"}

        def history(self, **kwargs):
            if kwargs.get('interval') == '1m':
                assert kwargs['prepost'] is True
                if self.symbol == 'BTC-USD':
                    return pd.DataFrame({'Close': [105]}, index=[bar_time])
                return pd.DataFrame()
            return pd.DataFrame({'Close': [99]}, index=[pd.Timestamp(now.date())-pd.Timedelta(days=1)])

    monkeypatch.setattr(yf, 'Ticker', Ticker)
    monkeypatch.setattr('betafolio.providers.expected_market_session',
                        lambda *args: (_ for _ in ()).throw(AssertionError('live quotes must not use US calendar')))
    quotes = await YFinanceMarketDataProvider().quotes(['ABC', 'BTC-USD'])
    assert quotes['ABC']['price'] == 102
    assert quotes['ABC']['previous_close'] == 99
    assert quotes['ABC']['valuation_type'] == 'post_market'
    assert quotes['BTC-USD']['price'] == 105
    assert quotes['BTC-USD']['valuation_type'] == 'intraday'
    assert datetime.fromisoformat(quotes['BTC-USD']['quote_as_of']) == bar_time.to_pydatetime()
