"""Provider construction and operator-only connection probes."""
import asyncio
from datetime import datetime, timezone
from .engine import EngineDependencies
from .providers import (CompositeResearchProvider, DDGSResearchProvider, ExaResearchProvider,
    PerplexityResearchProvider, PrimaryFeedResearchProvider, SecFilingsResearchProvider,
    TavilyResearchProvider, TickerNewsFeedProvider, YFinanceMarketDataProvider)
MODEL_PROVIDER_SLOTS = {'openai':'model_api_key_openai','anthropic':'model_api_key_anthropic','custom':'model_api_key_custom'}
SEARCH_PROVIDERS = {'tavily':('tavily_api_key',TavilyResearchProvider),
    'exa':('exa_api_key',ExaResearchProvider),'perplexity':('perplexity_api_key',PerplexityResearchProvider)}
READINESS_TOOL_PROMPT = 'Call the record_probe_note tool with the note ready.'

def read_secret(store, name):
    return store.get(name)

def resolve_model_key(settings, store):
    slot = MODEL_PROVIDER_SLOTS[settings.model.provider]
    return store.get(slot), 'encrypted', slot

class ConnectionService:
    def __init__(self, database):
        from .production import settings
        self.settings = settings(database).research
        self.secrets = database.credentials
        self.store = database


def safe_error_reason(exc, secret=None):
    """A short, secret-free category describing why a provider call failed."""
    from pydantic import ValidationError
    if isinstance(exc, ValidationError):
        return "the model returned an invalid briefing shape; check structured tool-output support"
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 401:
        return "the endpoint rejected the API key (HTTP 401). Check that the key belongs to the selected provider"
    if status == 403:
        return "the endpoint refused access (HTTP 403). Check the key's permissions and model access"
    if status == 404:
        return "the endpoint or model was not found (HTTP 404). Check the base URL and model identifier"
    if status == 429:
        return "the provider rate limit was reached (HTTP 429)"
    if status == 400:
        body = getattr(exc, "body", {})
        item = body.get("error", body) if isinstance(body, dict) else {}
        parameter = item.get("param") if isinstance(item, dict) else None
        if parameter in {"temperature", "max_tokens", "max_completion_tokens", "reasoning_effort"}:
            return f"the model rejected the {parameter} setting; adjust advanced model settings"
        if isinstance(parameter, str) and parameter.startswith(("tools", "response_format")):
            return "the endpoint rejected the research tool schema; choose a model supporting function tools"
        return "the endpoint rejected the research request (HTTP 400); check model capabilities and settings"
    if isinstance(status, int) and status >= 500:
        return f"the provider returned a server error (HTTP {status})"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in type(exc).__name__.lower():
        return "the provider request timed out; check the connection and retry"
    return "the provider request failed; check the selected model, credentials, and connection"

def create_model(settings, secret_store, api_key=None):
    key = api_key
    if key is None:
        key, _source, _used = resolve_model_key(settings, secret_store)
    if not key:
        raise ValueError("Add a model API key or configure its environment variable")
    cfg = settings.model
    if not cfg.model.strip():
        raise ValueError("Configure a research model before running research")
    selected_model = cfg.model
    base = cfg.base_url
    if cfg.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=selected_model, api_key=key, base_url=base, temperature=cfg.temperature, max_tokens=cfg.max_tokens, timeout=cfg.request_timeout_seconds, max_retries=0)
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=selected_model, api_key=key, base_url=base, temperature=cfg.temperature, max_tokens=cfg.max_tokens, timeout=cfg.request_timeout_seconds, max_retries=0)

def build_research(settings, secret_store, search_key=None):
    fallback = None
    if settings.search_provider in SEARCH_PROVIDERS:
        slot, provider = SEARCH_PROVIDERS[settings.search_provider]
        key = read_secret(secret_store, slot) if search_key is None else search_key
        if key:
            fallback = provider(key)
    elif settings.search_provider == "keyless":
        fallback = DDGSResearchProvider()
    providers = []
    if settings.search_provider != "disabled":
        sec_user_agent = settings.sec_contact or None
        if settings.primary_feeds_enabled:
            providers.append(PrimaryFeedResearchProvider(fallback=fallback, sec_user_agent=sec_user_agent))
        elif fallback is not None:
            providers.append(fallback)
        if settings.sec_company_filings_enabled:
            providers.append(SecFilingsResearchProvider(sec_user_agent=sec_user_agent))
        if settings.ticker_news_enabled:
            providers.append(TickerNewsFeedProvider(sources=settings.news_feed_sources))
    for provider in providers:
        # Contract §5 freshness window: items older than max_data_age_hours drop when a newer
        # item for the same symbol exists (30-day hard rule always applies).
        provider.max_data_age_hours = settings.max_data_age_hours
    return CompositeResearchProvider(providers) if providers else None

def build_dependencies(settings, secret_store, model_key=None, search_key=None):
    model = create_model(settings, secret_store, api_key=model_key)
    if model is None:
        raise ValueError("A research model is required")
    research = build_research(settings, secret_store, search_key=search_key)
    return EngineDependencies(research=research, market_data=YFinanceMarketDataProvider(),
                              history_enabled=True,
                              instrument_resolver=YFinanceMarketDataProvider(refresh_existing=False), model=model)

