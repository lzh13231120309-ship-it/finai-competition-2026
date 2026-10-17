"""专业级自动估值编排：事实 → 历史校准 → 三表联动预测 → FCFF → DCF/相对估值 → 敏感性/反向 DCF → 闸门 → 区间。

和旧引擎的关系
--------------
旧引擎（valuation.py / valuation_advanced.py）在真实 A 股上暴露的问题见 docs/work-log-valuation.md《估值引擎真实性审计》。
本模块不替换旧入口，而是新增一条"专业路径"（finance_cli 的 valuation-pro 子命令 / 工具 build_valuation_pro），
复用 model3（三表联动，已补强）和 dcf_core（数学内核），并把事实/推论/观点分开记录。

数据流（每一步都落在输出里，可审计）
------------------------------------
1. history()        事实 → 逐年历史（元，Decimal）。单位由 options['unit'] 指定，内部统一换成元。
2. route()          口径路由：金融企业（拒绝 FCFF，给 PB-ROE/DDM）；亏损（拒绝 DCF，只给相对估值）；周期（正常化盈利）。
3. calibrate()      驱动变量校准：EBIT 率按"核心经营 EBIT"（营业利润+财务费用−投资收益−公允价值变动）定义，
                    使基期 EBIT 与报表口径逐元一致；历史取 3–5 年均值/正常化，而非单年外推。
4. run_case()       给定杠杆（增速/利润率/capex/营运资本天数/WACC/g…）→ 完整三表预测（model3）→ FCFF → DCF。
                    情景、敏感性、反向 DCF 都走这同一个函数，因此"改任何驱动，三表、FCF、估值同时重算"。
5. build_wacc()     rf（10 年国债）、ERP、β（对沪深300 周收益 OLS + Blume 调整；可选行业去杠杆再加杠杆）、Kd、目标权重。
6. relative         同行规则 → IQR → 中位/四分位 → 隐含价值（relative_valuation）。
7. valuation_sanity 闸门；市值偏离 > 50% 须附市场隐含假设（反向 DCF）。
8. football_field   区间合成，**不输出目标价，只输出区间**。

证据分类（竞赛要求区分事实/推论/观点）
--------------------------------------
assumptions 表里 evidence_class ∈ {fact 事实, derived 推论(由事实计算), judgment 观点(启发式/假设)}。
"""
from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

from . import dcf_core as core
from . import model3
from . import relative_valuation as rv
from . import sensitivity as sens
from . import valuation_sanity as sanity
from .dcf_core import dec, ser

D = Decimal
ZERO, ONE = D(0), D(1)
ENGINE_VERSION = 'valuation-pro-1.0'

UNIT_SCALE = {'元': D(1), '万元': D(10) ** 4, '百万元': D(10) ** 6, '亿元': D(10) ** 8, 'CNY': D(1)}
PER_SHARE_CODES = {'basic_eps', 'diluted_eps'}
SHARE_CODES = {'total_shares'}

# 默认假设：每一条都是"观点"，写明依据，可被 options 覆盖。
DEFAULTS = {
    'years': 10,                    # 显式期 10 年：增速在显式期内线性衰减到永续增速，降低终值占比
    'terminal_growth': D('0.025'),  # 永续增长率：介于 10 年国债(≈1.7%)与长期名义 GDP 上限(4%)之间的偏保守取值
    'erp': D('0.06'),               # A 股股权风险溢价：实务区间 5%–7%（中国实务多用 6%；Damodaran 成熟市场 4.3%+国家风险）
    'erp_low': D('0.05'), 'erp_high': D('0.07'),
    'rf_fallback': D('0.0168'),     # 仅在未提供 10 年国债收益率时回落，且会被标注为未取数
    'credit_spread': D('0.012'),    # 缺债务成本数据时的信用利差
    'beta_default': D('1.0'),
    'beta_bounds': (D('0.6'), D('1.6')),   # 个股周收益回归的 β 标准误常达 0.1–0.2，截断到 [0.6,1.6]（启发式，截断会标注）
    'ke_floor_premium': D('0.045'),        # 股权成本下限 = rf + 4.5%：低于该值意味着隐含 ERP 低于历史最低量级（启发式）
    'min_kd': D('0.025'), 'max_kd': D('0.08'),
    'max_debt_weight': D('0.6'),
    'payout_ratio': D('0.30'),
    'tax_floor': D('0.15'), 'tax_cap': D('0.25'),
    'margin_fade_years': 3,
    'growth_cap': D('0.20'), 'growth_floor': D('-0.10'),
    'cyclical_growth_cap': D('0.06'),
    'terminal_capex_da': D('1.10'), # 终期 capex/D&A：稳态 ≈ 1 + g×资产年限/2；取 1.10（启发式）
    'roic_cap_spread': D('0.04'),   # 终期 ROIC 上限 = WACC + 4pp（竞争优势衰减）
    'mid_year': False,
}

CYCLICAL_HINTS = ('周期', '煤', '钢', '铜', '铝', '金属', '矿', '化工', '石油', '航运', '水泥', '有色', '黄金', '养殖', '面板', '光伏')
FINANCIAL_HINTS = ('银行', '保险', '证券', '券商', '金融', '信托', 'bank', 'insurance', 'securit')


# ===========================================================================
# 1. 历史
# ===========================================================================

def history(facts, unit='元'):
    """facts → {year(int): {code: Decimal(元)}}，仅取年度、合并口径、已核验事实；每股指标不换算。"""
    scale = UNIT_SCALE.get(unit)
    if scale is None:
        raise ValueError(f'不支持的单位 {unit}（支持 {sorted(UNIT_SCALE)}）')
    H = {}
    rank = {'primary': 0, 'summary': 1, 'inferred': 2}
    best = {}
    for f in facts or []:
        if f.get('status') != 'verified' or f.get('scope') not in (None, '', 'consolidated'):
            continue
        per = str(f.get('period', ''))
        if not per.endswith('-12-31') or f.get('period_type') in ('quarter', 'ytd'):
            continue
        v = dec(f.get('value'))
        if v is None:
            continue
        key = (int(per[:4]), f.get('code'))
        r = rank.get(f.get('source_tier', 'primary'), 0)
        if key not in best or r < best[key][0]:
            best[key] = (r, v if f.get('code') in PER_SHARE_CODES else v * scale)
    for (y, code), (_, v) in best.items():
        H.setdefault(y, {})[code] = v
    return H


def _g(h, *codes, default=None):
    for c in codes:
        if h.get(c) is not None:
            return h[c]
    return default


def derive_year(h):
    """单年派生指标。核心经营 EBIT = 营业利润 + 财务费用 − 投资收益 − 公允价值变动收益。"""
    d = {}
    rev = h.get('revenue')
    d['revenue'] = rev
    cogs = h.get('cost_of_revenue')
    d['gross_margin'] = ((rev - cogs) / rev) if (rev and cogs is not None and rev > 0) else None
    op = h.get('operating_profit')
    fin = h.get('financial_expenses', ZERO) or ZERO
    inv = h.get('investment_income', ZERO) or ZERO
    fv = h.get('fair_value_gain', ZERO) or ZERO
    if op is not None:
        ebit = op + fin - inv - fv
    elif h.get('profit_before_tax') is not None:
        ebit = h['profit_before_tax'] + fin - inv - fv
    else:
        ebit = None
    d['ebit'] = ebit
    d['ebit_margin'] = (ebit / rev) if (ebit is not None and rev and rev > 0) else None
    da = sum((h.get(c) or ZERO) for c in ('depreciation', 'rou_depreciation', 'amortization', 'long_term_prepaid_amortization'))
    d['da'] = da if any(h.get(c) is not None for c in ('depreciation', 'amortization')) else None
    d['da_ratio'] = (da / rev) if (d['da'] is not None and rev and rev > 0) else None
    d['ebitda'] = (ebit + da) if (ebit is not None and d['da'] is not None) else None
    cap = h.get('capex')
    d['capex'] = abs(cap) if cap is not None else None
    d['capex_ratio'] = (d['capex'] / rev) if (d['capex'] is not None and rev and rev > 0) else None
    pbt, tax = h.get('profit_before_tax'), h.get('income_tax')
    d['etr'] = (tax / pbt) if (pbt and tax is not None and pbt > 0) else None
    ar, inv_, ap = h.get('receivables'), h.get('inventory'), h.get('payables')
    d['ar_days'] = (ar * 365 / rev) if (ar is not None and rev and rev > 0) else None
    d['inv_days'] = (inv_ * 365 / cogs) if (inv_ is not None and cogs and cogs > 0) else None
    d['ap_days'] = (ap * 365 / cogs) if (ap is not None and cogs and cogs > 0) else None
    return d


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return (sum(vals) / len(vals)) if vals else None


# ===========================================================================
# 2. 运营视图资产负债表（喂 model3）与桥项
# ===========================================================================

def bridge_items(h):
    """EV→权益价值桥的各项（均为正数，元）。口径见 dcf_core.BRIDGE_ITEMS。"""
    z = lambda k: h.get(k) or ZERO
    debt = z('short_term_debt') + z('noncurrent_due_liabilities') + z('long_term_debt') + z('bonds_payable') + z('short_term_bonds')
    items = {
        'cash': z('cash'),
        'associates': z('long_term_equity_investment'),
        'non_operating_assets': (z('financial_assets_fvtpl') + z('other_equity_investment')
                                 + z('other_noncurrent_financial_assets') + z('investment_property')),
        'debt': debt, 'leases': z('lease_liabilities'),
        'preferred': z('other_equity_instruments'),
        'minority': z('minority_equity'),
        'contingent': ZERO, 'sbc': ZERO,
    }
    return items


