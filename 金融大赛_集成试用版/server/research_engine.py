"""Decimal financial analysis and valuation. Facts, assumptions, and signals stay separate."""
from decimal import Decimal, InvalidOperation, localcontext
from collections import defaultdict
import re, statistics, datetime

D=Decimal
def number(v):
    if isinstance(v,bool):raise ValueError('布尔值不是金额或比率')
    try:n=D(str(v))
    except (InvalidOperation,ValueError):raise ValueError('数字格式无效')
    if not n.is_finite() or abs(n)>D('1e30'):raise ValueError('非有限或超出范围的数字')
    return n
def fmt(n):return format(n.quantize(D('.000001')),'f') if n is not None else None
def period_key(p):
    m=re.fullmatch(r'((?:19|20)\d{2})(?:-(Q[1-4]|H1|9M|year-end|\d{2}-\d{2}))?',p or '')
    if not m:return None
    year=int(m[1]);kind=m[2] or 'FY'
    if re.fullmatch(r'\d{2}-\d{2}',kind):
        try:datetime.date.fromisoformat(m[1]+'-'+kind)
        except ValueError:return None
    return year,kind
def record_key(f):
    scope=f.get('scope');currency=f.get('currency')
    source=f.get('source_ref') or {}
    if not currency and source.get('sha256'):currency='未声明币种（限同文件:'+source['sha256'][:12]+'）'
    if scope=='未明确' and source.get('run_id') and f.get('evidence',{}).get('table_id'):
        scope='未声明口径（限同表:'+source['run_id'][:8]+':'+f['evidence']['table_id']+'）'
    return (f.get('issuer'),scope,currency,f.get('basis','未明确'),f.get('period'),f['name'])
def usable(f):
    key=record_key(f)
    if not period_key(f.get('period')) or not f.get('issuer') or not key[1] or not key[2] or key[1]=='未明确':return False
    if f.get('origin')=='model_semantic_candidate':return False
    expected='股' if f['name']=='shares_outstanding' else '元/股' if f['name'] in ('eps_basic','eps_diluted') else '%' if f['name']=='roe_weighted' else '元'
    if f.get('base_unit')!=expected:return False
    bad={'source_block_missing','raw_not_in_source_block','normalization_mismatch','invalid','unit_unknown','period_unknown','scope_unknown','currency_unknown'}
    if bad.intersection(f.get('review_reasons',[])):return False
    return f.get('status')=='parsed' and f.get('base_value') is not None

def ledger(records):
    grouped=defaultdict(list);issues=[]
    for f in records:
        if not usable(f):
            issues.append({'code':'excluded_unconfirmed_input','field_id':f.get('field_id'),'run_id':f.get('run_id'),
                           'metric':f['name'],'period':f.get('period'),'reasons':f.get('review_reasons',[])})
            continue
        grouped[record_key(f)].append(f)
    values={};evidence={}
    for key,items in grouped.items():
        units={f.get('base_unit') for f in items}; nums={number(f['base_value']) for f in items}
        if len(units)!=1 or len(nums)!=1:
            issues.append({'code':'conflicting_values','key':list(key),'candidates':[{'value':f['base_value'],'ref':f['source_ref']} for f in items]});continue
        values[key]=next(iter(nums)); evidence[key]=[f['source_ref'] for f in items]
    return values,evidence,issues

