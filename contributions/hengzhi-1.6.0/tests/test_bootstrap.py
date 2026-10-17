"""Installation preserves recipient settings and avoids redundant downloads."""
import importlib.util
import json
from pathlib import Path


def module(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('release_bootstrap',Path(__file__).resolve().parents[1]/'scripts/bootstrap.py')
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    monkeypatch.setattr(value,'ROOT',tmp_path)
    return value


def test_connections_are_recipient_local_and_keep_existing_credentials(tmp_path,monkeypatch):
    b=module(tmp_path,monkeypatch);folder=tmp_path/'data';folder.mkdir()
    path=folder/'config.json';path.write_text(json.dumps({'api_keys':{'example':'recipient-placeholder'},'provider':'custom','tools_enabled':{'system_access':False},'mcp_servers':[{'name':'other','enabled':False}]}))
    servers=b.save_connections();cfg=json.loads(path.read_text())
    assert cfg['api_keys']=={'example':'recipient-placeholder'}
    assert cfg['provider']=='custom' and not cfg['tools_enabled']['system_access']
    assert {s['name'] for s in servers}=={'financial-doc-extractor','fin-excel-audit'}
    assert cfg['mcp_servers'][0]=={'name':'other','enabled':False}
    assert all(str(tmp_path) in s['command'] for s in servers)
    assert all(s['enabled'] and s['auto_start'] for s in servers)
    assert '--read-only' in servers[1]['args']
    b.save_connections()
    assert len(json.loads(path.read_text())['mcp_servers'])==3


def test_dependency_install_is_cached_and_changes_invalidate_it(tmp_path,monkeypatch):
    b=module(tmp_path,monkeypatch);commands=[]
    def create(self,folder):
        binary=b.python_in(folder);binary.parent.mkdir(parents=True,exist_ok=True);binary.touch()
    monkeypatch.setattr(b.venv.EnvBuilder,'create',create)
    monkeypatch.setattr(b.subprocess,'run',lambda args,**kw:commands.append(args))
    requirement=tmp_path/'requirements.txt';requirement.write_text('example==1\n')
    b.install_environment(tmp_path/'env',[requirement]);b.install_environment(tmp_path/'env',[requirement])
    assert len([c for c in commands if 'install' in c])==1
    requirement.write_text('example==2\n');b.install_environment(tmp_path/'env',[requirement])
    assert len([c for c in commands if 'install' in c])==2
