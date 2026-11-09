"""An autonomous analyst; the application supplies data and renders its chosen report."""

import asyncio
import json

from .models import AnalysisRequest, ResearchNarrative
from .prompts import analyst_prompt, FINALIZE_PROMPT


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
    state = {"repairs": 0}

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
        if last:
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
        "portfolio_measures": metrics,
        "reference_material": {"news": [runtime.present(source) for source in sources]},
    }
    try:
        async with asyncio.timeout(runtime.remaining()):
            output = await agent.ainvoke(
                {"messages": [{"role": "user", "content": json.dumps(model_data(payload))}]},
                config={"recursion_limit": request.max_model_calls * 4 + 8},
            )
        narrative = ResearchNarrative.model_validate(output["structured_response"])
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
