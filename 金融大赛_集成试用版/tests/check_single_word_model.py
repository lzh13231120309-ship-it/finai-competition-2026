"""Real Qwen regression: one Word, incomplete data and a short currency clarification."""
import csv,json,os,re,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];STAGE=ROOT.parent/'第一阶段_金融提取'
RUN=STAGE/'local_data/single-word-model'/str(time.time_ns());os.environ['FINAGENT_DATA_DIR']=str(RUN);sys.path[:0]=[str(ROOT),str(STAGE)]
from fastapi.testclient import TestClient
from server import config,db
from server.app import app
from finance_extract import app as extraction
extraction.JOBS=RUN/'jobs';extraction.JOBS.mkdir(parents=True)
config.save({**config.DEFAULT_CONFIG,'auto_title':False,'temperature':.1,'max_tokens':3072,'agent_max_steps':24,'tools_enabled':{'financial_extraction':True,'system_access':False,'mcp':False}})
LAB=ROOT.parent/'案例实验室/严格案例_第一轮/输入';checks=[];cases=[]
def check(name,ok):
    checks.append({'name':name,'passed':bool(ok)})
    if not ok:raise AssertionError(name)
def chat(c,task,prompt,files=None,label=''):
    started=time.time()
    with c.stream('POST','/api/chat',json={'task_id':task,'content':prompt,'attachments':files or []}) as r:events=[json.loads(line[6:]) for line in r.iter_lines() if line.startswith('data: ')]
    cases.append({'label':label,'task_id':task,'events':events,'seconds':round(time.time()-started,2),'isolated_data':str(RUN)})
    final=next(x for x in events if x['type']=='done')
    text=final['text'];urls=re.findall(r'\]\(([^)]+)\)',text)
    check(label+'只有一个下载',len(urls)==1 and urls[0].endswith('report.docx'))
    check(label+'无内部字段名和文件格式',not any(x in text for x in ('operating_cash_flow','net_profit_parent','.csv)', '.json)', '.md)')))
    download=c.get(urls[0]);check(label+'Word可下载',download.status_code==200 and download.content[:2]==b'PK')
    check(label+'中文下载文件名',"filename*=utf-8''" in download.headers.get('content-disposition','') and 'report.docx' not in download.headers.get('content-disposition',''))
    check(label+'没有执行错误',not any(x['type']=='error' for x in events))
    cases[-1]['word_url']=urls[0]
    print(json.dumps({'case':label,'seconds':cases[-1]['seconds'],'checks':len(checks)},ensure_ascii=False),flush=True)
    return text
try:
    with TestClient(app) as c:
        task=c.post('/api/tasks',json={'title':'缺币种及简短补充'}).json()['task']['id']
        with (LAB/'模拟_毛利与周转.csv').open(encoding='utf-8-sig',newline='') as f:reader=csv.DictReader(f);headers=reader.fieldnames;rows=list(reader)
        for row in rows:row['currency']=''
        source=RUN/'模拟_缺币种.csv'
        with source.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=headers);w.writeheader();w.writerows(rows)
        with source.open('rb') as f:name=c.post('/api/upload',data={'task_id':task},files={'file':(source.name,f)}).json()['file']['name']
        first=chat(c,task,'请对上传公司的财报估值，资料不足要解释原因，并给完整Word。不要补充候选字段。',[name],'初轮缺币种')
        check('初轮未冒充有效估值','目前还不能形成可靠估值' in first)
        run_ids=set(json.loads((RUN/'finance_agent'/task/'runs.json').read_text(encoding='utf-8')))
        second=chat(c,task,'是人民币',label='简短人民币补充')
        check('补充信息后确实调用估值',any(x.get('name')=='build_financial_valuation' and x.get('ok') for x in cases[-1]['events']))
        check('没有重新创建解析任务',set(json.loads((RUN/'finance_agent'/task/'runs.json').read_text(encoding='utf-8')))==run_ids)
        values=[json.loads(x.read_text(encoding='utf-8')) for x in (RUN/'finance_agent'/task/'research').glob('*/result.json')]
        value=next(x for x in values if x['schema']=='finai.valuation.v1' and x['status']!='needs_input')
        check('人民币来自用户确认',value['source_snapshots'][0]['user_confirmation']['origin']=='user_confirmation')
        check('补充后输出实际DCF',value['dcf']['status']=='ok' and value['currency']=='CNY')
        task=c.post('/api/tasks',json={'title':'亏损公司单份完整Word'}).json()['task']['id'];names=[]
        for file in ['模拟_亏损与有效PB.csv','模拟_亏损估值方案.json']:
            with (LAB/file).open('rb') as f:names.append(c.post('/api/upload',data={'task_id':task},files={'file':(file,f)}).json()['file']['name'])
        text=chat(c,task,'请对这家亏损公司估值，解释不同方法是否适用，输出一份完整Word，不要补充候选字段。',names,'亏损公司')
        check('亏损保留PB解释','PB倍数中位数' in text and '当前永续DCF不适用' in text)
finally:
    file=ROOT/'tests'/('single-word-model-result-'+str(time.time_ns())+'.json')
    file.write_text(json.dumps({'checks':checks,'cases':cases,'isolated_data':str(RUN),'scope':'真实本地Qwen调用；明确模拟的开发案例'},ensure_ascii=False,indent=2),encoding='utf-8');print(file,flush=True)
