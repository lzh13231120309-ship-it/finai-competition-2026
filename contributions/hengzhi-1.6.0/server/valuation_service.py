"""Task-scoped valuation orchestration and immutable market/model snapshots."""
from __future__ import annotations
import hashlib
import json
from decimal import Decimal, InvalidOperation
from datetime import date
from . import finance_service as fs


def _date(raw, name, valuation_date=None):
    if not isinstance(raw,str) or len(raw)!=10:
        raise ValueError(f'{name} 必须是 YYYY-MM-DD 日期')
    try: parsed=date.fromisoformat(raw)
    except ValueError: raise ValueError(f'{name} 必须是 YYYY-MM-DD 日期') from None
    if parsed.isoformat()!=raw:raise ValueError(f'{name} 必须是 YYYY-MM-DD 日期')
    cutoff=date.fromisoformat(valuation_date) if valuation_date else date.today()
    if parsed>date.today() or parsed>cutoff:raise ValueError(f'{name} 不能晚于估值日期或当前日期')
    return parsed


def normalize_market(raw, valuation_date=None, reporting_currency=None):
    """Normalize explicit provenance; never fabricate sources for uncovered fields.

    Flat fields may inherit explicitly supplied common metadata. Nested metadata
    stays field-specific. Monetary/return currencies must match; no implicit FX.
    """
    if not isinstance(raw,dict):raise ValueError('market 必须是对象')
    if valuation_date:_date(valuation_date,'valuation_date')
    existing=raw.get('field_sources') or {}
    if not isinstance(existing,dict):raise ValueError('field_sources 必须是对象')
    out={};provenance={k:dict(v) for k,v in existing.items() if isinstance(v,dict)}
    common={k:raw[k] for k in ('source','source_url','as_of','currency') if raw.get(k)}
    numeric=('risk_free_rate','equity_risk_premium','beta','cost_of_debt','debt_weight')
    value_fields=set(numeric)|{'country_risk_premium','price','shares'}
    for key,value in raw.items():
        if key=='field_sources':continue
        if isinstance(value,dict) and 'value' in value:
            meta={k:v for k,v in value.items() if k!='value'}
            provenance[key]={**provenance.get(key,{}),**meta}
            if value['value'] is None or meta.get('status') in ('unavailable','missing','invalid','failed'):continue
            out[key]=value['value']
            if key=='shares' and meta.get('share_basis'):out['share_basis']=meta['share_basis']
        else:out[key]=value
        if key in value_fields and provenance.get(key,{}).get('status') in ('unavailable','missing','invalid','failed'):
            out.pop(key,None)
            continue
        if key in value_fields and out.get(key) is not None:
            # Inheritance comes only from explicit common metadata, never another field.
            provenance[key]={**common,**provenance.get(key,{})}
    for key,meta in provenance.items():
        if key not in value_fields or out.get(key) is None:continue
        if meta.get('as_of'):_date(meta['as_of'],f'market.{key}.as_of',valuation_date)
        if key=='shares':
            basis=out.get('share_basis') or meta.get('share_basis')
            if basis not in ('total_ordinary','issued_ordinary','shares_outstanding'):
                raise ValueError('market shares 需要 total_ordinary/issued_ordinary/shares_outstanding 股数口径')
            out['share_basis']=basis
            if not (meta.get('source') or meta.get('source_url')) or not meta.get('as_of'):
                raise ValueError('market shares 需要独立来源和日期')
        elif meta.get('currency') and reporting_currency and meta['currency']!=reporting_currency:
            raise ValueError(f'market.{key} 币种与财报不同；不支持自动汇率换算，请提供同币种输入及换算来源')
        if key in value_fields:
            try:parsed=Decimal(str(out[key]))
            except (InvalidOperation,ValueError,TypeError):raise ValueError(f'market.{key} 必须是有限数值') from None
            if not parsed.is_finite():raise ValueError(f'market.{key} 必须是有限数值')
    if out.get('as_of'):_date(out['as_of'],'market.as_of',valuation_date)
    currencies={meta.get('currency') for key,meta in provenance.items() if key in value_fields-{'shares'} and out.get(key) is not None and meta.get('currency')}
    if out.get('currency'):currencies.add(out['currency'])
    if len(currencies)>1:raise ValueError('market 各字段币种不一致；未进行自动换算')
    if reporting_currency and currencies and currencies!={reporting_currency}:raise ValueError('market 币种与财报不同；请提供同币种输入及换算来源')
    active=[key for key in numeric if out.get(key) is not None]
    records=[provenance.get(key,{}) for key in active]
    complete=bool(active) and all((m.get('source') or m.get('source_url')) and m.get('as_of') and m.get('currency') for m in records)
    if complete:
        out['source']='; '.join(dict.fromkeys(str(m.get('source') or m['source_url']) for m in records))
        out['as_of']=min(m['as_of'] for m in records)
        out['currency']=records[0]['currency']
    elif active and not raw.get('source'):
        # A subset's real source is not a source for an uncovered flat input.
        out.pop('source',None)
    if out.get('shares') is not None and not active:
        meta=provenance['shares']
        out['source']=meta.get('source') or meta['source_url']
        out['as_of']=meta['as_of']
    out['field_sources']=provenance
    out['provenance_complete_for_supplied_capm_fields']=complete
    return out


