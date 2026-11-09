"""An autonomous analyst; the application supplies data and renders its chosen report."""

import asyncio
import json
import re

from .models import AnalysisRequest, ResearchNarrative
from .metrics import briefing_financial_facts
from .prompts import analyst_prompt, FINALIZE_PROMPT


def readable_labels(value):
    """Translate application field names without changing values or ticker symbols."""
    if isinstance(value, dict):
        return {(key.replace('_', ' ') if key.islower() else key): readable_labels(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [readable_labels(item) for item in value]
    return value


def internal_references(narrative, sources, field_names):
    """Match known implementation tokens, not ordinary financial claims."""
    text = '\n'.join([narrative.executive_summary, *narrative.suggestions,
                      *(part for section in narrative.sections for part in (section.heading, section.content))])
    matches = set()
    for name in field_names:
        if '_' in name and re.search(r'(?<![\w])' + re.escape(name) + r'(?![\w])', text, re.I):
            matches.add(name)
    for source in sources:
        token = re.escape(source.id)
        # Short, word-like handles can also be ordinary prose (e.g. "one").
        pattern = (r'(?<![\w])' + token + r'(?![\w])' if len(source.id) >= 12 else
                   r'(?:source[\s_-]*(?:id)?\s*[:#]?\s*[`\[(]*|\[)' + token + r'(?![\w])')
        if re.search(pattern, text, re.I):
            matches.add(source.id)
    return sorted(matches)


def input_field_names(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(input_field_names(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(input_field_names(item) for item in value), set())
    return set()


async def generate_narrative(
    model,
    request,
    metrics,
    sources,
    *,
    research=None,
    market_data=None,
    activity_sink=None,
    deadline=None,
    activity=None,
    budget=None,
    clock=None,
):
    from langchain.agents import create_agent
    from langchain_core.messages import SystemMessage
    from langchain.agents.middleware import wrap_model_call
    from langchain.agents.structured_output import ToolStrategy
    from .research_runtime import ResearchRuntime, ResearchBudgetExceeded
    from .service import safe_error_reason
    from .research_clock import ResearchClock, model_data

    clock = clock or ResearchClock.for_request(request)

    runtime = ResearchRuntime(
        request,
        sources,
        research=research,
        market_data=market_data,
        deadline=deadline,
        activity=activity,
        budget=budget,
        activity_sink=activity_sink,
        clock=clock,
    )
    drafts = []
    state = {"repairs": 0, "rewriting": False}
    field_names = input_field_names(metrics) | input_field_names(briefing_financial_facts(metrics))

    def repair(error):
        state["repairs"] += 1
        if state["repairs"] > 2:
            raise ValueError("Briefing format could not be read")
        return (
            "Return the briefing with executive_summary as text and sections as an array of "
            '{"heading":"...","content":"..."}. Keep your analysis; correct only the format.'
        )

    @wrap_model_call
    async def observe(call, handler):
        last = (
            runtime.budget["model_calls"] + 1 >= request.max_model_calls
            or runtime.remaining() <= runtime.reserve
        )
        if runtime.budget["model_calls"] >= request.max_model_calls:
            raise ResearchBudgetExceeded("Finish with the information already collected")
        runtime.budget["model_calls"] += 1
        if last or state["rewriting"]:
            call = call.override(
                tools=[],
                system_message=SystemMessage(
                    content=call.system_message.content + "\n\n" + FINALIZE_PROMPT
                ),
            )
        await runtime.emit("analyst", "started")
        response = await handler(call)
        if response.structured_response is not None:
            narrative = ResearchNarrative.model_validate(response.structured_response)
            if not internal_references(narrative, runtime.sources, field_names):
                drafts.append(narrative)
        return response

    agent = create_agent(
        model,
        tools=runtime.tools(),
        middleware=[observe],
        response_format=ToolStrategy(ResearchNarrative, handle_errors=repair),
        system_prompt=analyst_prompt(request.report_length, has_question=bool(request.question.strip())),
    )
    payload = {
        "portfolio": request.snapshot.model_dump(mode="json", exclude={"account_id"}),
        "question": request.question,
        "report_length": request.report_length,
        "investment_profile": request.investment_profile,
        "include_suggestions": request.include_suggestions,
        "report_date": metrics.get("price_session"),
        "research_as_of": clock.now.isoformat(),
        "news_cutoff": clock.cutoff(request).isoformat(),
        "max_data_age_hours": request.max_data_age_hours,
        "research_available": research is not None and request.max_searches > 0,
        "portfolio_measures": readable_labels(model_data(metrics)),
        "financial_facts": readable_labels(briefing_financial_facts(metrics)),
        "reference_material": {"news": [runtime.present(source) for source in sources]},
    }
    field_names |= input_field_names(payload)
    try:
        async with asyncio.timeout(runtime.remaining()):
            output = await agent.ainvoke(
                {"messages": [{"role": "user", "content": json.dumps(model_data(payload))}]},
                config={"recursion_limit": request.max_model_calls * 4 + 8},
            )
            narrative = ResearchNarrative.model_validate(output["structured_response"])
            leaked = internal_references(narrative, runtime.sources, field_names)
            if leaked:
                await runtime.emit("briefing_cleanup", "started")
                state["rewriting"] = True
                rewrite = await agent.ainvoke(
                    {"messages": [*output["messages"], SystemMessage(content=
                        "Revise the briefing once for reader-facing presentation only. Preserve supported analysis, "
                        "amounts, qualifications and your chosen structure. Remove internal field names and tool "
                        "handles; use ordinary financial language and publisher names where useful. Do not explain "
                        "the cleanup or include the following internal tokens: " + json.dumps(leaked))]},
                    config={"recursion_limit": request.max_model_calls * 4 + 8},
                )
                narrative = ResearchNarrative.model_validate(rewrite["structured_response"])
                if internal_references(narrative, runtime.sources, field_names):
                    raise ValueError("Briefing still contains internal research references")
                await runtime.emit("briefing_cleanup", "completed")
    except asyncio.CancelledError:
        raise
    except Exception as error:
        runtime.budget["model_failure"] = safe_error_reason(error)
        await runtime.emit("analyst", "failed", reason=runtime.budget["model_failure"])
        narrative = drafts[-1] if drafts else ResearchNarrative()
    runtime.budget["analysis_written"] = bool(narrative.executive_summary or narrative.sections)
    if runtime.budget["analysis_written"]:
        await runtime.emit("analyst", "completed")
    return narrative


def create_research_tool(dependencies=None):
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel
    from .engine import analyze_portfolio

    class ToolInput(BaseModel):
        request: AnalysisRequest

    async def research(request: AnalysisRequest) -> dict:
        return (await analyze_portfolio(AnalysisRequest.model_validate(request), dependencies)).model_dump(
            mode="json"
        )

    return StructuredTool.from_function(
        coroutine=research,
        name="research_portfolio",
        description="Research a portfolio and return an executive briefing, current position changes and relevant news. Read only.",
        args_schema=ToolInput,
    )