def net_debt_like(items):
    """EV 与权益价值之差（债务类 − 资产类）；用于相对估值把 EV 倍数换成权益价值。"""
    return (items['debt'] + items['leases'] + items['preferred'] + items['minority'] + items['contingent'] + items['sbc']
            - items['cash'] - items['associates'] - items['non_operating_assets'])


def operating_facts(h, period):
    """把最新年报转成 model3 认识的"运营视图"事实：非经营性金融资产单列(冻结不随收入增长)，有息债务口径含一年内到期。

    返回 (facts, metrics, warnings)。所有"其他项"都显式计算，使 model3 总括行缺口为 0，
    不再出现倒挤出的负"其他流动负债"（旧引擎在紫金/神华上的缺陷）。
    """
    z = lambda k: h.get(k) or ZERO
    warnings = []
    wc_assets = {
        'receivables': z('receivables'), 'notes_receivable': z('notes_receivable'), 'prepayments': z('prepayments'),
        'other_receivables': z('other_receivables'), 'inventory': z('inventory'),
        'receivables_financing': z('receivables_financing'), 'contract_assets': z('contract_assets'),
    }
    ca_total = h.get('current_assets')
    other_ca = ZERO
    if ca_total is not None:
        other_ca = ca_total - z('cash') - z('financial_assets_fvtpl') - sum(wc_assets.values(), ZERO)
        if other_ca < 0:
            warnings.append(f'其他流动资产倒挤为负({core.ser(other_ca)})，已置 0 并并入非流动其他项；请核对科目口径')
            other_ca = ZERO
    wc_assets['other_current_assets'] = other_ca
    fixed_core = z('fixed_assets') + z('construction_in_progress')
    rou, intang, dta = z('right_of_use_assets'), z('intangibles'), z('deferred_tax_assets')
    total_assets = h.get('assets')
    mapped = z('cash') + sum(wc_assets.values(), ZERO) + fixed_core + rou + intang + dta
    other_nca = (total_assets - mapped) if total_assets is not None else ZERO
    debt_total = (z('short_term_debt') + z('noncurrent_due_liabilities') + z('long_term_debt') + z('bonds_payable')
                  + z('short_term_bonds') + z('lease_liabilities'))
    wc_liab = {'payables': z('payables'), 'notes_payable': z('notes_payable'), 'contract_liabilities': z('contract_liabilities'),
               'advance_receipts': z('advance_receipts'), 'tax_payables': z('tax_payables'), 'other_payables': z('other_payables')}
    cl_total = h.get('current_liabilities')
    other_cl = ZERO
    if cl_total is not None:
        other_cl = cl_total - z('short_term_debt') - z('noncurrent_due_liabilities') - z('short_term_bonds') - sum(wc_liab.values(), ZERO)
        if other_cl < 0:
            warnings.append(f'其他流动负债倒挤为负({core.ser(other_cl)})，已置 0；请核对')
            other_cl = ZERO
    liabilities = h.get('liabilities')
    dtl = z('deferred_tax_liabilities')
    other_ncl = ZERO
    if liabilities is not None:
        other_ncl = liabilities - debt_total - sum(wc_liab.values(), ZERO) - other_cl - dtl
        if other_ncl < 0:
            warnings.append(f'其他非流动负债倒挤为负({core.ser(other_ncl)})')
    facts = []

    def add(code, value):
        if value is None:
            return
        facts.append({'code': code, 'value': core.ser(value), 'period': period, 'scope': 'consolidated',
                      'status': 'verified', 'value_unit': '元', 'currency': 'CNY',
                      'evidence_ids': [f'pro:{code}:{period}']})
    add('cash', z('cash'))
    for k, v in wc_assets.items():
        add(k, v)
    add('fixed_assets', fixed_core)
    add('right_of_use_assets', rou)
    add('intangibles', intang)
    add('deferred_tax_assets', dta)
    add('other_noncurrent_assets', other_nca)
    for k, v in wc_liab.items():
        add(k, v)
    add('other_current_liabilities', other_cl)
    add('deferred_tax_liabilities', dtl)
    add('other_noncurrent_liabilities', other_ncl)
    add('assets', total_assets)
    add('liabilities', liabilities)
    add('equity', h.get('equity'))
    add('parent_equity', h.get('parent_equity'))
    add('minority_equity', h.get('minority_equity'))
    add('share_capital', h.get('share_capital'))
    add('revenue', h.get('revenue'))
    add('cost_of_revenue', h.get('cost_of_revenue'))
    metrics = [{'code': 'interest_bearing_debt', 'value': core.ser(debt_total), 'period': period, 'scope': 'consolidated',
                'unit': '元', 'status': 'calculated'}]
    return facts, metrics, warnings


# ===========================================================================
# 3. 路由与校准
# ===========================================================================

def route(hist, last_year, options):
    """口径路由。返回 profile 字典。"""
    h = hist[last_year]
    tag = str(options.get('industry') or '')
    is_fin = bool(options.get('is_financial')) or any(w in tag.lower() for w in FINANCIAL_HINTS) \
        or any(h.get(c) for c in ('loans_net', 'customer_deposits', 'insurance_reserves'))
    is_cyc = bool(options.get('is_cyclical')) or any(w in tag for w in CYCLICAL_HINTS)
    return {'financial': is_fin, 'cyclical': is_cyc, 'industry': tag}


def infer_shares(hist, last_year, options):
    """股数：优先 options.shares（公告总股本）；否则 NP/EPS（加权，推断）并用面值交叉验证股本。"""
    notes = []
    if options.get('shares'):
        return {'shares': dec(options['shares']), 'basis': 'provided', 'source': options.get('shares_source', '使用者提供'),
                'ok': True, 'notes': notes}
    h = hist[last_year]
    np_, eps, cap = h.get('parent_net_profit'), h.get('basic_eps'), h.get('share_capital')
    if np_ and eps and np_ * eps > 0:        # 同号（亏损公司 NP、EPS 同为负，比值仍是正的加权股数）
        est = np_ / eps
        shares, basis, ok = est, 'NP/EPS(加权平均，推断)', True
        if cap:
            for par in (D(1), D('0.1'), D('0.5'), D('0.25'), D(10)):
                cand = cap / par
                if abs(cand - est) / est < D('0.08'):
                    shares, basis = cand, f'股本÷面值{par}（与 NP/EPS 交叉验证一致）'
                    break
            else:
                ok = False
                notes.append('股本÷常见面值与 NP/EPS 推断股数均不吻合(>8%)，可能有大规模增发/回购或稀释；请提供公告总股本')
        return {'shares': shares, 'basis': basis, 'source': '推断(E2)', 'ok': ok, 'notes': notes}
    if cap:
        notes.append('缺 EPS，无法验证面值；按面值 1 元推算，若面值非 1 元将错 10 倍以上')
        return {'shares': cap, 'basis': '股本(面值1元假设)', 'source': '推断(E3)', 'ok': False, 'notes': notes}
    return {'shares': None, 'basis': 'unavailable', 'source': '', 'ok': False, 'notes': ['无股数']}


