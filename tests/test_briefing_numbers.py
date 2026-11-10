from betafolio.delivery import render_report
import pytest


@pytest.mark.parametrize('stored_percentage', [False, True])
@pytest.mark.parametrize('price,contribution,expected', [
    ('110', '20', '+10.00%; value change +$20.00'),
    ('90', '-20', '-10.00%; value change -$20.00'),
    ('100', '0', '0.00%; value change $0.00'),
])
def test_report_numbers_and_saved_report_compatibility(stored_percentage, price, contribution, expected):
    row = {'symbol': 'ABC', 'price': price, 'previous_close': '100', 'current_session': True}
    if stored_percentage:
        row['daily_change_pct'] = float(price) - 100
    _, text = render_report({'metrics': {
        'position_details': [row], 'daily_price_contributions': {'ABC': contribution},
    }})
    assert expected in text


def test_explicit_missing_percentage_does_not_fall_back_to_saved_prices():
    _, text = render_report({'metrics': {'position_details': [{
        'symbol': 'ABC', 'price': '110', 'previous_close': '100',
        'current_session': False, 'daily_change_pct': None,
    }], 'daily_price_contributions': {'ABC': '20'}}})
    assert 'day change Unavailable' in text
    assert '+10.00%' not in text and '+$20.00' not in text


def test_model_financial_facts_preserve_scale_sign_and_missing_amounts():
    from betafolio.metrics import briefing_financial_facts
    facts = briefing_financial_facts({
        'known_value': '150200000', 'cash': '0', 'daily_price_change_estimate': '150200',
        'position_values': {'ABC': '601000', 'XYZ': '900000'},
        'daily_price_contributions': {'ABC': '-60100.125'},
        'position_details': [
            {'symbol': 'ABC', 'price': '12.345', 'previous_close': '13', 'daily_change_pct': -5.04},
            {'symbol': 'XYZ', 'price': None, 'daily_change_pct': None},
        ],
    })
    assert facts['known_portfolio_value'] == '$150,200,000.00'
    assert facts['holdings_price_change_estimate'] == '+$150,200.00'
    assert facts['cash'] == '$0.00'
    assert facts['positions']['ABC']['holding_price_contribution'] == '-$60,100.12'
    assert facts['positions']['XYZ']['holding_price_contribution'] is None
    assert facts['quotes'][0]['price_per_share'] == '$12.34'
    assert facts['quotes'][0]['security_price_move'] == '-5.04%'
    assert facts['quotes'][1]['price_per_share'] is None
    assert facts['quotes'][1]['security_price_move'] is None
    assert facts['reporting_currency'] == 'USD'
    assert facts['daily_change_complete'] is False


async def test_internal_reference_cleanup_preserves_report_without_a_second_model_call():
    import json
    from datetime import datetime, timezone
    import httpx
    from langchain_openai import ChatOpenAI
    from betafolio.agent import generate_narrative
    from betafolio.models import AnalysisRequest, PortfolioSnapshot, Evidence

    calls = []
    source_id = '4473d11b4841bf31'
    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        summary = f'daily_price_contributions show +$150.22 (source ID {source_id}).'
        return httpx.Response(200, json={
            'id': 'reply', 'object': 'chat.completion', 'created': 1, 'model': 'test',
            'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': str(len(calls)), 'type': 'function', 'function': {
                        'name': 'ResearchNarrative', 'arguments': json.dumps({
                            'executive_summary': summary, 'sections': [], 'suggestions': []})}}]}}]})

    request = AnalysisRequest(snapshot=PortfolioSnapshot(as_of=datetime.now(timezone.utc)))
    sources = [Evidence(id=source_id, title='Market news', publisher='Newsroom',
                        url='https://example.com/news', excerpt='Relevant developments.')]
    budget = {}
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = ChatOpenAI(model='test', api_key='test', http_async_client=client, max_retries=0)
        narrative = await generate_narrative(model, request, {
            'daily_price_contributions': {'ABC': '150.22'}, 'known_value': '20033.06',
        }, sources, budget=budget)
    assert len(calls) == 1
    initial = json.loads(calls[0]['messages'][1]['content'])
    assert 'daily price contributions' in initial['portfolio_measures']
    assert 'daily_price_contributions' not in initial['portfolio_measures']
    assert initial['reference_material']['news'][0]['id_usage'].startswith('Tool handle')
    assert source_id not in narrative.executive_summary
    assert 'daily_price_contributions' not in narrative.executive_summary
    assert '+$150.22' in narrative.executive_summary and 'Newsroom' in narrative.executive_summary
    assert budget['analysis_written'] and 'model_failure' not in budget


def test_internal_reference_matching_preserves_ordinary_prose_and_checks_all_sections():
    from betafolio.agent import internal_references
    from betafolio.models import Evidence, ResearchNarrative, ReportSection
    sources = [Evidence(id='one', title='News', publisher='Newsroom', url='https://example.com')]
    clean = ResearchNarrative(executive_summary='One holding gained; Newsroom reported earnings.')
    assert internal_references(clean, sources, {'daily_price_contributions'}) == []
    dirty = ResearchNarrative(sections=[ReportSection(heading='daily_price_contributions',
                              content='Newsroom reported earnings (source ID one).')])
    assert internal_references(dirty, sources, {'daily_price_contributions'}) == ['daily_price_contributions', 'one']


def test_briefing_cleanup_and_exact_json_recovery_preserve_analysis():
    from langchain_core.messages import AIMessage
    from betafolio.agent import clean_narrative, narrative_from_output
    from betafolio.models import Evidence, ResearchNarrative
    source = Evidence(id='one', title='News', publisher='Newsroom', url='https://example.com')
    draft = ResearchNarrative(executive_summary='One holding rose +$150.22 (source ID one).')
    cleaned = clean_narrative(draft, [source], set())
    assert cleaned.executive_summary == 'One holding rose +$150.22 (Newsroom).'
    assert narrative_from_output({'messages':[AIMessage(content=cleaned.model_dump_json())]}) == cleaned
    with pytest.raises(ValueError):
        narrative_from_output({'messages':[AIMessage(content='An incomplete, unstructured draft')]})