def _merge_options(base,changes):
    result=dict(base)
    for key,value in changes.items():
        if isinstance(value,dict) and isinstance(result.get(key),dict):result[key]=_merge_options(result[key],value)
        else:result[key]=value
    return result


def _validate_run(task_id,run):
    if run.get('task_id')!=fs._id(task_id):raise ValueError('运行记录不属于当前任务')


def _source_snapshot(analysis,task_id):
    documents=analysis.get('documents') or []
    validated=0
    for document in documents:
        if not document.get('filename') or not document.get('sha256'):continue
        try:path=fs.resolve_attachment(task_id,document['filename'])
        except FileNotFoundError:raise ValueError('分析源附件已缺失，无法核验本次建模证据，请重新上传并分析') from None
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
        if digest.hexdigest()!=document['sha256']:raise ValueError('分析源附件内容已改变，当前分析证据已过期；请重新分析后建模')
        validated+=1
    return {'status':'verified_attachment_hashes' if documents and validated==len(documents) else 'saved_analysis_without_complete_document_hashes',
            'analysis_run_id':analysis['run_id'],'documents':documents}


def _compact(run):
    keys=('run_id','task_id','kind','ok','engine_version','created_at','parent_run_id','forensic_run_id',
          'company','filenames','valuation_review','model_plan','range','discount_rate','terminal_policy','assumption_sources',
          'data_quality','data_needs','stress_diagnostic','warnings','checks','input_snapshot_sha256','source_snapshot')
    out={key:run[key] for key in keys if key in run}
    out['scenarios']={name:{**{key:scenario[key] for key in ('label','parameters','valuation','scenario_status') if key in scenario},
                            'projection_rows':len(scenario.get('projection',[])),
                            'risk_adjustment_count':len(scenario.get('risk_adjustments',[])),
                            # 三表只回摘要：逐年三张表很大，需要时用
                            # read_valuation_model(section='three_statements') 分页取。
                            'three_statements':{key:(scenario.get('three_statements') or {}).get(key)
                                                for key in ('ok','mode','interest_basis','converged',
                                                            'checks','warnings','reason')
                                                if (scenario.get('three_statements') or {}).get(key) is not None},
                            'evidence_driven_adjustments':scenario.get('evidence_driven_adjustments'),
                            'floor_applied':[item for item in (scenario.get('floor_applied') or [])
                                             if item.get('binding')]}
                      for name,scenario in run.get('scenarios',{}).items()}
    # 这几项是"结论有多可靠"的直接证据，必须在建模返回里就能看到，
    # 否则模型要再发一轮读取请求才能拿到，实际使用中经常被漏掉。
    for key in ('financing_rates','scenario_distinctness','terminal_policy_checks'):
        if run.get(key) is not None:out[key]=run[key]
    if run.get('method_triangle'):
        triangle=run['method_triangle']
        out['method_triangle']={key:triangle.get(key) for key in
                                ('ok','dispersion','dispersion_alert','dispersion_note',
                                 'per_share','methods','note','reason') if key in triangle}
    if run.get('discount_rate_detail'):
        detail=run['discount_rate_detail']
        out['discount_rate_detail']={key:detail.get(key) for key in
                                     ('ok','wacc','cost_of_equity','weight_debt','beta',
                                      'risk_free_rate','equity_risk_premium','statement_calibrated',
                                      'statement_sources','summary','reason','attribution','sources',
                                      'external_parameters','assumed_parameters') if key in detail}
    if run.get('peers'):
        peer=run['peers']
        out['peers']={key:peer.get(key) for key in ('ok','warnings','currency','period','scope')}
        out['peers']['accepted_count']=len(peer.get('accepted_peers',[]))
        out['peers']['excluded_count']=len(peer.get('excluded_peers',[]))
        out['peers']['methods']={name:{k:method.get(k) for k in ('ok','n','sample_quality','median_multiple','equity_values','enterprise_values','per_share_values','reason') if k in method} for name,method in peer.get('methods',{}).items()}
    if run.get('monte_carlo'):out['monte_carlo']={key:value for key,value in run['monte_carlo'].items() if key not in ('variables','correlations')}
    if run.get('reverse_dcf'):out['reverse_dcf']=run['reverse_dcf']
    return out


