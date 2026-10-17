"""Deterministic financial extraction. Missing evidence is never guessed."""
import copy
import json
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

DEFAULT_FINANCIAL_SCHEMAS = json.loads(Path(__file__).with_name('schemas.json').read_text(encoding='utf-8'))
ALIASES = {
    '出质人': ['出质人','质押股东','股东名称','股东姓名'],
    '质权人': ['质权人','质押权人'],
    '质押股数': ['质押股数','本次质押数量','本次质押股数','质押数量'],
    '质押比例': ['质押比例','占公司总股本比例','占总股本比例'],
    '质押起始日': ['质押起始日','质押开始日期','质押起始日期'],
    '质押到期日': ['质押到期日','质押到期日期','质押结束日期'],
    '质押用途': ['质押用途','质押融资用途'],
    '股东名称': ['股东名称','股东姓名','持有人名称'],
    '变动前持股': ['变动前持股','变动前持股数量','减持前持股数量','增持前持股数量'],
    '变动后持股': ['变动后持股','变动后持股数量','减持后持股数量','增持后持股数量'],
    '变动方式': ['变动方式','增减持方式'],
    '变动日期': ['变动日期','股权变动日期','权益变动日期'],
    '变动金额': ['变动金额','成交金额','交易金额'],
    '项目名称': ['项目名称','中标项目名称','采购项目名称'],
    '中标单位': ['中标单位','中标人','中标供应商','成交供应商','供应商名称'],
    '中标金额': ['中标金额','成交金额','中标价','中标价格'],
    '招标单位': ['招标单位','招标人','采购人','采购单位'],
    '中标日期': ['中标日期','中标公示日期','成交日期'],
    '项目编号': ['项目编号','招标编号','采购项目编号'],
}
DATE_PATTERN = r'\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}\s*日?'
NUMBER_PATTERN = r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
UNITS = {'亿元':('100000000','元'),'万元':('10000','元'),'元':('1','元'), '亿股':('100000000','股'),'万股':('10000','股'),'股':('1','股'),'%':('1','%')}
MISSING = {'','-','--','—','–','不适用','未披露','无','N/A','null'}


def clean(value):
    return unicodedata.normalize('NFKC', str('' if value is None else value)).replace('\x00','').strip()


def compact(value):
    return re.sub(r'\s+','',clean(value))


def parse_page_range(page_range, total_pages):
    if page_range is None or page_range == '':
        return list(range(total_pages))
    if not isinstance(page_range,str):
        raise ValueError('page_range 必须是字符串')
    pages=set()
    for part in page_range.split(','):
        m=re.fullmatch(r'\s*(\d+)\s*(?:-\s*(\d+)\s*)?',part)
        if not m:
            raise ValueError('页码格式应为 1-3,5，不允许空项')
        start=int(m[1]);end=int(m[2] or m[1])
        if not 1 <= start <= end <= total_pages:
            raise ValueError(f'页码必须在 1 至 {total_pages} 之间，且起始页不能大于结束页')
        pages.update(range(start-1,end))
    return sorted(pages)


def resolve_schemas(schema=None):
    if schema is None or schema == '':
        return copy.deepcopy(DEFAULT_FINANCIAL_SCHEMAS)
    if isinstance(schema,str) and schema in DEFAULT_FINANCIAL_SCHEMAS:
        return {schema:copy.deepcopy(DEFAULT_FINANCIAL_SCHEMAS[schema])}
    try:
        custom=json.loads(schema) if isinstance(schema,str) else schema
    except (ValueError,TypeError) as exc:
        raise ValueError('未知 schema 名称或无效 JSON') from exc
    if not isinstance(custom,dict) or not custom:
        raise ValueError('schema 必须是非空对象')
    fields=custom.get('fields',custom)
    if not isinstance(fields,dict) or not fields or len(fields)>100:
        raise ValueError('fields 必须为包含 1–100 个字段的对象')
    for name,info in fields.items():
        if not isinstance(name,str) or not name.strip() or not isinstance(info,dict):
            raise ValueError('每个字段必须有名称和对象形式的定义')
        if info.get('type','string') not in {'string','number','integer','boolean'}:
            raise ValueError('仅支持 string、number、integer、boolean 字段')
        if 'aliases' in info and (not isinstance(info['aliases'],list) or any(not isinstance(x,str) or not x.strip() for x in info['aliases'])):
            raise ValueError('aliases 必须是非空字符串列表')
    return {'custom':{'name':custom.get('name','自定义'),'fields':fields}}