def calibrate(hist, last_year, profile, options, wacc_guess=D('0.08')):
    """从历史校准驱动路径的起点与目标。返回 (drivers, assumptions[list], warnings)。"""
    years = sorted(y for y in hist if hist[y].get('revenue'))
    d = {y: derive_year(hist[y]) for y in years}
    L = d[last_year]
    A, W = [], []

    def put(key, label, value, cls, source, basis, unit=''):
        A.append({'key': key, 'label': label, 'value': value, 'unit': unit, 'evidence_class': cls, 'source': source, 'basis': basis})

    n_hist = len(years)
    ret = {}
    # --- 收入增速 ---
    yoy = None
    if len(years) >= 2 and d[years[-2]]['revenue'] and d[years[-2]]['revenue'] > 0:
        yoy = L['revenue'] / d[years[-2]]['revenue'] - 1
    cagr3 = None
    if len(years) >= 4:
        r0 = d[years[-4]]['revenue']
        if r0 and r0 > 0:
            cagr3 = (L['revenue'] / r0) ** (ONE / D(3)) - 1
    cands = [x for x in (yoy, cagr3) if x is not None]
    cap = DEFAULTS['cyclical_growth_cap'] if profile['cyclical'] else DEFAULTS['growth_cap']
    if profile['cyclical'] and len(years) >= 2:
        # 周期股：以多年复合增速（含峰谷）代替最新同比，避免把单年峰值外推
        r0 = d[years[0]]['revenue']
        span = years[-1] - years[0]
        cagr_all = (L['revenue'] / r0) ** (ONE / D(span)) - 1 if (span > 0 and r0 and r0 > 0) else None
        raw = cagr_all if cagr_all is not None else (min(cands) if cands else D('0.03'))
        src = f'{years[0]}-{years[-1]} 年收入复合增速（周期股，不以最新同比外推）'
    elif cands:
        raw = min(cands)
        src = '近 3 年复合增速与最新同比取较低者（保守）'
    else:
        raw = D('0.05')
        src = '历史不足，默认 5%'
        W.append('历史收入不足 2 期，增速取默认值')
    g1 = min(max(raw, DEFAULTS['growth_floor']), cap)
    if options.get('growth_start') is not None:
        g1 = dec(options['growth_start'])
        src = '使用者指定'
    put('growth_start', '第 1 年收入增速', g1, 'derived' if 'growth_start' not in options else 'judgment',
        src, f'同比 {float(yoy):.1%}' if yoy is not None else '', '比率')
    ret['growth_start'] = g1
    # --- 毛利率 ---
    gm = L['gross_margin']
    if gm is None:
        gm = _mean([d[y]['gross_margin'] for y in years])
        W.append('缺营业成本，毛利率取历史均值')
    ret['gross_margin'] = gm
    put('gross_margin', '毛利率（基期）', gm, 'fact', f'{last_year} 年报 (营业收入−营业成本)/营业收入', '', '比率')
    # --- EBIT 率与目标（正常化） ---
    margins = [d[y]['ebit_margin'] for y in years if d[y]['ebit_margin'] is not None]
    m0 = L['ebit_margin']
    k = 5 if profile['cyclical'] else 3
    recent = [d[y]['ebit_margin'] for y in years[-k:] if d[y]['ebit_margin'] is not None]
    mean_k = _mean(recent)
    if m0 is None:
        m0 = mean_k
    target = mean_k if mean_k is not None else m0
    if options.get('ebit_margin_target') is not None:
        target = dec(options['ebit_margin_target'])
        tcls = 'judgment'
    else:
        tcls = 'derived'
        if not profile['cyclical'] and m0 is not None and mean_k is not None:
            # 稳健行业：目标 = 近 3 年均值与基期的中点，避免单年异常（如一次性收益）
            target = (m0 + mean_k) / 2
    ret.update(ebit_margin_last=m0, ebit_margin_target=target, ebit_margin_mean=mean_k)
    put('ebit_margin_last', '核心 EBIT 率（基期）', m0, 'derived', f'{last_year}：(营业利润+财务费用−投资收益−公允价值变动)/收入', '', '比率')
    put('ebit_margin_target', '目标(正常化) EBIT 率', target, tcls,
        f'{"近 5 年均值（周期股正常化）" if profile["cyclical"] else "近 3 年均值与基期中点"}' if tcls == 'derived' else '使用者指定',
        f'向目标线性过渡 {DEFAULTS["margin_fade_years"]} 年', '比率')
    # --- D&A / capex ---
    da_ratio = _mean([d[y]['da_ratio'] for y in years[-3:]])
    capex_ratio = _mean([d[y]['capex_ratio'] for y in years[-3:]])
    if da_ratio is None:
        da_ratio = D('0.04')
        W.append('缺折旧摊销数据，D&A/收入取 4% 默认值（观点）')
    if capex_ratio is None:
        capex_ratio = da_ratio * D('1.1')
        W.append('缺资本开支数据，capex/收入取 D&A×1.1（观点）')
    ret.update(da_ratio=da_ratio, capex_ratio_hist=capex_ratio)
    put('da_ratio', '折旧摊销 / 收入', da_ratio, 'derived', '近 3 年均值', '', '比率')
    put('capex_ratio_hist', '资本开支 / 收入（历史）', capex_ratio, 'derived', '近 3 年均值', '购建固定资产无形资产支付的现金（取绝对值）', '比率')
    # --- 营运资本天数（期末余额，与 model3 期初一致） ---
    for key, lbl in (('ar_days', '应收账款天数(收入口径)'), ('inv_days', '存货天数(成本口径)'), ('ap_days', '应付账款天数(成本口径)')):
        val = L[key]
        if val is None:
            val = ZERO
            W.append(f'缺{lbl}所需科目，取 0（影响营运资本，观点）')
        ret[key] = val
        put(key, lbl, val, 'derived', f'{last_year} 年末余额 ×365 / 收入或成本', '期末时点值，与三表期初一致', '天')
    # --- 税率 ---
    etrs = [d[y]['etr'] for y in years[-3:] if d[y]['etr'] is not None]
    etr = _mean(etrs)
    raw_etr = etr
    if etr is None:
        etr = DEFAULTS['tax_cap']
        W.append('缺有效税率，取法定 25%（观点）')
    tax = min(max(etr, DEFAULTS['tax_floor']), DEFAULTS['tax_cap'])
    if options.get('tax_rate') is not None:
        tax = dec(options['tax_rate'])
    ret['tax_rate'] = tax
    ret['etr_raw'] = raw_etr
    put('tax_rate', '所得税率（预测与终值）', tax, 'derived',
        '近 3 年有效税率均值，夹在 [15%, 25%]（高新 15%—法定 25%）', f'原始 {float(raw_etr):.1%}' if raw_etr is not None else '', '比率')
    # --- 少数股东损益占比 ---
    mp, npf = hist[last_year].get('minority_profit'), hist[last_year].get('net_profit')
    if mp is not None and npf and npf > 0:
        ret['minority_income_share'] = max(min(mp / npf, D('0.9')), ZERO)
    # --- 成本/现金收益率 ---
    h = hist[last_year]
    prev = hist.get(last_year - 1, {})
    avg_debt = None
    def debt_of(x):
        z = lambda k: x.get(k) or ZERO
        return z('short_term_debt') + z('noncurrent_due_liabilities') + z('long_term_debt') + z('bonds_payable') + z('short_term_bonds') + z('lease_liabilities')
    dn, dp = debt_of(h), debt_of(prev) if prev else None
    if dn:
        avg_debt = (dn + dp) / 2 if dp else dn
    int_exp = h.get('interest_expense')
    kd_book = (int_exp / avg_debt) if (int_exp and avg_debt and avg_debt > 0) else None
    ret['kd_book'] = kd_book
    ci, ic = h.get('cash'), h.get('interest_income')
    cash_prev = prev.get('cash') if prev else None
    avg_cash = (ci + cash_prev) / 2 if (ci is not None and cash_prev is not None) else ci
    cy = (ic / avg_cash) if (ic and avg_cash and avg_cash > 0) else None
    ret['cash_yield'] = min(max(cy, D('0.005')), D('0.04')) if cy is not None else D('0.015')
    # --- 历史 ROIC（NOPAT / 投资资本），用于终期 ROIC ---
    rois = []
    for y in years[-3:]:
        hy, dy = hist[y], d[y]
        if dy['ebit'] is None:
            continue
        ic_ = (hy.get('equity') or ZERO) + debt_of(hy) - (hy.get('cash') or ZERO) - bridge_items(hy)['non_operating_assets'] - (hy.get('long_term_equity_investment') or ZERO)
        if ic_ > 0:
            rois.append(dy['ebit'] * (ONE - tax) / ic_)
    ret['roic_hist'] = _mean(rois)
    # --- 基期 ---
    ret['revenue_base'] = L['revenue']
    ret['ebit_base'] = L['ebit']
    ret['ebitda_base'] = L['ebitda']
    ret['da_base'] = L['da']
    ret['parent_np'] = hist[last_year].get('parent_net_profit')
    return ret, A, W


# ===========================================================================
# 5. 资本成本
# ===========================================================================

def _rf_from(options):
    rf = options.get('rf')
    if isinstance(rf, dict):
        return dec(rf.get('value')), rf.get('source', ''), rf.get('as_of', '')
    if rf is not None:
        return dec(rf), options.get('rf_source', '使用者提供'), options.get('rf_as_of', '')
    return DEFAULTS['rf_fallback'], '未提供，回落默认 1.68%（需核验）', ''


def compute_beta(options):
    """β：对基准指数的周收益 OLS 回归 + Blume 调整；另给行业去杠杆再加杠杆路径。"""
    out = {'raw': None, 'blume': None, 'regression': None, 'industry': None, 'warnings': []}
    sp, bp = options.get('stock_prices'), options.get('benchmark_prices')
    if sp and bp:
        _, s_ret, m_ret = core.align_returns(sp, bp, step=int(options.get('beta_step', 5)))
        reg = core.ols_beta(s_ret, m_ret)
        out['regression'] = {k: (ser(v) if isinstance(v, D) else v) for k, v in reg.items()}
        if reg.get('ok'):
            out['raw'] = reg['beta']
            out['blume'] = core.blume_adjust(reg['beta'])
            if reg['r2'] < D('0.1'):
                out['warnings'].append(f"β 回归 R²={float(reg['r2']):.2f} < 0.10：个股与指数相关性弱，β 估计不稳健（启发式阈值）")
        else:
            out['warnings'].append(reg.get('reason', 'β 回归失败'))
    return out