def analysis(records):
    values,ev,issues=ledger(records); changes=[]; ratios=[];checks=[];signals=[];derived=[]
    # Same issuer, scope, currency and accounting basis only. Cumulative columns never become single quarters directly.
    groups=defaultdict(dict)
    for key,n in values.items():groups[(*key[:4],key[5])][key[4]]=(n,ev[key])
    for group,series in groups.items():
        quarterly={}
        for p,(n,refs) in series.items():
            y,kind=period_key(p)
            if kind.startswith('Q'):quarterly[(y,int(kind[-1]))]=(n,refs,'explicit_quarter')
        flows={'revenue','revenue_total','revenue_main','revenue_cost','cost_total','net_profit','net_profit_parent','net_profit_parent_adjusted',
            'operating_cash_flow','capex','selling_expense','admin_expense','rd_expense','finance_expense','tax_expense','operating_profit'}
        for year in ({period_key(p)[0] for p in series} if group[4] in flows else set()):
            for q,cur,prev in [(2,f'{year}-H1',f'{year}-Q1'),(3,f'{year}-9M',f'{year}-H1'),(4,str(year),f'{year}-9M')]:
                if cur in series and prev in series:
                    n=series[cur][0]-series[prev][0];refs=series[cur][1]+series[prev][1]
                    if (year,q) in quarterly and quarterly[(year,q)][0]!=n:
                        issues.append({'code':'quarter_reconciliation_conflict','group':list(group),'period':f'{year}-Q{q}'});quarterly.pop((year,q))
                        series.pop(f'{year}-Q{q}',None)
                    elif (year,q) not in quarterly:
                        quarterly[(year,q)]=(n,refs,'cumulative_difference')
                        derived.append({'issuer':group[0],'scope':group[1],'currency':group[2],'basis':group[3],
                            'metric':group[4],'period':f'{year}-Q{q}','value':fmt(n),'formula':f'{cur} - {prev}','sources':refs})
        for (year,q),(n,refs,method) in quarterly.items():
            series.setdefault(f'{year}-Q{q}',(n,refs))
        for p,(current,refs) in sorted(series.items()):
            year,kind=period_key(p)
            prior=str(year-1)+('' if kind=='FY' else '-'+kind)
            comparisons=[('yoy',prior)]
            if kind.startswith('Q'):
                q=int(kind[-1]); comparisons.append(('qoq',f'{year}-Q{q-1}' if q>1 else f'{year-1}-Q4'))
            for mode,prior in comparisons:
                if prior not in series:continue
                before,priorrefs=series[prior];delta=current-before
                status='ok' if before>0 else 'zero_base' if before==0 else 'negative_base'
                changes.append({'issuer':group[0],'scope':group[1],'currency':group[2],'basis':group[3],
                    'metric':group[4],'period':p,'comparison_period':prior,'mode':mode,'current':fmt(current),'previous':fmt(before),
                    'absolute_change':fmt(delta),'percent_change':fmt(delta/before*100) if status=='ok' else None,
                    'status':status,'sign_transition':'loss_to_profit' if before<0<current else 'profit_to_loss' if before>0>current else None,
                    'formula':'(current - previous) / previous * 100; 非正基数不输出常规增长率','sources':refs+priorrefs})
    # Finite difference signals are hypotheses, never causal claims.
    scopes={k[:5] for k in values}
    def get(base,metric):return values.get((*base,metric))
    def refs(base,*metrics):return [ref for m in metrics for ref in ev.get((*base,m),[])]
    for base in sorted(scopes,key=str):
        issuer,scope,currency,basis,period=base
        def ratio(name,top,bottom,mult=1):
            a,b=get(base,top),get(base,bottom)
            if a is None or b is None or b<=0:return
            ratios.append({'issuer':issuer,'scope':scope,'currency':currency,'basis':basis,'period':period,'metric':name,
                'value':fmt(a/b*mult),'formula':f'{top}/{bottom}'+('*100' if mult==100 else ''),'sources':refs(base,top,bottom)})
        ratio('net_margin_parent','net_profit_parent','revenue',100)
        profit_basis='net_profit' if get(base,'net_profit') is not None else 'net_profit_parent'
        before=len(ratios);ratio('cash_profit_ratio','operating_cash_flow',profit_basis)
        if len(ratios)>before:ratios[-1]['denominator_metric']=profit_basis
        ratio('debt_asset_ratio','liabilities_total','assets_total',100)
        ratio('current_ratio','current_assets','current_liabilities')
        a,b=get(base,'revenue'),get(base,'revenue_cost')
        if a is not None and a>0 and b is not None:
            ratios.append({'issuer':issuer,'scope':scope,'currency':currency,'basis':basis,'period':period,'metric':'gross_margin',
                'value':fmt((a-b)/a*100),'formula':'(revenue-revenue_cost)/revenue*100','sources':refs(base,'revenue','revenue_cost')})
        assets,liabilities,equity=[get(base,m) for m in ('assets_total','liabilities_total','equity_total')]
        if all(n is not None for n in (assets,liabilities,equity)):
            from finance_extract.numbers import MULTIPLIERS
            quantum={}
            for f in records:
                if record_key(f)[:5]!=base or f['name'] not in ('assets_total','liabilities_total','equity_total'):continue
                raw=str(f.get('raw','')).replace(',','').strip()
                decimals=re.search(r'\.(\d+)',raw)
                precision=len(decimals[1]) if decimals else 0
                q=D(MULTIPLIERS.get(f.get('unit'),1))*(D(10)**(-precision))
                quantum[f['name']]=max(quantum.get(f['name'],D(0)),q)
            # Half a displayed last digit for each term, rather than a percentage of assets.
            tolerance=max(D('.01'),sum(quantum.values())/2)
            delta=assets-liabilities-equity
            checks.append({'code':'balance_sheet','issuer':issuer,'scope':scope,'currency':currency,'basis':basis,'period':period,
                'difference':fmt(delta),'tolerance':fmt(tolerance),'passed':abs(delta)<=tolerance,
                'formula':'assets_total - liabilities_total - equity_total（不可用归母权益替代总权益）','sources':refs(base,'assets_total','liabilities_total','equity_total')})
        profit,cfo,adjusted=[get(base,m) for m in (profit_basis,'operating_cash_flow','net_profit_parent_adjusted')]
        def signal(code,text,metrics):signals.append({'code':code,'issuer':issuer,'scope':scope,'currency':currency,'basis':basis,'period':period,
            'text':text,'type':'rule_signal_requires_review','sources':refs(base,*metrics)})
        if profit is not None and profit>0 and cfo is not None and cfo<0:
            signal('profit_cash_divergence','净利润为正而经营现金流为负，核查回款、存货和非现金项目；不能由此直接断言造假。',[profit_basis,'operating_cash_flow'])
        elif profit is not None and profit>0 and cfo is not None and cfo/profit<D('.5'):
            signal('weak_cash_conversion','经营现金流/利润小于0.5（启发式阈值），核查利润与现金流统计口径及原因。',[profit_basis,'operating_cash_flow'])
        profit=get(base,'net_profit_parent')
        if profit is not None and adjusted is not None and profit!=0 and abs(profit-adjusted)/abs(profit)>D('.3'):
            signal('non_recurring_profit','归母利润与扣非归母利润差额超过归母利润绝对值的30%，核查非经常性损益明细。',['net_profit_parent','net_profit_parent_adjusted'])
        for field in ('accounts_receivable','inventory'):
            relevant=[c for c in changes if c['issuer']==issuer and c['scope']==scope and c['currency']==currency and c['basis']==basis and c['period']==period and c['mode']=='yoy']
            growth={c['metric']:c for c in relevant if c['status']=='ok'}
            if field in growth and 'revenue' in growth and number(growth[field]['percent_change'])-number(growth['revenue']['percent_change'])>20:
                signal(field+'_growth_above_revenue','应收或存货增速高于收入20个百分点（启发式阈值），核查周转和业务季节性。',[field,'revenue'])
    bases=defaultdict(set)
    for f in records:bases[(f.get('issuer'),f.get('scope'))].add(f.get('basis','未明确'))
    for key,basis in bases.items():
        if len(basis)>1:issues.append({'code':'accounting_basis_changed','issuer':key[0],'scope':key[1],'bases':sorted(basis),'notice':'未跨口径做增长比较，需重述可比数据及调整说明'})
    causal=[]
    for f in records:
        if f.get('narrative'):causal.append(f['narrative'])
    from .financial_explanations import build
    explanations=build(values,ev)
    return {'schema':'finai.analysis.v1','status':'completed_with_limits','input_fields':len(records),'usable_metric_groups':len(values),**explanations,
        'changes':changes,'derived_quarters':derived,'ratios':ratios,'reconciliations':checks,'signals':signals,'input_issues':issues,
        'causal_evidence':causal[:20], 'limitations':['数据不足时不补零，不将累计数直接比较为环比。','未声明币种只允许同一来源文件比较，未明确口径只允许同一原表比较；不能跨文件拼接或据此声称已确认币种。','原因须有管理层披露/附注证据；规则信号不能证明因果或财务舞弊。',
            '利润与现金流的统计范围可能不同，现金利润比仅作为核验信号。','尚未实现全部会计政策与行业财务指标解释。']}

