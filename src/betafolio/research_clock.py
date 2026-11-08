"""Run-local research time, separate from execution time and scheduling."""
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo


def research_timestamp(value: date | str | None, zone: str, now=None):
    if not value:
        return None
    now = now or datetime.now(timezone.utc)
    selected = date.fromisoformat(value) if isinstance(value, str) else value
    today = now.astimezone(ZoneInfo(zone)).date()
    if selected > today:
        raise ValueError("Research date cannot be in the future")
    return datetime.combine(selected, time.max, ZoneInfo(zone)).astimezone(timezone.utc) if selected < today else None


@dataclass(frozen=True)
class ResearchClock:
    now: datetime
    historical: bool

    @classmethod
    def for_request(cls, request):
        return cls(request.research_as_of or datetime.now(timezone.utc), request.research_as_of is not None)

    def cutoff(self, request):
        return min(self.now, request.reporting_cutoff) if request.reporting_cutoff else self.now


def model_data(value):
    """Remove execution-only metadata from reference data, recursively."""
    if isinstance(value, dict):
        return {key: model_data(item) for key, item in value.items()
                if key not in {"retrieved_at", "resolved_at", "created_at", "account_id"}}
    if isinstance(value, list):
        return [model_data(item) for item in value]
    return value


class DatedMarketData:
    """Only call explicitly date-aware capabilities; never fall back to live data."""
    def __init__(self, provider, as_of):
        self.provider, self.as_of = provider, as_of
        self._quotes = {}

    async def quotes(self, symbols):
        missing = [symbol for symbol in symbols if symbol not in self._quotes]
        if missing and hasattr(self.provider, "historical_quotes"):
            from .providers import completed_market_session
            latest = completed_market_session(self.as_of)
            for symbol, quote in (await self.provider.historical_quotes(missing, self.as_of)).items():
                if (quote.get('trading_date') and quote['trading_date'] <= latest
                        and (not quote.get('quote_as_of') or datetime.fromisoformat(quote['quote_as_of']) <= self.as_of)):
                    self._quotes[symbol] = quote
        return {symbol: self._quotes[symbol] for symbol in symbols if symbol in self._quotes}

    async def resolve_instrument(self, symbol):
        return None

    async def enrich_with_metadata(self, snapshot):
        result = snapshot.model_copy(deep=True)
        quotes = await self.quotes([row.symbol for row in result.positions])
        metadata = {}
        for row in result.positions:
            quote = quotes.get(row.symbol)
            if not quote:
                continue
            from .providers import valid_market_price
            row.price, row.previous_close = valid_market_price(quote['price']), valid_market_price(quote.get('previous_close'))
            row.price_session = date.fromisoformat(quote['trading_date'])
            row.price_as_of = datetime.fromisoformat(quote['quote_as_of']) if quote.get('quote_as_of') else None
            row.price_source = quote.get('source')
            metadata[row.symbol] = {key: value for key, value in quote.items() if key not in {'price', 'previous_close'}}
        return result, metadata

    async def history(self, snapshot):
        if hasattr(self.provider, "historical_history"):
            from .providers import completed_market_session
            latest = completed_market_session(self.as_of)
            return {symbol: {day: price for day, price in rows.items() if day <= latest}
                    for symbol, rows in (await self.provider.historical_history(snapshot, self.as_of)).items()}
        return {}