def market_data(task_id, args):
    from .valuation_market import get_market_data
    result = get_market_data(args, resolve_file=lambda name: fs.resolve_attachment(task_id,name))
    run = fs.save_run(task_id, {**result,'kind':'valuation_market','parameters':args})
    return {**result,'run_id':run['run_id'],'complete_result':f"/api/financial/artifacts/{task_id}/{run['run_id']}/run.json"}


def build_model(task_id, args):
    from .valuation_advanced import build_advanced_model
    from .valuation_peers import comparable_valuation
    if not isinstance(args.get('options') or {},dict):raise ValueError('options 必须是对象')
    options = _merge_options(args.get('options') or {},{k:v for k,v in args.items() if k not in ('task_id','options','run_id','filenames','forensic_run_id','export_formats') and v not in (None,'')})
    formats = args.get('export_formats') or ['xlsx','docx','json','md']
    if not isinstance(formats,list) or any(f not in ('xlsx','docx','json','md') for f in formats):
        raise ValueError('导出格式支持 xlsx、docx、json、md')
    analysis = fs.load_run(task_id,args['run_id']) if args.get('run_id') else fs._latest_run(task_id,('analysis',))
    filenames = args.get('filenames')
    if filenames is not None and (not isinstance(filenames,list) or any(not isinstance(name,str) for name in filenames)):raise ValueError('filenames 必须是文件名列表')
    if filenames and (not analysis or sorted(filenames)!=sorted(analysis.get('filenames') or [])):
        result = fs.analyze_financial_report(task_id, {'filenames':filenames})
        analysis = fs.load_run(task_id,result['run_id'])
    if not analysis: raise ValueError('请先上传并分析财报，或指定财报分析运行编号')
    _validate_run(task_id,analysis)
    if analysis.get('kind') != 'analysis': raise ValueError('run_id 必须指向财报分析结果')
    source_snapshot=_source_snapshot(analysis,task_id)
    forensic = None
    if args.get('forensic_run_id'):
        forensic=fs.load_run(task_id,args['forensic_run_id'])
        _validate_run(task_id,forensic)
        if forensic.get('kind')!='forensic' or forensic.get('parent_run_id')!=analysis['run_id']:
            raise ValueError('法证结果与本次财报分析不匹配')
    else:
        for entry in fs.list_runs(task_id):
            if entry['kind']=='forensic':
                candidate=fs.load_run(task_id,entry['run_id'])
                _validate_run(task_id,candidate)
                if candidate.get('parent_run_id')==analysis['run_id']: forensic=candidate;break
    report_ccy=analysis.get('currency') or next((f.get('currency') for f in analysis.get('facts',[]) if f.get('status')=='verified' and f.get('currency') not in (None,'','unknown')),None)
    options['market']=normalize_market(options.get('market') or {},options.get('valuation_date'),report_ccy)
    snapshot = None
    if options.get('market_symbol'):
        snapshot=market_data(task_id,{'symbol':options['market_symbol'],'benchmark':options.get('benchmark'),
            'period':'2y','as_of':options.get('valuation_date')})
        if snapshot.get('ok'):
            # A price in a different currency cannot enter reverse DCF silently.
            report_ccy=analysis.get('currency') or next((f.get('currency') for f in analysis.get('facts',[]) if f.get('currency')),None)
            if snapshot.get('currency')==report_ccy:
                options['market']=normalize_market({**snapshot.get('market_fields',{}),**options['market']},options.get('valuation_date'),report_ccy)
                options['market'].setdefault('currency',report_ccy)
    result=build_advanced_model(analysis,(forensic or {}).get('findings',[]),options)
    share_meta=options['market'].get('field_sources',{}).get('shares')
    if share_meta and options['market'].get('shares') is not None and 'shares' not in options.get('overrides',{}):
        result.setdefault('inputs_used',{})['shares']={**share_meta,'share_basis':options['market']['share_basis']}
        result.setdefault('assumption_sources',{})['shares']={**share_meta,'share_basis':options['market']['share_basis']}
    if snapshot:
        result['market_snapshot']=snapshot
        if snapshot.get('currency') != result.get('model_plan',{}).get('currency'):
            result.setdefault('warnings',[]).append('行情币种与财报不同或未知，未将价格和 beta 自动用于模型；请提供同币种数据或明确换算后的输入与来源。')
    peer_run_ids=options.get('peer_run_ids') or []
    if not isinstance(peer_run_ids,list) or any(not isinstance(run_id,str) for run_id in peer_run_ids):raise ValueError('peer_run_ids 必须是分析运行编号列表')
    if len(peer_run_ids)>200:raise ValueError('同行分析运行最多200个')
    peer_analysis_sources=[]
    for peer_id in dict.fromkeys(peer_run_ids):
        peer_analysis=fs.load_run(task_id,peer_id)
        _validate_run(task_id,peer_analysis)
        if peer_analysis.get('kind')!='analysis':raise ValueError('peer_run_ids 必须指向当前任务财报分析结果')
        peer_analysis_sources.append(_source_snapshot(peer_analysis,task_id))
    if peer_analysis_sources:
        result['peer_analysis_sources']=peer_analysis_sources
        result.setdefault('warnings',[]).append('同行分析运行仅提供财务证据；仍需 peers 中有来源、同日期和币种的市场估值快照，未以财报运行猜测市值或倍数。')
    peers=options.get('peers') or []
    if not isinstance(peers,list):raise ValueError('peers 必须是同行快照对象列表')
    if peers and result.get('ok'):
        base=result['scenarios']['base'];p=base['parameters'];quality=result.get('data_quality',{})
        target={'company':analysis.get('company'),'symbol':options.get('market_symbol'),
            'currency':result['model_plan'].get('currency'),'period':quality.get('reporting_period'),
            'scope':quality.get('reporting_scope') or 'consolidated','industry':options.get('industry'),
            'valuation_date':options.get('valuation_date') or date.today().isoformat(),
            'market_as_of':options['market'].get('as_of'),
            'revenue':p.get('revenue_base'),'net_income':p.get('net_income',p.get('parent_net_profit_base')),
            'ebitda':p.get('ebitda_base'),'shares':p.get('shares'),'net_debt':p.get('net_debt'),
            'minority_interest':p.get('minority_interest'),'non_operating_assets':p.get('non_operating_assets'),'book_equity':p.get('book_equity')}
        for fact in analysis.get('facts',[]):
            if fact.get('code')=='parent_equity' and fact.get('status')=='verified' and fact.get('period')==target['period'] and fact.get('currency')==target['currency'] and fact.get('scope')==target['scope']:
                target['book_equity']=fact.get('value')
        result['peers']=comparable_valuation(target,peers)
    from .valuation_review import review_model
    result['valuation_review']=review_model(result)
    payload={**result,'kind':'valuation_model','parent_run_id':analysis['run_id'],
        'forensic_run_id':(forensic or {}).get('run_id'),'filenames':analysis.get('filenames',[]),
        'company':analysis.get('company') or next((f.get('company') for f in analysis.get('facts',[]) if f.get('company')),''),
        'options':options,'source_snapshot':source_snapshot,'input_snapshot_sha256':hashlib.sha256(json.dumps({'analysis':analysis,'findings':(forensic or {}).get('findings',[]),'options':options},sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()}
    run=fs.save_run(task_id,payload)
    exports=[fs.export_financial_report(task_id,{'run_id':run['run_id'],'format':fmt}) for fmt in dict.fromkeys(formats)]
    summary=_compact(run)
    summary['exports']=exports
    summary['complete_result']=f"/api/financial/artifacts/{task_id}/{run['run_id']}/run.json"
    summary['note']='逐年预测与完整模拟结果已保存；read_valuation_model 按章节分页读取。Excel 中蓝色假设可编辑，模拟与同行章节为本次快照，修改后需重新运行。'
    return summary


def read_model(task_id,args):
    run=fs.load_run(task_id,args.get('run_id',''))
    _validate_run(task_id,run)
    if run.get('kind')!='valuation_model':raise ValueError('不是自动估值模型运行')
    section=args.get('section') or 'summary'
    scenario=args.get('scenario') or 'base'
    offset=max(0,int(args.get('offset') or 0));limit=min(100,max(1,int(args.get('limit') or 20)))
    # ---- 平衡三表：按情景读取 ----
    # 引擎已经生成逐年配平的三张表并做了五项勾稽，但如果读取接口拿不到，
    # 报告就只能引用 DCF 的结论、无法引用"资产=负债+权益残差 0 元"这类
    # 可机检的证据 —— 引擎的能力会卡在接口这一层用不上。
    if section in ('three_statements','three_statement_rows','fcff_cross_check'):
        model=(run.get('scenarios',{}).get(scenario) or {}).get('three_statements') or {}
        rows=model.get('statements') if section=='three_statements' else model.get('fcff_cross_check')
        rows=rows or []
        summary={key:model.get(key) for key in
                 ('ok','mode','interest_basis','converged','other_noncurrent_policy',
                  'opening_nwc','opening_nwc_core','checks','warnings','reason') if key in model}
        alignment=model.get('fcff_alignment') or {}
        summary['fcff_alignment']={key:value for key,value in alignment.items() if key!='core_vs_full'}
        summary['statement_count']=len(model.get('statements') or [])
        summary['fcff_cross_check_rows']=len(model.get('fcff_cross_check') or [])
        summary['financing_rates']=run.get('financing_rates')
        return {'run_id':run['run_id'],'section':section,'scenario':scenario,'result':summary,
                'items':rows[offset:offset+limit],'total':len(rows),
                'next_offset':offset+limit if offset+limit<len(rows) else None}
    if section=='summary': value={k:run.get(k) for k in ('model_plan','range','discount_rate','warnings','data_quality')}
    elif section=='projection': value=run.get('scenarios',{}).get(scenario,{}).get('projection',[])
    elif section in ('valuation_review','scenarios','assumption_sources','checks','peers','monte_carlo','reverse_dcf',
                     'sensitivity','terminal_policy','inputs_used','options','market_snapshot',
                     'discount_rate_detail','method_triangle','scenario_distinctness',
                     'financing_rates','terminal_policy_checks','stress_diagnostic'):
        value=run.get(section)
    else:raise ValueError('不支持的模型章节')
    if isinstance(value,list):return {'run_id':run['run_id'],'section':section,'total':len(value),'items':value[offset:offset+limit],'next_offset':offset+limit if offset+limit<len(value) else None}
    return {'run_id':run['run_id'],'section':section,'result':value}
