"""Speed modes change reasoning effort, preserving tools and evidence discipline."""
import asyncio
import importlib
import json

import httpx
import pytest
from server import agent, config, db, llm
from server.execution_modes import MODES, mode_name, native_thinking_supported
from test_agent_longrun import env


def test_mode_defaults_and_provider_boundary():
    assert mode_name(None)==mode_name([])=='balanced'
    assert set(MODES)=={'fast','balanced','deep'}
    assert native_thinking_supported('https://api.deepseek.com/v1','deepseek-flash')
    assert not native_thinking_supported('https://api.deepseek.com.evil.test','deepseek-flash')
    assert not native_thinking_supported('https://other.test/v1','deepseek-flash')
    assert not native_thinking_supported('https://api.deepseek.com/v1','unknown')


@pytest.mark.parametrize('mode,effort',[('fast','low'),('balanced','high'),('deep','max')])
def test_selected_mode_reaches_every_model_step_and_preserves_reasoning(env,monkeypatch,mode,effort):
    config.update({'provider':'deepseek','model':'deepseek-flash','api_keys':{'deepseek':'test-key'},'speed_mode':mode})
    calls=[]
    async def model(*args,**kwargs):
        calls.append((args,kwargs))
        if len(calls)==1:
            yield {'type':'reasoning','text':'检查单位与期间'}
            yield {'type':'tool_calls','tool_calls':[{'id':'c','type':'function','function':{'name':'calculator','arguments':'{"expression":"1+1"}'}}]}
            config.update({'speed_mode':'deep' if mode!='deep' else 'fast'})
        else:
            assert any(m.get('reasoning_content')=='检查单位与期间' for m in args[3])
            yield {'type':'text','text':'已核验'}
    monkeypatch.setattr(agent,'stream_chat',model)
    async def collect():return [ev async for ev in agent.run_agent(env,'计算',[])]
    events=asyncio.run(collect())
    assert events[-1]['type']=='done'
    assert len(calls)==2
    assert all(c[1]['reasoning_effort']==effort for c in calls)
    assert all(c[1]['max_tokens']==32768 for c in calls)
    assert all(c[0][4] for c in calls)
    history=agent._history_messages(env,20,True)
    assert any(m.get('reasoning_content')=='检查单位与期间' for m in history)
    assert all('reasoning_content' in m for m in history if m['role']=='assistant')
    assert next(e for e in events if e['type']=='step')['max']==1000


def test_request_mode_overrides_saved_default_and_unknown_provider_sends_no_native_effort(env,monkeypatch):
    config.update({'speed_mode':'deep'})
    captured=[]
    async def model(*args,**kwargs):
        captured.append((args,kwargs));yield {'type':'text','text':'结果'}
    monkeypatch.setattr(agent,'stream_chat',model)
    async def collect():return [ev async for ev in agent.run_agent(env,'追问',[],speed_mode='fast')]
    asyncio.run(collect())
    assert 'reasoning_effort' not in captured[0][1]
    assert any('本轮速度档位：快速' in str(m.get('content')) for m in captured[0][0][3])
    assert config.load()['speed_mode']=='deep'


@pytest.mark.parametrize('base,expected',[('https://api.deepseek.com/v1',True),('https://other.test/v1',False)])
def test_wire_payload_uses_only_documented_official_parameters(monkeypatch,base,expected):
    bodies=[]
    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    monkeypatch.setattr(llm,'_client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    async def collect():return [e async for e in llm.stream_chat(base,'key','deepseek-flash',[{'role':'user','content':'hi'}],reasoning_effort='low')]
    asyncio.run(collect())
    assert ('reasoning_effort' in bodies[0])==expected
    if expected:
        assert bodies[0]['thinking']=={'type':'enabled'}
        assert bodies[0]['reasoning_effort']=='low'


def test_config_mode_validation_and_chat_override(env,monkeypatch):
    api=importlib.import_module('server.app')
    async def model(*args,**kwargs):yield {'type':'text','text':'ok'}
    monkeypatch.setattr(agent,'stream_chat',model)
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://test') as c:
            for value in ('bad',[],{}):
                assert (await c.post('/api/config',json={'patch':{'speed_mode':value}})).status_code==400
            for mode in MODES:
                result=await c.post('/api/config',json={'patch':{'speed_mode':mode}})
                assert result.json()['config']['speed_mode']==mode
            assert (await c.post('/api/chat',json={'task_id':env,'content':'hi','speed_mode':'bad'})).status_code==400
            result=await c.post('/api/chat',json={'task_id':env,'content':'hi','speed_mode':'fast'})
            assert '快速档' in result.text
            assert config.load()['speed_mode']=='deep'
    asyncio.run(check())
