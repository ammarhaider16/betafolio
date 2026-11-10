"""Business-level analysis checks; report wording belongs to the analyst."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from betafolio.engine import EngineDependencies, analyze_portfolio
from betafolio.models import (
    AnalysisRequest,
    PortfolioSnapshot,
    Position,
    ResearchNarrative,
    ReportSection,
)


def snapshot():
    return PortfolioSnapshot(
        as_of=datetime.now(timezone.utc), cash=100, positions=[Position(symbol="AAPL", quantity=2, price=100)]
    )


def test_offline_portfolio_keeps_holdings_and_cash():
    request = AnalysisRequest(snapshot=snapshot())
    result = asyncio.run(analyze_portfolio(request, EngineDependencies(offline=True)))
    assert result.metrics["known_value"] == "300"
    assert result.executive_summary and request.snapshot.positions[0].quantity == 2


def test_bad_market_data_cannot_poison_saved_values():
    attempted = []

    class Market:
        async def enrich(self, value):
            attempted.append(True)
            value.positions[0].price = Decimal("NaN")
            return value

    result = asyncio.run(
        analyze_portfolio(AnalysisRequest(snapshot=snapshot()), EngineDependencies(market_data=Market(), offline=True))
    )
    assert result.metrics["known_value"] == "300" and result.status != "failed"
    assert attempted and "saved values are shown" in " ".join(result.warnings)


def test_analyst_report_is_not_rewritten_or_rejected_for_missing_citations(monkeypatch):
    async def narrative(*args, **kwargs):
        return ResearchNarrative(
            executive_summary="The fund mix matters more than one headline.",
            sections=[
                ReportSection(
                    heading="A connected view", content="The positions share an exposure worth watching."
                )
            ],
        )

    monkeypatch.setattr("betafolio.agent.generate_narrative", narrative)
    result = asyncio.run(
        analyze_portfolio(AnalysisRequest(snapshot=snapshot()), EngineDependencies(model=object()))
    )
    assert result.executive_summary == "The fund mix matters more than one headline."
    assert result.sections[0].heading == "A connected view"


def test_briefing_that_skips_available_research_is_marked_incomplete(monkeypatch):
    async def narrative(*args, **kwargs):
        kwargs["budget"]["analysis_written"] = True
        return ResearchNarrative(executive_summary="Portfolio arithmetic alone.")

    monkeypatch.setattr("betafolio.agent.generate_narrative", narrative)
    result = asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=snapshot()),
                                         EngineDependencies(model=object(), research=object())))
    assert result.status == "partial"
    assert "Current news was not researched" in " ".join(result.warnings)


def test_model_free_research_requires_explicit_offline_mode():
    result = asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=snapshot()), EngineDependencies()))
    assert result.status == 'failed'
    assert not result.executive_summary and not result.sections
    assert 'configured model' in result.error


def test_retired_toggle_cannot_disable_configured_model(monkeypatch):
    from betafolio.config import Settings
    from betafolio.service import build_dependencies
    cfg = Settings.model_validate({'model_enabled': False, 'model': {'model': 'configured'}})
    model = object()
    monkeypatch.setattr('betafolio.service.create_model', lambda *a, **kw: model)
    monkeypatch.setattr('betafolio.service.build_research', lambda *a, **kw: None)
    assert build_dependencies(cfg, object()).model is model
    assert 'model_enabled' not in cfg.model_dump()
    assert cfg.model.temperature == 1
