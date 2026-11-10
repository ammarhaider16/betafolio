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


def clean_narrative(narrative, sources, field_names):
    """Replace exact implementation tokens without asking a model to rewrite analysis."""
    def clean(text):
        for source in sources:
            if source.id not in internal_references(
                ResearchNarrative(executive_summary=text), [source], set()
            ):
                continue
            replacement = source.publisher or 'the cited publication'
            token = r'(?<!\w)' + re.escape(source.id) + r'(?!\w)'
            pattern = (r'(?:source[\s_-]*(?:id)?\s*[:#]?\s*)?' + token if len(source.id) >= 12
                       else r'(?:source[\s_-]*(?:id)?\s*[:#]?\s*|(?<=\[))' + token)
            text = re.sub(pattern,
                          lambda _: replacement, text, flags=re.I)
        for name in sorted(field_names, key=len, reverse=True):
            if '_' in name:
                text = re.sub(r'(?<!\w)' + re.escape(name) + r'(?!\w)',
                              lambda _: name.replace('_', ' '), text, flags=re.I)
        return text

    return ResearchNarrative(
        executive_summary=clean(narrative.executive_summary),
        sections=[{'heading':clean(s.heading), 'content':clean(s.content)} for s in narrative.sections],
        suggestions=[clean(s) for s in narrative.suggestions],
    )


def narrative_from_output(output):
    """Accept a validated result or an exact JSON briefing, never a headline fallback."""
    value = output.get('structured_response')
    if value is not None:
        return ResearchNarrative.model_validate(value)
    message = next((m for m in reversed(output.get('messages', [])) if m.type == 'ai'), None)
    if message:
        for call in message.tool_calls:
            if call.get('name') == 'ResearchNarrative':
                return ResearchNarrative.model_validate(call['args'])
        content = message.content
        if isinstance(content, str):
            raw = content.strip()
            if raw.startswith('```json') and raw.endswith('```'):
                raw = raw[7:-3].strip()
            if raw.startswith('{'):
                return ResearchNarrative.model_validate_json(raw)
    raise ValueError('The model did not return a structured briefing')


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
    state = {"repairs": 0, "formatting": False}
    counted_usage = set()
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
        if last or state["formatting"]:
            call = call.override(
                tools=[],
                system_message=SystemMessage(
                    content=call.system_message.content + "\n\n" + FINALIZE_PROMPT
                ),
            )
        await runtime.emit("analyst", "started")
        runtime.budget.pop('output_finish_reason', None)
        response = await handler(call)
        for message in getattr(response, 'result', []) or []:
            metadata = getattr(message, 'response_metadata', {})
            finish = metadata.get('finish_reason') or metadata.get('stop_reason')
            if finish in {'stop', 'length', 'max_tokens', 'tool_calls', 'end_turn'}:
                runtime.budget['output_finish_reason'] = finish
            usage = getattr(message, 'usage_metadata', None)
            identifier = getattr(message, 'id', None)
            if usage and (not identifier or identifier not in counted_usage):
                if identifier:
                    counted_usage.add(identifier)
                for name in ('input_tokens', 'output_tokens'):
                    if isinstance(usage.get(name), int):
                        runtime.budget[name] = runtime.budget.get(name, 0) + usage[name]
                if all(isinstance(usage.get(name), int) for name in ('input_tokens','output_tokens')):
                    runtime.budget['usage_reports'] = runtime.budget.get('usage_reports',0)+1
        if response.structured_response is not None:
            narrative = ResearchNarrative.model_validate(response.structured_response)
            if narrative.executive_summary or narrative.sections:
                drafts.append(clean_narrative(narrative, runtime.sources, field_names))
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
            try:
                narrative = narrative_from_output(output)
            except ValueError:
                # A completed prose draft can be reformatted without replaying all source text
                # or performing the research again. There is only one such recovery attempt.
                last = next((m for m in reversed(output.get('messages', [])) if m.type == 'ai'), None)
                draft = last.content if last else ''
                if not draft or runtime.remaining() <= runtime.reserve:
                    raise
                await runtime.emit('briefing_format', 'started')
                state['formatting'] = True
                repaired = await agent.ainvoke(
                    {'messages':[{'role':'user', 'content':json.dumps({
                        'task':'Format this existing analyst draft as ResearchNarrative. Preserve supported '
                               'analysis and amounts. Do not add claims or perform more research.',
                        'draft':draft})}]},
                    config={'recursion_limit':12},
                )
                narrative = narrative_from_output(repaired)
                await runtime.emit('briefing_format', 'completed')
            leaked = internal_references(narrative, runtime.sources, field_names)
            if leaked:
                await runtime.emit("briefing_cleanup", "started")
                narrative = clean_narrative(narrative, runtime.sources, field_names)
                await runtime.emit("briefing_cleanup", "completed")
    except asyncio.CancelledError:
        raise
    except Exception as error:
        runtime.budget["model_failure"] = safe_error_reason(error)
        runtime.budget['model_error_type'] = type(error).__name__
        if runtime.budget.get('output_finish_reason') in {'length', 'max_tokens'}:
            runtime.budget['model_failure'] = 'The model reached its output length limit before completing the briefing.'
        if state['formatting']:
            await runtime.emit('briefing_format', 'failed', reason=runtime.budget['model_failure'])
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
