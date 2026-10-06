"""Literal CSV/text adapter; native inputs never claim an OCR engine ran."""
import csv, io, re, datetime
from collections import Counter
from pathlib import Path
from .extract import extract_middle, METRICS
from .numbers import quantity, infer_unit

TEXT_SUFFIXES={'.csv','.tsv','.txt','.md'}
def decode(raw):
    if raw.startswith((b'\xff\xfe',b'\xfe\xff')):
        return raw.decode('utf-16')
    for enc in ('utf-8-sig','gb18030'):
        try:return raw.decode(enc)
        except UnicodeDecodeError:pass
    raise ValueError('无法可靠解码，请另存为UTF-8；不替换乱码后猜测数字')

def parse_native(path, name, digest):
    p=Path(path); text=decode(p.read_bytes()); blocks=[];fields=[]
    if len(text)>2_000_000:raise ValueError('文本超过200万字符限制，请拆分后处理')
    if p.suffix.lower() in ('.csv','.tsv'):
        delimiter='\t' if p.suffix.lower()=='.tsv' else ','
        source=csv.DictReader(io.StringIO(text),delimiter=delimiter,strict=True)
        if source.fieldnames:
            source.fieldnames=[h.strip() for h in source.fieldnames]
            duplicates=[h for h,n in Counter(source.fieldnames).items() if n>1]
        required={'metric','period','raw','unit'}
        if not required.issubset(source.fieldnames or []):
            # A generic CSV is preserved as a table for the existing extraction layer.
            import html
            rows=list(csv.reader(io.StringIO(text),delimiter=delimiter))
            if len(rows)>10000:raise ValueError('CSV超过10000行')
            body='<table>'+''.join('<tr>'+''.join('<td>'+html.escape(c)+'</td>' for c in row)+'</tr>' for row in rows)+'</table>'
            return extract_middle({'pages':[{'page_idx':0,'blocks':[{'index':0,'type':'table','content':body}]}]},name,digest)
        if duplicates:raise ValueError('CSV表头重复，可能覆盖字段，请先确认各列含义：'+ '、'.join(duplicates))
        allowed=set(METRICS.values())|{'capex','equity_total','revenue_cost','accounts_receivable','inventory','current_assets','current_liabilities'}
        for i,row in enumerate(source):
            if i>=10000:raise ValueError('CSV超过10000行')
            if None in row:raise ValueError(f'CSV第{i+2}行的列数超过表头，不能静默丢弃多余值')
            metric=(row.get('metric') or '').strip()
            if metric not in allowed:continue
            literal=io.StringIO();writer=csv.writer(literal,delimiter=delimiter);writer.writerow([row.get(h,'') for h in source.fieldnames])
            quote=literal.getvalue().strip('\r\n'); q=quantity(row.get('raw') or '',row.get('unit') or None)
            reviews=[]
            if q['status']!='parsed':reviews.append(q['status'])
            period=(row.get('period') or '').strip()
            if not re.fullmatch(r'(?:19|20)\d{2}(?:-(?:Q[1-4]|H1|9M|year-end|\d{2}-\d{2}))?',period):reviews.append('period_unknown')
            elif re.fullmatch(r'\d{4}-\d{2}-\d{2}',period):
                try:datetime.date.fromisoformat(period)
                except ValueError:reviews.append('period_unknown')
            scope=(row.get('scope') or '未明确').strip();currency=(row.get('currency') or '').strip() or None
            if scope not in ('合并','母公司'):reviews.append('scope_unknown')
            if not currency:reviews.append('currency_unknown')
            blocks.append({'page':1,'block':i,'kind':'text','bbox':None,'text':quote})
            fields.append({'name':metric,'label':metric,**q,'period':period or None,'issuer':row.get('issuer') or None,
                'scope':scope,'basis':row.get('basis') or '未明确','currency':currency,'column_header':period,'origin':'native_literal',
                'review_reasons':reviews,'evidence':{'page':1,'block':i,'bbox':None,'row':i+2,'column':source.fieldnames.index('raw')+1,'quote':quote},
                'source_location_kind':'csv_row'})
        return {'schema':'finai.extraction','schema_version':'1.1','source':{'filename':name,'sha256':digest},
            'engine':'native_csv_literal','fields':fields,'events':[],'tables':[],'blocks':blocks,
            'warnings':[] if fields else ['CSV未识别规范金融字段，请使用模板或查看原始表格'],
            'review_required':any(f['review_reasons'] for f in fields)}
    for i,line in enumerate(text.splitlines()):
        if line.strip():blocks.append({'index':i,'type':'text','content':line})
    result=extract_middle({'pages':[{'page_idx':0,'blocks':blocks}]},name,digest)
    result['engine']='native_text_literal'
    for f in result['fields']:f['source_location_kind']='text_line'
    result['warnings'].append('纯文本行号或CSV行号不是原始财报页码；本地来源链接用于定位输入文件。')
    return result