def field_aliases(field,info=None):
    return sorted(set([field]+ALIASES.get(field,[])+(info or {}).get('aliases',[])),key=lambda s:(-len(s),s))


def kind(field):
    if any(s in field for s in ['日期','起始日','到期日','生效日','时间']):return 'date'
    if '比例' in field:return 'ratio'
    if '金额' in field:return 'money'
    if any(s in field for s in ['股数','数量','持股']):return 'shares'
    return 'text'


def decimal_text(number):
    value=format(number,'f')
    return value.rstrip('0').rstrip('.') if '.' in value else value


def normalize_value(field,raw,unit_hint='',info=None):
    text=clean(raw);out={'value':None,'unit':None,'status':'missing'}
    if text in MISSING:return out
    k=kind(field)
    if k=='date':
        m=re.fullmatch(DATE_PATTERN,text)
        if m:
            try:
                y,mo,d=map(int,re.findall(r'\d+',text))
                return {'value':date(y,mo,d).isoformat(),'unit':None,'status':'valid'}
            except ValueError:pass
        return {**out,'status':'invalid'}
    if k in {'money','shares','ratio'}:
        value=re.sub(r'^人民币\s*','',text).replace(' ','')
        m=re.fullmatch('('+NUMBER_PATTERN+r')\s*(亿元|万元|元|亿股|万股|股|%)?',value)
        if not m:return {**out,'status':'invalid'}
        unit=m[2] or unit_hint
        allowed={'money':{'亿元','万元','元'},'shares':{'亿股','万股','股','%'},'ratio':{'%'}}[k]
        if unit not in allowed:return {**out,'status':'ambiguous_unit'}
        scale,base=UNITS[unit]
        try:
            with localcontext() as ctx:
                ctx.prec=max(28,len(m[1])+10)
                number=Decimal(m[1].replace(',',''))*Decimal(scale)
        except InvalidOperation:return {**out,'status':'invalid'}
        if number<0 or (base=='%' and number>100) or (base=='股' and number!=number.to_integral_value()):
            return {**out,'status':'invalid'}
        return {'value':decimal_text(number),'unit':base,'status':'valid'}
    typ=(info or {}).get('type','string')
    if typ in {'number','integer'}:
        try:
            num=Decimal(text.replace(',',''))
            if not num.is_finite() or abs(num.adjusted())>1000 or (typ=='integer' and num!=num.to_integral_value()):raise ValueError()
            return {'value':decimal_text(num),'unit':None,'status':'valid'}
        except (InvalidOperation,ValueError):return {**out,'status':'invalid'}
    if typ=='boolean':
        mapping={'true':True,'false':False,'是':True,'否':False}
        if text.lower() not in mapping:return {**out,'status':'invalid'}
        return {'value':mapping[text.lower()],'unit':None,'status':'valid'}
    return {'value':text,'unit':None,'status':'valid'}


def header_unit(header):
    m=re.search(r'[（(]\s*(亿元|万元|元|亿股|万股|股|%)\s*[)）]',clean(header))
    return m[1] if m else ''


def header_matches(header,field,info=None):
    h=re.sub(r'\([^)]*\)','',compact(header))
    return h in [compact(a) for a in field_aliases(field,info)]


def prepare_table(rows,page,index,bbox=None,header_rows=1):
    width=max((len(r) for r in rows),default=0)
    raw=[list(r)+[None]*(width-len(r)) for r in rows]
    cells=[[clean(c) for c in row] for row in raw]
    if not cells:return None
    header_rows=min(header_rows,len(cells))
    # Fill horizontal spans in upper header rows only, never financial data cells.
    upper=[]
    for row in cells[:header_rows]:
        upper.append(row.copy())
    for row in upper[:-1]:
        prev=''
        for i,v in enumerate(row):
            if v:prev=v
            else:row[i]=prev
    header=[''.join(dict.fromkeys(compact(row[c]) for row in upper if row[c])) for c in range(width)]
    keys=[];used=set()
    for i,h in enumerate(header):
        key=h or f'column_{i+1}';base=key;n=2
        while key in used:key=f'{base}__{n}';n+=1
        keys.append(key);used.add(key)
    data=cells[header_rows:]
    return {'page':page,'table_index':index,'rows':len(cells),'cols':width,'bbox':bbox,'header':header,'column_keys':keys,'header_rows':header_rows,'data':data,'raw_cells':raw,'records':[dict(zip(keys,row)) for row in data]}


