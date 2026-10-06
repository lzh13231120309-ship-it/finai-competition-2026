"""User-visible delivery, clarification boundaries and stale evidence checks."""
import asyncio,json,re
from copy import deepcopy
from docx import Document
import pytest
from test_financial_agent import context,seed,call
from server import finance_delivery as delivery,financial_context,agent,research_agent as ra,research_engine as engine,presentation
from test_research_engine import record

def export(ctx):
    rid,folder=seed(ctx);fa=ctx[0];task=ctx[-2]
    call(fa,task,'validate_financial_result',{'run_id':rid})
    out=call(fa,task,'export_financial_result',{'run_id':rid})
    return rid,folder,out

def test_multiple_outputs_and_duplicate_exports_become_one_word(context):
    rid,path,out=export(context);task=context[-2]
    result=delivery.build(task,[out,out],[],'extraction')
    assert result['content'].count('](')==1
    assert not re.search(r'\.(csv|json|md)\)',result['content'])
    folder,meta=delivery.folder(task,result['id'])
    doc=Document(folder/'report.docx')
    assert any('关键字段' in p.text for p in doc.paragraphs)
    assert len(meta['exports'])==1

def test_report_does_not_survive_changed_source_fields(context):
    rid,path,out=export(context);result=delivery.build(context[-2],[out],[])
    raw=json.loads((path/'result/financial.json').read_text(encoding='utf-8'))
    raw['fields'][0]['raw']='100';context[0].dump(path/'result/financial.json',raw)
    with pytest.raises(ValueError):delivery.folder(context[-2],result['id'])

def test_stale_or_cross_task_exports_not_promoted(context):
    _,_,out=export(context)
    result=delivery.build(context[-1],[out],[],'valuation','没有可用材料')
    _,meta=delivery.folder(context[-1],result['id'])
    assert meta['exports']==[] and '尚未形成' in result['content']

@pytest.mark.parametrize('reply',['是人民币','币种为人民币','继续','详细解释','给我一份Word报告'])
def test_clarifications_continue_valuation(context,reply):
    db=context[2];task=context[-2]
    db.add_message(task,'user','请估值')
    db.add_message(task,'assistant','请确认币种')
    db.add_message(task,'user',reply)
    assert agent.continuing_financial_intent(task,reply)=='valuation'

def test_unrelated_question_stays_unrelated(context):
    db=context[2];task=context[-2]
    db.add_message(task,'user','请估值');db.add_message(task,'user','今天天气怎样')
    assert agent.continuing_financial_intent(task,'今天天气怎样') is None
    assert agent.continuing_financial_intent(task,'继续') is None

def test_currency_confirmation_is_local_and_not_source_rewrite(context):
    db=context[2];task=context[-2]
    db.add_message(task,'user','请估值',attachments=[{'name':'target.csv'}])
    db.add_message(task,'user','是人民币')
    confirmation=financial_context.currency(task)
    original=record('revenue','2024',1000,currency=None,review_reasons=['currency_unknown'])
    original['source_ref']['filename']='target.csv'
    records=[deepcopy(original)]
    assert financial_context.apply(records,{'filename':'target.csv'},confirmation)
    assert records[0]['currency']=='CNY' and original['currency'] is None
    assert records[0]['source_ref']['user_confirmation']['origin']=='user_confirmation'
    assert engine.usable(records[0])
    db.add_message(task,'user','请估值',attachments=[{'name':'other.csv'}])
    assert financial_context.currency(task) is None

def test_confirmation_does_not_fix_unknown_period_or_change_known_currency(context):
    c={'currency':'CNY','filename':'a.csv'}
    rows=[record('revenue','2024',100,currency=None,review_reasons=['currency_unknown','period_unknown']),record('revenue','2024',200,currency='USD')]
    for r in rows:r['source_ref']['filename']='a.csv'
    financial_context.apply(rows,{'filename':'a.csv'},c)
    assert not engine.usable(rows[0]) and rows[1]['currency']=='USD'

def test_multiple_documents_not_silently_assigned_one_currency(context):
    db=context[2];task=context[-2]
    db.add_message(task,'user','请估值',attachments=[{'name':'a.pdf'},{'name':'b.pdf'}]);db.add_message(task,'user','是人民币')
    assert financial_context.currency(task) is None

def test_legacy_link_wall_is_replaced_without_overwriting_history(context):
    _,_,out=export(context);db=context[2];task=context[-2]
    db.add_message(task,'user','请解读公告')
    db.add_message(task,'tool',json.dumps(out),tool_calls={'name':'export_financial_result'})
    original='[查看数据](fake.json) '*48
    db.add_message(task,'assistant',original)
    shown=delivery.history(task,db.list_messages(task))
    assert shown[-1]['content'].count('](')==1
    assert db.list_messages(task)[-1]['content']==original

def test_internal_problem_codes_are_readable_chinese():
    assert presentation.human('operating_cash_flow period_unknown currency_unknown')=='经营现金流 报告期间尚未确认 币种尚未确认'

def test_context_properties_do_not_create_endless_missing_metric_searches(context):
    rid,_,_=export(context);result=call(context[0],context[-2],'validate_financial_result',{'run_id':rid,'required_fields':['currency','period']})
    assert result['missing_fields']==[] and result['context_issues']

def test_legacy_nonfinancial_reply_keeps_its_actual_content(context):
    _,_,out=export(context);db=context[2];task=context[-2]
    db.add_message(task,'user','请解读公告');db.add_message(task,'tool',json.dumps(out),tool_calls={'name':'export_financial_result'})
    db.add_message(task,'assistant','报告已完成');db.add_message(task,'user','今天天气怎样');db.add_message(task,'assistant','我需要城市信息')
    assert delivery.history(task,db.list_messages(task))[-1]['content']=='我需要城市信息'

def test_unknown_scope_is_explained_as_scope_instead_of_currency():
    from server import estimation
    rows=[record('revenue','2024',1000,scope='母公司')]
    with pytest.raises(ValueError,match='统计范围'):estimation.prepare(rows)

def test_known_issuer_with_missing_currency_asks_for_currency():
    from server import estimation
    rows=[record('revenue','2024',1000,currency=None,review_reasons=['currency_unknown'])]
    with pytest.raises(ValueError,match='币种'):estimation.prepare(rows)
