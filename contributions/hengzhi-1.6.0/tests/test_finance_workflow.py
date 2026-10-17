import hashlib,json,threading
from pathlib import Path
import pytest
from server import config,finance_service as fs
from server.finance_workflow import run_workflow

@pytest.fixture
def env(tmp_path,monkeypatch):
 monkeypatch.setattr(config,'DATA_DIR',tmp_path);monkeypatch.setattr(config,'UPLOAD_DIR',tmp_path/'uploads')
 folder=tmp_path/'uploads/t';folder.mkdir(parents=True);(folder/'financial.txt').write_text('财报原文')
 return folder

def analysis():
 return fs.save_run('t',{'kind':'analysis','filenames':['financial.txt'],'documents':[{'filename':'financial.txt','sha256':hashlib.sha256('财报原文'.encode()).hexdigest()}],'facts':[{'code':'revenue','value':'100','currency':'CNY','status':'verified','period':'2025-12-31','scope':'consolidated','evidence_ids':['a123456789abcdef:p1:b1']}],'metrics':[{'code':'revenue_yoy','value':'.1','status':'calculated'}],'checks':[{'status':'pass'}]})

def test_analysis_once_and_reference_reused(env,monkeypatch):
 calls=[]
 def analyze(t,args):calls.append(args);return {'run_id':analysis()['run_id']}
 monkeypatch.setattr(fs,'analyze_financial_report',analyze)
 monkeypatch.setattr(fs,'build_pro_valuation_model',lambda t,args:{'ok':False,'refusals':['缺少市场参数'],'run_id':'v'})
 r=run_workflow('t',{'goals':['extraction','financial_analysis','valuation'],'financial_filenames':['financial.txt']})
 assert len(calls)==1 and r['analysis_run_id']
 assert [s['status'] for s in r['stages']]==['completed','completed','needs_input']
 assert not r['completed'] and (fs.run_dir('t',r['run_id'])/'报告.md').is_file()

def test_no_guessing_latest_analysis(env,monkeypatch):
 analysis();monkeypatch.setattr(fs,'build_pro_valuation_model',lambda *args:pytest.fail('must not use latest'))
 r=run_workflow('t',{'goals':['valuation']});assert r['stages'][0]['status']=='needs_input'

def test_changed_source_rejected_for_reuse(env):
 a=analysis();(env/'financial.txt').write_text('已变更')
 r=run_workflow('t',{'goals':['financial_analysis','valuation'],'analysis_run_id':a['run_id']})
 assert r['stale_analysis_files']==['financial.txt'] and all(s['status']=='needs_input' for s in r['stages'])

def test_report_never_used_as_own_source(env):
 with pytest.raises(ValueError,match='不能同时'):run_workflow('t',{'financial_filenames':['financial.txt'],'report_filename':'financial.txt'})
 with pytest.raises(ValueError):run_workflow('t',{'goals':[]})
 with pytest.raises(ValueError):run_workflow('t',{'financial_filenames':['../financial.txt']})

def test_quality_only_stays_incomplete_with_score(env):
 r=run_workflow('t',{'goals':['quality'],'report_text':'报告摘要：本公司收入增长，数据尚未核验。'})
 assert r['stages'][0]['status']=='needs_review'
 assert r['stages'][0]['overall_quality_score'] is None
 assert not r['completed']

def test_cancel_prevents_later_steps(env,monkeypatch):
 control=threading.Event()
 def analyze(t,args):a=analysis();control.set();return {'run_id':a['run_id']}
 monkeypatch.setattr(fs,'analyze_financial_report',analyze)
 monkeypatch.setattr(fs,'build_pro_valuation_model',lambda *args:pytest.fail('cancelled stage called'))
 r=run_workflow('t',{'financial_filenames':['financial.txt']},control)
 assert all(s['status']=='cancelled' for s in r['stages']) and r['analysis_run_id']

def test_announcements_survive_analysis_failure(env,monkeypatch):
 (env/'ann.txt').write_text('公告')
 monkeypatch.setattr(fs,'analyze_financial_report',lambda *args:(_ for _ in ()).throw(ValueError('财报解析失败')))
 monkeypatch.setattr(fs,'extract_announcement_records',lambda *args:{'run_id':'announcement','records':[{'股数':100}],'n_checks_flagged':0})
 r=run_workflow('t',{'goals':['extraction','financial_analysis'],'financial_filenames':['financial.txt'],'announcement_filenames':['ann.txt']})
 assert r['stages'][0]['record_count']==1 and r['stages'][1]['status']=='needs_input'

