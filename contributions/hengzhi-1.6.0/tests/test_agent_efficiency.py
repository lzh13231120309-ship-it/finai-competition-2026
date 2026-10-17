"""Efficient execution keeps verification while avoiding redundant tool loops."""
import asyncio,json,time
from server import agent,config,db
from test_agent_longrun import env,collect


def tool(i,name='calculator',args=None):
    return {'type':'tool_calls','tool_calls':[{'id':str(i),'type':'function','function':{'name':name,'arguments':json.dumps(args or {'expression':'1+1'})}}]}


def test_readonly_repeat_uses_cache_without_reexecuting(env,monkeypatch):
    calls=[];step=0
    async def local(name,args,tid): calls.append(name);return 'skill instruction'
    async def model(*args,**kwargs):
        nonlocal step
        step+=1
        if step<=2:yield tool(step,'load_skill',{'name':'example'})
        else:yield {'type':'text','text':'结论'}
    monkeypatch.setattr(agent,'run_local_tool',local);monkeypatch.setattr(agent,'stream_chat',model)
    assert collect(env)[-1]['type']=='done'
    assert calls==['load_skill']


def test_repeat_no_progress_switches_to_answer_without_removing_longrun_cap(env,monkeypatch):
    calls=0;final_tools=[]
    async def model(*args,**kwargs):
        nonlocal calls
        calls+=1
        tools=args[4]
        if tools is None:
            final_tools.append(tools)
            yield {'type':'text','text':'已核验结果与尚待核验事项'}
        else:yield tool(calls)
    monkeypatch.setattr(agent,'stream_chat',model)
    events=collect(env)
    assert calls<20 and final_tools==[None] and events[-1]['type']=='done'
    assert config.load()['agent_max_steps']==1000


def test_effectful_commands_are_not_result_cached(env,monkeypatch):
    steps=0;executed=[]
    async def local(*args): executed.append(args);return 'done'
    async def model(*args,**kwargs):
        nonlocal steps
        steps+=1
        if steps<3:yield tool(steps,'write_file',{'path':'new','content':'data'})
        else:yield {'type':'text','text':'完成'}
    monkeypatch.setattr(agent,'stream_chat',model);monkeypatch.setattr(agent,'run_local_tool',local)
    collect(env)
    assert len(executed)==2


def test_compaction_is_fast_for_nearly_1000_messages():
    messages=[{'role':'system','content':'system'},{'role':'user','content':'original goal'}]
    for i in range(480):
        messages += [{'role':'assistant','content':'','tool_calls':tool(i)['tool_calls']},
            {'role':'tool','tool_call_id':str(i),'content':'a'*12000}]
    started=time.monotonic();out=agent._compact_context(messages,60000)
    assert agent._context_size(out)<=60000
    assert time.monotonic()-started<.7



def test_read_cache_invalidates_when_source_file_changes(env,tmp_path,monkeypatch):
    file=tmp_path/'evidence.txt';file.write_text('first')
    args={'path':str(file)}
    first=agent._read_cache_key(env,'read_file',args)
    file.write_text('updated financial evidence')
    assert first!=agent._read_cache_key(env,'read_file',args)


def test_continue_targets_latest_specific_request(env):
    db.add_message(env,'user','分析财报')
    db.add_message(env,'assistant','财报已完成')
    db.add_message(env,'user','对它进行估值')
    db.add_message(env,'user','继续')
    assert agent._current_focus(env)=='对它进行估值'


def test_model_cannot_ignore_conclusion_mode_and_keep_calling_tools(env,monkeypatch):
    steps=0;executed=[]
    async def local(*args): executed.append(args);return '1+1 = 2'
    async def model(*args,**kwargs):
        nonlocal steps
        steps+=1;yield tool(steps)
    monkeypatch.setattr(agent,'stream_chat',model);monkeypatch.setattr(agent,'run_local_tool',local)
    events=collect(env)
    assert len(executed)==1 and steps<20
    assert events[-1]['type']=='paused' and events[-1]['reason']=='repeated_tools'
