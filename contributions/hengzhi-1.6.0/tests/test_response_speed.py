"""Reduce preparation latency without weakening financial verification."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from server import agent, config, db
from server.mcp_client import MCPManager, MCPError
from test_agent_longrun import env


async def answer(*args, **kwargs):
    yield {'type': 'text', 'text': '结论与依据'}


def run(tid, text, attachments=None):
    async def collect():
        return [e async for e in agent.run_agent(tid, text, attachments or [])]
    return asyncio.run(collect())


def test_new_task_title_needs_no_extra_model_request(env, monkeypatch):
    import server.llm as llm
    forbidden = AsyncMock(side_effect=AssertionError('title must not call model'))
    monkeypatch.setattr(llm, 'complete', forbidden)
    monkeypatch.setattr(agent, 'stream_chat', answer)
    db.touch_task(env, title='新任务')
    config.update({'auto_title': True})
    events = run(env, '分析公司甲财报')
    assert any(e['type'] == 'title' and e['title'] == '分析公司甲财报' for e in events)
    assert events[-1]['type'] == 'done'
    forbidden.assert_not_awaited()


def test_custom_task_title_is_preserved(env, monkeypatch):
    monkeypatch.setattr(agent, 'stream_chat', answer)
    config.update({'auto_title': True})
    run(env, '分析新问题')
    assert db.get_task(env)['title'] == 'test'


def test_followup_does_not_automatically_analyze_all_task_files(env, monkeypatch):
    folder = config.UPLOAD_DIR / env
    folder.mkdir(); (folder / 'old.pdf').write_bytes(b'old file')
    config.update({'tools_enabled': {'financial_tools': True}})
    def forbidden(*args):
        pytest.fail('a follow-up should request only needed evidence through tools')
    monkeypatch.setattr(agent.finance_service, 'analyze_financial_report', forbidden)
    monkeypatch.setattr(agent, 'stream_chat', answer)
    assert run(env, '解释年报里经营现金流的含义')[-1]['type'] == 'done'
    assert any(t['function']['name'] == 'analyze_financial_report' for t in agent.local_tools(config.load()))


def test_fresh_attachment_still_gets_deterministic_preflight(env, monkeypatch):
    config.update({'tools_enabled': {'financial_tools': True}})
    calls=[]; contexts=[]
    def analysis(tid, args):
        calls.append((tid, args))
        return {'run_id': 'new-run', 'fact_count': 2, 'review_count': 1}
    async def model(*args, **kwargs):
        contexts.extend(args[3]); yield {'type':'text','text':'保留待复核事项'}
    monkeypatch.setattr(agent.finance_service, 'analyze_financial_report', analysis)
    monkeypatch.setattr(agent, 'stream_chat', model)
    run(env, '分析这个年报', [{'name':'new.pdf','kind':'pdf','size':10,'text':'','pages':1}])
    assert calls == [(env, {'filenames':['new.pdf']})]
    assert any('new-run' in str(m) and '不再重复执行' in str(m) for m in contexts)


def client(name, ready=False):
    c=SimpleNamespace(running=ready, _ready=ready, error='', tools=[])
    c.refresh_tools=AsyncMock(return_value=[])
    return c


def test_mcp_discovery_starts_independent_servers_concurrently_in_stable_order():
    async def check():
        manager=MCPManager(); gate=asyncio.Event(); started=[]
        for name in ('first','second'):
            c=client(name)
            async def ready(c=c,name=name):
                started.append(name)
                if len(started)==2:gate.set()
                await gate.wait()
                c.running=c._ready=True
                c.tools=[{'name':'read','inputSchema':{'properties':{}}}]
            c.ensure_ready=ready;manager.clients[name]=c
        tools=await asyncio.wait_for(manager.all_tools(['first','second']),.5)
        assert [t['_mcp']['server'] for t in tools]==['first','second']
        assert not manager.last_issues
        assert 'type' not in manager.clients['first'].tools[0]['inputSchema']
    asyncio.run(check())


def test_agent_does_not_retry_failed_mcp_startup_twice(env,monkeypatch):
    manager=MCPManager();c=client('broken')
    c.ensure_ready=AsyncMock(side_effect=MCPError('not installed'))
    manager.clients['broken']=c
    monkeypatch.setattr(agent,'mcp_manager',manager)
    monkeypatch.setattr(agent,'stream_chat',answer)
    config.update({'mcp_servers':[{'name':'broken','enabled':True}]})
    events=run(env,'解释DCF')
    assert c.ensure_ready.await_count==1
    assert any('not installed' in e.get('text','') for e in events)
    assert events[-1]['type']=='done'


def test_discovery_cancel_cleans_up_all_pending_servers():
    async def check():
        manager=MCPManager();ready=asyncio.Event();started=[];closed=[]
        for name in ('first','second'):
            c=client(name)
            async def wait(name=name):
                started.append(name)
                if len(started)==2:ready.set()
                try:await asyncio.sleep(10)
                finally:closed.append(name)
            c.ensure_ready=wait;manager.clients[name]=c
        discovery=asyncio.create_task(manager.all_tools(['first','second']))
        await asyncio.wait_for(ready.wait(),.5)
        discovery.cancel()
        with pytest.raises(asyncio.CancelledError):await discovery
        assert set(closed)=={'first','second'}
    asyncio.run(check())
