"""Local document routing and evidence-based review suggestions, without invented facts."""
from pathlib import Path
from collections import Counter
import re

PROFILES = [('pledge','股份质押',('质押','质权人')),('equity_change','权益变动',('权益变动','股权变动')),('award','中标或合同',('中标','招标','合同金额')),('financial','财务报告',('年度报告','季度报告','营业收入','归母','财务报表'))]

def classify(text):
    found=[{'type':kind,'label':label} for kind,label,terms in PROFILES if any(x in text for x in terms)]
    return found or [{'type':'unknown','label':'暂未确定类型'}]

def selected_pages(total,pages='',limit=12):
    if not pages:
        indices=list(range(min(total,10)))
        if total>10:indices.extend([total-2,total-1])
    else:
        indices=[]
        for part in pages.split(','):
            bounds=[int(x) for x in part.split('-')]
            lo,hi=bounds[0],bounds[-1]
            if lo<1 or hi<lo or hi>total:raise ValueError(f'页码超出范围或顺序错误，文件共{total}页')
            indices.extend(range(lo-1,min(hi,lo+limit-1)))
            if len(set(indices))>=limit:break
    return sorted(set(indices))[:limit]

def inspect_file(path,pages=''):
    path=Path(path)
    plan={'mode':'auto','local_only':True,'profiles':classify(path.name),'sampled_pages':[], 'selection_pages':pages or 'all'}
    if path.suffix.lower() in ('.png','.jpg','.jpeg'):
        return {**plan,'tier':'basic','ocr':'ocr','reason':'图片没有PDF文字层，自动选择本地OCR；识别后需对照原图。'}
    if path.suffix.lower() in ('.docx','.xlsx'):
        return {**plan,'tier':'flash','ocr':'auto','reason':'Office文件先采用原生内容解析。'}
    from pypdf import PdfReader
    reader=PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(''):
        raise ValueError('PDF需要密码，请先用你有权限的工具解密后再提交。')
    total=len(reader.pages)
    if not total:raise ValueError('PDF没有页面')
    indices=selected_pages(total,pages)
    sample=[]; texts=[]
    for index in indices:
        text=reader.pages[index].extract_text() or ''
        count=len(re.sub(r'\s+','',text))
        sample.append({'page':index+1,'text_characters':count,'needs_ocr':count<30})
        texts.append(text)
    needs=any(x['needs_ocr'] for x in sample)
    reason='检查页中存在少文字或无文字层页面，选择本地OCR。' if needs else '检查页存在可读取文字层，优先保留原数字，选择原生解析。'
    if len(indices)<total and not pages:reason+='本次仅抽查部分页面，未检查页仍可能有扫描内容。'
    return {**plan,'tier':'basic' if needs else 'flash','ocr':'auto' if needs else 'txt',
            'reason':reason,'page_count':total,'sampled_pages':sample,'profiles':classify('\n'.join(texts)+path.name),
            'inspection_scope':'只检查所列抽样页的文字层，未做完整识别质量评分'}

def review_guidance(result,plan):
    entries=[('f'+str(i),f) for i,f in enumerate(result['fields'])]
    entries += [(f'e{i}:{name}',f) for i,e in enumerate(result['events']) for name,f in e['fields'].items()]
    counts=Counter(reason for _,f in entries for reason in set(f.get('review_reasons',[])))
    priority=[]
    urgent={'invalid','unit_unknown','period_unknown','period_requires_document_context','percentage_out_of_range','conflicting_values_or_scope'}
    for identity,f in entries:
        reasons=f.get('review_reasons',[])
        level='优先核对' if urgent.intersection(reasons) else '对照原文'
        if reasons:priority.append({'field_id':identity,'name':f.get('name'),'page':f.get('evidence',{}).get('page'),'priority':level,'reasons':reasons})
    priority.sort(key=lambda x:(x['priority']!='优先核对',x['page'] or 0))
    tips=['先核对金额和单位，再核对报告期与统计口径；程序建议不能替代人工确认。']
    if not entries:tips.append('没有提取到已支持字段，请看原始表格；文字PDF可试高质量模型，扫描件可比较OCR与高质量模型。')
    if result['fields']:tips.append('财务指标按年度与季度分别核验，确认合并或母公司及扣非口径。')
    types={e['type'] for e in result['events']}
    if 'pledge' in types:tips.append('质押先区分本次与累计数量，不把两张表混用。')
    if 'equity_change' in types:tips.append('权益变动要核对主体、变动前后股数及比例；前后总股本可能变化。')
    if types.intersection({'award','contract'}):tips.append('确认中标与签署合同的区别、是否含税及金额是否覆盖整个项目。')
    return {'method':'本地文字层检查和可解释金融规则，复杂模式使用已部署视觉模型；不调用云端对话模型',
            'plan':plan,'profiles':classify('\n'.join(b['text'] for b in result['blocks'])[:200000]),
            'candidate_fields':len(entries),'fields_with_flags':sum(bool(f.get('review_reasons')) for _,f in entries),
            'reason_counts':dict(counts),'review_queue':priority,'next_steps':tips,
            'confidence_note':'未提供概率置信度；字段未标异常也仍需人工核验。'}
