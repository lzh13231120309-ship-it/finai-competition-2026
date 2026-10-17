import pytest
from openpyxl import Workbook
from server import config,finance_service as fs,workbook_verification as wv

@pytest.fixture
def setup(tmp_path,monkeypatch):
 monkeypatch.setattr(config,'DATA_DIR',tmp_path)
 monkeypatch.setattr(config,'load',lambda:{'mcp_servers':[{'name':'fin-excel-audit','enabled':True}]})
 folder=fs.run_dir('t','v');folder.mkdir(parents=True)
 b=Workbook();s=b.active;s.title='DCF估值';s['B13']='=100+200';b.save(folder/'报告.xlsx')
 return {'run_id':'v','scenarios':{'base':{'valuation':{'equity_value':'300'}}}}

def test_independent_match_and_snapshot(setup,monkeypatch):
 async def read(*args):return 300
 monkeypatch.setattr(wv,'_read',read);r=wv.verify_workbook('t',setup)
 assert r['status']=='matched' and r['workbook_sha256'] and r['run_id']

def test_mismatch_and_unsupported_are_not_passes(setup,monkeypatch):
 async def wrong(*args):return 400
 monkeypatch.setattr(wv,'_read',wrong);assert wv.verify_workbook('t',setup)['status']=='mismatched'
 async def missing(*args):return None
 monkeypatch.setattr(wv,'_read',missing);assert wv.verify_workbook('t',setup)['status']=='needs_review'

def test_missing_plugin_never_claims_verification(setup,monkeypatch):
 monkeypatch.setattr(config,'load',lambda:{'mcp_servers':[]});assert wv.verify_workbook('t',setup)['status']=='unavailable'

def test_hardcoded_output_is_not_independent_formula_check(setup):
 b=Workbook();s=b.active;s.title='DCF估值';s['B13']=300;b.save(fs.run_dir('t','v')/'报告.xlsx')
 assert wv.verify_workbook('t',setup)['status']=='needs_review'

def test_cache_is_removed_before_mcp_and_original_unchanged(setup,monkeypatch):
 import zipfile,xml.etree.ElementTree as ET,hashlib
 folder=fs.run_dir('t','v');path=folder/'报告.xlsx';before=hashlib.sha256(path.read_bytes()).hexdigest()
 async def read(server,copied,sheet,cell):
  assert copied!=path
  ns='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
  with zipfile.ZipFile(copied) as z:
   tree=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
   for c in tree.iter(ns+'c'):
    if c.find(ns+'f') is not None:assert c.find(ns+'v') is None
  return 300
 monkeypatch.setattr(wv,'_read',read);r=wv.verify_workbook('t',setup)
 assert r['formula_caches_cleared']==1 and r['status']=='matched'
 assert hashlib.sha256(path.read_bytes()).hexdigest()==before
 assert not list(folder.glob('formula-audit-*.xlsx'))
