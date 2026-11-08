"""Search HTTP contracts and prepared content, without paid requests."""
from datetime import datetime, timezone
import json

import httpx
import pytest

from betafolio import providers


@pytest.fixture
def transport(monkeypatch):
    client = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))

    return install


@pytest.mark.parametrize("name", ["tavily", "exa", "perplexity"])
@pytest.mark.parametrize("empty", [False, True])
async def test_search_request_and_content_contract(transport, name, empty):
    classes = {"tavily": providers.TavilyResearchProvider, "exa": providers.ExaResearchProvider,
               "perplexity": providers.PerplexityResearchProvider}
    cutoff = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)

    def respond(request):
        payload = json.loads(request.content)
        assert request.url == f"https://api.{name}.{'com' if name == 'tavily' else 'ai'}/search"
        assert payload["query"] == "AAPL supply chain exposure"
        if name == "exa":
            assert request.headers["x-api-key"] == "secret"
            assert payload["contents"] == {"highlights": True}
            assert payload["endPublishedDate"] == cutoff.isoformat()
            row = {"highlights": ["Extracted passage.", "More context."], "publishedDate": "2026-11-06T12:00:00Z"}
        elif name == "perplexity":
            assert request.headers["authorization"] == "Bearer secret"
            assert payload["max_tokens_per_page"] == 2048
            assert payload["search_before_date_filter"] == "11/07/2026"
            row = {"snippet": "Extracted passage.", "date": "2026-11-06", "last_updated": "2026-11-07"}
        else:
            assert payload["api_key"] == "secret"
            assert payload["include_raw_content"] == "text"
            assert payload["end_date"] == "2026-11-07"
            row = {"raw_content": "Extracted passage.", "content": "Short summary.", "published_date": "2026-11-06"}
        return httpx.Response(200, json={"results": [] if empty else [dict(row, url="https://example.com/story", title="News")]})

    transport(respond)
    items = await classes[name]("secret").search_events("AAPL supply chain exposure", reporting_cutoff=cutoff)
    if empty:
        assert items == []
    else:
        assert items[0].excerpt.startswith("Extracted passage.")
        assert items[0].published_at.date().isoformat() == "2026-11-06"
        assert items[0].content_type == ("retrieved_page" if name == "tavily" else "provider_excerpt")


@pytest.mark.parametrize("provider", [providers.ExaResearchProvider, providers.PerplexityResearchProvider])
@pytest.mark.parametrize("failure", [401, 429, "timeout", "malformed"])
async def test_probe_failures_are_not_successful_empty_searches(transport, provider, failure):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("secret provider response", request=request)
        if failure == "malformed":
            return httpx.Response(200, json={"error": "secret provider response"})
        return httpx.Response(failure, text="secret provider response")

    transport(respond)
    outcomes = await providers.probe_research_sources(provider("private-key"))
    assert outcomes["search"]["status"] == "failed"
    assert "secret" not in str(outcomes) and "private-key" not in str(outcomes)


@pytest.mark.parametrize("provider", [providers.ExaResearchProvider, providers.PerplexityResearchProvider])
async def test_update_date_is_not_a_publication_date(transport, provider):
    transport(lambda request: httpx.Response(200, json={"results": [
        {"url": "https://example.com/story", "title": "Undated", "last_updated": "2026-11-07"}
    ]}))
    source = (await provider("key").search("background research"))[0]
    assert source.published_at is None
    assert source.content_type == "search_excerpt"
