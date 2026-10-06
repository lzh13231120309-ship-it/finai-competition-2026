"""Human readable financial delivery from verified artifacts, with stable numbers."""
from decimal import Decimal as D
import html,re
LABELS={'revenue':'营业收入','net_profit_parent':'归母净利润','net_profit_parent_adjusted':'扣非归母净利润','operating_cash_flow':'经营现金流',
 'revenue_cost':'营业成本','gross_margin':'毛利率','net_margin_parent':'归母净利率','cash_profit_ratio':'现金利润比','debt_asset_ratio':'资产负债率','current_ratio':'流动比率',
 'assets_total':'总资产','liabilities_total':'总负债','equity_total':'总权益','equity_parent':'归母净资产','cash':'货币资金','operating_profit':'营业利润',
 'accounts_receivable':'应收账款','inventory':'存货','shares_outstanding':'总股数','interest_bearing_debt_total':'有息负债','depreciation_amortization':'折旧摊销','capex':'资本开支',
 'issuer':'公司','industry':'行业','currency':'币种','base_period':'基期','valuation_date':'估值日','growth_rates':'收入增长率','ebit_margin':'经营利润率假设','tax_rate':'税率假设',
 'da_ratio':'折旧摊销/收入','capex_ratio':'资本开支/收入','nwc_ratio':'经营营运资金/收入','wacc':'折现率','terminal_growth':'永续增长率','shares_outstanding':'总股数',
 'net_debt':'净债务与权益桥接调整','comparables':'可比公司','basis':'假设依据','financial_basis':'会计口径','net_debt_basis':'权益桥接依据'}
ORIGINS={'user_supplied':'用户提供','observed':'原文事实','historical_estimate':'历史数据估计','peer_estimate':'可比公司估计','scenario_assumption':'探索情景假设','peer_input_screened':'可比资料筛选'}
LABELS.update({'current_assets':'流动资产','current_liabilities':'流动负债','revenue_total':'营业总收入','revenue_main':'主营收入','net_profit':'净利润','cost_total':'营业总成本','selling_expense':'销售费用','admin_expense':'管理费用','rd_expense':'研发费用','finance_expense':'财务费用','tax_expense':'所得税费用','excluded_unconfirmed_input':'未确认字段已排除','conflicting_values':'相同口径数值冲突','accounting_basis_changed':'会计口径变化','quarter_reconciliation_conflict':'单季还原勾稽冲突'})
LABELS.update({'receivable_days':'应收周转天数（收入近似）','inventory_days':'存货周转天数（成本近似）'})
LABELS.update({'roe_weighted':'加权平均净资产收益率','eps_basic':'基本每股收益','eps_diluted':'稀释每股收益'})
LABELS['conflicting_values_or_scope']='数值冲突或统计范围需要核对'
LABELS.update({'period_unknown':'报告期间尚未确认','scope_unknown':'统计范围尚未确认','currency_unknown':'币种尚未确认','unit_unknown':'金额或数量单位尚未确认','source_block_missing':'原文位置未找到','raw_not_in_source_block':'数值与对应原文不一致','normalization_mismatch':'原值与单位换算结果不一致','conflicting_values':'同口径数值存在冲突','invalid_numeric_value':'数字格式需要核对','percentage_out_of_range':'比例超出合理取值范围','model_semantic_candidate':'模型补充项目尚待核验','invalid':'该项目尚未通过核验','needs_review':'仍有项目待核验','needs_input':'需要补充关键资料'})

def num(value,digits=2):
    if value is None:return '未计算'
    try:return f'{D(str(value)):,.{digits}f}'
    except Exception:return str(value)

def human(text):
    text=str(text)
    for key in sorted(LABELS,key=len,reverse=True):text=text.replace(key,LABELS[key])
    return text

def readable_excerpt(text):
    """Render parser HTML as quotation text; original evidence remains unchanged."""
    return re.sub(r'\s+',' ',html.unescape(re.sub(r'</?(?:table|thead|tbody|tr|td|th|div|p|span|br|b|strong|i|em)(?:\s[^>]*)?/?>',' ',str(text),flags=re.I))).strip()

def period_label(value):
    text=str(value)
    if len(text)==4 and text.isdigit():return text+'年度'
    labels={'year-end':'年末','H1':'年上半年','9M':'年前三季度','Q1':'年第一季度','Q2':'年第二季度','Q3':'年第三季度','Q4':'年第四季度'}
    parts=text.split('-',1)
    if len(parts)==2 and parts[1] in labels:return parts[0]+labels[parts[1]]
    return text

def change_text(c):
    name=LABELS.get(c['metric'],c['metric']);mode='同比' if c['mode']=='yoy' else '环比'
    if c['percent_change'] is not None:return f"{name}{mode}{'增长' if D(c['percent_change'])>=0 else '下降'}{num(abs(D(c['percent_change'])))}%"
    if c.get('sign_transition')=='loss_to_profit':return name+'由亏损转为盈利，负基数下不使用常规增长率'
    if c.get('sign_transition')=='profit_to_loss':return name+'由盈利转为亏损'
    return name+'比较期为零或负数，需结合绝对变化判断'

