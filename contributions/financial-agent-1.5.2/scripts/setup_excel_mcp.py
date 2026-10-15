"""Install vendored Excel MCP into a separate project environment."""
import json,os,subprocess,venv
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
venv.EnvBuilder(with_pip=True).create(ROOT/'.venv-mcp')
bin_dir=ROOT/'.venv-mcp'/('Scripts' if os.name=='nt' else 'bin')
python=bin_dir/('python.exe' if os.name=='nt' else 'python')
subprocess.run([str(python),'-m','pip','install',str(ROOT/'integrations/excel-mcp')],check=True)
config_file=ROOT/'data/config.json';config_file.parent.mkdir(parents=True,exist_ok=True)
cfg=json.loads(config_file.read_text()) if config_file.exists() else {}
folders=[ROOT/'data/uploads',ROOT/'data/financial',ROOT/'exports']
for folder in folders:folder.mkdir(parents=True,exist_ok=True)
args=['stdio','--read-only']
for folder in folders:args.extend(['--allow-dir',str(folder)])
server={'name':'fin-excel-audit','command':str(bin_dir/('excel-mcp-server.exe' if os.name=='nt' else 'excel-mcp-server')),'args':args,'cwd':str(ROOT),'enabled':True,'auto_start':True}
cfg['mcp_servers']=[s for s in cfg.get('mcp_servers',[]) if s.get('name')!='fin-excel-audit']+[server]
tmp=config_file.with_suffix('.tmp')
with tmp.open('w',encoding='utf-8') as stream:
    os.chmod(tmp,0o600);json.dump(cfg,stream,ensure_ascii=False,indent=2)
tmp.replace(config_file);os.chmod(config_file,0o600)
print('Read-only Excel MCP configured for this project. Restart the Agent.')
