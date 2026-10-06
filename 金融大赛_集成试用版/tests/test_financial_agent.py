import asyncio
import json
import shutil
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT.parent / '第一阶段_金融提取'
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(STAGE))


@pytest.fixture
def context(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(STAGE))
    from server import config, db, financial_agent as fa
    from finance_extract import app as extraction
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(config, 'UPLOAD_DIR', tmp_path / 'uploads')
    monkeypatch.setattr(config, 'DB_PATH', tmp_path / 'agent.db')
    monkeypatch.setattr(config, 'CONFIG_PATH', tmp_path / 'config.json')
    monkeypatch.setattr(extraction, 'JOBS', tmp_path / 'jobs')
    extraction.JOBS.mkdir()
    config.UPLOAD_DIR.mkdir()
    db.init()
    task = db.create_task()['id']
    other = db.create_task()['id']
    return fa, config, db, extraction, task, other


def call(fa, task, name, args=None):
    return json.loads(asyncio.run(fa.dispatch(name, args or {}, task)))


def seed(ctx):
    fa, config, db, extraction, task, other = ctx
    from finance_extract.numbers import quantity
    run_id = 'a' * 32
    folder = extraction.JOBS / run_id
    folder.mkdir()
    extraction.save_state(folder, {'id': run_id, 'status': 'done', 'suffix': '.pdf', 'pages': '', 'filename': 'test.pdf'})
    block = {'page': 1, 'block': 0, 'bbox': [0, 0, 1, 1], 'text': '股东：张三。中标金额：1,000万元。项目名称：金融数据平台。', 'kind': 'text'}
    field = {'name': 'award_amount', **quantity('1,000万元'), 'review_reasons': [],
             'evidence': {'page': 1, 'block': 0, 'bbox': block['bbox'], 'quote': '中标金额：1,000万元'}}
    payload = {'fields': [field], 'events': [], 'blocks': [block], 'source': {'filename': 'test.pdf', 'sha256': 'test'}, 'warnings': [], 'review_required': False}
    fa.dump(folder / 'result' / 'financial.json', payload)
    fa.dump(fa.task_folder(task) / 'runs.json', {run_id: {'attachment_id': 'test', 'tier': 'auto', 'pages': ''}})
    (folder / 'input.pdf').write_bytes(b'%PDF-test')
    return run_id, folder


def test_tool_registration_and_no_key_local_model(context):
    from server import agent
    assert context[0].NAMES <= {t['function']['name'] for t in agent.local_tools({})}
    assert not context[0].NAMES & {t['function']['name'] for t in agent.local_tools({'tools_enabled': {'financial_extraction': False}})}
    assert {t['function']['name'] for t in context[0].phase_tools(context[0].TOOLS, 'attachments')} == {'list_financial_attachments'}
    assert {t['function']['name'] for t in context[0].phase_tools(context[0].TOOLS, 'export')} == {'export_financial_result'}
    assert context[0].phase_tools(context[0].TOOLS, 'finish') == []


def test_task_attachment_scope_and_content_identity(context):
    fa, config, db, extraction, task, other = context
    folder = config.UPLOAD_DIR / task
    folder.mkdir()
    (folder / 'sample.pdf').write_bytes(b'first')
    rows = call(fa, task, 'list_financial_attachments')['attachments']
    assert len(rows) == 1
    assert call(fa, other, 'list_financial_attachments')['attachments'] == []
    (folder / 'sample.pdf').write_bytes(b'changed')
    assert call(fa, task, 'list_financial_attachments')['attachments'][0]['attachment_id'] != rows[0]['attachment_id']
    assert not call(fa, task, 'extract_financial_document', {'attachment_id': '../../secret'})['ok']


def test_cross_task_access_rejected(context):
    rid, folder = seed(context)
    assert not call(context[0], context[-1], 'inspect_financial_result', {'run_id': rid})['ok']
    assert not call(context[0], context[-2], 'inspect_financial_result', {'run_id': '../secret'})['ok']