def analysis_paragraphs(data):
    changes=sorted(data.get('changes',[]),key=lambda x:(x['period'],x['mode']=='yoy'),reverse=True)
    main=next((c for c in changes if c['metric']=='revenue'),None)
    selected=[c for c in changes if c['metric'] in ('revenue','net_profit_parent','net_profit_parent_adjusted','operating_cash_flow')]
    if main:selected=[c for c in selected if (c['issuer'],c['period'],c['scope'],c['currency'],c['basis'])==(main['issuer'],main['period'],main['scope'],main['currency'],main['basis'])]
    paragraphs=[]
    if selected:
        c=selected[0];intro=f"{c['issuer']}在{period_label(c['period'])}，"+'；'.join(change_text(x) for x in selected[:3])+'。'
        rev=next((x for x in selected if x['metric']=='revenue' and x['percent_change'] is not None),None)
        profit=next((x for x in selected if x['metric']=='net_profit_parent' and x['percent_change'] is not None),None)
        if rev and profit and rev['mode']==profit['mode']:
            if abs(D(profit['percent_change'])-D(rev['percent_change']))<=1:intro+='利润与收入增速基本相当；需要结合毛利率、费用和非经常项目判断增长质量。'
            elif D(profit['percent_change'])>D(rev['percent_change']):intro+='利润增速高于收入，盈利表现改善；是否来自成本、费用或非经常项目，仍需对应附注支持。'
            else:intro+='利润增速未超过收入，需继续核对成本费用、产品结构及非经常项目，暂不能断定单一原因。'
        paragraphs.append(intro)
    else:paragraphs.append('当前资料尚不能形成可靠的增长比较。可以先阅读已识别指标；补充相同主体、期间与会计口径的比较数据后，分析会更完整。')
    period=main['period'] if main else None
    ratios=[r for r in data.get('ratios',[]) if not main or (r['issuer'],r['period'],r['scope'],r['currency'],r['basis'])==(main['issuer'],main['period'],main['scope'],main['currency'],main['basis'])]
    gross=next((r for r in ratios if r['metric']=='gross_margin'),None)
    cash=next((r for r in ratios if r['metric']=='cash_profit_ratio'),None)
    if gross or cash:
        parts=[]
        if gross:
            previous_period=str(int(gross['period'][:4])-1)+gross['period'][4:]
            previous=next((x for x in data.get('ratios',[]) if x['metric']=='gross_margin' and x['period']==previous_period and all(x[k]==gross[k] for k in ('issuer','scope','currency','basis'))),None)
            detail='用于观察主营业务的盈利空间；缺少往期毛利率时不能直接判断改善'
            if previous:
                delta=D(gross['value'])-D(previous['value']);detail='观察主营业务盈利空间，较上年同期'+('基本持平' if abs(delta)<D('.1') else ('提高' if delta>0 else '下降')+num(abs(delta))+'个百分点')
            parts.append('毛利率为'+num(gross['value'])+'%，'+detail)
        if cash:
            v=D(cash['value']);profit_label=LABELS.get(cash.get('denominator_metric','net_profit_parent'),'利润')
            parts.append('经营现金流约为'+profit_label+'的'+num(v)+'倍，'+('利润向现金的转化偏弱，需要关注回款及营运资金占用' if v<D('.5') else '现金实现情况可供进一步核对，不宜仅凭这一比率下结论')+('；以归母利润代理时需注意统计范围差异' if cash.get('denominator_metric','net_profit_parent')=='net_profit_parent' else ''))
        paragraphs.append('；'.join(parts)+'。')
    matched=lambda x:not main or all(x[k]==main[k] for k in ('issuer','period','scope','currency','basis'))
    bridge=next((b for b in data.get('profit_bridges',[]) if matched(b)),None)
    if bridge:paragraphs.append(f"毛利较上年变化{num(D(bridge['gross_profit_change'])/10000)}万元，其中收入变化贡献{num(D(bridge['revenue_effect'])/10000)}万元、毛利率变化贡献{num(D(bridge['margin_effect'])/10000)}万元。这是数值桥接；价格、销量与产品结构等具体原因还需业务披露支持。" if bridge['currency']=='CNY' else f"毛利变化{num(bridge['gross_profit_change'])} {bridge['currency']}，收入变化贡献{num(bridge['revenue_effect'])}、毛利率变化贡献{num(bridge['margin_effect'])}；属于数值分解，业务原因仍待核实。")
    turnover=[x for x in data.get('turnover',[]) if matched(x) and x['status']=='ok']
    if turnover:paragraphs.append('按年初年末平均余额估算，'+'、'.join(LABELS[x['metric']]+'为'+num(x['value'])+'天' for x in turnover)+'。需结合季节性、账龄与减值附注核查。')
    signals=[s for s in data.get('signals',[]) if not main or (s['issuer'],s['period'],s['scope'],s['currency'],s['basis'])==(main['issuer'],main['period'],main['scope'],main['currency'],main['basis'])]
    if signals:paragraphs.append('值得继续核查的是：'+'；'.join(s['text'] for s in signals[:2]))
    excerpts=data.get('causal_evidence',[])
    if excerpts:paragraphs.append('材料中找到了与业绩原因或会计处理有关的披露，详细报告保留了页码和原文。它们是进一步核验的线索，需要确认是否解释上述同一期指标。')
    if data.get('input_issues'):paragraphs.append(f"另有{len(data['input_issues'])}项输入或口径问题已单独列出，未作为可靠事实参与计算。")
    return paragraphs