def probe_tool():
    """A trivial tool whose only purpose is to see whether the model calls tools."""
    from langchain_core.tools import StructuredTool

    def record_probe_note(note: str) -> str:
        """Record one probe note. Call this tool with the note 'ready'."""
        return note

    return StructuredTool.from_function(record_probe_note)

def readiness_ok(tool_calling, structured_output):
    """Pass only after the research call and structured finalization both work."""
    return tool_calling == structured_output == "ok"

def readiness_detail(tool_calling, structured_output, tool_reason="", structured_reason=""):
    """One short, plain-language sentence a normal user can act on; never key material."""
    if tool_calling == "fail" and structured_output == "fail":
        return (f"The model answered neither readiness probe — {tool_reason or structured_reason or 'the model did not answer'}. "
                "Check the model endpoint, API key, and model name before running research.")
    if tool_calling == "fail":
        return (f"The tool-calling check could not complete — {tool_reason or 'the model did not answer'}. "
                "Research runs need tool calls to gather evidence; check the model endpoint, API key, and model name.")
    if structured_output == "fail":
        return (f"The structured-output check failed — {structured_reason or 'the model did not answer'}. "
                "Written briefings need JSON output; prefer a model advertised with JSON output.")
    if tool_calling == "weak" and structured_output == "weak":
        return ("This model is weak at both tool calling and structured output — reports may stay thin and spend calls fixing formatting. "
                "A model advertised with tool calling and JSON output is recommended for research runs.")
    if tool_calling == "weak":
        return "The model did not make the requested tool call. Check tool support for the model and gateway."
    if structured_output == "weak":
        return "The model called the briefing tool but omitted its summary. Choose a model that completes structured tool responses."
    return "The model completed a tool-result round trip and returned the briefing schema. Longer research runs still depend on source access and the time budget."

async def _probe_tool_calling(model):
    try:
        bound = model.bind_tools([probe_tool()], tool_choice="any")
        response = await bound.ainvoke(READINESS_TOOL_PROMPT)
    except Exception as exc:
        return "fail", safe_error_reason(exc), []
    calls = getattr(response, "tool_calls", None) or []
    if len(calls) != 1 or calls[0].get("name") != "record_probe_note" or calls[0].get("args") != {"note": "ready"}:
        return "weak", "", []
    from langchain_core.messages import HumanMessage, ToolMessage
    return "ok", "", [HumanMessage(content=READINESS_TOOL_PROMPT), response,
                         ToolMessage(content="ready", tool_call_id=calls[0]["id"], name="record_probe_note")]

async def _probe_structured_output(model, history):
    from .models import ResearchNarrative
    from langchain_core.messages import HumanMessage
    try:
        # Exercise the same nested output schema, required tool choice and tool-result
        # conversation as the actual agent, rather than an unrelated JSON text reply.
        bound = model.bind_tools([ResearchNarrative], tool_choice="any")
        response = await bound.ainvoke([*history, HumanMessage(content=
            'Finalize with the ResearchNarrative tool. Set executive_summary to "ready", '
            'sections to [], and suggestions to []. No portfolio research is needed.')])
        calls = getattr(response, "tool_calls", None) or []
        if len(calls) != 1 or calls[0].get("name") != "ResearchNarrative":
            return "fail", "the model did not call the required briefing output tool"
        narrative = ResearchNarrative.model_validate(calls[0]["args"])
        if not narrative.executive_summary:
            return "weak", ""
    except Exception as exc:
        return "fail", safe_error_reason(exc)
    return "ok", ""

async def probe_model_readiness(settings, secret_store, model=None, api_key=None):
    """Probe a research tool/result round trip and the actual briefing schema, without portfolio data."""
    steps = [{'id':name,'label':label,'status':'skipped','message':'Not run'} for name,label in
             [('connection','Connection'),('tools','Tool calling'),('briefing','Briefing output')]]
    tool_calling, structured_output = 'fail', 'fail'
    try:
        async with asyncio.timeout(65):
            if model is None:
                model = create_model(settings, secret_store, api_key=api_key)
            await model.ainvoke('Reply with the single word ready.')
            steps[0].update(status='passed',message='The model connection works.')
            tool_calling, reason, history = await _probe_tool_calling(model)
            steps[1].update(status='passed' if tool_calling == 'ok' else 'failed',
                            message='The model can use research tools.' if tool_calling == 'ok' else reason or 'This model did not make the requested tool call. Check tool support.')
            if tool_calling == 'ok':
                structured_output, reason = await _probe_structured_output(model, history)
                steps[2].update(status='passed' if structured_output == 'ok' else 'failed',
                                message='The model can produce a briefing.' if structured_output == 'ok' else reason or 'The model did not return a complete briefing.')
    except Exception as error:
        pending = next((step for step in steps if step['status']=='skipped'),steps[-1])
        pending.update(status='failed',message=safe_error_reason(error,api_key))
    ok = all(step['status']=='passed' for step in steps)
    detail = 'Your model is ready for research.' if ok else next(step['message'] for step in steps if step['status']=='failed')
    return {'ok':ok,'message':detail,'detail':detail,'steps':steps,
            'tool_calling':tool_calling,'structured_output':structured_output,
            'tested_at':datetime.now(timezone.utc).isoformat()}