def test_validation_missing_is_not_zero_and_export_requires_validation(context):
    rid, folder = seed(context)
    fa, task = context[0], context[-2]
    assert not call(fa, task, 'export_financial_result', {'run_id': rid})['ok']
    report = call(fa, task, 'validate_financial_result', {'run_id': rid, 'required_fields': ['award_amount', 'absent']})
    assert report['missing_fields'] == ['absent']
    assert report['review_required']
    output = call(fa, task, 'export_financial_result', {'run_id': rid})
    assert output['ok'] and set(output['downloads'])=={'report.docx'}
    assert (folder/'agent_financial.json').is_file()
    data = json.loads((folder / 'agent_financial.json').read_text(encoding='utf-8'))
    assert data['fields'][0]['base_value'] == '10000000'
    assert not any(f['name'] == 'absent' for f in data['fields'])
    by_id = call(fa, task, 'validate_financial_result', {'run_id': rid, 'required_fields': ['f0']})
    assert by_id['missing_fields'] == []


def test_context_compaction_preserves_task_and_latest_evidence(context):
    from server import agent
    messages = [{'role': 'system', 'content': '系统要求'}, {'role': 'user', 'content': '处理指定公告'}]
    for i in range(5):
        messages.append({'role': 'tool', 'content': json.dumps({'ok': True, 'run_id': str(i), 'items': [{'raw': 'x'*6000}]} )})
    original_last = messages[-1]['content']
    agent.compact_financial_context(messages, budget=9000)
    assert messages[1]['content'] == '处理指定公告' and messages[-1]['content'] == original_last
    assert json.loads(messages[2]['content'])['context_compacted']


@pytest.mark.parametrize('raw,quote,unit', [('999', '中标金额：999万元', '万元'), ('1', '中标金额：1,000万元', '万元'), ('1,000', '中标金额：1,000万元', '亿元')])
def test_fabricated_source_partial_number_and_unit_rejected(context, raw, quote, unit):
    rid, folder = seed(context)
    result = call(context[0], context[-2], 'add_evidence_fields', {'run_id': rid, 'fields': [
        {'name': 'fake_amount', 'raw': raw, 'quote': quote, 'block_id': 'p1:b0', 'kind': 'number', 'unit': unit}]})
    assert not result['ok']
    assert not (folder / 'agent_additions.json').exists()


def test_evidence_candidate_and_changed_result_requires_revalidation(context):
    rid, folder = seed(context)
    fa, task = context[0], context[-2]
    call(fa, task, 'validate_financial_result', {'run_id': rid})
    original_export = call(fa, task, 'export_financial_result', {'run_id': rid})
    candidate = {'name': 'project_name', 'raw': '金融数据平台', 'quote': '项目名称：金融数据平台', 'block_id': 'p1:b0', 'kind': 'text'}
    result = call(fa, task, 'add_evidence_fields', {'run_id': rid, 'fields': [candidate]})
    assert result['ok'] and len(result['accepted']) == 1
    assert not call(fa, task, 'export_financial_result', {'run_id': rid})['ok']
    assert not fa.export_is_current(task, original_export)
    report = call(fa, task, 'validate_financial_result', {'run_id': rid, 'required_fields': ['project_name']})
    assert report['missing_fields'] == [] and report['review_required']
    assert call(fa, task, 'export_financial_result', {'run_id': rid})['ok']
    assert len(call(fa, task, 'add_evidence_fields', {'run_id': rid, 'fields': [candidate]})['accepted']) == 0


def test_model_cannot_claim_completion_without_artifact(context, monkeypatch):
    from server import agent
    fa, config, db, extraction, task, other = context
    cfg = {**config.DEFAULT_CONFIG, 'provider': 'local_finance', 'model': 'test-model', 'auto_title': False,
           'tools_enabled': {'financial_extraction': True, 'system_access': False}}
    monkeypatch.setattr(config, 'load', lambda: cfg)
    async def model(*args, **kwargs):
        yield {'type': 'text', 'text': '已提取成功（模拟错误行为）。'}
    monkeypatch.setattr(agent, 'stream_chat', model)
    async def collect():
        return [event async for event in agent.run_agent(task, '请做结构化提取', [])]
    events = asyncio.run(collect())
    done=next(e for e in events if e['type']=='done')
    assert done.get('partial') is True and '尚未形成' in done['text']
    assert '已提取成功（模拟错误行为）' not in done['text']
    assert done['text'].count('](')==1 and '.docx)' in done['text']
    assert not any('已提取成功（模拟错误行为）' in (e.get('text') or '') for e in events if e['type']=='delta')


def test_raw_tampering_and_numeric_tampering_detected(context):
    rid, folder = seed(context)
    fa = context[0]
    p = folder / 'result' / 'financial.json'
    data = json.loads(p.read_text(encoding='utf-8'))
    data['fields'][0]['raw'] = '999万元'
    fa.dump(p, data)
    report = call(fa, context[-2], 'validate_financial_result', {'run_id': rid})
    assert {'raw_not_in_source_block', 'normalization_mismatch'} <= set(report['issues'][0]['reasons'])


