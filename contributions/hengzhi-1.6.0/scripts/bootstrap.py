"""Prepare project-only environments and verify every bundled MCP connection."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT=Path(__file__).resolve().parents[1]


def python_in(folder):
    return folder/('Scripts/python.exe' if os.name=='nt' else 'bin/python')


def digest(paths):
    value=hashlib.sha256()
    for path in paths:
        value.update(path.name.encode());value.update(path.read_bytes())
    return value.hexdigest()


def install_environment(folder,requirements,*,package=None):
    python=python_in(folder)
    fingerprint=digest(requirements+([package/'pyproject.toml'] if package else []))
    if package:
        fingerprint=digest([p for p in sorted(package.rglob('*')) if p.is_file() and p.suffix in {'.py','.toml'}])
    marker=folder/'.finagent-installed.json'
    try:cached=json.loads(marker.read_text())
    except (OSError,ValueError):cached={}
    moved=cached.get('location')!=str(folder.resolve())
    if not python.exists() or cached.get('fingerprint')!=fingerprint or moved:
        venv.EnvBuilder(with_pip=True).create(folder)
        args=[str(python),'-m','pip','install']
        for requirement in requirements:args+=['-r',str(requirement)]
        if package:args.append(str(package))
        subprocess.run(args,check=True)
        subprocess.run([str(python),'-m','pip','check'],check=True)
        marker.write_text(json.dumps({'fingerprint':fingerprint,'location':str(folder.resolve())}))
    return python


def ensure_main():
    return install_environment(ROOT/'.venv',[ROOT/'requirements.txt',ROOT/'requirements-helpers.txt'])


def server_configs():
    folders=[ROOT/'data/uploads',ROOT/'data/financial',ROOT/'exports']
    for folder in folders:folder.mkdir(parents=True,exist_ok=True)
    args=['stdio','--read-only']
    for folder in folders:args+=['--allow-dir',str(folder)]
    excel_bin=ROOT/'.venv-mcp'/('Scripts' if os.name=='nt' else 'bin')
    return [
        {'name':'financial-doc-extractor','command':str(python_in(ROOT/'.venv-doc-mcp')),'args':[str(ROOT/'integrations/financial-doc-extractor/server.py')],
         'cwd':str(ROOT),'env':{'FINANCIAL_DOC_AUDIT_DIR':str(ROOT/'data/plugin-audit'),'FINANCIAL_DOC_ALLOWED_DIRS':json.dumps([str(x) for x in folders])},'enabled':True,'auto_start':True},
        {'name':'fin-excel-audit','command':str(excel_bin/('excel-mcp-server.exe' if os.name=='nt' else 'excel-mcp-server')),'args':args,'cwd':str(ROOT),'enabled':True,'auto_start':True},
    ]


def save_connections():
    path=ROOT/'data/config.json';path.parent.mkdir(parents=True,exist_ok=True)
    cfg=json.loads(path.read_text()) if path.exists() else {}
    bundled=server_configs();names={s['name'] for s in bundled}
    cfg['mcp_servers']=[s for s in cfg.get('mcp_servers',[]) if s.get('name') not in names]+bundled
    # Preserve recipient credentials, provider, permissions and custom extensions.
    tmp=path.with_suffix('.tmp')
    with tmp.open('w',encoding='utf-8') as stream:
        os.chmod(tmp,0o600);json.dump(cfg,stream,ensure_ascii=False,indent=2)
    tmp.replace(path);os.chmod(path,0o600)
    return bundled


async def probe_connections(servers):
    sys.path.insert(0,str(ROOT))
    from server.mcp_client import MCPClient
    from openpyxl import Workbook
    from reportlab.pdfgen import canvas
    workbook=ROOT/'data/uploads/installation-check.xlsx'
    book=Workbook();book.active.title='Check';book.active['A1']=17;book.active['A2']=23;book.active['A3']='=A1*A2';book.save(workbook);book.close()
    document=ROOT/'data/uploads/installation-check.pdf'
    pdf=canvas.Canvas(str(document));pdf.drawString(72,720,'SYNTHETIC INSTALLATION CHECK: Revenue 1200, Cost 900.');pdf.save()
    results=[]
    for server in servers:
        client=MCPClient(server['name'],server['command'],server['args'],env=server.get('env'),cwd=server.get('cwd'))
        try:
            await client.start()
            tool='list_schemas' if server['name']=='financial-doc-extractor' else 'read_range'
            arguments={} if tool=='list_schemas' else {'path':str(workbook),'sheet':'Check','range':'A3','mode':'values'}
            response=await client.call_tool(tool,arguments)
            if response.get('isError'):raise RuntimeError('MCP tool returned an error')
            data=response.get('structuredContent')
            if data is None:data=json.loads(next(x['text'] for x in response['content'] if x.get('type')=='text'))
            if tool=='list_schemas':
                if not isinstance(data,dict) or not data or data.get('error'):raise RuntimeError('No valid financial schemas')
            elif data.get('values')!=[[391]] or data.get('uncalculated'):raise RuntimeError('Independent Excel formula check failed')
            if tool=='list_schemas':
                parsed=await client.call_tool('parse_pdf',{'pdf_path':str(document),'ocr_mode':'off','extract_tables':False})
                if parsed.get('isError'):raise RuntimeError('Document parsing tool failed')
                parsed_data=json.loads(next(x['text'] for x in parsed['content'] if x.get('type')=='text'))
                if parsed_data.get('pages')!=1 or '1200' not in parsed_data.get('text',''):raise RuntimeError('Synthetic document parse failed')
                denied=await client.call_tool('parse_pdf',{'pdf_path':str(ROOT/'README.md'),'ocr_mode':'off'})
                denial=json.loads(next(x['text'] for x in denied['content'] if x.get('type')=='text'))
                if not denial.get('error'):raise RuntimeError('Document MCP directory confinement failed')
            results.append({'name':server['name'],'connected':True,'tools':len(client.tools),'sample_call_passed':True})
        finally:await client.stop()
    return results


def ensure_extensions(*,verify=True):
    os.environ['FINAGENT_DATA_DIR']=str(ROOT/'data')
    print('正在准备文档提取与Excel MCP（首次需要联网，已安装则复用）…',flush=True)
    install_environment(ROOT/'.venv-doc-mcp',[ROOT/'integrations/financial-doc-extractor/requirements.txt'])
    install_environment(ROOT/'.venv-mcp',[],package=ROOT/'integrations/excel-mcp')
    servers=save_connections()
    report={'mcp':asyncio.run(probe_connections(servers)) if verify else [],'skills':len(list((ROOT/'seed/skills').glob('*/SKILL.md'))),
            'knowledge_notes':len(list((ROOT/'seed/knowledge').rglob('*.md'))),'credentials':'请在设置中填写自己的API Key，或配置自己的本地模型'}
    (ROOT/'data/install-status.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False),flush=True)
    return report


if __name__=='__main__':
    python=ensure_main()
    if Path(sys.prefix).resolve()!=(ROOT/'.venv').resolve():
        subprocess.run([str(python),str(Path(__file__).resolve())],check=True)
    else:ensure_extensions()