def valuation_paragraphs(data):
    if data['status']=='needs_input':return ['目前还不能形成可靠估值：'+human(data['reason'])+'。已有财务分析可以继续查看；补齐这些关键资料后，我会在相同依据上重算。']
    audit=data.get('estimation_audit',{});d=data.get('dcf',{});a=data['assumptions'];paras=[]
    estimates=[x for x in audit.get('parameters',[]) if x['origin'] in ('historical_estimate','peer_estimate','scenario_assumption')]
    if estimates:paras.append('我先根据已有资料建立了一版条件性估值，尚未确认的参数已单独标注。收入增长参考'+('同口径历史趋势' if any(x['parameter']=='growth_rates' and x['origin']=='historical_estimate' for x in estimates) else '可比资料或探索情景')+'，不会将假设写成已披露事实。')
    else:paras.append('以下估值采用你提供的经营假设，并与可用的可比公司数据交叉观察。')
    if d.get('status')=='ok':
        if d.get('price_per_share') is not None:
            interval=f"折现率与永续增长率变化下约为{num(d['sensitivity_price_low'])}—{num(d['sensitivity_price_high'])} {data['currency']}/股。这是模型情景区间，不是对未来价格的保证。" if d.get('sensitivity_price_low') is not None else '当前敏感性网格未形成有效区间。'
            paras.append(f"基准DCF约为{num(d['price_per_share'])} {data['currency']}/股，"+interval)
        else:
            interval=f"敏感性区间约为{num(D(d['sensitivity_enterprise_low'])/10000)}—{num(D(d['sensitivity_enterprise_high'])/10000)} 万{data['currency']}。" if d.get('sensitivity_enterprise_low') is not None else '当前敏感性网格未形成有效区间。'
            paras.append(f"基准DCF企业价值约为{num(D(d['enterprise_value'])/10000)} 万{data['currency']}；"+interval+('净债务调整尚缺，暂不把企业价值换算成权益价值或股价。' if a.get('net_debt') is None else '股数尚缺，暂不输出每股价值。'))
        if d.get('terminal_weight') is not None and D(d['terminal_weight'])>D('.7'):paras.append('终值占比偏高，结果主要依赖稳定期的长期假设；应优先检查折现率、增长率和再投资需求，再考虑增加预测细节。')
        if d.get('sensitivity_excluded_points'):paras.append(f"有{d['sensitivity_excluded_points']}个敏感性点因稳定期现金流非正而不适用；显示区间仅来自有效点，详细报告保留了排除原因。")
    else:paras.append(human(d.get('reason','DCF暂不可计算')))
    relatives=[r for r in data.get('relative',[]) if r.get('status')=='ok']
    if relatives:
        r=relatives[0];paras.append(f"相对估值参考{len(r['included'])}家可比公司，{r['method']}倍数中位数为{num(r['median_multiple'])}倍。"+('它与DCF采用不同方法，不能直接取平均视为最终结论；详细报告列出了入选、排除理由及区间。'))
    else:paras.append('当前缺少足够有效的可比公司倍数，暂不编造同行区间；补充同业、同日且有来源的公司资料后可继续估计。')
    missing=audit.get('missing',[])
    if missing:paras.append('优先补充：'+'、'.join(x['label'] for x in missing)+'。详细报告说明了每一项会影响什么。')
    return paras

def chat(data,downloads):
    analysis=data['schema']=='finai.analysis.v1';paras=analysis_paragraphs(data) if analysis else valuation_paragraphs(data)
    intro='### '+('经营表现与值得关注的变化' if analysis else '估值判断与假设')+'\n\n'+'\n\n'.join(paras[:4]);lines=[]
    if analysis:
        rows=[c for c in data.get('changes',[]) if c['metric'] in ('revenue','net_profit_parent','operating_cash_flow')]
        main=next((c for c in sorted(data.get('changes',[]),key=lambda x:x['period'],reverse=True) if c['metric']=='revenue'),None)
        if main:rows=[c for c in rows if all(c[k]==main[k] for k in ('issuer','period','scope','currency','basis'))]
        rows=sorted(rows,key=lambda x:x['period'],reverse=True)[:3]
        if rows:
            lines+=['','|关键指标|期间|变化|','|---|---|---|']
            for c in rows:lines.append('|'+LABELS.get(c['metric'],c['metric'])+'|'+period_label(c['period'])+'|'+change_text(c).replace('|','/')+'|')
    lines+=['',f"[下载完整分析 Word]({downloads['report.docx']})"]
    return intro+'\n'+'\n'.join(lines)+'\n\n'
