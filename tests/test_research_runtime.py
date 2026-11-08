"""The tools supply prepared context and preserve an observable run trace."""

import asyncio
import pytest
from datetime import datetime, timezone
from betafolio.models import AnalysisRequest, PortfolioSnapshot, Evidence
from betafolio.research_runtime import ResearchRuntime


def test_news_tool_prepares_article_content_without_a_grade_checklist(monkeypatch):
    class Search:
        async def search(self, *args, **kwargs):
            return [
                Evidence(
                    id="one",
                    title="Company expands investment",
                    url="https://news.example/article",
                    publisher="Newsroom",
                    excerpt="Publisher summary.",
                )
            ]

    async def retrieve(url):
        return "The company announced a new investment in production."

    monkeypatch.setattr("betafolio.providers.retrieve_public_text", retrieve)

    async def exercise():
        runtime = ResearchRuntime(
            AnalysisRequest(snapshot=PortfolioSnapshot(as_of=datetime.now(timezone.utc))),
            [],
            research=Search(),
        )
        result = await runtime.tools()[0].ainvoke({"query": "company investment"})
        assert result[0]["content"] == "The company announced a new investment in production."
        assert "supports_factual_findings" not in result[0] and "content_type" not in result[0]
        assert [event["status"] for event in runtime.activity] == ["started", "passed"]
        assert {tool.name for tool in runtime.tools()} == {"search_news", "read_article", "get_market_data"}

    asyncio.run(exercise())


@pytest.mark.parametrize("content_type", ["provider_excerpt", "retrieved_page"])
async def test_prepared_content_is_not_fetched_again(monkeypatch, content_type):
    async def forbidden(*args):
        pytest.fail("Prepared provider content must not trigger automatic page fetching")

    monkeypatch.setattr("betafolio.providers.retrieve_public_text", forbidden)
    source = Evidence(id="one", url="https://example.com/story", title="News", publisher="Publisher",
                      excerpt="Provider-prepared context.", content_type=content_type)
    runtime = ResearchRuntime(AnalysisRequest(snapshot=PortfolioSnapshot(as_of=datetime.now(timezone.utc))), [])
    result = await runtime.prepare_news([source])
    assert result[0]["content"] == source.excerpt
    # A later metadata-only duplicate cannot discard already available context.
    runtime.register([source.model_copy(update={"id": "duplicate", "excerpt": "Short", "content_type": "search_excerpt"})])
    assert runtime.sources[0].excerpt == source.excerpt
    if content_type == "retrieved_page":
        result = await runtime.tools()[1].ainvoke({"source_id": "one"})
        assert result[0]["content"] == source.excerpt


async def test_explicit_read_expands_provider_excerpt_and_retains_it_on_failure(monkeypatch):
    source = Evidence(id="one", url="https://example.com/story", title="News", publisher="Publisher",
                      excerpt="Useful excerpt.", content_type="provider_excerpt")
    runtime = ResearchRuntime(AnalysisRequest(snapshot=PortfolioSnapshot(as_of=datetime.now(timezone.utc))), [source])

    async def broken(*args):
        raise ValueError("private error")

    monkeypatch.setattr("betafolio.providers.retrieve_public_text", broken)
    result = await runtime.tools()[1].ainvoke({"source_id": "one"})
    assert result["source"]["content"] == source.excerpt
    assert "private error" not in str(result)

    async def full(*args):
        return "The full article adds useful details."

    monkeypatch.setattr("betafolio.providers.retrieve_public_text", full)
    result = await runtime.tools()[1].ainvoke({"source_id": "one"})
    assert result[0]["content"] == "The full article adds useful details."
    assert runtime.sources[0].content_type == "retrieved_page"