def build_wacc(drivers, hist_last, bridge, profile, options, market_cap, tax):
    rf, rf_src, rf_asof = _rf_from(options)
    erp = dec(options.get('erp'), DEFAULTS['erp'])
    beta_info = compute_beta(options)
    industry = None
    if options.get('industry_betas'):
        gross = bridge['debt'] + bridge['leases']
        eq = market_cap if market_cap else (hist_last.get('parent_equity') or ZERO)
        d_e = (gross / eq) if eq and eq > 0 else ZERO
        industry = core.industry_beta_path(options['industry_betas'], tax, d_e)
        beta_info['industry'] = {'unlevered_median': ser(industry['unlevered_median']), 'relevered': ser(industry['relevered']),
                                 'peers': industry['peers'], 'target_debt_to_equity': ser(d_e), 'warnings': industry['warnings']}
    method = options.get('beta_method')
    if options.get('beta') is not None:
        beta, bsrc = dec(options['beta']), '使用者指定'
    elif method == 'industry' and industry and industry['relevered'] is not None:
        beta, bsrc = industry['relevered'], '行业β去杠杆(Hamada)后按目标D/E再加杠杆'
    elif beta_info['blume'] is not None:
        beta, bsrc = beta_info['blume'], f"对沪深300周收益 OLS 回归(n={beta_info['regression']['n']}) + Blume 调整"
    else:
        beta, bsrc = DEFAULTS['beta_default'], '无价格序列，取 β=1.0（观点；需提供行情）'
        beta_info['warnings'].append('未提供股价/指数序列，β 取 1.0')
    beta_unclipped = beta
    lo_b0, hi_b0 = DEFAULTS['beta_bounds']
    if options.get('beta') is None and (beta < lo_b0 or beta > hi_b0):
        beta = min(max(beta, lo_b0), hi_b0)
        bsrc += f'；回归/调整后 β={float(beta_unclipped):.2f} 超出 [{lo_b0},{hi_b0}]，已截断（启发式，标准误大）'
        beta_info['warnings'].append(f'β 被截断到 {float(beta):.2f}（原 {float(beta_unclipped):.2f}）')
    ke_raw = core.capm(rf, beta, erp)
    ke_floor = rf + DEFAULTS['ke_floor_premium']
    ke = max(ke_raw, ke_floor)
    ke_note = ''
    if ke > ke_raw and options.get('beta') is None:
        ke_note = f'CAPM 股权成本 {float(ke_raw):.2%} 低于下限 rf+{DEFAULTS["ke_floor_premium"]:.1%}={float(ke_floor):.2%}，取下限（启发式）'
        beta_info['warnings'].append(ke_note)
    else:
        ke = ke_raw
    kd_book = drivers.get('kd_book')
    if kd_book is not None:
        kd = min(max(kd_book, DEFAULTS['min_kd']), DEFAULTS['max_kd'])
        kd_src = f'利息费用/平均有息债务={float(kd_book):.2%}，夹在[{DEFAULTS["min_kd"]:.1%},{DEFAULTS["max_kd"]:.0%}]'
    else:
        kd = rf + DEFAULTS['credit_spread']
        kd_src = 'rf + 信用利差 1.2%（无债务/利息数据，观点）'
    if options.get('cost_of_debt') is not None:
        kd, kd_src = dec(options['cost_of_debt']), '使用者指定'
    gross_debt = bridge['debt'] + bridge['leases']
    if options.get('target_debt_weight') is not None:
        wd, wd_src = dec(options['target_debt_weight']), '使用者指定目标资本结构'
    else:
        eq = market_cap if market_cap else (hist_last.get('parent_equity') or ZERO)
        wd = gross_debt / (gross_debt + eq) if (gross_debt + eq) > 0 else ZERO
        wd_src = ('市值权重 D/(D+E)，D=有息债务+租赁(账面)' if market_cap else '市值缺失，用账面权益代替(观点)')
        if wd > DEFAULTS['max_debt_weight']:
            wd, wd_src = DEFAULTS['max_debt_weight'], wd_src + '；超过上限 60% 已截断'
    wacc = core.wacc_build(ke, kd, tax, wd)
    if options.get('wacc') is not None:
        wacc_used, wacc_src = dec(options['wacc']), '使用者指定'
    else:
        wacc_used, wacc_src = wacc, 'CAPM + 加权'
    # 区间：ERP 5%-7%、β 取 [min(raw,blume), max(raw,blume)]
    betas = [b for b in (beta_info['raw'], beta_info['blume'], beta) if b is not None]
    lo_b, hi_b = min(betas), max(betas)
    wacc_lo = core.wacc_build(core.capm(rf, lo_b, DEFAULTS['erp_low']), kd, tax, wd)
    wacc_hi = core.wacc_build(core.capm(rf, hi_b, DEFAULTS['erp_high']), kd, tax, wd)
    return {'rf': rf, 'rf_source': rf_src, 'rf_as_of': rf_asof, 'erp': erp, 'beta': beta, 'beta_source': bsrc, 'beta_detail': beta_info,
            'cost_of_equity': ke, 'cost_of_equity_capm': ke_raw, 'cost_of_equity_note': ke_note, 'beta_unclipped': beta_unclipped,
            'cost_of_debt': kd, 'cost_of_debt_source': kd_src, 'debt_weight': wd, 'debt_weight_source': wd_src,
            'tax': tax, 'wacc_computed': wacc, 'wacc': wacc_used, 'wacc_source': wacc_src,
            'wacc_range': {'low': wacc_lo, 'high': wacc_hi, 'basis': 'ERP 5%–7% 与 β 的原始/Blume 区间两端'},
            'erp_range': (DEFAULTS['erp_low'], DEFAULTS['erp_high'])}


# ===========================================================================
# 4. 单次完整重算（情景/敏感性/反向 DCF 共用）
# ===========================================================================

def _paths(ctx, lv):
    dr, N = ctx['drivers'], ctx['years']
    g_end = ctx['g_path_end']
    g1 = dr['growth_start']
    fade = DEFAULTS['margin_fade_years']
    d_gr, d_mg = dec(lv.get('d_growth'), ZERO), dec(lv.get('d_margin'), ZERO)
    d_cx, d_nwc = dec(lv.get('d_capex'), ZERO), dec(lv.get('d_nwc_days'), ZERO)
    gm, m0, mt = dr['gross_margin'], dr['ebit_margin_last'], dr['ebit_margin_target']
    cap_term = dr['da_ratio'] * ctx['terminal_capex_da']
    paths = {k: [] for k in ('revenue_growth', 'gross_margin', 'opex_ratio', 'da_to_revenue', 'capex_to_revenue',
                             'receivable_days', 'inventory_days', 'payable_days')}
    margins = []
    for t in range(1, N + 1):
        g_t = (g1 + (g_end - g1) * D(t - 1) / D(max(N - 1, 1))) + d_gr
        m_t = m0 + (mt - m0) * min(D(t) / D(fade), ONE) + d_mg
        opex = max(gm - m_t, ZERO)
        cx = dr['capex_ratio_hist'] + (cap_term - dr['capex_ratio_hist']) * D(t) / D(N) + d_cx
        paths['revenue_growth'].append(g_t)
        paths['gross_margin'].append(gm)
        paths['opex_ratio'].append(opex)
        paths['da_to_revenue'].append(dr['da_ratio'])
        paths['capex_to_revenue'].append(max(cx, ZERO))
        paths['receivable_days'].append(max(dr['ar_days'] + d_nwc, ZERO))
        paths['inventory_days'].append(dr['inv_days'])
        paths['payable_days'].append(dr['ap_days'])
        margins.append(m_t)
    return paths, margins


def run_case(ctx, lv=None):
    """杠杆 → 三表 → FCFF → DCF。lv 键：d_growth,d_margin,d_capex,d_nwc_days,d_tax,d_wacc,wacc,g,exit_multiple,terminal_roic。"""
    lv = lv or {}
    paths, margins = _paths(ctx, lv)
    tax = min(max(ctx['drivers']['tax_rate'] + dec(lv.get('d_tax'), ZERO), ZERO), D('0.5'))
    params = {'years': ctx['years'], 'tax_rate': tax, 'cost_of_debt': ctx['kd_book'], 'cash_yield': ctx['drivers']['cash_yield'],
              'impairment_to_revenue': ZERO, 'revenue_growth': paths['revenue_growth'][0], 'gross_margin': paths['gross_margin'][0],
              'opex_ratio': paths['opex_ratio'][0], 'da_to_revenue': ctx['drivers']['da_ratio'],
              'capex_to_revenue': paths['capex_to_revenue'][0], 'receivable_days': paths['receivable_days'][0],
              'inventory_days': paths['inventory_days'][0], 'payable_days': paths['payable_days'][0]}
    if ctx['drivers'].get('minority_income_share') is not None:
        params['minority_income_share'] = ctx['drivers']['minority_income_share']
    funding = {'payout_ratio': ctx['payout'], 'mode': 'auto', 'interest_basis': ctx.get('interest_basis', 'beginning')}
    three = model3.build(ctx['op_facts'], ctx['op_metrics'], params, paths, funding)
    rows = three.get('statements') or []
    if not rows:
        return {'ok': False, 'reason': '三表预测无输出（缺收入基数）', 'equity': None, 'three': three}
    fc = three['fcff_cross_check']
    fcff = [x['fcff'] for x in fc]
    ebit_n = rows[-1]['income_statement']['ebit']
    da_n = rows[-1]['cash_flow']['depreciation_amortization']
    capex_n = rows[-1]['cash_flow']['capex']
    out = {'ok': True, 'three': three, 'fcff': fcff, 'fcff_detail': fc, 'ebit_n': ebit_n, 'da_n': da_n, 'capex_n': capex_n,
           'ebitda_n': ebit_n + da_n, 'paths': paths, 'margins': margins, 'tax': tax, 'levers': lv}
    r = revalue(ctx, out, wacc=dec(lv.get('wacc')) if lv.get('wacc') is not None else ctx['wacc'] + dec(lv.get('d_wacc'), ZERO),
                g=dec(lv.get('g')), exit_multiple=dec(lv.get('exit_multiple')), roic=dec(lv.get('terminal_roic')))
    out.update(r)
    return out


