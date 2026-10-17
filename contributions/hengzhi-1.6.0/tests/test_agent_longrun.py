"""Long task, stall and cancellation behavior using deterministic model streams."""
import asyncio
import json
import importlib
import time

import httpx
import pytest
from server import agent, config, db, llm

@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, path in [('DATA_DIR',tmp_path),('CONFIG_PATH',tmp_path/'config.json'),('DB_PATH',tmp_path/'agent.db'),('UPLOAD_DIR',tmp_path/'uploads'),('SKILLS_DIR',tmp_path/'skills'),('KNOWLEDGE_DIR',tmp_path/'knowledge'),('LOG_DIR',tmp_path/'logs')]:
        monkeypatch.setattr(config,name,path)
    for p in (config.UPLOAD_DIR,config.SKILLS_DIR,config.KNOWLEDGE_DIR,config.LOG_DIR): p.mkdir()
    db.init()
    config.save({'provider':'ollama','model':'test','auto_title':False,'tools_enabled':{'financial_tools':False},'agent_idle_timeout':.04})
    monkeypatch.setattr(agent,'build_system_prompt',lambda _: 'test system')
    return db.create_task('test')['id']


def collect(tid, event=None):
    async def run(): return [e async for e in agent.run_agent(tid,'执行完整任务',[],event)]
    return asyncio.run(run())


def test_default_can_finish_after_100_tool_steps(env, monkeypatch):
    count=0
    async def model(*args,**kwargs):
        nonlocal count
        count+=1
        if count<=100:
            yield {'type':'tool_calls','tool_calls':[{'id':f'c{count}','type':'function','function':{'name':'calculator','arguments':json.dumps({'expression':f'{count}+1'})}}]}
        else: yield {'type':'text','text':'任务完成'}
    monkeypatch.setattr(agent,'stream_chat',model)
    events=collect(env)
    assert count==101 and events[-1]['type']=='done'
    assert db.list_messages(env)[-1]['content']=='任务完成'


def test_silent_model_pauses_and_keeps_partial_answer(env, monkeypatch):
    async def model(*args,**kwargs):
        yield {'type':'text','text':'已经核验的结果'}
        await asyncio.sleep(10)
    monkeypatch.setattr(agent,'stream_chat',model)
    async def run():
        return await asyncio.wait_for(_all(agent.run_agent(env,'继续',[])),.5)
    events=asyncio.run(run())
    assert events[-1]['type']=='paused'
    assert any('已经核验的结果' in m['content'] for m in db.list_messages(env))

async def _all(stream): return [e async for e in stream]


def test_cancel_silent_stream_promptly_preserves_reasoning(env, monkeypatch):
    async def run():
        ready=asyncio.Event(); cancelled=asyncio.Event(); closed=asyncio.Event()
        async def model(*args,**kwargs):
            try:
                yield {'type':'reasoning','text':'部分分析依据'}
                ready.set(); await asyncio.sleep(10)
            finally: closed.set()
        monkeypatch.setattr(agent,'stream_chat',model)
        task=asyncio.create_task(_all(agent.run_agent(env,'继续',[],cancelled)))
        await ready.wait(); start=time.monotonic(); cancelled.set()
        events=await asyncio.wait_for(task,.5)
        assert time.monotonic()-start<.5 and closed.is_set()
        assert events[-1]['type']=='cancelled'
        assert any(m['reasoning']=='部分分析依据' for m in db.list_messages(env))
    asyncio.run(run())


def test_token_truncation_continues_instead_of_false_done(env, monkeypatch):
    calls=0
    async def model(*args,**kwargs):
        nonlocal calls
        calls+=1
        yield {'type':'text','text':'第一段' if calls==1 else '第二段完成'}
        yield {'type':'finish','reason':'length' if calls==1 else 'stop'}
    monkeypatch.setattr(agent,'stream_chat',model)
    events=collect(env)
    assert calls==2 and events[-1]['type']=='done'
    assert '第一段第二段完成'==events[-1]['text']


def test_empty_model_naturally_pauses_not_blank_done(env,monkeypatch):
    async def model(*args,**kwargs):
        if False: yield {}
    monkeypatch.setattr(agent,'stream_chat',model)
    events=collect(env)
    assert events[-1]['type']=='paused' and events[-1]['reason']=='empty_response'