def test_long_blocks_pagination_returns_valid_json(context):
    rid, folder = seed(context)
    fa = context[0]
    p = folder / 'result' / 'financial.json'
    data = json.loads(p.read_text(encoding='utf-8'))
    data['blocks'][0]['text'] = 'x' * 6000 + '末尾证据'
    fa.dump(p, data)
    first = call(fa, context[-2], 'inspect_financial_result', {'run_id': rid, 'view': 'blocks'})
    assert first['items'][0]['next_text_offset'] == 2500
    last = call(fa, context[-2], 'inspect_financial_result', {'run_id': rid, 'view': 'blocks', 'block_id': 'p1:b0', 'text_offset': 5000})
    assert last['items'][0]['text'].endswith('末尾证据')


def test_search_missing_field_evidence_and_field_id_not_block_id(context):
    rid, folder = seed(context)
    fa, task = context[0], context[-2]
    result = call(fa, task, 'find_financial_evidence', {'run_id': rid, 'query': 'project_name'})
    assert result['total_matches'] == 1
    assert result['items'][0]['block_id'] == 'p1:b0'
    assert '金融数据平台' in result['items'][0]['text']
    assert not call(fa, task, 'inspect_financial_result', {'run_id': rid, 'view': 'fields', 'block_id': 'f0'})['ok']


def test_financial_images_are_routed_to_tools(context):
    from server import agent
    result = agent._user_content('请做结构化提取', [{'name': 'scan.png', 'size': 3, 'kind': 'image', 'data_url': 'data:SECRET'}])
    assert isinstance(result, str) and 'scan.png' in result and 'SECRET' not in result


def test_disabled_tool_hallucination_cannot_execute(context, monkeypatch):
    from server import agent
    fa, config, db, extraction, task, other = context
    cfg = {**config.DEFAULT_CONFIG, 'provider': 'custom', 'model': 'test-model', 'auto_title': False,
           'base_url_overrides': {'custom': 'http://127.0.0.1:17907/v1'},
           'tools_enabled': {'financial_extraction': False, 'system_access': False}}
    monkeypatch.setattr(config, 'load', lambda: cfg)
    count = 0
    async def model(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            yield {'type': 'tool_calls', 'tool_calls': [{'id': 'call-test', 'type': 'function', 'function': {'name': 'list_financial_attachments', 'arguments': '{}'}}]}
        else:
            yield {'type': 'text', 'text': '工具未启用，未执行提取。'}
    monkeypatch.setattr(agent, 'stream_chat', model)
    async def collect():
        return [event async for event in agent.run_agent(task, 'test', [])]
    events = asyncio.run(collect())
    assert next(e for e in events if e['type'] == 'tool_end')['ok'] is False
    assert not (fa.task_folder(task) / 'audit.jsonl').exists()


def test_model_selected_research_enters_verified_workflow(context,monkeypatch):
    from server import agent
    fa,config,db,extraction,task,other=context
    cfg={**config.DEFAULT_CONFIG,'provider':'local_finance','model':'test','auto_title':False,'agent_max_steps':2,'tools_enabled':{'financial_extraction':True,'system_access':False,'mcp':False}}
    monkeypatch.setattr(config,'load',lambda:cfg)
    monkeypatch.setattr(agent,'financial_intent',lambda text:None)
    available=[]
    async def model(*args,**kwargs):
        available.append([t['function']['name'] for t in args[4]])
        name='analyze_financial_reports' if len(available)==1 else 'list_financial_attachments'
        arguments='{"run_ids":["invented"]}' if len(available)==1 else '{}'
        yield {'type':'tool_calls','tool_calls':[{'id':'case'+str(len(available)),'type':'function','function':{'name':name,'arguments':arguments}}]}
    monkeypatch.setattr(agent,'stream_chat',model)
    async def collect():return [x async for x in agent.run_agent(task,'一种未识别的请求',[])]
    events=asyncio.run(collect())
    assert next(x for x in events if x['type']=='tool_end')['ok'] is False
    assert available[1]==['list_financial_attachments']
    done=next(x for x in events if x['type']=='done')
    assert done.get('partial') is True and '尚未形成' in done['text']
    assert not (fa.task_folder(task)/'research').exists()
