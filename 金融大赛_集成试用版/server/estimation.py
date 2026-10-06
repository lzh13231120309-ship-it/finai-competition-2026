"""Reproducible evidence estimates and disclosed scenarios; never rewrite source facts."""
from copy import deepcopy
from decimal import Decimal as D
import datetime, statistics
from . import research_engine as e

def prepare(records, supplied=None):
    if supplied is not None and (not isinstance(supplied,dict) or supplied.get('schema')!='finai.valuation.assumptions.v1'):
        raise ValueError('估值方案须采用模板JSON；可以只填已知项，未知项保留null')
    a=deepcopy(supplied or {'schema':'finai.valuation.assumptions.v1'})
    values,refs,issues=e.ledger(records);audit=[];missing=[];peer_reviews=[]
    def assign(key,value,origin,basis,sources=None,confidence='低',bounds=None):
        a[key]=value
        audit.append({'parameter':key,'value':value,'origin':origin,'basis':basis,'sources':sources or [],'confidence':confidence,'range':bounds})
    # Unknowns may be estimated; explicit invalid values must not be silently repaired.
    for k,v in a.items():
        if v is not None and k not in ('schema','comparables'):
            audit.append({'parameter':k,'value':v,'origin':'user_supplied','basis':a.get('basis') or '用户提供；商业合理性仍待核验','sources':[],'confidence':'待核验','range':None})
    targets=[k for k in values if k[5]=='revenue' and e.period_key(k[4])[1]=='FY' and k[1]=='合并']
    if not a.get('issuer'):
        # The first source is the target; later sources can contain peer financials.
        first_run=records[0].get('run_id') if records else None
        issuers={f.get('issuer') for f in records if f.get('run_id')==first_run and f.get('issuer') and f.get('origin')!='model_semantic_candidate' and f.get('status')=='parsed' and f.get('base_value') is not None and f['name']=='revenue' and e.period_key(f.get('period')) and e.period_key(f.get('period'))[1]=='FY'}
        if len(issuers)!=1:raise ValueError('需要明确被估值的公司；请上传包含完整年度营业收入的材料或在方案中填写issuer')
        assign('issuer',next(iter(issuers)),'observed','第一份原始材料明确的年度收入主体；币种、数值及统计范围另行核验',confidence='高')
    targets=[k for k in targets if k[0]==a['issuer']]
    if not targets:
        declared=[f for f in records if f.get('issuer')==a['issuer'] and f['name']=='revenue' and e.period_key(f.get('period')) and e.period_key(f['period'])[1]=='FY']
        if declared and all(f.get('scope')!='合并' for f in declared):raise ValueError('完整年度收入的统计范围尚未确认是合并报表；请核对原文口径。已有币种确认不解决统计范围问题，不能把未知口径当作合并报表')
        if any(f.get('scope')=='合并' and not f.get('currency') for f in declared):raise ValueError('基期币种尚未确认；请明确本文件采用人民币、美元或其他币种，不能据公司名称猜币种')
        other=[k for k in values if k[0]==a['issuer'] and k[5]=='revenue' and e.period_key(k[4])[1]=='FY']
        if other:raise ValueError('完整年度收入的统计范围尚未确认是合并报表；请核对原文口径。已有币种确认不解决统计范围问题，不能把未知口径当作合并报表')
        raise ValueError('缺少可核验的完整年度营业收入；不能把季度收入直接乘四')
    if not a.get('currency'):
        currencies={k[2] for k in targets if not k[2].startswith('未声明')}
        if len(currencies)!=1:raise ValueError('基期币种缺失或冲突；不能据公司名称猜币种')
        assign('currency',next(iter(currencies)),'observed','已核验基期财报的币种',confidence='高')
    if not a.get('base_period'):
        periods=sorted({k[4] for k in targets if k[2]==a['currency']})
        if not periods:raise ValueError('缺少可核验的完整年度营业收入；不能把季度收入直接乘四')
        assign('base_period',periods[-1],'observed','最新可用完整年度，不代表最新已披露年度',confidence='高')
    p=a['base_period'];year=e.period_key(p)
    if not year or year[1]!='FY':raise ValueError('基期必须为完整年度')
    basekeys=[k for k in targets if k[2]==a['currency'] and k[4]==p]
    if len(basekeys)!=1:raise ValueError('基期合并收入缺失或存在调整前后口径冲突，需先确认')
    basekey=basekeys[0];accounting=basekey[3]
    a['financial_basis']=accounting
    def fact(metric,period=p,balance=False,issuer=None):
        periods={period,period+'-year-end'} if balance else {period}
        keys=[k for k in values if k[0]==(issuer or a['issuer']) and k[1]=='合并' and k[2]==a['currency'] and k[3]==accounting and k[4] in periods and k[5]==metric]
        return (values[keys[0]],refs[keys[0]]) if len(keys)==1 else (None,[])
    revenue,rrefs=fact('revenue')
    if revenue is None or revenue<=0:raise ValueError('基期营业收入需为正且口径唯一')
    if not a.get('industry'):
        industry='bank' if '银行' in a['issuer'] else 'insurance' if '保险' in a['issuer'] else 'securities' if '证券' in a['issuer'] else 'unclassified'
        assign('industry',industry,'observed' if industry!='unclassified' else 'scenario_assumption','名称明确金融机构时排除工业DCF，否则行业尚待确认',confidence='低')
    if not a.get('valuation_date'):
        dates={x.get('as_of') for x in a.get('comparables',[]) if isinstance(x,dict) and x.get('as_of')}
        date=next(iter(dates)) if len(dates)==1 else datetime.date.today().isoformat()
        assign('valuation_date',date,'scenario_assumption','统一采用提供的可比报价日期；无报价时采用生成日期，仅是情景参考时点')
    if a.get('comparables') is None:a['comparables']=[]
    if not isinstance(a['comparables'],list) or len(a['comparables'])>40:raise ValueError('可比公司需为不超过40条的数组')
    peers=[]
    for peer in a['comparables']:
        if not isinstance(peer,dict):raise ValueError('可比公司条目格式无效')
        peer=deepcopy(peer);reason=None
        if peer.get('name')==a['issuer']:reason='剔除目标公司自身'
        elif peer.get('currency') and peer['currency']!=a['currency']:reason='币种不同，未提供可靠汇率转换'
        elif peer.get('industry') and a['industry'] not in ('industrial','unclassified') and peer['industry']!=a['industry']:reason='行业不一致'
        elif peer.get('as_of')!=a['valuation_date']:reason='报价日期与估值日不一致'
        elif not peer.get('basis'):reason='缺少可比选择/来源依据'
        if reason:peer_reviews.append({'name':peer.get('name','未命名'),'included':False,'reason':reason});continue
        # Compute missing multiples from explicitly unit-labelled market inputs, not names.
        if peer.get('market_cap') is not None and peer.get('unit')=='元' and peer.get('currency')==a['currency']:
            cap=e.number(peer['market_cap'])
            if cap<=0:reason='市值非正'
            else:
                for multiple,metric in [('pe','net_profit_parent'),('pb','equity_parent')]:
                    amount,sources=fact(metric,balance=multiple=='pb',issuer=peer.get('name'))
                    if peer.get(multiple) is None and amount is not None and amount>0:
                        peer[multiple]=str(cap/amount);peer[multiple+'_sources']=sources
                        peer[multiple+'_basis']='提供的同日市值/同口径年度归母利润或归母权益；非TTM'
        if reason:peer_reviews.append({'name':peer.get('name'),'included':False,'reason':reason});continue
        tags=set(a.get('business_tags',[]));ptags=set(peer.get('business_tags',[]))
        overlap=sorted(tags&ptags)
        if tags and ptags and not overlap:
            peer_reviews.append({'name':peer.get('name'),'included':False,'reason':'业务标签无交集'});continue
        score=(40 if peer.get('industry')==a['industry'] and a['industry'] not in ('industrial','unclassified') else 0)+(30 if overlap else 0)
        pr,prs=fact('revenue',issuer=peer.get('name'))
        if pr is not None and D('.25')<=pr/revenue<=4:score+=15
        peer_reviews.append({'name':peer.get('name'),'included':True,'score':score,'reason':'同业/业务标签与规模核验；未提供的维度仍待确认','business_overlap':overlap})
        peer['_similarity_score']=score;peers.append(peer)
    peers.sort(key=lambda x:x['_similarity_score'],reverse=True);a['comparables']=peers
    assign('comparables',peers,'peer_input_screened','从本任务提供的公司池筛选，保留排除理由；没有自动获取实时市场行情',confidence='待核验')
    def peer_ratio(parameter):
        observations=[];evidence=[]
        for peer in peers:
            if peer['_similarity_score']<40:continue
            rr,rrs=fact('revenue',issuer=peer.get('name'))
            if rr is None or rr<=0:continue
            metric={'ebit_margin':'operating_profit','capex_ratio':'capex','da_ratio':'depreciation_amortization'}.get(parameter)
            val,src=fact(metric,issuer=peer.get('name')) if metric else (None,[])
            if val is not None and 0<=val/rr<=1:observations.append(val/rr);evidence+=src+rrs
        return (statistics.median(observations),evidence) if len(observations)>=2 else (None,[])
    if a.get('growth_rates') is None:
        series=sorted([(int(k[4]),v,refs[k]) for k,v in values.items() if k[:4]==basekey[:4] and k[5]=='revenue' and e.period_key(k[4])[1]=='FY' and int(k[4])<=year[0]])
        rates=[];src=[]
        for older,newer in zip(series,series[1:]):
            if newer[0]==older[0]+1 and older[1]>0 and newer[1]>0:rates.append(newer[1]/older[1]-1);src+=older[2]+newer[2]
        if rates:
            center=statistics.median(rates[-3:]);start=max(D('-.2'),min(D('.2'),center));origin='historical_estimate';basis='最近最多3个同口径完整年度同比的中位数，首年限幅±20%，5年逐步收敛至3%；历史不保证未来'
        else:
            peer_rates=[];peer_sources=[]
            for peer in peers:
                if peer['_similarity_score']<40:continue
                current,cr=fact('revenue',issuer=peer.get('name'));previous,pr=fact('revenue',str(year[0]-1),issuer=peer.get('name'))
                if current is not None and previous is not None and current>0 and previous>0:peer_rates.append(current/previous-1);peer_sources+=cr+pr
            if len(peer_rates)>=2:
                start=max(D('-.2'),min(D('.2'),statistics.median(peer_rates)));origin='peer_estimate';basis='至少两家同业/业务可比公司的同口径年度增长中位数，首年限幅±20%，逐步收敛至3%';src=peer_sources
            else:start=D('.03');origin='scenario_assumption';basis='缺少连续年度增长依据，以3%温和增长作探索情景；不是市场预测，也不是观测事实'
        growth=[str(start+(D('.03')-start)*D(i)/4) for i in range(5)]
        assign('growth_rates',growth,origin,basis,src,bounds={'first_year_low':str(max(D('-.8'),start-D('.05'))),'first_year_high':str(min(D('.8'),start+D('.05')))})
    defaults={'tax_rate':D('.25'),'da_ratio':D('.02'),'capex_ratio':D('.04'),'nwc_ratio':D('.15'),'wacc':D('.10'),'terminal_growth':D('.02')}
    for parameter,default in defaults.items():
        if a.get(parameter) is not None:continue
        ratio=None;src=[]
        if parameter in ('da_ratio','capex_ratio'):
            v,src=fact('depreciation_amortization' if parameter=='da_ratio' else 'capex')
            if v is not None and 0<=v/revenue<=1:ratio=v/revenue;src+=rrefs
        if ratio is None:ratio,src=peer_ratio(parameter)
        if ratio is not None:assign(parameter,str(ratio),'historical_estimate' if any(s in rrefs for s in src) else 'peer_estimate','基期或至少两家可比公司的支出/收入中位数延续为预测假设',src)
        else:assign(parameter,str(default),'scenario_assumption','探索情景占位，不能视为公司实际税率、资本成本或资本开支；需要实证替换',bounds={'low':str(max(D(0),default-D('.02'))),'high':str(default+D('.02'))})
    if a.get('ebit_margin') is None:
        val,src=fact('operating_profit');origin='historical_estimate';basis='以营业利润/收入近似经营利润率，营业利润可能含投资收益，尚需调整至EBIT'
        margin=val/revenue if val is not None else None
        if margin is None:margin,src=peer_ratio('ebit_margin');origin='peer_estimate';basis='至少两家可比公司营业利润率中位数，作为EBIT利润率近似，仍需调整口径'
        if margin is None:
            val,src=fact('net_profit_parent');tax=e.number(a['tax_rate'])
            if val is not None and tax<1:margin=val/(revenue*(1-tax));origin='historical_estimate';basis='归母净利率除以(1-假设税率)的粗略代理；少数权益、利息与非经常损益未调整，低可信度'
        if margin is None:margin=D('.1');origin='scenario_assumption';basis='没有利润率依据，以10%经营利润率作探索情景，尚无企业事实支持'
        assign('ebit_margin',str(margin),origin,basis,src+rrefs, bounds={'low':str(max(D('-1'),margin-D('.03'))),'high':str(min(D('1'),margin+D('.03')))})
    if a.get('shares_outstanding') is None:
        val,src=fact('shares_outstanding',balance=True)
        if val is not None:assign('shares_outstanding',str(val),'observed','原文明确总股数，不使用股本金额替代',src,confidence='高')
        else:a['shares_outstanding']=None;missing.append({'field':'shares_outstanding','label':'总股数','impact':'仍可估算总价值，不能输出每股价值'})
    if a.get('net_debt') is None:
        debt,ds=fact('interest_bearing_debt_total',balance=True);cash,cs=fact('cash',balance=True)
        if debt is not None and cash is not None:
            assign('net_debt',str(debt-cash),'historical_estimate','已披露有息负债合计减货币资金；受限现金、少数权益和非经营资产尚待核验',ds+cs)
            a['net_debt_basis']='有息负债合计减货币资金的近似桥接，后续需核对限制及其他调整'
        else:a['net_debt']=None;missing.append({'field':'net_debt','label':'净债务及权益桥接调整','impact':'DCF只输出企业价值，不能输出权益价值或股价'})
    if not a.get('basis'):a['basis']='衡知：历史/可比依据与探索情景逐项列示，均待用户确认'
    w=e.number(a['wacc']);g=e.number(a['terminal_growth'])
    if w<=g:raise ValueError('WACC必须大于永续增长率；明确填写的非法参数不会自动修复')
    if a.get('wacc_grid') is None:
        grid=[w-D('.02'),w,w+D('.02')]
        a['wacc_grid']=[str(x) for x in grid if g<x<=1]
        if len(a['wacc_grid'])<2:a['wacc_grid']=[str(w),str(min(D('1'),w+D('.01')))]
    if a.get('terminal_growth_grid') is None:a['terminal_growth_grid']=[str(x) for x in [g-D('.01'),g,g+D('.01')] if -D('.1')<=x<=D('.1') and x<min(e.number(y) for y in a['wacc_grid'])]
    if len(a['comparables'])<2:missing.append({'field':'comparables','label':'至少两家有依据且同日的可比公司','impact':'DCF情景可继续，PE/PB区间暂不能形成'})
    a=e.assumptions(a,allow_partial=True)
    return a,{'parameters':audit,'missing':missing,'peer_screening':peer_reviews,'needs_confirmation':any(x['origin'] not in ('observed',) for x in audit),'method':'证据优先，历史估计，其次可比公司，最后明确标注的探索情景；不估造原始财务事实'}