def text_candidates(text,field,info=None,labels=None):
    """Only labelled values. Do not search an unrelated window or the whole document."""
    text=clean(text)
    builtin_fields={f for spec in DEFAULT_FINANCIAL_SCHEMAS.values() for f in spec['fields']}
    all_labels=sorted({a for f in builtin_fields for a in field_aliases(f)}|set(field_aliases(field,info))|set(labels or []),key=len,reverse=True)
    stop='|'.join(re.escape(a) for a in all_labels)
    found=[]
    for alias in field_aliases(field,info):
        pattern=re.compile(r'(?<![\w\u4e00-\u9fff])'+re.escape(alias)+r'(?:\s*\(([^)]+)\))?\s*[:：]\s*([^\n;；。]+)')
        for m in pattern.finditer(text):
            value=re.split(r'(?:[,，]\s*|\s+)(?=(?:'+stop+r')\s*[:：])',m[2])[0].strip().rstrip(',，')
            hint=m[1] if m[1] in UNITS else ''
            if value and value not in MISSING:
                found.append({'raw':value,'unit_hint':hint,'quote':m[0],'offset':m.start(),'method':'label'})
    # Same labelled value may be found via aliases; retain one copy per source offset.
    unique={ (c['offset'],c['raw']):c for c in found }
    return list(unique.values())


def extract_field(text,field,description=''):
    candidates=text_candidates(text,field)
    values=list(dict.fromkeys(c['raw'] for c in candidates))
    return values[0] if len(values)==1 else ''


def build_record(fields,candidates,record_id):
    values={};normalized={};evidence={};issues=[]
    for field,info in fields.items():
        sources=candidates.get(field,[])
        checked=[(s,normalize_value(field,s['raw'],s.get('unit_hint',''),info)) for s in sources]
        checked=[(s,n) for s,n in checked if n['status']!='missing']
        distinct={(str(n['value']),n['unit']) if n['status']=='valid' else ('raw',s['raw']) for s,n in checked}
        if len(distinct)>1:
            values[field]='';normalized[field]={'value':None,'unit':None,'status':'conflict'}
            evidence[field]={'status':'conflict','candidates':sources};issues.append(f'{field}:conflict')
        elif checked:
            s,n=checked[0];normalized[field]=n
            values[field]=s['raw'] if n['status']=='valid' else ''
            evidence[field]={'status':n['status'],'candidates':sources}
            if any(c.get('needs_review') for c in sources):issues.append(f'{field}:evidence_review')
            if n['status']!='valid':issues.append(f'{field}:{n["status"]}')
        else:
            values[field]='';normalized[field]={'value':None,'unit':None,'status':'missing'}
            evidence[field]={'status':'missing','candidates':[]};issues.append(f'{field}:missing')
    return {'record_id':record_id,'fields':values,'normalized':normalized,'evidence':evidence,'quality':{'filled':sum(bool(v) for v in values.values()),'total':len(fields),'issues':issues,'needs_review':bool(issues)}}


