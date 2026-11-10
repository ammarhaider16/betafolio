"""Acceptance checks for reviewable metadata and all-position news coverage."""
import asyncio
from decimal import Decimal


from betafolio.enrichment import PortfolioEnrichmentAgent
from betafolio.imports import DraftPosition


def test_enrichment_preserves_user_prices_and_missing_quantities():
    class Market:
        async def resolve_instrument(self, symbol):
            return {"symbol": symbol, "asset_type": "stock", "instrument_name": "Apple Inc.",
                    "sector": "Technology", "exchange": "Nasdaq", "source": "market"}

        async def enrich_with_metadata(self, snapshot):
            row = snapshot.positions[0]
            row.price = Decimal("210")
            row.previous_close = Decimal("205")
            return snapshot, {row.symbol: {"source": "market", "trading_date": "2026-10-17"}}

    draft = DraftPosition(symbol="AAPL", quantity=None, price=200)
    result = asyncio.run(PortfolioEnrichmentAgent(Market()).enrich([draft]))
    enriched = result["positions"][0]
    assert enriched["quantity"] is None
    assert enriched["price"] == "200"
    assert enriched["previous_close"] is None
    assert enriched["instrument_name"] == "Apple Inc."
    assert "price" not in result["changes"][0]["fields"]

    missing_price = asyncio.run(PortfolioEnrichmentAgent(Market()).enrich([DraftPosition(symbol="AAPL")]))
    quote = missing_price["positions"][0]
    assert quote["price"] == "210" and quote["previous_close"] == "205"
    assert missing_price["changes"][0]["price_as_of"] == "2026-10-17"