def assumptions(a,allow_partial=False):
    if not isinstance(a,dict) or a.get('schema')!='finai.valuation.assumptions.v1':raise ValueError('需要规范估值假设JSON模板')
    for k in ('issuer','currency','valuation_date','base_period','basis','industry')+(() if allow_partial and a.get('net_debt') is None else ('net_debt_basis',)):
        if not isinstance(a.get(k),str) or not a[k].strip():raise ValueError(f'缺少{k}及其依据')
    datetime.date.fromisoformat(a['valuation_date'])
    if not period_key(a['base_period']) or period_key(a['base_period'])[1]!='FY':raise ValueError('DCF基期必须是完整年度，不自动把季度乘四')
    if not isinstance(a.get('growth_rates'),list) or not 1<=len(a['growth_rates'])<=10:raise ValueError('预测期限必须1至10年')
    for k in ('ebit_margin','tax_rate','da_ratio','capex_ratio','nwc_ratio','wacc','terminal_growth','shares_outstanding','net_debt'):
        if allow_partial and k in ('shares_outstanding','net_debt') and a.get(k) is None:continue
        number(a.get(k))
    for k in ('tax_rate','da_ratio','capex_ratio','nwc_ratio'):
        if not 0<=number(a[k])<=1:raise ValueError(k+'必须采用0至1小数')
    if not -1<=number(a['ebit_margin'])<=1:raise ValueError('利润率必须在-1至1')
    if any(not -1<number(g)<=2 for g in a['growth_rates']):raise ValueError('收入增长率超出支持范围')
    if not 0<number(a['wacc'])<=1 or not -D('.1')<=number(a['terminal_growth'])<=D('.1') or number(a['wacc'])<=number(a['terminal_growth']):raise ValueError('WACC必须大于永续增长率，且参数需处于合理计算范围')
    if a.get('shares_outstanding') is not None and number(a['shares_outstanding'])<=0:raise ValueError('股数必须为正数')
    for k in ('wacc_grid','terminal_growth_grid'):
        if not isinstance(a.get(k),list) or not 2<=len(a[k])<=9:raise ValueError('敏感性网格每轴需要2至9个点')
    for w in a['wacc_grid']:
        for g in a['terminal_growth_grid']:
            if not 0<number(w)<=1 or not -D('.1')<=number(g)<=D('.1') or number(w)<=number(g):raise ValueError('敏感性网格存在WACC不大于永续增长率的非法点')
    if not isinstance(a.get('comparables',[]),list) or len(a.get('comparables',[]))>40:raise ValueError('可比公司数量上限40')
    if any(not isinstance(p,dict) for p in a.get('comparables',[])):raise ValueError('可比公司条目必须为对象')
    return a

