"""Transparent model readiness and method reconciliation, without a blended price."""
from decimal import Decimal, InvalidOperation


def num(value):
    if value is None or isinstance(value,bool):return None
    try:value=Decimal(str(value));return value if value.is_finite() else None
    except (ValueError,TypeError,InvalidOperation):return None


def review_model(run):
    scenarios=run.get('scenarios') or {};base=scenarios.get('base') or {}
    val=base.get('valuation') or {};params=base.get('parameters') or {}
    sources=run.get('assumption_sources') or {}
    financial=(run.get('model_plan') or {}).get('method')=='residual_income'
    critical=('book_equity','cost_of_equity','roe','payout_ratio','terminal_roe') if financial else ('revenue_base','revenue_growth','gross_margin','opex_ratio','capex_to_revenue','receivable_days','inventory_days','payable_days','wacc','terminal_growth','terminal_roic')
    defaults=[];missing=[];assumptions=[]
    for key in critical:
        if params.get(key) is None:missing.append(key);continue
        source=sources.get(key) or {}
        category=source.get('source')
        if category in (None,'default_assumption'):defaults.append(key)
        elif category not in ('fact','metric','derived'):assumptions.append(key)
    # Capital-cost provenance is recorded separately from driver provenance.
    rate=run.get('discount_rate') or {}
    if rate.get('method') not in (None,'default_assumption'):
        defaults=[k for k in defaults if k not in ('wacc','cost_of_equity')]
    bridge=[]
    if not financial:
        for key in ('net_debt','minority_interest','non_operating_assets'):
            source=sources.get(key) or {}
            bridge.append({'parameter':key,'value':params.get(key),'source':source,'assumed':source.get('source') in (None,'default_assumption')})
    conditional_bridge=[row['parameter'] for row in bridge if row['assumed']]
    peers=(run.get('peers') or {}).get('methods') or {}
    dcf=num(val.get('equity_value'));comparisons=[]
    for key,method in peers.items():
        values=method.get('equity_values') or {};peer=num(values.get('median'))
        if not method.get('ok') or peer is None:continue
        count=method.get('n') or 0
        gap=(dcf-peer)/abs(peer) if dcf is not None and peer!=0 else None
        comparisons.append({'method':key,'sample_count':count,'sample_status':'thin_sample' if count<3 else 'available',
            'equity_value_low':values.get('low'),'equity_value_median':values.get('median'),'equity_value_high':values.get('high'),
            'dcf_equity_value':val.get('equity_value'),'relative_gap':str(gap) if gap is not None else None,
            'disagreement':abs(gap)>Decimal('.5') if gap is not None else False,
            'note':'差异阈值50%仅为本项目诊断规则；同行倍数相关，不代表多个独立概率证据。'})
    reasons=[]
    if not run.get('ok'):reasons.append('模型输入或计算检查未通过')
    if missing:reasons.append('缺少关键驱动：'+', '.join(missing))
    statements=base.get('three_statements') or {}
    if statements and not statements.get('ok'):reasons.append('三表预测未通过检查，不能宣称三表完整配平')
    if defaults:reasons.append('关键驱动仍含默认假设：'+', '.join(defaults))
    if conditional_bridge:reasons.append('权益桥接含未核实假设：'+', '.join(conditional_bridge))
    if params.get('shares') is None:reasons.append('缺少可靠总股本，每股价值不可用')
    if any(row['disagreement'] for row in comparisons):reasons.append('现金流估值与同行估值存在明显分歧，请核对增长、盈利质量、资本效率及样本适配性')
    if peers and not comparisons:reasons.append('同行估值缺少可用于股权比较的有效结果；企业价值不能直接与权益价值比较')
    terminal=(run.get('terminal_policy_checks') or {})
    if terminal.get('ok') is False:reasons.append('终值政策检查存在未通过项')
    if run.get('illustrative'):reasons.append('基础财报仍有未解决问题，本次为示意模型')
    tornado=(run.get('sensitivity') or {}).get('tornado') or []
    actions=[]
    for name in defaults:actions.append({'parameter':name,'action':'补充公司证据或明确预测理由；历史观察不等于未来预测'})
    for name in conditional_bridge:actions.append({'parameter':name,'action':'核对净债务、少数权益或非经营性资产；未披露不等于零'})
    if params.get('shares') is None:actions.append({'parameter':'shares','action':'提供同日期总普通股股数及来源，区分加权平均股数和单一股份类别'})
    return {'ok':bool(run.get('ok')),'status':'conditional' if reasons else 'ready_for_review',
        'summary':'估值已计算，但结论需结合列出的条件与缺口解释。' if reasons else '计算与现有诊断通过，仍需人工复核预测假设。',
        'default_drivers':defaults,'missing_drivers':missing,'explicit_assumptions':assumptions,
        'equity_bridge':bridge,'conditional_equity_bridge':conditional_bridge,
        'per_share_available':num(val.get('value_per_share')) is not None,
        'method_comparisons':comparisons,'reasons':reasons,'next_actions':actions,
        'top_sensitivities':tornado[:3],
        'note':'不自动平均不同方法，不把账面净资产当作价值底线，不把诊断覆盖率当置信概率。'}
