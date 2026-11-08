"""Real preflight policy with deterministic provider probes and no network."""
import pytest

from betafolio import providers, service
from betafolio.config import ModelConfig, Settings, save_settings
from betafolio.secrets import SecretStore


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(SecretStore, 'get', lambda self, name: 'test-key' if name == 'model_api_key_custom' else None)
    save_settings(tmp_path, Settings(model_enabled=True, model=ModelConfig(model='test-model')))
    return service.ApplicationService(tmp_path)


async def test_preflight_checks_once_and_rechecks_changed_model(configured, monkeypatch):
    calls = []

    async def model(*args, **kwargs):
        calls.append('model')
        return {'ok': True, 'steps': [], 'detail': 'Ready'}

    async def sources(*args, **kwargs):
        calls.append('sources')
        # Empty successful search still proves a usable news connection.
        return {'search': {'status': 'empty', 'reason': ''}}

    monkeypatch.setattr(service, 'probe_model_readiness', model)
    monkeypatch.setattr(providers, 'probe_research_sources', sources)
    await configured.ensure_research_ready()
    await configured.ensure_research_ready()
    assert calls == ['model', 'sources']
    assert configured.research_readiness()['state'] == 'ready'
    settings = configured.settings
    settings.model.model = 'another-model'
    save_settings(configured.directory, settings)
    assert configured.research_readiness()['state'] == 'needs_check'
    await configured.ensure_research_ready()
    assert calls == ['model', 'sources', 'model']


async def test_failing_model_blocks_sources_and_run_creation(configured, monkeypatch):
    calls = []

    async def model(*args, **kwargs):
        calls.append('model')
        return {'ok': False, 'steps': [], 'detail': 'Authentication failed'}

    async def sources(*args, **kwargs):
        pytest.fail('News checks must not run after model failure')

    monkeypatch.setattr(service, 'probe_model_readiness', model)
    monkeypatch.setattr(providers, 'probe_research_sources', sources)
    for _ in range(2):
        with pytest.raises(ValueError):
            await configured.ensure_research_ready()
    with pytest.raises(ValueError):
        configured.submit()
    assert calls == ['model']
    assert configured.store.history() == []


@pytest.mark.parametrize("name", ["exa", "perplexity"])
async def test_search_key_selection_and_revision(configured, monkeypatch, name):
    from betafolio.connections import cached

    keys = {"tavily_api_key": "unrelated", "model_api_key_custom": "model-key"}
    monkeypatch.setattr(SecretStore, "get", lambda self, slot: keys.get(slot))
    settings = configured.settings
    settings.search_provider = name
    settings.primary_feeds_enabled = settings.ticker_news_enabled = False
    save_settings(configured.directory, settings)
    result = await configured.test_connection("search")
    assert not result["ok"]
    assert "API key" in result["sources"]["search"]["reason"]
    keys[f"{name}_api_key"] = "selected-key"

    async def probe(research, **kwargs):
        selected = providers.research_tool_sources(research)["search"]
        assert selected.api_key == "selected-key"
        assert isinstance(selected, service.SEARCH_PROVIDERS[name][1])
        return {"search": {"status": "empty", "reason": ""}}

    monkeypatch.setattr(providers, "probe_research_sources", probe)
    assert (await configured.test_connection("search"))["ok"]
    assert cached(configured, "search")
    keys["tavily_api_key"] = "unrelated-change"
    assert cached(configured, "search")
    keys[f"{name}_api_key"] = "replacement"
    assert not cached(configured, "search")