def test_live_file_change_keeps_saved_results_but_invalidates_delivery(env,monkeypatch):
 a=analysis()
 def model(*args):(env/'financial.txt').write_text('分析中被改写');return {'ok':True,'run_id':'model'}
 monkeypatch.setattr(fs,'build_pro_valuation_model',model)
 r=run_workflow('t',{'goals':['valuation'],'analysis_run_id':a['run_id']})
 assert r['source_changed_during_run']==['financial.txt'] and r['stages'][0]['status']=='needs_review'

def test_workflow_tool_is_registered():assert fs.TOOL_HANDLERS['run_finance_workflow'] is fs.run_finance_workflow

def test_new_artifacts_are_downloadable_with_closed_file_allowlist(env,monkeypatch):
 from server import app as api
 from fastapi.testclient import TestClient
 monkeypatch.setattr(api.db,'get_task',lambda tid:{'id':tid} if tid=='t' else None)
 directory=fs.run_dir('t','x');directory.mkdir(parents=True)
 for name in ['records.json','findings.json','valuation_pro.json','valuation_pro.xlsx','quality_report.html']:(directory/name).write_text('fixture')
 with TestClient(api.app) as c:
  for name in ['records.json','findings.json','valuation_pro.json','valuation_pro.xlsx','quality_report.html']:
   r=c.get('/api/financial/artifacts/t/x/'+name);assert r.status_code==200
   if name.endswith('html'):assert 'sandbox' in r.headers['content-security-policy']
  assert c.get('/api/financial/artifacts/t/x/config.json').status_code==404
  assert c.post('/api/financial/workflow',json={'task_id':'t','run_id':'w','goals':['valuation']}).status_code==200
  assert c.post('/api/financial/workflow/cancel',json={'task_id':'t','run_id':'old'}).json()['status']=='not_running'


def test_reused_analysis_report_cannot_self_certify(env):
 a=analysis()
 with pytest.raises(ValueError,match='不能同时'):run_workflow('t',{'analysis_run_id':a['run_id'],'report_filename':'financial.txt'})

def test_quality_only_uses_explicit_fact_evidence(env):
 a=analysis()
 r=run_workflow('t',{'goals':['quality'],'analysis_run_id':a['run_id'],'report_text':'2025年合并营业收入100元。'})
 assert r['stages'][0]['fact_verification']['checked']>=1
 assert r['stages'][0]['overall_quality_score'] is None

def test_old_cancellation_does_not_stop_new_workflow(env,monkeypatch):
 from server import app as api
 from fastapi.testclient import TestClient
 monkeypatch.setattr(api.db,'get_task',lambda tid:{'id':tid})
 control=threading.Event();api._workflow_controls[('t','new')]=control
 try:
  with TestClient(api.app) as client:
   assert client.post('/api/financial/workflow/cancel',json={'task_id':'t','run_id':'old'}).json()['status']=='not_running'
   assert not control.is_set()
   assert client.post('/api/financial/workflow/cancel',json={'task_id':'t','run_id':'new'}).json()['ok']
   assert control.is_set()
 finally:api._workflow_controls.pop(('t','new'),None)

def test_analysis_payload_task_scope(env):
 a=analysis();path=fs.run_dir('t',a['run_id'])/'run.json';a['task_id']='other';fs._write_json(path,a)
 with pytest.raises(ValueError,match='不属于'):run_workflow('t',{'analysis_run_id':a['run_id']})

def test_chat_stop_signals_workflow_thread_without_waiting(env,monkeypatch):
 import asyncio
 from server import agent
 started=threading.Event();finished=threading.Event();captured=[]
 def worker(t,args,control):
  captured.append(control);started.set();control.wait(2);finished.set();return {'ok':True}
 monkeypatch.setitem(fs.TOOL_HANDLERS,'run_finance_workflow',worker)
 async def check():
  task=asyncio.create_task(agent.run_local_tool('run_finance_workflow',{},'t'))
  assert await asyncio.to_thread(started.wait,1)
  task.cancel()
  with pytest.raises(asyncio.CancelledError):await task
  assert captured[0].is_set()
  assert await asyncio.to_thread(finished.wait,1)
 asyncio.run(check())