def test_parser_tracks_token_finish_and_tool_activity():
    events=llm._parse_chunk({'choices':[{'finish_reason':'length','delta':{'tool_calls':[{'index':0,'function':{'arguments':'a'}}]}}]}, {})
    assert {'type':'finish','reason':'length'} in events
    assert any(e['type']=='activity' for e in events)


def test_http_cancel_without_any_network_output(env, monkeypatch):
    api=importlib.import_module('server.app')
    async def run():
        ready=asyncio.Event()
        async def model(*args,**kwargs):
            ready.set(); await asyncio.sleep(10)
            if False: yield {}
        monkeypatch.setattr(agent,'stream_chat',model)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://test') as client:
            req=asyncio.create_task(client.post('/api/chat',json={'task_id':env,'content':'执行','run_id':'old'}))
            await ready.wait()
            mismatch=await client.post('/api/chat/cancel',json={'task_id':env,'run_id':'another'})
            assert not mismatch.json()['cancelled']
            duplicate=await client.post('/api/chat',json={'task_id':env,'content':'重复','run_id':'new'})
            assert duplicate.status_code==409
            start=time.monotonic()
            response=await client.post('/api/chat/cancel',json={'task_id':env,'run_id':'old'})
            assert response.json()['cancelled'] and time.monotonic()-start<.3
            output=await asyncio.wait_for(req,.5)
            assert 'cancelled' in output.text and env not in api._running_tasks
    asyncio.run(run())


def test_context_checkpoint_keeps_objective_artifacts_and_tool_pairs():
    messages=[{'role':'system','content':'system'}, {'role':'user','content':'分析公司甲的2025年财务报告'}]
    for i in range(60):
        messages.extend([{'role':'assistant','content':'核验进度'+str(i),'tool_calls':[{'id':str(i),'function':{'name':'inspect_document','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':str(i),'content':json.dumps({'run_id':str(i),'complete_result':'/api/financial/runs/result/'+str(i),'evidence':'a'*5000})}])
    compact=agent._compact_context(messages,20000)
    assert len(json.dumps(compact,ensure_ascii=False)) <= 20000
    assert any(m.get('content')=='分析公司甲的2025年财务报告' for m in compact)
    assert any('/api/financial/runs/result/' in str(m.get('content')) for m in compact)
    calls={c['id'] for m in compact for c in m.get('tool_calls',[])}
    assert all(m['tool_call_id'] in calls for m in compact if m['role']=='tool')


def test_unlimited_setting_and_explicit_step_pause(env,monkeypatch):
    async def model(*args,**kwargs):
        yield {'type':'tool_calls','tool_calls':[{'id':'c','type':'function','function':{'name':'calculator','arguments':'{"expression":"1"}'}}]}
    monkeypatch.setattr(agent,'stream_chat',model)
    config.update({'agent_max_steps':2})
    events=collect(env)
    assert events[-1]['type']=='paused' and events[-1]['reason']=='step_limit'
    assert len([e for e in events if e['type']=='step'])==2
    config.update({'agent_max_steps':0})
    calls=0
    async def eventually_done(*args,**kwargs):
        nonlocal calls
        calls+=1
        if calls<105:
            yield {'type':'tool_calls','tool_calls':[{'id':str(calls),'type':'function','function':{'name':'calculator','arguments':json.dumps({'expression':str(calls)})}}]}
        else: yield {'type':'text','text':'完成'}
    monkeypatch.setattr(agent,'stream_chat',eventually_done)
    events=collect(env)
    assert events[-1]['type']=='done' and calls==105


def test_disabled_idle_timeout_does_not_pause_active_response(env,monkeypatch):
    config.update({'agent_idle_timeout':0})
    async def model(*args,**kwargs):
        await asyncio.sleep(.03)
        yield {'type':'text','text':'完成'}
    monkeypatch.setattr(agent,'stream_chat',model)
    assert collect(env)[-1]['type']=='done'


def test_cancel_before_producer_starts_releases_registry(env):
    api=importlib.import_module('server.app')
    async def run():
        response=await api.api_chat(api.ChatBody(task_id=env,content='待执行任务',run_id='instant'))
        result=await api.api_chat_cancel({'task_id':env,'run_id':'instant'})
        assert result['cancelled'] and not result['stopping']
        events=[e async for e in response.body_iterator]
        assert any('cancelled' in e for e in events)
        assert env not in api._active_runs
        assert db.list_messages(env)[-1]['content']=='待执行任务'
    asyncio.run(run())


def test_original_objective_survives_many_continuations(env):
    db.add_message(env,'user','原始目标：分析公司甲')
    for _ in range(25):
        db.add_message(env,'assistant','已完成一些核验')
        db.add_message(env,'user','继续')
    messages=agent._history_messages(env,20)
    assert messages[0]['content']=='原始目标：分析公司甲'


def test_legacy_settings_upgrade_without_overwriting_custom_limits(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'CONFIG_PATH',tmp_path/'config.json')
    config.CONFIG_PATH.write_text(json.dumps({'agent_max_steps':50,'max_tokens':8192,'api_keys':{'private':'secret'}}))
    upgraded=config.load()
    assert upgraded['agent_max_steps']==1000 and upgraded['max_tokens']==32768
    assert upgraded['api_keys']['private']=='secret'
    config.CONFIG_PATH.write_text(json.dumps({'agent_max_steps':2000,'max_tokens':16000}))
    custom=config.load()
    assert custom['agent_max_steps']==2000 and custom['max_tokens']==16000


def test_truncated_tool_arguments_are_never_executed(env,monkeypatch):
    invoked=[]
    async def model(*args,**kwargs):
        yield {'type':'tool_calls','tool_calls':[{'id':'truncated','function':{'name':'write_file','arguments':'{"path":'}}]}
        yield {'type':'finish','reason':'length'}
    async def local(*args): invoked.append(args); return 'unexpected'
    monkeypatch.setattr(agent,'stream_chat',model)
    monkeypatch.setattr(agent,'run_local_tool',local)
    events=collect(env)
    assert events[-1]['type']=='paused' and events[-1]['reason']=='truncated_tool'
    assert not invoked


def test_real_sse_stream_closes_transport_on_idle_timeout(env,monkeypatch):
    async def run():
        closed=asyncio.Event()
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                while True:
                    yield b': keep-alive\n\n'
                    await asyncio.sleep(.01)
            async def aclose(self): closed.set()
        original_client=httpx.AsyncClient
        monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs: original_client(transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=Body())),**kwargs))
        events=await asyncio.wait_for(_all(agent.run_agent(env,'执行',[])),.5)
        assert events[-1]['type']=='paused' and events[-1]['reason']=='idle_timeout'
        assert closed.is_set()
        assert any(m['content']=='partial' for m in db.list_messages(env))
    asyncio.run(run())