def valuation(records,a,allow_partial=False):
    a=assumptions(a,allow_partial);values,ev,issues=ledger(records)
    issuer=a['issuer'];p=a['base_period'];currency=a['currency']
    # Do not silently select between original/restated numbers or merged/parent accounts.
    def select(metric,balance=False):
        periods={p,p+'-year-end'} if balance else {p}
        found=[(k,n) for k,n in values.items() if k[0]==issuer and k[1]=='合并' and k[2]==currency and k[4] in periods and k[5]==metric and (not a.get('financial_basis') or k[3]==a['financial_basis'])]
        if len(found)!=1:raise ValueError(f'{metric}的合并{p}基期数据缺失或存在多口径冲突，需要人工确认')
        k,n=found[0];return n,ev[k]
    warnings=['预测参数均为条件性假设，请逐项核对依据及适用范围。','可比公司资料限本任务提供的材料，未连接实时行情，不自动证明业务可比性。']
    relative=[]
    peers=a.get('comparables',[])
    pe_metric=a.get('pe_profit_metric','net_profit_parent')
    if pe_metric not in ('net_profit_parent','net_profit_parent_adjusted'):raise ValueError('PE利润口径仅支持归母或扣非归母')
    for metric,field,balance in [('pe',pe_metric,False),('pb','equity_parent',True)]:
        usable_peers=[]; excluded=[]
        for peer in peers:
            try:
                if not peer.get('name') or not peer.get('basis'):raise ValueError('缺名称或依据')
                date=datetime.date.fromisoformat(peer['as_of'])
                if date!=datetime.date.fromisoformat(a['valuation_date']):raise ValueError('可比公司估值日期不一致')
                multiple=number(peer[metric])
                if not 0<multiple<=200:raise ValueError('非正或极端倍数，排除并待核验')
                usable_peers.append((peer['name'],multiple))
            except (ValueError,KeyError,TypeError):excluded.append(peer.get('name','未命名') if isinstance(peer,dict) else '格式无效')
        try:amount,sources=select(field,balance)
        except ValueError:
            relative.append({'method':metric.upper(),'status':'missing_base','excluded':excluded});continue
        if amount<=0 or len(usable_peers)<2:
            relative.append({'method':metric.upper(),'status':'nonpositive_base_or_insufficient_peers','excluded':excluded});continue
        nums=sorted(n for name,n in usable_peers);median=statistics.median(nums)
        relative.append({'method':metric.upper(),'status':'ok','base_metric':field,'base_value':fmt(amount),'median_multiple':fmt(median),
            'equity_low':fmt(amount*nums[0]),'equity_mid':fmt(amount*median),'equity_high':fmt(amount*nums[-1]),
            'price_low':fmt(amount*nums[0]/number(a['shares_outstanding'])) if a.get('shares_outstanding') is not None else None,'price_mid':fmt(amount*median/number(a['shares_outstanding'])) if a.get('shares_outstanding') is not None else None,
            'price_high':fmt(amount*nums[-1]/number(a['shares_outstanding'])) if a.get('shares_outstanding') is not None else None,'included':[name for name,n in usable_peers],'excluded':excluded,'sources':sources,
            'range_basis':'已通过输入检查可比倍数的最小值至最大值，不代表概率置信区间'})
    result={'schema':'finai.valuation.v1','status':'completed_with_limits','issuer':issuer,'currency':currency,'valuation_date':a['valuation_date'],
        'assumptions':a,'input_issues':issues,'relative':relative,'warnings':warnings,
        'assumption_origin':'user_uploaded_assumptions_not_model_generated','financial_formula_basis':'https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/free-cash-flow-valuation'}
    if a['industry'] in ('bank','insurance','securities','financial'):
        result['dcf']={'status':'unsupported_industry','reason':'金融机构不套用工业企业FCFF；本版可输出PB/PE，金融行业专项模型尚未实现。'}
        return result
    revenue,sources=select('revenue')
    if revenue<=0:raise ValueError('DCF基期营业收入必须为正数')
    base_revenue=revenue;nwc=revenue*number(a['nwc_ratio']);forecast=[];wacc=number(a['wacc']);g=number(a['terminal_growth']);netdebt=number(a['net_debt']) if a.get('net_debt') is not None else None;shares=number(a['shares_outstanding']) if a.get('shares_outstanding') is not None else None
    for i,gr in enumerate(a['growth_rates'],1):
        revenue*=1+number(gr);ebit=revenue*number(a['ebit_margin'])
        # Negative EBIT does not automatically earn an immediate tax benefit.
        nopat=ebit-max(ebit,D(0))*number(a['tax_rate']);da=revenue*number(a['da_ratio']);capex=revenue*number(a['capex_ratio'])
        newnwc=revenue*number(a['nwc_ratio']);delta=newnwc-nwc;nwc=newnwc;fcff=nopat+da-capex-delta
        forecast.append({'year':i,'revenue':fmt(revenue),'ebit':fmt(ebit),'nopat':fmt(nopat),'da':fmt(da),'capex':fmt(capex),
            'nwc':fmt(nwc),'delta_nwc':fmt(delta),'fcff':fmt(fcff),'pv_fcff':fmt(fcff/(1+wacc)**i)})
    # Stable terminal reinvestment uses terminal revenue, not blind FCFF*(1+g).
    terminal_rev=revenue*(1+g);terminal_ebit=terminal_rev*number(a['ebit_margin'])
    terminal_nopat=terminal_ebit-max(terminal_ebit,D(0))*number(a['tax_rate'])
    terminal_fcff=terminal_nopat+terminal_rev*(number(a['da_ratio'])-number(a['capex_ratio']))-revenue*g*number(a['nwc_ratio'])
    if terminal_fcff<=0:
        result['dcf']={'status':'nonpositive_terminal_cash_flow','reason':'稳定期FCFF非正，当前永续DCF不适用；保留预测现金流与有效的相对估值，需补充有依据的扭亏或稳定期经营假设。',
            'base_revenue':fmt(base_revenue),'base_sources':sources,'forecast':forecast,'terminal_fcff':fmt(terminal_fcff)}
        warnings.append('没有因DCF不适用而删除其他有效方法，也没有把非正终值改成正数。')
        return result
    def calc(w,tg):
        tr=revenue*(1+tg);te=tr*number(a['ebit_margin']);tn=te-max(te,D(0))*number(a['tax_rate'])
        tf=tn+tr*(number(a['da_ratio'])-number(a['capex_ratio']))-revenue*tg*number(a['nwc_ratio'])
        if tf<=0:raise ValueError('敏感性假设下稳定期现金流非正')
        pv=sum(number(row['fcff'])/(1+w)**row['year'] for row in forecast)
        tv=tf/(w-tg);pvtv=tv/(1+w)**len(forecast);enterprise=pv+pvtv;equity=enterprise-netdebt if netdebt is not None else None
        return {'enterprise_value':fmt(enterprise),'equity_value':fmt(equity),'price_per_share':fmt(equity/shares) if equity is not None and shares is not None else None,
            'terminal_value':fmt(tv),'pv_terminal_value':fmt(pvtv),'pv_forecast':fmt(pv),'terminal_weight':fmt(pvtv/enterprise) if enterprise else None}
    with localcontext() as ctx:
        ctx.prec=38;dcf=calc(wacc,g);sensitivity=[]
        for w in a['wacc_grid']:
            for tg in a['terminal_growth_grid']:
                try:point={'status':'ok',**calc(number(w),number(tg))}
                except ValueError as exc:point={'status':'invalid_cash_flow','reason':str(exc),'enterprise_value':None,'equity_value':None,'price_per_share':None}
                sensitivity.append({'wacc':str(w),'terminal_growth':str(tg),**point})
    valid_sensitivity=[x for x in sensitivity if x['status']=='ok']
    dcf.update({'status':'ok','base_revenue':fmt(base_revenue),'base_sources':sources,'forecast':forecast,'terminal_fcff':fmt(terminal_fcff),
        'formulas':{'fcff':'EBIT - max(EBIT,0)*tax + D&A - capex - change_in_NWC','terminal':'稳定期收入、NOPAT、折旧、资本开支及营运资金分别重算；TV=FCFF(n+1)/(WACC-g)',
            'equity':'企业价值 - 显式净债务；本版净债务需包含少数股权/非经营资产等调整（如适用）'},
        'sensitivity':sensitivity,'sensitivity_price_low':fmt(min(number(x['price_per_share']) for x in valid_sensitivity)) if valid_sensitivity and dcf['price_per_share'] is not None else None,
        'sensitivity_price_high':fmt(max(number(x['price_per_share']) for x in valid_sensitivity)) if valid_sensitivity and dcf['price_per_share'] is not None else None,
        'sensitivity_enterprise_low':fmt(min(number(x['enterprise_value']) for x in valid_sensitivity)) if valid_sensitivity else None,
        'sensitivity_enterprise_high':fmt(max(number(x['enterprise_value']) for x in valid_sensitivity)) if valid_sensitivity else None,
        'sensitivity_valid_points':len(valid_sensitivity),'sensitivity_excluded_points':len(sensitivity)-len(valid_sensitivity),
        'equity_bridge':{'enterprise_value':dcf['enterprise_value'],'net_debt':str(netdebt) if netdebt is not None else None,'equity_value':dcf['equity_value'],'shares_outstanding':str(shares) if shares is not None else None}})
    if dcf['terminal_weight'] is not None and number(dcf['terminal_weight'])>D('.7'):warnings.append('终值占企业价值超过70%（启发式阈值），结果对稳定期假设依赖较高。')
    if len(valid_sensitivity)!=len(sensitivity):warnings.append('部分敏感性点的稳定期现金流非正，已保留原因并排除；区间仅含有效点，不代表完整网格均适用。')
    if dcf['equity_value'] is not None and number(dcf['equity_value'])<=0:warnings.append('企业价值不足覆盖净债务，权益值非正；不截断或伪造成正股价。')
    if netdebt is None:warnings.append('净债务及少数股权等权益桥接调整缺失，目前只给企业价值，不能当成权益价值或股价。')
    if shares is None:warnings.append('总股数缺失，当前不输出每股价值；股本金额不能直接当成股数。')
    result['dcf']=dcf
    return result