def extract_document(parsed,schema=None):
    results={}
    for key,spec in resolve_schemas(schema).items():
        fields=spec['fields'];records=[]
        for table in parsed.get('tables',[]):
            matches={f:[i for i,h in enumerate(table['header']) if header_matches(h,f,info)] for f,info in fields.items()}
            if sum(bool(v) for v in matches.values())<2:continue
            for rownum,row in enumerate(table['data'],table.get('header_rows',1)+1):
                if [compact(c) for c in row]==[compact(c) for c in table['header']] or not any(row):continue
                if any(compact(c) in {'合计','总计','小计'} for c in row):continue
                candidates={}
                for field,indices in matches.items():
                    candidates[field]=[{'raw':row[i],'unit_hint':header_unit(table['header'][i]),'quote':row[i], 'header':table['header'][i],'page':table['page'],'table_index':table['table_index'],'row':rownum,'column':i+1,'bbox':table.get('bbox'),'method':'table','source':table.get('source','native'),'needs_review':table.get('needs_review',False)} for i in indices if i<len(row) and row[i]]
                if any(candidates.values()):records.append(build_record(fields,candidates,f'p{table["page"]}-t{table["table_index"]}-r{rownum}'))
        labels=[a for f,info in fields.items() for a in field_aliases(f,info)]
        identity={'pledge':'出质人','equity_change':'股东名称','bid_win':'项目名称','executive_change':'姓名'}.get(key)
        for page in parsed.get('page_details',[]):
            # Page boundaries are kept: joining entities across pages requires model review.
            text=clean(page.get('prose',page.get('text','')))
            starts=[]
            if identity:
                starts=sorted({c['offset'] for c in text_candidates(text,identity,fields.get(identity),labels)})
            # Repeated primary labels identify separate labelled events on one page.
            cuts=[0]+starts[1:]+[len(text)]
            for section,(begin,end) in enumerate(zip(cuts,cuts[1:]),1):
                prose={f:[] for f in fields}
                for f,info in fields.items():
                    for c in text_candidates(text[begin:end],f,info,labels):
                        source=page.get('source','native')
                        prose[f].append({**c,'offset':c['offset']+begin,'page':page['page'],'source':source,'needs_review':source!='native'})
                if any(prose.values()):records.append(build_record(fields,prose,f'labelled-p{page["page"]}-s{section}'))
        if len(records)==1:summary=copy.deepcopy(records[0])
        elif records:
            combined={f:[s for record in records for s in record['evidence'][f]['candidates']] for f in fields}
            summary=build_record(fields,combined,'summary')
            # A compatibility summary must never synthesize a new event from incomplete rows.
            for f in fields:
                if not all(r['fields'][f] for r in records) and summary['fields'][f]:
                    summary['fields'][f]=''
                    summary['normalized'][f]={'value':None,'unit':None,'status':'multiple_records'}
                    summary['evidence'][f]['status']='multiple_records'
                    summary['quality']['issues'].append(f'{f}:multiple_records')
            summary['quality']['filled']=sum(bool(v) for v in summary['fields'].values())
            summary['quality']['needs_review']=True
        else:summary=build_record(fields,{},'summary')
        summary.pop('record_id',None)
        results[key]={'name':spec['name'],**summary,'records':records}
    return results


def validate_candidates(parsed,candidates):
    """Check a host model's proposed records against page text. This proves textual
    support only, not the semantic relationship between a value and an entity.
    """
    if not isinstance(candidates,list) or len(candidates)>1000:raise ValueError('records 必须是最多 1000 条记录的数组')
    pages={p['page']:p['text'] for p in parsed['page_details']}
    records=[];rejected=[]
    for index,candidate in enumerate(candidates):
        if not isinstance(candidate,dict) or candidate.get('schema') not in DEFAULT_FINANCIAL_SCHEMAS:
            raise ValueError('每条记录必须指定一个内置 schema')
        schema=candidate['schema'];fields=DEFAULT_FINANCIAL_SCHEMAS[schema]['fields'];sources={}
        provided=candidate.get('fields')
        if not isinstance(provided,dict) or set(provided)-set(fields):raise ValueError('fields 必须为对象且不能包含未知字段')
        for field,item in provided.items():
            if item is None:continue
            if not isinstance(item,dict):raise ValueError('字段证据应包含 raw、page、quote')
            raw=item.get('raw');quote=item.get('quote');page=item.get('page')
            if not isinstance(raw,str) or not isinstance(quote,str) or not isinstance(page,int) or isinstance(page,bool):
                raise ValueError('raw/quote 必须是字符串，page 必须为整数')
            if not compact(raw) or not compact(quote) or compact(quote) not in compact(pages.get(page,'')) or compact(raw) not in compact(quote):
                rejected.append({'record':index,'field':field,'reason':'evidence_not_found'});continue
            sources[field]=[{'raw':raw,'quote':quote,'page':page,'method':'model_text_grounding','needs_review':True}]
        record=build_record(fields,sources,f'model-{index+1}')
        record['schema']=schema
        record['quality']['needs_review']=True
        records.append(record)
    return {'records':records,'rejected':rejected,'warning':'文本证据校验通过不等于字段关系正确，仍须复核主体、事项和单位。'}
