"""Offline demonstration data; never presented as a live account."""
from datetime import datetime, timezone
from .models import PortfolioSnapshot, Position


SAMPLE = [
    ('VTI','40.5','etf','Vanguard Total Stock Market ETF','280','278'),
    ('VXUS','65','etf','Vanguard Total International Stock ETF','65','65.2'),
    ('BND','70','etf','Vanguard Total Bond Market ETF','74','73.9'),
    ('AAPL','12','stock','Apple','225','222'),
    ('MSFT','8','stock','Microsoft','420','418'),
    ('NVDA','18','stock','NVIDIA','135','133'),
    ('GOOGL','15','stock','Alphabet Class A','175','174'),
    ('AMZN','10','stock','Amazon','205','203'),
    ('JPM','12','stock','JPMorgan Chase','240','238'),
    ('COST','3','stock','Costco Wholesale','950','944'),
]


def demo_holdings() -> PortfolioSnapshot:
    """Fictional quantities; live imports do not reuse illustrative offline prices."""
    return PortfolioSnapshot(as_of=datetime.now(timezone.utc),source='sample',cash='3250',positions=[
        Position(symbol=symbol,quantity=quantity,asset_type=kind,instrument_name=name)
        for symbol,quantity,kind,name,_,_ in SAMPLE])


def demo_snapshot() -> PortfolioSnapshot:
    snapshot = demo_holdings()
    snapshot.source = 'demonstration'
    for position, (_,_,_,_,price,previous) in zip(snapshot.positions,SAMPLE):
        from decimal import Decimal
        position.price, position.previous_close = Decimal(price),Decimal(previous)
    return snapshot
