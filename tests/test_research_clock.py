"""A selected research clock must reach prices, news and model inputs without live fallback."""
import json
from datetime import datetime, timezone

import httpx
import pandas as pd
import pytest
from langchain_openai import ChatOpenAI

from betafolio.engine import analyze_portfolio, EngineDependencies
from betafolio.models import AnalysisRequest, PortfolioSnapshot, Position, Evidence
from betafolio.providers import YFinanceMarketDataProvider, completed_market_session
from betafolio.research_clock import research_timestamp
from betafolio.research_runtime import ResearchRuntime


@pytest.mark.parametrize('stamp,session', [('2026-10-17T23:00:00+00:00','2026-10-16'),
    ('2026-12-25T23:00:00+00:00','2026-12-24'), ('2026-10-19T12:00:00+00:00','2026-10-16'),
    ('2026-10-19T18:00:00+00:00','2026-10-16')])
def test_historical_daily_quotes_use_completed_sessions(stamp, session):
    assert completed_market_session(datetime.fromisoformat(stamp)) == session


def test_date_timezone_and_conflicting_cutoffs():
    now = datetime(2026, 3, 10, 12, tzinfo=timezone.utc)
    assert research_timestamp('2026-03-08', 'America/New_York', now).isoformat().startswith('2026-03-09T03:59:59')
    assert research_timestamp('2026-03-10', 'America/New_York', now) is None
    with pytest.raises(ValueError):
        research_timestamp('2026-03-11', 'America/New_York', now)
    snapshot = PortfolioSnapshot(as_of=now)
    with pytest.raises(ValueError, match='must agree'):
        AnalysisRequest(snapshot=snapshot, research_as_of='2020-01-02T23:59:00Z', reporting_cutoff='2020-01-01T23:59:00Z')


async def test_yfinance_historical_quotes_and_history_never_request_current_info(monkeypatch):
    calls = []
    class Ticker:
        def history(self, **kwargs):
            calls.append(kwargs)
            return pd.DataFrame({'Close':[99,100,999]}, index=pd.to_datetime(['2020-01-02','2020-01-03','2026-10-01']))
        def get_info(self):
            pytest.fail('Historical prices must never fetch current quote info')
    monkeypatch.setattr('yfinance.Ticker', lambda symbol: Ticker())
    provider = YFinanceMarketDataProvider()
    as_of = datetime(2020,1,3,23,tzinfo=timezone.utc)
    quote = (await provider.historical_quotes(['AAPL'], as_of))['AAPL']
    assert quote['price'] == 100 and quote['previous_close'] == 99
    snapshot = PortfolioSnapshot(as_of=as_of, positions=[Position(symbol='AAPL',quantity=1)])
    history = await provider.historical_history(snapshot, as_of)
    assert history['AAPL'] == {'2020-01-02':99.,'2020-01-03':100.}
    assert all(call['end'] == '2020-01-04' and call['auto_adjust'] is False for call in calls)


async def test_model_and_tools_share_selected_clock_without_execution_date_or_live_fallback(monkeypatch):
    as_of = datetime(2020,1,3,23,tzinfo=timezone.utc)
    wall = datetime.now(timezone.utc)
    seen = []
    class Market:
        async def historical_quotes(self, symbols, timestamp):
            assert timestamp == as_of
            return {'AAPL':{'price':100,'previous_close':99,'trading_date':'2020-01-03',
                            'quote_as_of':None,'retrieved_at':wall.isoformat(),'source':'test'}}
        async def quotes(self, *args):
            pytest.fail('Live quote leak')
        async def resolve_instrument(self, *args):
            pytest.fail('Current identity leak')
    source = Evidence(id='dated',url='https://example.com/article',title='Dated announcement',publisher='Publisher',
                      published_at='2020-01-03T12:00:00Z',excerpt='Relevant announcement.',content_type='provider_excerpt')
    class Search:
        async def search_events(self, query, **kwargs):
            assert kwargs['reporting_cutoff'] == as_of
            return [source, source.model_copy(update={'id':'future','published_at':wall}),
                    source.model_copy(update={'id':'undated','published_at':None}),
                    source.model_copy(update={'id':'updated','updated_at':wall})]
    async def forbidden(*args, **kwargs):
        pytest.fail('Historical article reads cannot fetch today\'s pages')
    monkeypatch.setattr('betafolio.providers.retrieve_public_text', forbidden)

    def respond(request):
        payload = json.loads(request.content)
        seen.append(payload)
        name, args = [('get_market_data',{'symbol':'AAPL'}), ('search_news',{'query':'AAPL outlook','symbol':'AAPL'}),
                      ('ResearchNarrative',{'executive_summary':'Position outlook.','sections':[]})][len(seen)-1]
        return httpx.Response(200,json={'id':'reply','object':'chat.completion','created':1,'model':'test',
            'choices':[{'index':0,'finish_reason':'tool_calls','message':{'role':'assistant','content':None,
                'tool_calls':[{'id':str(len(seen)),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]})
    saved = PortfolioSnapshot(as_of=wall,cash=500,positions=[Position(symbol='AAPL',quantity=2,price=999,
                              price_session=wall.date(),instrument_name='FUTURE_RENAME',sector='FUTURE_SECTOR')])
    request = AnalysisRequest(snapshot=saved,research_as_of=as_of)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = ChatOpenAI(model='test',api_key='test',http_async_client=client,max_retries=0)
        result = await analyze_portfolio(request,EngineDependencies(model=model,market_data=Market(),research=Search()))
    assert result.metrics['known_value'] == '700' and result.research_as_of == as_of
    assert saved.positions[0].price == 999 and saved.as_of == wall
    initial = json.loads(seen[0]['messages'][1]['content'])
    assert initial['financial_facts']['known portfolio value'] == '$700.00'
    assert initial['financial_facts']['positions']['AAPL']['holding value'] == '$200.00'
    assert initial['research_as_of'] == as_of.isoformat() and initial['portfolio']['as_of'].startswith('2020-01-03')
    inputs = json.dumps([message for call in seen for message in call['messages'] if message['role'] in ('user','tool')])
    assert wall.isoformat() not in inputs and 'FUTURE_RENAME' not in inputs and 'FUTURE_SECTOR' not in inputs
    assert 'historical' not in inputs.lower() and 'simulation' not in inputs.lower()
    assert len(result.sources) == 1 and result.sources[0].id == 'dated'
    runtime = ResearchRuntime(request, result.sources)
    assert (await runtime.tools()[1].ainvoke({'source_id':'dated'}))[0]['content'] == 'Relevant announcement.'


async def test_missing_historical_capability_does_not_reuse_saved_prices():
    result = await analyze_portfolio(AnalysisRequest(research_as_of='2020-01-03T23:00:00Z',
        snapshot=PortfolioSnapshot(as_of=datetime.now(timezone.utc),cash=100,
                                   positions=[Position(symbol='AAPL',quantity=2,price=999)])), EngineDependencies(market_data=object(), offline=True))
    assert result.metrics['known_value'] == '100'
    assert result.metrics['position_details'][0]['price'] is None