def revalue(ctx, case, wacc, g=None, exit_multiple=None, roic=None):
    """在已算好的 FCFF 上改 WACC/g/退出倍数/终期 ROIC 重新折现（不重算三表）。"""
    g = g if g is not None else ctx['g']
    tax = case['tax']
    ebit_n = case['ebit_n']
    nopat_n = ebit_n - max(ebit_n, ZERO) * tax
    if roic is None:
        roic_hist = ctx['drivers'].get('roic_hist')
        spread = DEFAULTS['roic_cap_spread']
        base_roic = ctx.get('terminal_roic_override')
        if base_roic is None:
            base_roic = min(max(roic_hist if roic_hist is not None else wacc, wacc), wacc + spread)
        roic = base_roic
    ctx_roic = ctx.get('_roic_cache', {})
    try:
        fcf_next = core.value_driver_fcff(nopat_n * (ONE + g), g, roic)
    except ValueError as e:
        return {'dcf': {'ok': False, 'reasons': [str(e)]}, 'equity': None, 'ev': None, 'roic_t': roic}
    simple_next = case['fcff'][-1] * (ONE + g)
    bridge = ctx['bridge']
    shares = ctx.get('shares')
    d = core.dcf_value(case['fcff'], wacc, {'method': 'gordon', 'g': g, 'fcff_next': fcf_next}, bridge,
                       mid_year=ctx['mid_year'], shares=shares, ebitda_ltm=ctx.get('ebitda_base'), ebitda_terminal=case['ebitda_n'],
                       allow_above_gdp=ctx.get('allow_above_gdp', False))
    res = {'dcf': d, 'equity': d.get('equity_value') if d['ok'] else None, 'ev': d.get('enterprise_value') if d['ok'] else None,
           'roic_t': roic, 'wacc_used': wacc, 'g_used': g, 'terminal_fcff_value_driver': fcf_next, 'terminal_fcff_simple': simple_next,
           'terminal_reinvestment': (g / roic) if roic else None}
    if d['ok']:
        d_simple = core.dcf_value(case['fcff'], wacc, {'method': 'gordon', 'g': g, 'fcff_next': simple_next}, bridge,
                                  mid_year=ctx['mid_year'], shares=shares, ebitda_terminal=case['ebitda_n'],
                                  allow_above_gdp=ctx.get('allow_above_gdp', False))
        res['dcf_simple_gordon'] = d_simple
        mult = exit_multiple if exit_multiple is not None else ctx.get('exit_multiple')
        if mult is not None and case['ebitda_n'] and case['ebitda_n'] > 0:
            d_exit = core.dcf_value(case['fcff'], wacc, {'method': 'exit_multiple', 'g': g, 'metric_n': case['ebitda_n'],
                                                         'multiple': mult}, bridge, mid_year=ctx['mid_year'], shares=shares,
                                    ebitda_terminal=case['ebitda_n'])
            res['dcf_exit_multiple'] = d_exit
            res['equity_exit'] = d_exit.get('equity_value') if d_exit['ok'] else None
    return res


# ===========================================================================
# 6-8. 编排
# ===========================================================================

def jsonable(x):
    """递归转 JSON 安全：Decimal→字符串（去尾零），NaN/Inf→None，tuple→list。"""
    if isinstance(x, D):
        return ser(x) if x.is_finite() else None
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    return x


def _eq(case):
    return case.get('equity') if case and case.get('ok') else None


def _range_from(values):
    vals = [v for v in values if v is not None]
    return (min(vals), max(vals)) if vals else (None, None)


def financial_path(hist, last_year, profile, options, market_cap, shares_info, wacc_info_fn):
    """金融企业：拒绝 FCFF，给 PB-ROE（合理 PB=(ROE−g)/(Ke−g)）与 DDM 下限，并附适用条件。"""
    h = hist[last_year]
    years = sorted(hist)
    pref = h.get('other_equity_instruments') or ZERO
    bv_common = (h.get('parent_equity') or ZERO) - pref
    roes = []
    for y in years[-3:]:
        hy, hp = hist[y], hist.get(y - 1)
        np_ = hy.get('parent_net_profit')
        be = (hy.get('parent_equity') or ZERO) - (hy.get('other_equity_instruments') or ZERO)
        if np_ is not None and be > 0:
            if hp and hp.get('parent_equity'):
                be = (be + (hp['parent_equity'] - (hp.get('other_equity_instruments') or ZERO))) / 2
            roes.append(np_ / be)
    roe = _mean(roes)
    rf, _, _ = _rf_from(options)
    beta_info = compute_beta(options)
    beta = dec(options.get('beta'), beta_info['blume'] if beta_info['blume'] is not None else DEFAULTS['beta_default'])
    erp = dec(options.get('erp'), DEFAULTS['erp'])
    ke = core.capm(rf, beta, erp)
    g = dec(options.get('terminal_growth'), DEFAULTS['terminal_growth'])
    out = {'applicable': False, 'reason': '金融企业（银行/保险/券商）负债是经营性的，利息既是经营成本又是融资成本，'
           '无法区分经营/融资现金流，FCFF/EV/EBITDA 不适用；改用 PB-ROE 与 DDM（股权口径）。',
           'bv_common': bv_common, 'roe_hist': roes, 'roe': roe, 'cost_of_equity': ke, 'beta': beta, 'g': g,
           'rf': rf, 'erp': erp, 'warnings': [
               '账面净资产扣除其他权益工具(优先股/永续债)得普通股东权益；优先股股息未从净利润中扣除，ROE 略高估。',
               '银行估值对资产质量（不良/拨备覆盖）高度敏感，这里不含信用损失情景，PB-ROE 仅为简化实现。']}
    if roe is None or bv_common <= 0:
        out['pb_roe'] = {'ok': False, 'reason': '缺少 ROE 或账面权益'}
        return out
    def val(r, k, gg):
        try:
            pb = core.justified_pb(r, k, gg)
        except ValueError:
            return None
        return pb, bv_common * pb
    base = val(roe, ke, g)
    low = val(roe - D('0.01'), ke + D('0.01'), g)
    high = val(roe + D('0.01'), ke - D('0.01'), g)
    out['pb_roe'] = {'ok': base is not None, 'justified_pb': base[0] if base else None,
                     'equity_value': {'low': low[1] if low else None, 'base': base[1] if base else None, 'high': high[1] if high else None},
                     'justified_pb_range': {'low': low[0] if low else None, 'high': high[0] if high else None},
                     'formula': '合理 PB = (ROE − g)/(Ke − g)；权益价值 = 普通股账面权益 × 合理 PB',
                     'range_rule': 'ROE ±1pp 与 Ke ∓1pp 同向组合（低估值：ROE−1pp & Ke+1pp）',
                     'matrix_roe_ke': {'row_name': 'roe', 'col_name': 'cost_of_equity',
                                       'rows': [ser(roe + D(i) * D('0.01')) for i in (-1, 0, 1)],
                                       'cols': [ser(ke + D(j) * D('0.01')) for j in (-1, 0, 1)],
                                       'values': [[(val(roe + D(i) * D('0.01'), ke + D(j) * D('0.01'), g) or (None, None))[1]
                                                   for j in (-1, 0, 1)] for i in (-1, 0, 1)]}}
    payout = dec(options.get('payout'), D('0.30'))
    np_ = h.get('parent_net_profit')
    if np_ and ke > g:
        out['ddm'] = {'payout_assumption': payout, 'equity_value': core.ddm_gordon(np_ * (ONE + g) * payout, ke, g),
                      'note': '按给定派息率(观点)与 g=永续增速的一阶段 DDM：留存部分的再投资收益未计入，是保守下限，仅作交叉验证。'}
    if market_cap:
        base_v = out['pb_roe'].get('equity_value', {}).get('base')
        out['market_pb'] = market_cap / bv_common
        out['market_implied_roe'] = None
        if base_v:
            out['pb_roe']['vs_market'] = base_v / market_cap - 1
        # 市场隐含 ROE：PB_mkt = (ROE−g)/(Ke−g) → ROE = g + PB_mkt(Ke−g)
        out['market_implied_roe'] = g + (market_cap / bv_common) * (ke - g)
    return out


def _football(ctx_out):
    rows = []
    def add(kind, method, low, mid, high, basis, in_composite=True):
        if low is None or high is None:
            return
        lo, hi = min(low, high), max(low, high)
        rows.append({'kind': kind, 'method': method, 'low': lo, 'mid': mid, 'high': hi, 'basis': basis, 'unit': '元', 'currency': 'CNY',
                     'in_composite': in_composite})
    sc = ctx_out.get('scenarios') or {}
    if sc:
        add('dcf', 'DCF 三情景（压力–基准–乐观）', _eq(sc.get('stress')), _eq(sc.get('base')), _eq(sc.get('optimistic')),
            '三情景分别完整重算三表与 DCF')
    sg = (ctx_out.get('sensitivity') or {}).get('wacc_g')
    if sg and sg.get('rows') and sg.get('values'):
        n = len(sg['rows'])
        vals = sg['values']
        lo = vals[0][-1] if n else None     # g 最低 × WACC 最高
        hi = vals[-1][0] if n else None     # g 最高 × WACC 最低
        add('dcf', 'DCF 基准 WACC×g 矩阵角点', lo, _eq(sc.get('base')), hi, 'WACC ±1pp、g ±1pp 角点（保持现金流不变）')
    wm = (ctx_out.get('sensitivity') or {}).get('wacc_exit')
    if wm and wm.get('values'):
        flat = [v for line in wm['values'] for v in line if v is not None]
        if flat:
            add('dcf', 'DCF 退出倍数法 (WACC×退出倍数)', min(flat), (ctx_out.get('dcf_exit') or {}).get('equity_value'), max(flat),
                '同行 EV/EBITDA 的 Q1–Q3 × 终年 EBITDA，WACC ±1pp')
    rel = (ctx_out.get('relative') or {}).get('valuation') or {}
    members = ((rel.get('composite') or {}).get('methods')) or []
    for m, r in (rel.get('by_multiple') or {}).items():
        if r.get('applicable'):
            add('relative', r['label'], r['low']['equity_value'], r['mid']['equity_value'], r['high']['equity_value'],
                f"同行 IQR 后 Q1/中位/Q3 × 目标指标；n={ctx_out['relative']['statistics']['stats'][m]['after']['n']}"
                + ('' if m in members else '；仅展示，不计入综合区间'), in_composite=(m in members))
    mk = ctx_out.get('market') or {}
    if mk.get('range_52w'):
        add('market', '52 周价格区间 × 股数', mk['range_52w'][0], mk.get('market_cap'), mk['range_52w'][1], '收盘价最低/最高 × 股数')
    return rows


