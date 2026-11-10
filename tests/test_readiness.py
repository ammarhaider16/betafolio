"""FR-15 model readiness probe and endpoint checks; faked chat model, no network."""
import asyncio

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from betafolio.service import probe_model_readiness, readiness_ok




class FakeChatModel(BaseChatModel):
    """Chat-model double returning canned tool calls, canned text, or errors."""
    script: list
    seen: list = Field(default_factory=list)
    tool_schemas: list = Field(default_factory=list)
    cursor: int = 0

    @property
    def _llm_type(self):
        return "fake-readiness-model"

    def bind_tools(self, tools, **kwargs):
        self.tool_schemas.append(list(tools))
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        step = self.script[self.cursor]
        self.cursor += 1
        if step.get("error") is not None:
            raise step["error"]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content=step.get("content", ""), tool_calls=step.get("tool_calls") or []))])


def tool_call():
    return [{"name": "record_probe_note", "args": {"note": "ready"}, "id": "probe-1", "type": "tool_call"}]


def run_probe(script):
    model = FakeChatModel(script=script)
    return asyncio.run(probe_model_readiness(None, None, model=model)), model






def final_call(summary="ready"):
    return [{"name": "ResearchNarrative", "args": {"executive_summary": summary,
             "sections": [], "suggestions": []}, "id": "final", "type": "tool_call"}]


def test_readiness_uses_actual_briefing_schema_and_tool_result_round_trip():
    result, model = run_probe([{"content":"ready"}, {"tool_calls": tool_call()}, {"tool_calls": final_call()}])
    assert result["ok"] is True
    assert result["tool_calling"] == result["structured_output"] == "ok"
    assert model.tool_schemas[0][0].name == "record_probe_note"
    assert model.tool_schemas[1][0].__name__ == "ResearchNarrative"
    assert any(message.type == "tool" and message.tool_call_id == "probe-1"
               for messages in model.seen for message in messages)
    assert all("AAPL" not in str(messages) for messages in model.seen)


def test_readiness_rejects_json_text_that_never_calls_finalize_tool():
    result, _ = run_probe([{"content":"ready"}, {"tool_calls": tool_call()}, {"content": '{"summary":"ready","ok":true}'}])
    assert result["tool_calling"] == "ok"
    assert result["structured_output"] == "fail"
    assert result["ok"] is False


@pytest.mark.parametrize("calls", [[], [{"name":"unrelated","args":{},"id":"bad","type":"tool_call"}]])
def test_readiness_does_not_accept_an_unrelated_tool_call(calls):
    result, _ = run_probe([{"content":"ready"}, {"tool_calls": calls}, {"tool_calls": final_call()}])
    assert result["tool_calling"] == "weak"


def test_readiness_rejects_invalid_briefing_shape():
    invalid = [{"name":"ResearchNarrative","args":{"sections":"wrong shape"},"id":"bad","type":"tool_call"}]
    result, _ = run_probe([{"content":"ready"}, {"tool_calls": tool_call()}, {"tool_calls": invalid}])
    assert not result["ok"]
    assert result["structured_output"] == "fail"


def test_readiness_errors_do_not_expose_provider_messages():
    result, model = run_probe([{"error":Exception("bad key secret-abc-123")}] * 2)
    assert not result["ok"] and model.cursor == 1
    assert "secret-abc-123" not in result["detail"]
    assert readiness_ok("ok", "ok") and not readiness_ok("ok", "weak") and not readiness_ok("ok", "fail")


async def test_connection_checks_reuse_successes_retry_failures_and_keep_research_independent(monkeypatch):
    from copy import deepcopy
    from types import SimpleNamespace
    from betafolio.connections import check_all, revision
    from betafolio.production import ProductionSettings, ready, email_ready
    from betafolio.service import ConnectionService

    class Store:
        def __init__(self):
            self.data={'settings':ProductionSettings(email={'sender':'briefings@example.com'}).model_dump(mode='json')}
            self.keys={}
            self.credentials=SimpleNamespace(get=self.keys.get)
        def get(self,key,default=None):return deepcopy(self.data.get(key,default))
        def set(self,key,value):self.data[key]=deepcopy(value)

    database=Store()
    state={'model':True,'email':True}
    calls={'model':0,'search':0,'email':0}
    skipped=[]
    async def model(*args):
        calls['model']+=1
        return {'ok':state['model']}
    async def search(*args,**kwargs):
        calls['search']+=1
        skipped.append(kwargs.get('skip',set()))
        return {'search':{'status':'passed','tested_at':'source-check'}}
    async def mail(*args,**kwargs):
        calls['email']+=1
        assert args[1]=='admin@example.com'
        if not state['email']:
            raise RuntimeError('Do not persist provider payload')
    monkeypatch.setattr('betafolio.connections.probe_model_readiness',model)
    monkeypatch.setattr('betafolio.connections.build_research',lambda *a:object())
    monkeypatch.setattr('betafolio.providers.probe_research_sources',search)
    monkeypatch.setattr('betafolio.mailer.send_mail',mail)
    first=await check_all(database,'admin@example.com')
    await check_all(database,'admin@example.com')
    assert calls=={'model':2,'search':1,'email':1}
    assert database.get('readiness')['email']['tested_at']==first['email']['tested_at']
    email_revision=revision(ConnectionService(database),'email')
    database.keys['smtp_password']='unused'
    assert revision(ConnectionService(database),'email')==email_revision
    database.keys['email_api_key']='changed'
    await check_all(database,'admin@example.com')
    assert calls['search']==1 and calls['email']==2
    state['model']=False
    saved=database.get('readiness')['email']
    await check_all(database,'admin@example.com')
    assert database.get('readiness')['email']==saved
    state['model']=True
    state['email']=False
    await check_all(database,'admin@example.com',force=['email'])
    assert ready(database) and not email_ready(database)
    state['email']=True
    await check_all(database,'admin@example.com')
    assert email_ready(database) and calls['email']==4
    # A failed optional feed is retried without re-probing the successful main search provider.
    saved=database.get('readiness')
    saved['search']['sources']['fed']={'status':'failed'}
    database.set('readiness',saved)
    await check_all(database,'admin@example.com')
    assert skipped[-1]=={'search'} and calls['email']==4