def test_mcp_cancel_releases_pending_future_without_stopping_shared_server(monkeypatch):
    from server.mcp_client import MCPClient
    async def run():
        client=MCPClient('test','unused')
        sent=asyncio.Event()
        async def send(payload): sent.set()
        monkeypatch.setattr(client,'_send',send)
        request=asyncio.create_task(client.request('tools/call',{'name':'test'}))
        await sent.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError): await request
        assert not client._pending
    asyncio.run(run())


def test_latest_wide_parallel_tool_group_also_fits_context_budget():
    messages=[{'role':'system','content':'system'},{'role':'user','content':'原始任务'}]
    calls=[{'id':str(i),'function':{'name':'inspect_document','arguments':'{}'}} for i in range(6)]
    messages.append({'role':'assistant','content':'','tool_calls':calls})
    for i in range(6): messages.append({'role':'tool','tool_call_id':str(i),'content':json.dumps({'document_id':str(i),'text':'a'*12000})})
    compact=agent._compact_context(messages,60000)
    assert len(json.dumps(compact,ensure_ascii=False)) <= 60000
    assert len([m for m in compact if m['role']=='tool'])==6
    assert all(json.loads(m['content'])['document_id']==m['tool_call_id'] for m in compact if m['role']=='tool')


def test_usage_only_stream_does_not_extend_idle_deadline(env,monkeypatch):
    async def model(*args,**kwargs):
        while True:
            yield {'type':'usage','usage':{'total_tokens':1}}
            await asyncio.sleep(.005)
    monkeypatch.setattr(agent,'stream_chat',model)
    start=time.monotonic()
    events=collect(env)
    assert events[-1]['type']=='paused' and time.monotonic()-start<.3


def test_clean_eof_before_sse_terminal_pauses_instead_of_false_done(env,monkeypatch):
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"unfinished"}}]}\n\n'
    original_client=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs: original_client(transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=Body())),**kwargs))
    events=collect(env)
    assert events[-1]['type']=='paused' and events[-1]['reason']=='model_error'
    assert any(m['content']=='unfinished' for m in db.list_messages(env))