def build_pro_model(facts, options=None):
    """入口。facts 为项目内事实列表（来自 PDF 管线或 valuation_ashare）；options 见 docs。返回 JSON 安全字典。"""
    options = dict(options or {})
    unit = options.get('unit', '元')
    res = {'engine_version': ENGINE_VERSION, 'ok': False, 'company': options.get('company', ''), 'currency': options.get('currency', 'CNY'),
           'unit': '元', 'input_unit': unit, 'warnings': [], 'refusals': [], 'assumptions': [], 'notes': []}
    hist = history(facts, unit)
    years = sorted(y for y in hist if hist[y].get('revenue') or hist[y].get('parent_net_profit'))
    if not years:
        res['refusals'].append('没有年度合并口径的已核验事实，无法建模')
        return jsonable(res)
    ly = options.get('base_year') or max(y for y in years if hist[y].get('revenue') or True)
    ly = int(ly)
    h = hist[ly]
    res['base_period'] = f'{ly}-12-31'
    HKEYS = ('revenue', 'cost_of_revenue', 'operating_profit', 'financial_expenses', 'investment_income', 'fair_value_gain',
             'profit_before_tax', 'income_tax', 'parent_net_profit', 'depreciation', 'rou_depreciation', 'amortization',
             'long_term_prepaid_amortization', 'capex', 'receivables', 'inventory', 'payables', 'cash', 'assets', 'equity')
    res['history'] = {str(y): {k: hist[y].get(k) for k in HKEYS if hist[y].get(k) is not None} for y in years[-5:]}
    profile = route(hist, ly, options)
    res['profile'] = profile
    vd = options.get('valuation_date') or date.today().isoformat()
    res['valuation_date'] = vd
    try:
        bs_age = (date.fromisoformat(vd) - date(ly, 12, 31)).days
    except ValueError:
        bs_age = None
    shares_info = infer_shares(hist, ly, options)
    shares = shares_info['shares']
    price = dec(options.get('price'))
    market_cap = dec(options.get('market_cap')) or ((price * shares) if (price and shares) else None)
    mk = {'price': price, 'price_date': options.get('price_date'), 'shares': shares, 'shares_basis': shares_info['basis'],
          'shares_source': shares_info['source'], 'market_cap': market_cap}
    sp = options.get('stock_prices')
    if sp and shares:
        last250 = [p for _, p in sp[-250:]]
        mk['range_52w'] = (D(str(min(last250))) * shares, D(str(max(last250))) * shares)
    res['market'] = mk
    res['warnings'].extend(shares_info['notes'])
    items = bridge_items(h)
    nd_like = net_debt_like(items)
    res['bridge_items'] = items
    res['net_debt_like'] = nd_like

    # ------------------ 相对估值（所有路径都做） ------------------
    pe_net = h.get('parent_net_profit')
    hist_np = [hist[y].get('parent_net_profit') for y in years[-5:] if hist[y].get('parent_net_profit') is not None]
    drivers, assumptions, cal_w = (None, [], [])
    ebit_hist = []
    if not profile['financial']:
        drivers, assumptions, cal_w = calibrate(hist, ly, profile, options)
        res['warnings'].extend(cal_w)
    d_last = derive_year(h)
    pe_equity = (h.get('parent_equity') or ZERO) - (h.get('other_equity_instruments') or ZERO)
    prev_np = hist.get(ly - 1, {}).get('parent_net_profit')
    np_growth = ((pe_net / prev_np - 1) if (pe_net and prev_np and prev_np > 0 and pe_net > 0) else None)
    target = {'code': options.get('code', ''), 'name': res['company'], 'revenue': h.get('revenue'), 'net_income': pe_net,
              'book_equity': pe_equity, 'ebitda': d_last['ebitda'], 'np_growth': np_growth,
              'is_financial': profile['financial'], 'is_cyclical': profile['cyclical'], 'net_debt_like': nd_like}
    if profile['cyclical'] and hist_np:
        target['normalized_net_income'] = sum(hist_np) / len(hist_np)
        e_hist = [derive_year(hist[y])['ebitda'] for y in years[-5:] if derive_year(hist[y])['ebitda'] is not None]
        if e_hist:
            target['normalized_ebitda'] = sum(e_hist) / len(e_hist)
    cands = options.get('peer_candidates') or []
    rel = None
    if cands:
        rules = {'industry': options.get('industry_key'), **(options.get('peer_rules') or {})}
        rel = rv.run_relative(target, cands, rules, nd_like)
        res['relative'] = rel
        res['warnings'].extend(rel['warnings'])
    else:
        res['warnings'].append('未提供同行候选，未做相对估值。')

    # ------------------ 金融路径 ------------------
    if profile['financial']:
        fin = financial_path(hist, ly, profile, options, market_cap, shares_info, None)
        res['financial'] = fin
        res['refusals'].append(fin['reason'])
        sc = {}
        pb = (fin.get('pb_roe') or {}).get('equity_value') or {}
        ff = []
        if pb.get('low') is not None and pb.get('high') is not None:
            ff.append({'kind': 'financial', 'method': 'PB-ROE（合理 PB）', 'low': pb['low'], 'mid': pb['base'], 'high': pb['high'],
                       'basis': 'ROE±1pp 与 Ke∓1pp', 'unit': '元', 'currency': 'CNY'})
        ddm = fin.get('ddm')
        if ddm:
            ff.append({'kind': 'financial', 'method': 'DDM（保守下限）', 'low': ddm['equity_value'], 'mid': ddm['equity_value'], 'high': ddm['equity_value'],
                       'basis': ddm['note'], 'unit': '元', 'currency': 'CNY'})
        for m, r in ((rel or {}).get('valuation', {}).get('by_multiple') or {}).items():
            if r.get('applicable'):
                ff.append({'kind': 'relative', 'method': r['label'], 'low': r['low']['equity_value'], 'mid': r['mid']['equity_value'],
                           'high': r['high']['equity_value'], 'basis': '同行 IQR 后 Q1/中位/Q3', 'unit': '元', 'currency': 'CNY'})
        if mk.get('range_52w'):
            ff.append({'kind': 'market', 'method': '52 周价格区间 × 股数', 'low': mk['range_52w'][0], 'mid': market_cap, 'high': mk['range_52w'][1],
                       'basis': '', 'unit': '元', 'currency': 'CNY'})
        res['football_field'] = ff
        gates = {'market_cap': market_cap, 'n_peers': len(rel['selection']['included']) if rel else None, 'bs_age_days': bs_age,
                 'shares_inference_ok': shares_info['ok'] if shares_info['basis'] != 'provided' else None}
        res['gates'] = sanity.run_gates(gates)
        res['valuation_range'] = _range_of(ff, market_cap, dcf=None)
        res['ok'] = True
        res['no_target_price'] = '不输出目标价，仅输出区间；不构成投资建议。'
        return jsonable(res)

    # ------------------ 非金融：DCF ------------------
    res['assumptions'] = assumptions
    res['years'] = int(options.get('years', DEFAULTS['years']))
    tax = drivers['tax_rate']
    wacc_info = build_wacc(drivers, h, items, profile, options, market_cap, tax)
    wacc = wacc_info['wacc']
    g = dec(options.get('terminal_growth'), DEFAULTS['terminal_growth'])
    years_n = int(options.get('years', DEFAULTS['years']))
    if not 5 <= years_n <= 15:
        res['refusals'].append('预测年数须在 5–15')
        return jsonable(res)
    normalized_ebit = drivers['ebit_margin_target'] * drivers['revenue_base'] if drivers['ebit_margin_target'] is not None else None
    ctx = {'drivers': drivers, 'years': years_n, 'wacc': wacc, 'g': g, 'g_path_end': g, 'bridge': items, 'shares': shares,
           'mid_year': bool(options.get('mid_year', DEFAULTS['mid_year'])), 'payout': dec(options.get('payout'), DEFAULTS['payout_ratio']),
           'kd_book': drivers['kd_book'] if drivers['kd_book'] is not None else wacc_info['cost_of_debt'],
           'terminal_capex_da': dec(options.get('terminal_capex_da'), DEFAULTS['terminal_capex_da']),
           'ebitda_base': drivers['ebitda_base'], 'allow_above_gdp': bool(options.get('allow_above_gdp')),
           'terminal_roic_override': dec(options.get('terminal_roic')), 'exit_multiple': dec(options.get('exit_multiple')),
           'interest_basis': options.get('interest_basis', 'beginning')}
    op_facts, op_metrics, op_w = operating_facts(h, f'{ly}-12-31')
    ctx['op_facts'], ctx['op_metrics'] = op_facts, op_metrics
    res['warnings'].extend(op_w)
    res['wacc'] = wacc_info
    res['drivers'] = drivers
    # 退出倍数：同行 EV/EBITDA（IQR 后中位数）；否则 options
    if rel:
        st = rel['statistics']['stats']['ev_ebitda']
        if st['quartile_ok'] and ctx['exit_multiple'] is None:
            ctx['exit_multiple'] = st['after']['median']
            ctx['exit_multiple_source'] = f"同行 EV/EBITDA IQR 后中位数（n={st['after']['n']}）"
            ctx['exit_q'] = (st['after']['q1'], st['after']['q3'])
    refuse = None
    if normalized_ebit is None or normalized_ebit <= 0:
        refuse = (f'正常化核心 EBIT {ser(normalized_ebit) if normalized_ebit is not None else "缺失"} ≤ 0：'
                  '公司没有可外推的正的经营利润，Gordon 终值与整条 FCFF 流无经济意义，DCF 拒绝输出。'
                  '改用 PS/EV-Sales/PB（见 relative）；如需 DCF，请显式提供扭亏后目标 EBIT 率(ebit_margin_target)并说明依据。')
    elif drivers['ebit_margin_last'] is not None and drivers['ebit_margin_last'] < 0 and 'ebit_margin_target' not in options:
        res['warnings'].append('基期核心 EBIT 为负但近年均值为正：DCF 假设利润率回归均值，这是观点而非事实，请核对')
    sc_out = {}
    gate_ctx = {'market_cap': market_cap, 'n_peers': len(rel['selection']['included']) if rel else None, 'bs_age_days': bs_age,
                'is_cyclical': profile['cyclical'], 'ebit_margin_last': drivers['ebit_margin_last'],
                'ebit_margin_mean': drivers['ebit_margin_mean'], 'normalized_ebit': normalized_ebit, 'etr': drivers.get('etr_raw'),
                'shares_inference_ok': (shares_info['ok'] if shares_info['basis'] != 'provided' else None),
                'wacc': wacc, 'net_cash': -nd_like if (items['cash'] - items['debt'] - items['leases']) > 0 else None,
                'minority_equity': items['minority']}
    if refuse:
        res['refusals'].append(refuse)
        res['dcf'] = {'ok': False, 'reasons': [refuse]}
        res['gates'] = sanity.run_gates(gate_ctx)
        ff = _football({'relative': rel, 'market': mk})
        res['football_field'] = ff
        res['valuation_range'] = _range_of(ff, market_cap, dcf=None)
        res['ok'] = bool(ff)
        res['no_target_price'] = '不输出目标价，仅输出区间；不构成投资建议。'
        res['assumptions'] = assumptions
        return jsonable(res)

    # 三情景（每个情景完整重算三表与 DCF）
    levers = {'base': {},
              'optimistic': {'d_growth': D('0.03'), 'd_margin': D('0.015')},
              'stress': {'d_growth': D('-0.04'), 'd_margin': D('-0.03'), 'd_capex': D('0.01'), 'd_nwc_days': D(15),
                         'd_wacc': D('0.01'), 'g': g - D('0.005')}}
    levers.update(options.get('scenario_levers') or {})
    base_case = run_case(ctx, {})
    if not base_case.get('ok') or not base_case['dcf'].get('ok'):
        res['refusals'].extend(base_case.get('dcf', {}).get('reasons') or [base_case.get('reason', '基准 DCF 失败')])
        res['dcf'] = {'ok': False, 'reasons': res['refusals']}
        res['gates'] = sanity.run_gates(gate_ctx)
        ff = _football({'relative': rel, 'market': mk})
        res['football_field'] = ff
        res['valuation_range'] = _range_of(ff, market_cap, dcf=None)
        return jsonable(res)
    ctx['roic_base'] = base_case['roic_t']
    res['terminal'] = {'g': g, 'roic': base_case['roic_t'], 'terminal_capex_da': ctx['terminal_capex_da'], 'mid_year': ctx['mid_year'],
                       'exit_multiple': ctx.get('exit_multiple'), 'exit_multiple_source': ctx.get('exit_multiple_source')}
    scen = {'base': base_case}
    for name in ('optimistic', 'stress'):
        scen[name] = run_case(ctx, levers[name])
    res['scenarios'] = {n: {'levers': levers[n], 'equity': _eq(c), 'ev': c.get('ev'), 'ok': bool(c.get('ok') and c['dcf'].get('ok')),
                            'dcf': c.get('dcf'), 'wacc': c.get('wacc_used'), 'g': c.get('g_used'),
                            'tie_out_ok': c['three'].get('ok') if c.get('three') else None,
                            'reasons': (c.get('dcf') or {}).get('reasons')} for n, c in scen.items()}
    res['dcf'] = base_case['dcf']
    res['terminal_cross_check'] = {
        'gordon_value_driver': {'terminal_fcff': base_case['terminal_fcff_value_driver'], 'equity_value': base_case['equity'],
                                'tv_share': base_case['dcf']['terminal_value_share'],
                                'implied_exit_ev_ebitda': base_case['dcf'].get('implied_exit_multiple')},
        'gordon_simple': {'terminal_fcff': base_case['terminal_fcff_simple'],
                          'equity_value': (base_case.get('dcf_simple_gordon') or {}).get('equity_value'),
                          'tv_share': (base_case.get('dcf_simple_gordon') or {}).get('terminal_value_share'),
                          'note': '末年 FCFF×(1+g) 的简单 Gordon，对照用：若与价值驱动法差异大，说明末年 capex/NWC 不是稳态'},
        'exit_multiple': None}
    if base_case.get('dcf_exit_multiple'):
        de = base_case['dcf_exit_multiple']
        res['terminal_cross_check']['exit_multiple'] = {
            'multiple': ctx['exit_multiple'], 'source': ctx.get('exit_multiple_source', 'options'), 'equity_value': de.get('equity_value'),
            'tv_share': de.get('terminal_value_share'), 'implied_terminal_growth': de.get('implied_terminal_growth'),
            'note': '退出倍数取同行当前 EV/EBITDA，隐含永续增长率须与 g 互验：显著高于 g 说明倍数偏乐观'}
        res['dcf_exit'] = de

    # 三表勾稽
    three = base_case['three']
    res['three_statement'] = {'ok': three.get('ok'), 'mode': three.get('mode'), 'checks': three.get('checks'),
                              'opening_balanced': three['opening_balance'].get('balanced'),
                              'opening_residual': three['opening_balance'].get('residual'),
                              'statements': three.get('statements'), 'opening_balance': {k: three['opening_balance'][k] for k in
                              ('cash', 'debt', 'equity', 'total_assets', 'total_liabilities', 'retained_earnings', 'minority_equity', 'share_capital')},
                              'fcff_detail': base_case['fcff_detail'], 'warnings': three.get('warnings'),
                              'opening_detail': {k: three['opening_balance'].get(k) for k in
                                                 ('wc_assets', 'wc_liab', 'ncl', 'fa', 'revenue', 'cost_of_revenue', 'cash', 'debt',
                                                  'share_capital', 'retained_earnings', 'minority_equity', 'equity', 'period')},
                              'funding': {'mode': three.get('mode'), 'payout_ratio': ctx['payout'], 'minimum_cash': ZERO,
                                          'interest_basis': three.get('interest_basis'), 'cost_of_debt': ctx['kd_book'],
                                          'cash_yield': drivers['cash_yield'],
                                          'minority_income_share': (drivers.get('minority_income_share')
                                                                    if drivers.get('minority_income_share') is not None else
                                                                    (three['opening_balance']['minority_equity'] / three['opening_balance']['equity']
                                                                     if three['opening_balance'].get('equity') else ZERO))}}
    res['paths'] = base_case['paths']

    # 敏感性
    def eq_fn(lv):
        c = run_case(ctx, lv)
        return _eq(c)
    meta = {'currency': 'CNY', 'unit': '元', 'base_period': f'{ly}-12-31', 'metric': '权益价值(普通股)', 'valuation_date': vd,
            'axis_note': '行轴/列轴含义见 row_name/col_name；单元格 None=该组合被模型拒绝(如 g≥WACC)'}
    def fast(lv):   # 不改现金流：只重新折现
        w = dec(lv.get('wacc'))
        r = revalue(ctx, base_case, w, g=dec(lv.get('g')), exit_multiple=dec(lv.get('exit_multiple')), roic=ctx['roic_base'])
        return r.get('equity') if r.get('dcf', {}).get('ok') else None
    def fast_exit(lv):
        r = revalue(ctx, base_case, dec(lv.get('wacc')), exit_multiple=dec(lv.get('exit_multiple')), roic=ctx['roic_base'])
        return r.get('equity_exit')
    S = {}
    w_axis = sens.axis(wacc, D('0.005'), 2)
    g_axis = sens.axis(g, D('0.005'), 2)
    S['wacc_g'] = sens.two_way(fast, g_axis, w_axis, 'g', 'wacc', meta={**meta, 'rows': '永续增长率 g', 'cols': 'WACC'})
    if ctx.get('exit_multiple') and base_case.get('dcf_exit_multiple'):
        m0 = ctx['exit_multiple']
        m_axis = [m0 * D(x) for x in ('0.8', '0.9', '1', '1.1', '1.2')]
        S['wacc_exit'] = sens.two_way(fast_exit, m_axis, w_axis, 'exit_multiple', 'wacc', meta={**meta, 'rows': '退出 EV/EBITDA 倍数', 'cols': 'WACC'})
    S['growth_margin'] = sens.two_way(eq_fn, [D(x) for x in ('-0.04', '-0.02', '0', '0.02', '0.04')],
                                      [D(x) for x in ('-0.03', '-0.015', '0', '0.015', '0.03')], 'd_growth', 'd_margin',
                                      meta={**meta, 'rows': '收入增速变动(pp, 加到每个预测年)', 'cols': 'EBIT 率变动(pp, 加到每个预测年)',
                                            'note': '每格完整重算三表预测与 DCF'})
    S['capex_nwc'] = sens.two_way(eq_fn, [D(x) for x in ('-0.02', '-0.01', '0', '0.01', '0.02')], [D(x) for x in ('-20', '-10', '0', '10', '20')],
                                  'd_capex', 'd_nwc_days', meta={**meta, 'rows': '资本开支/收入变动(pp)', 'cols': '应收账款天数变动(天)',
                                                                 'note': '每格完整重算三表预测与 DCF'})
    specs = [{'name': 'd_growth', 'label': '收入增速 ±2pp', 'low': D('-0.02'), 'high': D('0.02'), 'unit': 'pp'},
             {'name': 'd_margin', 'label': 'EBIT 率 ±2pp', 'low': D('-0.02'), 'high': D('0.02'), 'unit': 'pp'},
             {'name': 'd_capex', 'label': '资本开支率 ±1pp', 'low': D('-0.01'), 'high': D('0.01'), 'unit': 'pp'},
             {'name': 'd_nwc_days', 'label': '应收天数 ±15 天', 'low': D('-15'), 'high': D('15'), 'unit': '天'},
             {'name': 'd_tax', 'label': '税率 ±3pp', 'low': D('-0.03'), 'high': D('0.03'), 'unit': 'pp'},
             {'name': 'd_wacc', 'label': 'WACC ±1pp', 'low': D('-0.01'), 'high': D('0.01'), 'unit': 'pp'},
             {'name': 'g', 'label': '永续增长率 ±0.5pp', 'low': g - D('0.005'), 'high': g + D('0.005'), 'unit': 'pp'},
             {'name': 'terminal_roic', 'label': '终期 ROIC ±3pp', 'low': max(ctx['roic_base'] - D('0.03'), g + D('0.005')), 'high': ctx['roic_base'] + D('0.03'),
              'unit': 'pp'}]
    S['tornado'] = sens.tornado(eq_fn, {}, specs, base_value=base_case['equity'])
    S['tornado']['meta'] = {**meta, 'note': '按权益价值摆幅降序；每个杠杆单独变动、其余保持基准'}
    res['sensitivity'] = S

    # 反向 DCF
    if market_cap:
        rd = {'target_market_cap': market_cap}
        def solve(name, lo, hi):
            f = lambda x: eq_fn({name: x})
            return core.bisect(f, lo, hi, market_cap, tol=D('1e-6'))
        sg = solve('d_growth', D('-0.15'), D('0.25'))
        sm = solve('d_margin', D('-0.20'), D('0.30'))
        base_g5 = _mean(base_case['paths']['revenue_growth'][:5])
        rd['growth'] = ({'ok': True, 'd_growth_pp': sg['x'], 'implied_avg_growth_first5': base_g5 + sg['x'], 'base_avg_growth_first5': base_g5,
                         'text': f"要使基准 DCF 等于当前市值，需要收入增速在每个预测年上移 {float(sg['x'])*100:+.1f}pp（前 5 年均值 {float(base_g5):.1%} → {float(base_g5 + sg['x']):.1%}），其余假设不变"}
                        if sg.get('ok') else {'ok': False, 'reason': sg.get('reason'), 'text': '市值落在可调增速范围之外，说明市场定价无法由增速单一因素解释'})
        base_mt = base_case['margins'][-1]
        rd['margin'] = ({'ok': True, 'd_margin_pp': sm['x'], 'base_terminal_ebit_margin': base_mt, 'implied_terminal_ebit_margin': base_mt + sm['x'],
                         'text': f"要使基准 DCF 等于当前市值，需要 EBIT 率每年上移 {float(sm['x'])*100:+.1f}pp（终年 {float(base_mt):.1%} → {float(base_mt + sm['x']):.1%}），其余不变"}
                        if sm.get('ok') else {'ok': False, 'reason': sm.get('reason'), 'text': '市值落在可调利润率范围之外'})
        rd['note'] = '反向 DCF 是"市场在押注什么"的条件性解读，不是已实现增长或未来预测；单一因素解释会夸大该因素的调整幅度。'
        res['reverse_dcf'] = rd
        gate_ctx['market_implied'] = rd if (sg.get('ok') or sm.get('ok')) else {'text': 'n/a'}

    # 闸门
    dd = base_case['dcf']
    explicit_g = base_case['paths']['revenue_growth']
    gate_ctx.update({'tv_share': dd.get('terminal_value_share'), 'implied_ev_ebitda': dd.get('implied_ev_ebitda_ltm'),
                     'implied_pe': (dd['equity_value'] / pe_net) if (pe_net and pe_net > 0) else None,
                     'terminal_capex_da': (base_case['capex_n'] / base_case['da_n']) if base_case['da_n'] else None,
                     'explicit_growth': explicit_g, 'terminal_growth': g, 'spread': wacc - g, 'enterprise_value': dd.get('enterprise_value'),
                     'equity_base': dd['equity_value'], 'equity_value': dd['equity_value'],
                     'minority_profit_share': drivers.get('minority_income_share'),
                     'minority_equity_share': (items['minority'] / h['equity']) if h.get('equity') else None,
                     'three_statement_ok': three.get('ok'), 'terminal_reinvestment': base_case['terminal_reinvestment'],
                     'implied_exit_multiple': dd.get('implied_exit_multiple'), 'peer_ev_ebitda': ctx.get('exit_multiple')})
    res['gates'] = sanity.run_gates(gate_ctx)
    res['market_implied_text'] = (res.get('reverse_dcf') or {}).get('growth', {}).get('text')

    # 足球场 + 区间
    res['scenarios_equity'] = {n: _eq(c) for n, c in scen.items()}
    res['football_field'] = _football({'scenarios': scen, 'sensitivity': S, 'dcf_exit': res.get('dcf_exit'), 'relative': rel, 'market': mk})
    dcf_rng = _range_from([S['wacc_g']['values'][0][-1], S['wacc_g']['values'][-1][0], _eq(scen['stress']), _eq(scen['optimistic'])]) \
        if S['wacc_g'].get('values') else (None, None)
    res['valuation_range'] = _range_of(res['football_field'], market_cap, dcf={'low': _eq(scen['stress']), 'base': _eq(scen['base']), 'high': _eq(scen['optimistic'])})
    res['ok'] = True
    res['no_target_price'] = '不输出目标价，仅输出区间；不构成投资建议。'
    res['per_share_range'] = ({'low': res['valuation_range']['low'] / shares, 'high': res['valuation_range']['high'] / shares,
                               'unit': '元/股', 'shares_basis': shares_info['basis']}
                              if (shares and res['valuation_range'].get('low') is not None) else None)
    res['evidence_note'] = 'assumptions[].evidence_class：fact=事实(年报原文)，derived=推论(由事实计算)，judgment=观点(启发式/默认假设)。'
    res['time_note'] = (f'估值基期为 {ly}-12-31 资产负债表；估值日 {vd}，相隔 {bs_age} 天。DCF 价值对应基期，'
                        '未滚动到估值日（也未扣减期间分红），与市值比较存在时间错配。') if bs_age is not None else ''
    return jsonable(res)


def _range_of(ff, market_cap, dcf):
    """估值区间合成（不输出目标价）：

    - DCF 区间 = 压力–乐观情景；相对估值区间 = 各适用倍数 Q1/Q3 的中位数；
    - 两区间重叠 → 取重叠区作"共识区间"；不重叠 → 取并集并标注 overlap=False，由读者判断；
    - 同时给出市值相对共识区间的位置。
    """
    rel = [r for r in ff if r['kind'] == 'relative' and r.get('in_composite', True)]
    fin = [r for r in ff if r['kind'] == 'financial']
    out = {'dcf': None, 'relative': None, 'overlap': None, 'low': None, 'high': None, 'market_position': None}
    def med(vs):
        vs = sorted(vs)
        n = len(vs)
        return None if not vs else (vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2)
    if dcf and dcf.get('low') is not None and dcf.get('high') is not None:
        out['dcf'] = {'low': min(dcf['low'], dcf['high']), 'base': dcf.get('base'), 'high': max(dcf['low'], dcf['high'])}
    if rel:
        out['relative'] = {'low': med([r['low'] for r in rel]), 'mid': med([r['mid'] for r in rel]), 'high': med([r['high'] for r in rel]),
                           'methods': [r['method'] for r in rel]}
    if fin:
        out['financial'] = {'low': med([r['low'] for r in fin]), 'high': med([r['high'] for r in fin])}
        out['relative'] = out['relative'] or {'low': out['financial']['low'], 'high': out['financial']['high'], 'mid': None,
                                              'methods': [r['method'] for r in fin]}
        if rel and fin:
            out['relative'] = {'low': med([r['low'] for r in rel + fin]), 'high': med([r['high'] for r in rel + fin]), 'mid': None,
                               'methods': [r['method'] for r in rel + fin]}
    a, b = out['dcf'], out['relative']
    if a and b and b['low'] is not None:
        lo, hi = max(a['low'], b['low']), min(a['high'], b['high'])
        if lo <= hi:
            out.update(overlap=True, low=lo, high=hi, rule='DCF 区间与相对估值区间的重叠部分')
        else:
            out.update(overlap=False, low=min(a['low'], b['low']), high=max(a['high'], b['high']),
                       rule='两区间不重叠：取并集作宽区间，分歧本身是结论（见闸门与反向 DCF）')
    elif a:
        out.update(low=a['low'], high=a['high'], rule='仅 DCF 区间（无相对估值）')
    elif b and b['low'] is not None:
        out.update(low=b['low'], high=b['high'], rule='仅相对估值区间（DCF 不适用/被拒绝）')
    if market_cap and out['low'] is not None:
        if market_cap < out['low']:
            out['market_position'] = '市值低于估值区间'
        elif market_cap > out['high']:
            out['market_position'] = '市值高于估值区间'
        else:
            out['market_position'] = '市值位于估值区间内'
        out['market_cap'] = market_cap
    return out
