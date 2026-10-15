"""三情景预测与估值压力测试：让"发现的风险"真正进到模型里。

竞赛方案里最值钱的一句话是「风险如何改变现金流和估值」。多数作品停在
"算了一堆比率"，做不出"把回款天数拖到压力情景，DCF 区间实时下降"这个闭环。
本模块就是补这一环。

约定（与 financial_analysis / forensics 保持一致）：
- 金额一律 Decimal，单位：元；比例是分数（0.2 = 20%）。
- 不输出单点目标价，只输出按情景分布的价值区间 —— 避免变成荐股。
- 所有假设都要记录来源与依据，可被质疑、可被替换。
- 压力情景的参数必须由法证发现**推导**出来，不允许手填一个悲观数字了事。

核心公式::

    FCFF = EBIT * (1 - tax) + D&A - Capex - ΔNWC
    EV   = Σ FCFF_t / (1 + WACC)^t + TV / (1 + WACC)^n
    TV   = FCFF_{n+1} / (WACC - g)
    Equity = EV - 净债务 - 少数股东权益 + 非经营性资产
"""
from __future__ import annotations

from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext

ZERO = Decimal(0)
ONE = Decimal(1)

# 法证发现 → 估值参数的传导规则。每条都写清"哪个异常动哪个参数"，
# 这样压力情景不是拍脑袋，而是从证据链自动生成的。
RISK_TO_PARAM = {
    'R_AR_01': {'params': ['receivable_days'], 'direction': +1, 'step': Decimal('0.20'),
                'path': '应收天数上升 → 营运资本占用增加 → FCFF 下降'},
    'R_INV_01': {'params': ['inventory_days'], 'direction': +1, 'step': Decimal('0.20'),
                 'path': '存货天数上升 + 跌价率 → 现金占用与减值损失增加'},
    'R_CFO_01': {'params': ['wacc', 'terminal_growth'], 'direction': +1, 'step': Decimal('0.005'),
                 'path': '盈利现金含量不足 → 风险溢价上调 WACC、终值增长率下调'},
    'R_MARGIN_01': {'params': ['gross_margin'], 'direction': -1, 'step': Decimal('0.02'),
                    'path': '毛利率向同行回归 → EBIT 与终值下降'},
    'R_NREC_01': {'params': ['sustainable_profit_discount'], 'direction': +1, 'step': Decimal('0.10'),
                  'path': '扣非口径下调可持续利润 → 整条 FCFF 流按折让打折'},
    'R_GW_01': {'params': ['impairment_to_revenue'], 'direction': +1, 'step': Decimal('0.03'),
                'path': '商誉减值 → 首年一次性税前损失 → EV 与净资产同步下降'},
    'R_DEBT_01': {'params': ['net_debt'], 'direction': +1, 'step': Decimal('0.05'),
                  'path': '短债缺口 → 净债务增加 → EV 转权益价值下降'},
    'R_SETTLE_01': {'params': ['receivable_days'], 'direction': +1, 'step': Decimal('0.10'),
                    'path': '收现比偏低 → 营运资本占用 → FCFF'},
    'R_EXP_01': {'params': ['opex_ratio'], 'direction': +1, 'step': Decimal('0.01'),
                 'path': '费用率低估（费用被资本化）→ 期间费用率上调 → EBIT 与终值下降'},
    'R_OCF_01': {'params': ['near_term_fcff_haircut'], 'direction': +1, 'step': Decimal('0.10'),
                 'path': '经营现金流恶化 → 近端两年 FCFF 折减'},
    'R_CAP_01': {'params': ['capex_to_revenue'], 'direction': +1, 'step': Decimal('0.02'),
                 'path': '资本开支强度上升 → FCFF 分子减少（若为费用资本化，同时也意味着真实费用更高）'},
    'R_RPT_01': {'params': ['sustainable_profit_discount'], 'direction': +1, 'step': Decimal('0.05'),
                 'path': '体外资金占用 → 可分配给股东的利润低于账面 → 估值基数折让'},
    'R_DISC_01': {'params': ['data_confidence'], 'direction': -1, 'step': Decimal('0'),
                  'path': '关键科目缺失 → 全部估值结果降级为参考值'},
    'R_TIEOUT_01': {'params': ['data_confidence'], 'direction': -1, 'step': Decimal('0'),
                    'path': '数据可信度不足 → 全部估值结果降级为参考值'},
}

SEVERITY_WEIGHT = {'critical': Decimal('1.0'), 'high': Decimal('0.7'),
                   'medium': Decimal('0.4'), 'low': Decimal('0.15')}

DEFAULT_ASSUMPTIONS = {
    'years': 5,
    'revenue_growth': Decimal('0.08'),
    'gross_margin': Decimal('0.30'),
    'opex_ratio': Decimal('0.15'),          # 销售+管理+研发费用 / 收入
    'tax_rate': Decimal('0.15'),
    'da_to_revenue': Decimal('0.05'),       # 折旧摊销 / 收入
    'capex_to_revenue': Decimal('0.06'),    # 资本开支 / 收入
    'receivable_days': Decimal('60'),
    'inventory_days': Decimal('60'),
    'payable_days': Decimal('60'),
    'wacc': Decimal('0.10'),
    'terminal_growth': Decimal('0.03'),
    # 下面三个旋钮是"法证发现 → 估值"的落点。它们必须真实存在于模型里，
    # 否则对应规则会被静默记为"参数缺失、无法传导"，压力情景与基准情景
    # 数值一模一样，而报告上看不出任何异常（实测踩过：11 条规则里 4 条失效）。
    'impairment_to_revenue': Decimal('0'),        # 减值损失 / 收入，一次性，计入首年 EBIT
    'near_term_fcff_haircut': Decimal('0'),       # 近端（前两年）FCFF 折减比例
    'sustainable_profit_discount': Decimal('0'),  # 扣非后可持续利润对报告利润的折让
    'net_debt': Decimal('0'),
    # 有息负债总额与类现金总额。它们不直接参与折现，作用是给压力情景提供
    # **正确的规模基准** —— 净现金公司（net_debt 为负）如果拿净值当基准，
    # 「债务风险」这条规则会退化成 +0.03 元的无效调整（见 risks_to_parameters）。
    'gross_debt': Decimal('0'),
    'cash_like': Decimal('0'),
    'minority_interest': Decimal('0'),
    'non_operating_assets': Decimal('0'),
    # 倍数交叉验证的基数，不参与折现，只用于算隐含 EV/EBITDA 与 P/E。
    'ebitda_base': None,
    'parent_net_profit_base': None,
    'shares': None,
}

SCENARIOS = {
    'base': {'label': '基准情景', 'note': '管理层计划中可被历史与行业证据支持的部分'},
    'optimistic': {'label': '乐观情景', 'note': '需求、价格或良率改善；不把所有参数同时调到最好'},
    'stress': {'label': '压力情景', 'note': '结构性下行下限 + 最高优先级风险到回款、毛利、减值或资本开支的传导'},
}

# 压力情景的**结构性兜底**。
#
# 为什么需要它：压力情景此前完全由法证发现驱动。当扫描没有产出发现时
# （数据干净、或调用方没传 findings），`stress` 的参数与 `base` 逐位相同，
# 三情景估值一模一样 —— 报告上完全看不出异常，但"压力测试"其实什么都没测。
# 这是"静默失效"里最危险的一类：它不报错，只是悄悄把风险章节变成摆设。
#
# 因此压力情景改为两层，**证据优先、兜底填缝**：
#   ① 证据驱动的传导（RISK_TO_PARAM）：法证发现按严重度映射到参数；
#   ② 结构性兜底（本表）：只作用于①没有覆盖到的参数。
# 不做相加（双重计算），也不取"更严重者"（会吞掉温和的证据，实测消融增量为 0）。
# 两层在 `applied` 里用 source 与 binding 明确区分，报告可以据此说明
# "哪些压力来自证据、哪些来自情景假设"，不会把假设冒充成结论。
#
# 幅度取"温和衰退"量级而非极端值，理由：压力情景的作用是检验结论的
# 稳健性，把参数打到极端只会得到一个显然为负的结论，失去信息量。
STRESS_FLOOR = {
    'revenue_growth': (Decimal('-0.06'), 'absolute', '增速下调 6 个百分点'),
    'gross_margin': (Decimal('-0.02'), 'absolute', '毛利率下调 2 个百分点'),
    'opex_ratio': (Decimal('0.01'), 'absolute', '费用率上升 1 个百分点（收入下滑但费用刚性）'),
    'receivable_days': (Decimal('15'), 'absolute', '回款天数增加 15 天'),
    'inventory_days': (Decimal('10'), 'absolute', '存货天数增加 10 天'),
    'payable_days': (Decimal('-5'), 'absolute', '付款天数减少 5 天（供应商收紧账期）'),
    'capex_to_revenue': (Decimal('0.01'), 'absolute', '资本开支率上升 1 个百分点'),
    'wacc': (Decimal('0.01'), 'absolute', '折现率上升 1 个百分点（风险溢价扩大）'),
    'terminal_growth': (Decimal('-0.005'), 'absolute', '永续增长率下调 0.5 个百分点'),
    'cost_of_debt': (Decimal('0.01'), 'absolute', '借款成本上升 1 个百分点（再融资利差扩大）'),
}
STRESS_FLOOR_SOURCE = '结构性压力兜底（情景假设，非证据驱动）'


def _clamp_stress_param(key, value):
    """把压力参数夹回有意义的区间。

    不夹会产生两种荒唐结果：一是"压力情景比基准情景更乐观"
    （毛利率被压成负数、周转天数被压成负数使营运资本反而释放现金），
    二是参数越界触发 validate() 直接让整个压力情景跑不出来。
    """
    if key in ('gross_margin', 'opex_ratio'):
        return max(value, Decimal('0.01'))
    if key in ('receivable_days', 'inventory_days', 'payable_days', 'terminal_growth'):
        return max(value, ZERO)
    if key == 'revenue_growth':
        return max(value, Decimal('-0.99'))
    if key in ('capex_to_revenue', 'wacc', 'cost_of_debt'):
        return max(value, ZERO)
    return value


def _d(value, default=None):
    if value is None or value == '':
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _s(value):
    if value is None:
        return ''
    if isinstance(value, Decimal):
        text = format(value.normalize(), 'f')
        return '0' if text == '-0' else text
    return str(value)


def _nwc_days(params) -> Decimal:
    """净营运资本占用，换算到**收入口径**的天数。

    这里有一个必须处理的单位问题：三项周转天数的分母并不相同 ——

        应收周转天数 = 期间天数 × 平均应收账款 / 营业收入
        存货周转天数 = 期间天数 × 平均存货     / 营业成本
        应付周转天数 = 期间天数 × 平均应付账款 / 营业成本

    把三者直接相加再乘收入，等于默认存货与应付也用收入作分母，
    会把营运资本占用放大 1/(1 − 毛利率) 倍。实测某公司真实净营运资本
    44.2 亿元（占收入 8.41%）被算成 103.5 亿元（占收入 19.69%），
    ΔNWC 随之虚增一倍以上，FCFF 与估值被系统性压低。

    正确的换算是把成本口径的两项按 COGS/收入 =（1 − 毛利率）折算到收入口径：

        NWC = 收入/天数 × [ 应收天数 +（存货天数 − 应付天数）×（1 − 毛利率）]

    毛利率取不到时退化为原口径（等于假设成本率 100%），并应在报告里说明。
    """
    receivable = _d(params.get('receivable_days'), ZERO)
    inventory = _d(params.get('inventory_days'), ZERO)
    payable = _d(params.get('payable_days'), ZERO)
    gross_margin = _d(params.get('gross_margin'), None)
    if gross_margin is None:
        cost_ratio = ONE
    else:
        cost_ratio = max(min(ONE - gross_margin, ONE), ZERO)
    return receivable + (inventory - payable) * cost_ratio


def project(params: dict) -> dict:
    """按业务驱动做三表式的关键科目预测，并逐年算 FCFF。

    参数是"驱动变量"而不是直接外推净利润 —— 这是竞赛反复强调的一点。
    """
    years = int(_d(params.get('years'), 5))
    growth = _d(params.get('revenue_growth'), Decimal('0.08'))
    gm = _d(params.get('gross_margin'), Decimal('0.30'))
    opex = _d(params.get('opex_ratio'), Decimal('0.15'))
    tax = _d(params.get('tax_rate'), Decimal('0.15'))
    da_r = _d(params.get('da_to_revenue'), Decimal('0.05'))
    capex_r = _d(params.get('capex_to_revenue'), Decimal('0.06'))
    impairment_r = _d(params.get('impairment_to_revenue'), ZERO)
    near_term_haircut = _d(params.get('near_term_fcff_haircut'), ZERO)
    profit_discount = _d(params.get('sustainable_profit_discount'), ZERO)
    revenue_0 = _d(params.get('revenue_base'), None)

    rows = []
    nwc_days = _nwc_days(params)
    prev_nwc = None
    if revenue_0 is not None and nwc_days is not None:
        prev_nwc = revenue_0 * nwc_days / Decimal('365')

    with localcontext() as ctx:
        ctx.prec = 28
        revenue = revenue_0
        for t in range(1, years + 1):
            if revenue is None:
                break
            revenue = revenue * (ONE + growth)
            ebit = revenue * (gm - opex)
            if t == 1:
                # 减值是一次性损失，只落在首年，且税前扣除
                ebit -= revenue * impairment_r
            nopat = ebit * (ONE - tax)
            da = revenue * da_r
            capex = revenue * capex_r
            nwc = revenue * nwc_days / Decimal('365')
            delta_nwc = (nwc - prev_nwc) if prev_nwc is not None else nwc
            fcff = nopat + da - capex - delta_nwc
            if t <= 2:
                # 近端现金流折减：对应"经营现金流恶化、短期回款不畅"类发现
                fcff = fcff * (ONE - near_term_haircut)
            # 扣非折让作用在整条现金流上（含终值，因为终值由末年 FCFF 推出）
            fcff = fcff * (ONE - profit_discount)
            rows.append({
                'year': t, 'revenue': revenue, 'ebit': ebit, 'nopat': nopat,
                'depreciation_amortization': da, 'capex': capex,
                'nwc': nwc, 'delta_nwc': delta_nwc, 'fcff': fcff,
            })
            prev_nwc = nwc
    return {'years': years, 'rows': rows, 'parameters': {
        'revenue_base': _s(revenue_0), 'revenue_growth': _s(growth), 'gross_margin': _s(gm),
        'opex_ratio': _s(opex), 'tax_rate': _s(tax),
        'impairment_to_revenue': _s(impairment_r),
        'near_term_fcff_haircut': _s(near_term_haircut),
        'sustainable_profit_discount': _s(profit_discount),
        'receivable_days': _s(_d(params.get('receivable_days'))),
        'inventory_days': _s(_d(params.get('inventory_days'))),
        'payable_days': _s(_d(params.get('payable_days'))),
        # 换算到收入口径后的净营运资本占用天数，以及占收入的比例。
        # 报告里引用营运资本假设时用这两个数，不要引用三项天数的简单相加 ——
        # 那三项的分母不同（应收用收入、存货与应付用营业成本），直接相加是错的。
        'nwc_days_revenue_basis': _s(nwc_days),
        'nwc_to_revenue': _s(nwc_days / Decimal('365')) if nwc_days is not None else '',
    }}


def dcf(projection: dict, wacc, terminal_growth, net_debt=ZERO,
        minority_interest=ZERO, non_operating_assets=ZERO, shares=None) -> dict:
    """把预测的 FCFF 折成企业价值与权益价值，并给出区间而不是单点。"""
    wacc = _d(wacc, Decimal('0.10'))
    g = _d(terminal_growth, Decimal('0.03'))
    net_debt = _d(net_debt, ZERO)
    minority_interest = _d(minority_interest, ZERO)
    non_operating_assets = _d(non_operating_assets, ZERO)
    rows = projection.get('rows') or []
    warnings = []
    if not rows:
        return {'ok': False, 'warnings': ['没有可折现的预测期 FCFF，无法估值。'],
                'enterprise_value': None, 'equity_value': None}

    if g >= wacc:
        warnings.append('永续增长率不低于 WACC，终值公式不成立；已拒绝计算，请检查假设。')
        return {'ok': False, 'warnings': warnings,
                'enterprise_value': None, 'equity_value': None,
                'wacc': _s(wacc), 'terminal_growth': _s(g)}

    with localcontext() as ctx:
        ctx.prec = 28
        pv_explicit = ZERO
        for r in rows:
            pv_explicit += r['fcff'] / ((ONE + wacc) ** r['year'])
        last_fcff = rows[-1]['fcff']
        # 终值用 Gordon 增长模型，终值现金流按"下一年"口径，避免低估
        terminal_fcff = last_fcff * (ONE + g)
        terminal_value = terminal_fcff / (wacc - g)
        pv_terminal = terminal_value / ((ONE + wacc) ** len(rows))
        ev = pv_explicit + pv_terminal
        equity = ev - net_debt - minority_interest + non_operating_assets

    out = {
        'ok': True,
        'wacc': _s(wacc), 'terminal_growth': _s(g),
        'pv_explicit_fcff': _s(pv_explicit),
        'terminal_value': _s(terminal_value),
        'pv_terminal_value': _s(pv_terminal),
        'terminal_value_share': _s(pv_terminal / ev) if ev else '',
        'enterprise_value': _s(ev),
        'net_debt': _s(net_debt), 'minority_interest': _s(minority_interest),
        'non_operating_assets': _s(non_operating_assets),
        'equity_value': _s(equity),
        'warnings': list(warnings),
    }
    # 终值占比过高意味着结论几乎全由永续假设决定，必须显式警告
    if ev and pv_terminal / ev > Decimal('0.75'):
        out['warnings'].append(
            '终值占企业价值超过 75%，结论对永续假设高度敏感，必须展示敏感性矩阵。')
    sh = _d(shares)
    if sh and sh > 0:
        out['value_per_share'] = _s(equity / sh)
        out['shares'] = _s(sh)
    return out


def sensitivity(projection: dict, net_debt=ZERO, minority_interest=ZERO,
                non_operating_assets=ZERO,
                wacc_center=Decimal('0.10'), g_center=Decimal('0.03'),
                steps=2, wacc_step=Decimal('0.01'), g_step=Decimal('0.005')) -> dict:
    """WACC × 永续增长率 二维敏感性矩阵（竞赛明确要求 DCF 输出该矩阵）。"""
    wacc_center = _d(wacc_center, Decimal('0.10'))
    g_center = _d(g_center, Decimal('0.03'))
    steps = max(int(steps), 1)
    waccs = [wacc_center + wacc_step * i for i in range(-steps, steps + 1)]
    gs = [g_center + g_step * i for i in range(-steps, steps + 1)]
    matrix = []
    for g in gs:
        row = {'terminal_growth': _s(g), 'cells': []}
        for w in waccs:
            res = dcf(projection, w, g, net_debt, minority_interest, non_operating_assets)
            row['cells'].append({'wacc': _s(w), 'equity_value': res.get('equity_value'),
                                 'ok': res.get('ok', False)})
        matrix.append(row)
    return {'wacc_axis': [_s(w) for w in waccs], 'growth_axis': [_s(g) for g in gs],
            'matrix': matrix,
            'note': '每个单元格是按对应 WACC 与永续增长率重算的权益价值（元）。'}


# ---------------------------------------------------------------------------
# 假设校准：把驱动变量锚定到公司自己的历史数据上
#
# 为什么必须做这件事：DCF 的分歧 90% 不在折现公式，而在假设从哪来。
# 实测过的一份报告里，收入增速 8%、毛利率 30%、资本开支率 6%、税率 15%
# 全是教科书默认值，与被估值公司的实际情况（真实增速 15.34%、毛利率 60.53%、
# 资本开支率 12.33%、有效税率 24.14%）相差一倍以上，算出来的估值区间
# 看似专业，其实与这家公司无关。竞赛三大考核点第一条是"可验证"——
# 一个说不出依据的假设，天然不可验证。
#
# 因此这里的纪律是：**每一个驱动变量都要么来自已核验事实/指标，
# 要么显式标注为假设并写明理由**，绝不静默套默认值。
# ---------------------------------------------------------------------------

# 历史增速外推的封顶/保底值。单一年份的高增长不能直接当作五年期长期假设。
GROWTH_CAP = Decimal('0.20')
GROWTH_FLOOR = Decimal('-0.10')

# 证据层级优先级：主表原文 > 摘要页 > 坐标推断。
_TIER_ORDER = {'primary': 0, 'summary': 1, 'inferred': 2}


def _pct(value, digits=2):
    """把比例渲染成百分比文本，用于说明与告警。"""
    v = _d(value)
    if v is None:
        return '—'
    try:
        return f"{(_d(v) * 100).quantize(Decimal(1).scaleb(-digits))}%"
    except (InvalidOperation, ValueError):
        return _s(v)


class _FactPicker:
    """按 (期间, 口径, 证据层级) 优先级挑选已核验事实。

    优先级顺序有依据，不能随便改：
      ① 期间越新越好 —— 估值基数要用最新一期；
      ② 同期间优先**合并**口径 —— 母公司口径与合并口径绝不能混用，
         混用会让"少数股东权益"被重复计一次；
      ③ 同期间同口径优先 primary（主表原文）而不是 summary（摘要页四舍五入）
         —— 实测农夫山泉 2025 年营业收入在主表是 52,552,910,000、
         在摘要页是 52,553,000,000，差 90,000 元；选错层级会让
         "报告内可复算"的校验平白失败。
    """

    def __init__(self, facts):
        self.verified = [f for f in (facts or [])
                         if f.get('status') == 'verified' and f.get('value') not in (None, '')]

    def series(self, code, scope='consolidated'):
        """同一科目、同一口径的全部期间值，按期间升序；同期间取优先级最高的一条。"""
        hits = [f for f in self.verified if f.get('code') == code]
        scoped = [f for f in hits if f.get('scope') == scope]
        hits = scoped or hits
        best = {}
        for f in hits:
            key = str(f.get('period', ''))
            rank = (1 if f.get('scope') == 'consolidated' else 0,
                    -_TIER_ORDER.get(f.get('source_tier', 'primary'), 0))
            if key not in best or rank > best[key][0]:
                best[key] = (rank, f)
        return [(key, best[key][1]) for key in sorted(best)]

    def one(self, code, scope='consolidated', period=None, strict=False):
        """取指定科目的一条事实。strict=True 且给了 period 时，期间对不上就返回 None
        —— 宁可留缺口，也不能把不同期间的科目混在一起算比率。"""
        series = self.series(code, scope)
        if not series:
            return None
        if period is not None:
            for key, fact in series:
                if key == str(period):
                    return fact
            if strict:
                return None
        return series[-1][1]


class _MetricPicker:
    """从 analyze_financials 产出的指标里取最新一期、合并口径优先的一条。"""

    def __init__(self, metrics):
        self.metrics = [m for m in (metrics or []) if m.get('value') not in (None, '')]

    def one(self, code, scope='consolidated'):
        hits = [m for m in self.metrics if m.get('code') == code]
        scoped = [m for m in hits if m.get('scope') == scope]
        hits = scoped or hits
        if not hits:
            return None
        return sorted(hits, key=lambda m: (str(m.get('period', '')),
                                          1 if m.get('scope') == 'consolidated' else 0))[-1]


def calibrate_from_facts(facts, metrics, growth_cap=GROWTH_CAP) -> dict:
    """从已核验事实与指标里校准 DCF 驱动变量，并逐项记录来源。

    返回 {'params', 'provenance', 'notes', 'warnings', 'checks'}：
    - params     —— 可直接并入 DEFAULT_ASSUMPTIONS 的驱动变量
    - provenance —— 每个参数的来源（fact / metric / derived）、依据文本、
                    期间与证据 ID；报告里"假设表"直接引用它
    - checks     —— 自检项（目前是模型隐含 EBIT 与报表口径的差异）
    """
    fact = _FactPicker(facts)
    metric = _MetricPicker(metrics)
    params, provenance, notes, warnings, checks = {}, {}, [], [], []

    def put(key, value, source, detail, period=None, evidence_ids=None):
        if value is None:
            return False
        params[key] = value
        provenance[key] = {'source': source, 'detail': detail, 'period': period,
                           'evidence_ids': list(evidence_ids or [])}
        return True

    # ---- 收入基数与报告期 ----
    revenue = fact.one('revenue')
    period = str(revenue.get('period')) if revenue else None
    if revenue:
        put('revenue_base', _d(revenue['value']), 'fact',
            f"最新一期营业收入（{revenue.get('source_tier', 'primary')} 级证据，"
            f"{revenue.get('scope')} 口径）", period, revenue.get('evidence_ids'))

    # ---- 收入增速：近 N 年复合增速与最新同比**取较低者**，再封顶 ----
    series = fact.series('revenue')
    candidates = []
    if len(series) >= 3:
        first_p, first_f = series[0]
        last_p, last_f = series[-1]
        try:
            years = int(last_p[:4]) - int(first_p[:4])
        except (ValueError, TypeError):
            years = 0
        first_v, last_v = _d(first_f['value']), _d(last_f['value'])
        if years >= 2 and first_v and last_v and first_v > 0 and last_v > 0:
            with localcontext() as ctx:
                ctx.prec = 28
                cagr = (last_v / first_v) ** (ONE / Decimal(years)) - ONE
            candidates.append((cagr, f"{first_p[:4]}–{last_p[:4]} 年营业收入复合增速 {_pct(cagr)}"))
    yoy_metric = metric.one('revenue_yoy')
    if yoy_metric:
        yoy = _d(yoy_metric['value'])
        if yoy is not None:
            candidates.append((yoy, f"最新一期营业收入同比 {_pct(yoy)}"))
    if candidates:
        raw, _ = min(candidates, key=lambda item: item[0])
        capped = min(max(raw, GROWTH_FLOOR), growth_cap)
        detail = '；'.join(text for _, text in candidates) + '；取较低者'
        if capped != raw:
            detail += f"，并封顶至 {_pct(capped)}"
        put('revenue_growth', capped, 'derived',
            detail + '（不把单一年份的高增长外推为长期假设）', period)
    else:
        warnings.append('未取到连续两年以上的营业收入，收入增速沿用默认假设 '
                        f'{_pct(DEFAULT_ASSUMPTIONS["revenue_growth"])}，缺少历史依据，请人工设定。')

    # ---- 毛利率 ----
    gm = metric.one('gross_margin')
    if gm:
        put('gross_margin', _d(gm['value']), 'metric',
            'gross_margin 指标 =（营业收入 − 营业成本）/ 营业收入',
            gm.get('period'), gm.get('evidence_ids'))

    # ---- 期间费用率：用**真实费用科目**算，不用"毛利率 − 净利率"反推 ----
    #
    # 反推法（旧实现）等价于把 EBIT 当成净利润：opex = gm − net_margin
    # ⇒ EBIT = revenue × (gm − opex) = revenue × net_margin。
    # 于是 EBIT 里已经扣过一次所得税，再乘 (1 − 税率) 就是**重复扣税**。
    # 实测农夫山泉因此把 EBIT 从 209.8 亿元压到 158.7 亿元，低估 24%。
    expense_codes = ('selling_expenses', 'administrative_expenses',
                     'research_expenses', 'taxes_surcharges')
    parts = [fact.one(code, period=period, strict=True) for code in expense_codes]
    parts = [p for p in parts if p]
    expense_sum = sum((_d(p['value']) for p in parts), ZERO)
    if revenue and parts and expense_sum > 0:
        with localcontext() as ctx:
            ctx.prec = 28
            opex = expense_sum / _d(revenue['value'])
        put('opex_ratio', opex, 'derived',
            '（' + ' + '.join(p['code'] for p in parts) + '）/ revenue'
            '；不含其他收益、投资收益等贷方项目，EBIT 口径略偏保守',
            period, [e for p in parts for e in p.get('evidence_ids', [])])
    else:
        net_margin = metric.one('net_margin')
        if gm and net_margin:
            opex = max(Decimal('0.01'), _d(gm['value']) - _d(net_margin['value']))
            put('opex_ratio', opex, 'derived',
                'gross_margin − net_margin 反推（降级估计）', gm.get('period'))
            warnings.append('未取到销售/管理/研发/税金及附加明细，期间费用率由'
                            '「毛利率 − 净利率」反推；该口径下 EBIT ≈ 净利润，'
                            '会系统性低估息税前利润并造成重复扣税，请人工核对。')

    # ---- 有效税率：所得税费用 / 税前利润 ----
    pbt = fact.one('profit_before_tax', period=period, strict=True)
    tax = fact.one('income_tax', period=period, strict=True)
    if pbt and tax and (_d(pbt['value']) or ZERO) > 0:
        with localcontext() as ctx:
            ctx.prec = 28
            rate = _d(tax['value']) / _d(pbt['value'])
        if ZERO <= rate <= Decimal('0.5'):
            put('tax_rate', rate, 'derived', 'income_tax / profit_before_tax', period,
                list(pbt.get('evidence_ids', [])) + list(tax.get('evidence_ids', [])))
        else:
            warnings.append(f'计算出的有效税率 {_pct(rate)} 超出 [0%, 50%] 合理区间'
                            '（可能存在递延所得税或一次性项目），已沿用默认假设，请人工核对。')

    # ---- 折旧摊销率 ----
    da = metric.one('depreciation_total')
    if da and revenue and (_d(revenue['value']) or ZERO) > 0:
        with localcontext() as ctx:
            ctx.prec = 28
            put('da_to_revenue', _d(da['value']) / _d(revenue['value']), 'derived',
                'depreciation_total / revenue', da.get('period'), da.get('evidence_ids'))

    # ---- 资本开支率：务必取绝对值 ----
    # 港股/IFRS 现金流量表里"购买物业厂房及设备"是投资流出、原文以括号列报、
    # 入库后带负号。直接除会得到负的资本开支率，FCFF 被系统性高估。
    capex = fact.one('capex', period=period, strict=True) or fact.one('capex')
    if capex and revenue and (_d(revenue['value']) or ZERO) > 0:
        with localcontext() as ctx:
            ctx.prec = 28
            put('capex_to_revenue', abs(_d(capex['value'])) / _d(revenue['value']), 'derived',
                'abs(capex) / revenue', capex.get('period'), capex.get('evidence_ids'))

    # ---- 周转天数（直接取指标，不重算） ----
    for key, code in (('receivable_days', 'receivables_days'),
                      ('inventory_days', 'inventory_days'),
                      ('payable_days', 'payables_days')):
        m = metric.one(code)
        if m:
            put(key, _d(m['value']), 'metric', f'{code} 指标（报告期天数口径）', m.get('period'))

    # ---- 有息负债 / 类现金 / 净债务 ----
    #
    # 必须优先复用 financial_analysis 已算好的指标：那里的取值阶梯
    # （总括行优先、明细回退）与金额去重是净现金口径的唯一权威实现。
    # 估值侧再自己拼一遍必然出现两套数字 —— 实测旧实现只取
    # short_term_debt − cash，漏掉租赁负债 1.03 亿元与长期银行存款 110.88 亿元，
    # 净债务差 109.85 亿元，权益价值等额低估。
    debt_metric = metric.one('interest_bearing_debt')
    cash_metric = metric.one('cash_and_deposits')
    net_cash_metric = metric.one('net_cash')
    netted_deposits = False
    if debt_metric:
        put('gross_debt', _d(debt_metric['value']), 'metric',
            'interest_bearing_debt 指标（计息借贷 + 租赁负债，已按金额去重）',
            debt_metric.get('period'), debt_metric.get('evidence_ids'))
    if cash_metric:
        put('cash_like', _d(cash_metric['value']), 'metric',
            'cash_and_deposits 指标（货币资金 + 长期银行存款）',
            cash_metric.get('period'), cash_metric.get('evidence_ids'))
        netted_deposits = True
    if net_cash_metric:
        put('net_debt', -_d(net_cash_metric['value']), 'derived',
            '−net_cash 指标 = 有息负债 − 类现金（长期银行存款已在类现金中扣除）',
            net_cash_metric.get('period'), net_cash_metric.get('evidence_ids'))
        provenance['net_debt']['derived_from'] = ['interest_bearing_debt', 'cash_and_deposits']
        provenance['net_debt']['components'] = {
            'interest_bearing_debt': _s(params.get('gross_debt')),
            'cash_and_deposits': _s(params.get('cash_like')),
        }
        netted_deposits = True
    elif debt_metric and cash_metric:
        put('net_debt', _d(debt_metric['value']) - _d(cash_metric['value']), 'derived',
            'interest_bearing_debt − cash_and_deposits', debt_metric.get('period'))
        provenance['net_debt']['derived_from'] = ['interest_bearing_debt', 'cash_and_deposits']
        provenance['net_debt']['components'] = {
            'interest_bearing_debt': _s(debt_metric['value']),
            'cash_and_deposits': _s(cash_metric['value']),
        }
    else:
        # 指标不可用时的降级口径。口径必须与 financial_analysis 的
        # interest_bearing_debt 指标一致（总括行优先、明细回退），
        # 否则同一个科目在不同路径下会得到两个数 —— 这是最容易骗过评审的错误。
        # 额外加一道对账：明细之和显著大于总括行时，说明总括行漏了口径
        # （例如「計息銀行借貸」不含应付债券），此时改用明细并告警。
        total_debt = fact.one('borrowings')
        details = [f for f in (fact.one('short_term_debt'), fact.one('long_term_debt'),
                               fact.one('bonds_payable')) if f]
        debt_facts = []
        if total_debt:
            debt_facts = [total_debt]
            detail_sum = sum((_d(f['value']) for f in details), ZERO)
            total_v = _d(total_debt['value'])
            if details and detail_sum > total_v * Decimal('1.02'):
                debt_facts = details
                warnings.append(
                    f"「{total_debt.get('raw_label') or 'borrowings'}」"
                    f"（{_s(total_v)}）小于长短借款明细之和（{_s(detail_sum)}），"
                    '已改用明细，请核对总括行是否遗漏了债券或其他借款。')
        else:
            debt_facts = details
        lease_total = fact.one('lease_liabilities')
        if lease_total:
            debt_facts.append(lease_total)
        else:
            debt_facts += [f for f in (fact.one('lease_liabilities_current'),
                                       fact.one('lease_liabilities_noncurrent')) if f]
        cash_fact = fact.one('cash')
        if debt_facts or cash_fact:
            debt_v = sum((_d(f['value']) for f in debt_facts), ZERO)
            cash_v = _d(cash_fact['value']) if cash_fact else ZERO
            anchor = debt_facts[0] if debt_facts else cash_fact
            put('net_debt', debt_v - cash_v, 'fact',
                (' + '.join(f['code'] for f in debt_facts) or '0') + ' − cash'
                '（interest_bearing_debt 指标不可用时的降级口径）', anchor.get('period'))
            entry = provenance['net_debt']
            entry['derived_from'] = [f['code'] for f in debt_facts] + (['cash'] if cash_fact else [])
            entry['components'] = {f['code']: _s(f['value'])
                                   for f in debt_facts + ([cash_fact] if cash_fact else [])}
            if debt_facts:
                warnings.append('未取到 interest_bearing_debt 指标，'
                                '净债务由科目事实直接拼装，可能漏计部分债务口径。')

    # ---- 少数股东权益 ----
    minority = fact.one('minority_equity')
    if minority:
        put('minority_interest', _d(minority['value']), 'fact',
            '少数股东权益（时点值）', minority.get('period'), minority.get('evidence_ids'))

    # ---- 非经营性资产 ----
    # 长期银行存款若已在 net_cash 里净掉，这里绝不能再加一次，否则重复计一次
    # 110 亿元级别的资产，权益价值凭空高出 30%+。
    non_op_codes = ['financial_assets_fvtpl']
    if not netted_deposits:
        non_op_codes.append('long_term_deposits')
    non_op = [f for f in (fact.one(code) for code in non_op_codes) if f]
    if non_op:
        put('non_operating_assets', sum((_d(f['value']) for f in non_op), ZERO), 'fact',
            ' + '.join(f['code'] for f in non_op)
            + '；不产生经营现金流但属于股东，须加回权益价值', non_op[0].get('period'),
            [e for f in non_op for e in f.get('evidence_ids', [])])

    # ---- 总股本：归母净利润 / 基本每股收益 ----
    parent_profit = fact.one('parent_net_profit') or fact.one('net_profit')
    eps = fact.one('basic_eps')
    if parent_profit and eps and (_d(eps['value']) or ZERO) > 0:
        with localcontext() as ctx:
            ctx.prec = 28
            shares = _d(parent_profit['value']) / _d(eps['value'])
        put('shares', shares, 'derived',
            'parent_net_profit / basic_eps（近似总股本）', parent_profit.get('period'))
        notes.append('总股本由「归属于母公司股东的净利润 ÷ 基本每股收益」推导，属 E2 级派生值；'
                     '存在股本变动或稀释性工具时，应以年报披露的加权平均股数为准。')

    # ---- 倍数交叉验证用的基数 ----
    ebitda = metric.one('ebitda')
    if ebitda:
        put('ebitda_base', _d(ebitda['value']), 'metric',
            'ebitda 指标 = 除税前溢利 + 财务费用 + 折旧摊销',
            ebitda.get('period'), ebitda.get('evidence_ids'))
    if parent_profit:
        put('parent_net_profit_base', _d(parent_profit['value']), 'fact',
            '最新一期归属于母公司股东的净利润',
            parent_profit.get('period'), parent_profit.get('evidence_ids'))

    # ---- 自检：模型隐含 EBIT 与报表口径的差异 ----
    #
    # 这一条把"假设"变成"可验证"：模型隐含 EBIT 与
    # 「除税前溢利 + 财务费用」直接对比，差异过大就告警。
    # 差异主要来自其他收益、投资收益、减值等项目没进期间费用率 ——
    # 这是本模型的已知简化，写明比藏着好。
    if revenue and 'opex_ratio' in params and 'gross_margin' in params:
        implied_ebit = _d(revenue['value']) * (_d(params['gross_margin']) - _d(params['opex_ratio']))
        pbt_fact = fact.one('profit_before_tax', period=period, strict=True)
        fin_fact = fact.one('financial_expenses', period=period, strict=True)
        if pbt_fact and implied_ebit:
            reported_ebit = _d(pbt_fact['value']) + (_d(fin_fact['value']) if fin_fact else ZERO)
            if reported_ebit and reported_ebit > 0:
                with localcontext() as ctx:
                    ctx.prec = 28
                    gap = (implied_ebit - reported_ebit) / reported_ebit
                checks.append({
                    # name/ok/detail 三个字段与其他校验条目对齐，便于统一消费；
                    # code/status 保留原有语义（下游按 status 判断是否"需要关注"）。
                    # 混用两种结构会让调用方必须写两套解析逻辑，实测已经踩到过
                    # 一个直接取 c['name'] 的消费方在遇到本条目时 KeyError。
                    'name': 'ebit_calibration',
                    'ok': abs(gap) <= Decimal('0.15'),
                    'detail': f'模型隐含 EBIT {_s(implied_ebit)} 元 vs 报表口径 '
                              f'{_s(reported_ebit)} 元，差异 {_pct(gap)}。',
                    'code': 'ebit_calibration',
                    'status': 'pass' if abs(gap) <= Decimal('0.15') else 'attention',
                    'implied_ebit': _s(implied_ebit), 'reported_ebit': _s(reported_ebit),
                    'difference_ratio': _s(gap),
                    'formula': 'implied_ebit = revenue × (gross_margin − opex_ratio); '
                               'reported_ebit = profit_before_tax + financial_expenses',
                    'note': '模型隐含 EBIT 与「除税前溢利 + 财务费用」的差异，'
                            '主要来自其他收益、投资收益、减值等项目未纳入期间费用率。',
                })
                if abs(gap) > Decimal('0.15'):
                    warnings.append(f'模型隐含 EBIT 与报表口径相差 {_pct(gap)}，'
                                    '超过 15% 经验阈值；期间费用率可能遗漏了重要的损益项目，'
                                    '请对照利润表人工核对。')

    return {'params': params, 'provenance': provenance,
            'notes': notes, 'warnings': warnings, 'checks': checks}


def multiples_check(valuation: dict, ebitda=None, parent_net_profit=None, shares=None,
                    comparable_ev_ebitda=None, comparable_pe=None) -> dict:
    """用倍数法给 DCF 做交叉验证。

    竞赛要求 DCF 之外还要有可比公司估值。但"可比公司名单"依赖外部市场数据，
    本项目拿不到可靠的同业倍数，所以这里分两层：
      ① 自检（永远可用）—— 算 DCF 隐含的 EV/EBITDA 与 P/E，落在常识区间之外就告警。
         这一步能抓住绝大多数"模型算错但看起来正常"的情况。
      ② 反推（需要调用方给可比倍数）—— 用 EV/EBITDA 与 P/E 反推隐含权益价值，
         与 DCF 结果并列，形成双方法交叉验证。
    """
    ev = _d(valuation.get('enterprise_value'))
    equity = _d(valuation.get('equity_value'))
    out = {'ok': False, 'warnings': []}
    if ev is None or equity is None:
        out['warnings'].append('缺少企业价值或权益价值，无法做倍数交叉验证。')
        return out

    net_debt = _d(valuation.get('net_debt'), ZERO)
    non_op = _d(valuation.get('non_operating_assets'), ZERO)
    eb = _d(ebitda)
    profit = _d(parent_net_profit)
    share_count = _d(shares)

    with localcontext() as ctx:
        ctx.prec = 28
        if eb and eb > 0:
            implied_ev_ebitda = ev / eb
            out['implied_ev_ebitda'] = _s(implied_ev_ebitda)
            # 消费/食品饮料类公司的 EV/EBITDA 常见区间约 8–30 倍。
            # 超出 [3, 40] 基本可以判定为模型输入有误，而不是"公司特别便宜/贵"。
            if not (Decimal('3') <= implied_ev_ebitda <= Decimal('40')):
                out['warnings'].append(
                    f'DCF 隐含 EV/EBITDA 为 {_s(implied_ev_ebitda)} 倍，'
                    '落在 [3, 40] 常识区间之外，请先检查收入、利润率与净债务输入。')
        if profit and profit > 0:
            implied_pe = equity / profit
            out['implied_pe'] = _s(implied_pe)
            if not (Decimal('5') <= implied_pe <= Decimal('60')):
                out['warnings'].append(
                    f'DCF 隐含 P/E 为 {_s(implied_pe)} 倍，落在 [5, 60] 常识区间之外，'
                    '请先检查净利润口径与权益价值桥接。')

        # ---- 可比倍数反推 ----
        comparables = {}
        multiple_ev = _d(comparable_ev_ebitda)
        if multiple_ev and multiple_ev > 0 and eb and eb > 0:
            implied_equity = multiple_ev * eb - net_debt + non_op
            comparables['ev_ebitda'] = {
                'multiple': _s(multiple_ev), 'basis': _s(eb),
                'enterprise_value': _s(multiple_ev * eb),
                'equity_value': _s(implied_equity),
                'value_per_share': (_s(implied_equity / share_count)
                                    if share_count and share_count > 0 else ''),
            }
        multiple_pe = _d(comparable_pe)
        if multiple_pe and multiple_pe > 0 and profit and profit > 0:
            implied_equity = multiple_pe * profit
            comparables['pe'] = {
                'multiple': _s(multiple_pe), 'basis': _s(profit),
                'equity_value': _s(implied_equity),
                'value_per_share': (_s(implied_equity / share_count)
                                    if share_count and share_count > 0 else ''),
            }
        if comparables:
            out['comparables'] = comparables
            values = [_d(c['equity_value']) for c in comparables.values()]
            values = sorted(v for v in values if v is not None)
            if values:
                out['comparable_range'] = {
                    'low': _s(values[0]), 'high': _s(values[-1]),
                    'text': f"可比倍数法隐含权益价值 {_s(values[0])} – {_s(values[-1])} 元",
                }
                if equity and values[0] > 0:
                    with localcontext() as ctx:
                        ctx.prec = 28
                        out['dcf_vs_comparable_gap'] = _s((equity - values[0]) / values[0])
        else:
            out['warnings'].append(
                '未提供可比公司倍数（可在 overrides 里给 comparable_ev_ebitda / comparable_pe），'
                '本次只做了隐含倍数自检，未做双方法交叉验证。')

    out['ok'] = bool(out.get('implied_ev_ebitda') or out.get('implied_pe')
                     or out.get('comparables'))
    out['disclaimer'] = ('隐含倍数是 DCF 结果的换算，不是市场报价；'
                         '可比倍数须由使用者提供并说明可比组选取依据。')
    return out


def _normalize_params(params: dict) -> dict:
    """把数值型驱动变量统一成 Decimal。

    调用方既可能传字符串（来自 JSON 请求 / 前端表单），也可能传 Decimal。
    原样带下去会变成"有的参数是 str、有的是 Decimal"的混合字典，
    比较、排序、序列化都容易出意外（例如 '0.06' != Decimal('0.06')）。
    非数值（如 shares=None）原样保留。
    """
    out = {}
    for key, value in params.items():
        if value is None or isinstance(value, bool):
            out[key] = value
            continue
        converted = _d(value)
        out[key] = converted if converted is not None else value
    return out


def risks_to_parameters(base_params: dict, findings: list, scenario: str) -> dict:
    """把法证发现映射成压力情景的参数改动，并逐条记录传导来源。

    这是"风险联动估值"的落点：压力情景不是手填的悲观数字，而是从证据链推出的。
    """
    # 允许被直接调用（不经 run_scenarios）：缺失的驱动变量用默认假设补齐。
    # 否则只要调用方少给一个参数，整个情景就 KeyError 崩掉 —— "某个参数没配"
    # 应该是"用默认值并在假设表里标注"，而不是让压力测试跑不出来。
    params = _normalize_params({**DEFAULT_ASSUMPTIONS, **(base_params or {})})
    applied = []
    if scenario != 'stress':
        # 乐观情景只动需求与效率，且不同时把所有参数调到最好
        if scenario == 'optimistic':
            params['revenue_growth'] = _d(params['revenue_growth']) + Decimal('0.04')
            params['gross_margin'] = _d(params['gross_margin']) + Decimal('0.01')
            params['receivable_days'] = _d(params['receivable_days']) * Decimal('0.90')
            applied.append({'source': '情景假设', 'parameter': 'revenue_growth / gross_margin / receivable_days',
                            'adjustment': '收入增速 +4pp、毛利率 +1pp、应收天数 -10%',
                            'path': '需求与效率改善，但不叠加所有最优假设'})
        return {'parameters': params, 'applied': applied, 'unmatched_risks': []}

    unmatched = []
    touched = set()

    # ---- 第一层：结构性压力下限（与法证发现无关） ----
    # 先只**算出目标值**、不动参数，等证据驱动的传导跑完后再逐参数取"更严重者"。
    #
    # 为什么不直接相加：结构性下限描述的是"行业普遍的下行"，证据传导描述的是
    # "这家公司特有的风险"。同一个参数上两者相加是双重计算 —— 一条
    # 「毛利率将下滑」的发现叠加 −2pp 的行业下限，会得到 −4pp 的人为悲观值。
    # 取更严重者（下行参数取小、上行参数取大）既保证下限一定生效，
    # 又不会凭空放大：证据已经更悲观时，下限自动让位。
    floor_targets = {}
    for key, (delta, mode, description) in STRESS_FLOOR.items():
        if key not in params or params[key] is None:
            continue                      # 该参数不在本次建模口径内（如无有息负债）
        base_v = _d(params[key])
        if base_v is None:
            continue
        candidate = base_v * (ONE + delta) if mode == 'relative' else base_v + delta
        floor_targets[key] = (base_v, _clamp_stress_param(key, candidate), description)

    for f in findings or []:
        rule_id = f.get('rule_id', '')
        mapping = RISK_TO_PARAM.get(rule_id)
        if not mapping:
            unmatched.append({'rule_id': rule_id, 'reason': '该规则未定义估值传导路径，需人工决定是否入模。'})
            continue
        weight = SEVERITY_WEIGHT.get(f.get('severity', 'medium'), Decimal('0.4'))
        delta = mapping['step'] * weight
        for p in mapping['params']:
            if p == 'data_confidence':
                continue
            if p not in params or params[p] is None:
                unmatched.append({'rule_id': rule_id,
                                  'reason': f'参数 {p} 缺失，无法传导，已在报告中列为待补假设。'})
                continue
            base_v = _d(params[p])
            if p == 'terminal_growth':
                params[p] = base_v - delta if mapping['direction'] > 0 else base_v + delta
                params[p] = max(params[p], Decimal('0'))
            else:
                # 规模基准：比例类参数（毛利率、费用率、周转天数）一律用**绝对值**做基准，
                # 于是 step 对它们等价于绝对步长（0.02 = 2 个百分点）。
                #
                # 用 abs 而不是原值，是为了修一个实测踩到的坑：净现金公司
                # （net_debt = −177.73 亿元）遇到 R_DEBT_01「短债缺口」时，
                # 原写法 max(base_v, 1) 把负的基准钳到 1，调整量变成
                # 0.05 × 0.7 × 1 = 0.035 **元** —— 压力情景里"债务风险"完全没进模型，
                # 三情景数值看起来一模一样，而报告上看不出任何异常。
                scale = abs(base_v)
                if p == 'net_debt':
                    # 净债务还有第二个坑：即使取绝对值，用"净现金"的规模做基准也不对 ——
                    # 债务风险的量纲是有息负债，不是净额。优先用有息负债总额做基准，
                    # 取不到才退回绝对值。
                    scale = abs(_d(params.get('gross_debt')) or ZERO) or scale
                shift = abs(delta * max(scale, ONE))
                params[p] = base_v + shift if mapping['direction'] > 0 else base_v - shift
            if p in ('gross_margin', 'opex_ratio'):
                params[p] = max(params[p], Decimal('0.01'))
            elif p in ('receivable_days', 'inventory_days', 'payable_days'):
                # 周转天数不能被调成负数（否则营运资本占用变成"释放现金"，
                # 压力情景反而比基准情景更乐观）。
                params[p] = max(params[p], ZERO)
            elif p in ('impairment_to_revenue', 'near_term_fcff_haircut',
                       'sustainable_profit_discount'):
                # 这三个是比例类折让/损失，夹到 [0, 0.9]：
                # 超过 90% 的折让在语义上已经不是"压力情景"而是"公司没了"。
                params[p] = min(max(params[p], ZERO), Decimal('0.9'))
            touched.add(p)
            applied.append({
                'source': f"{rule_id} {f.get('title', '')}",
                'severity': f.get('severity'),
                'risk_score': f.get('risk_score'),
                'parameter': p,
                'from': _s(base_v), 'to': _s(params[p]),
                'path': mapping['path'],
            })
    # ---- 第二层：证据优先，结构性兜底只填证据没覆盖的参数 ----
    #
    # 为什么不是相加：结构性兜底描述的是"行业普遍的下行"，证据传导描述的是
    # "这家公司特有的风险"。同一个参数上两者相加是双重计算 —— 一条
    # 「毛利率将下滑」的发现叠加 −2pp 的行业兜底，会得到 −4pp 的人为悲观值。
    #
    # 为什么也不是"取更严重者"：那样温和的证据会被兜底完全吸收。实测过：
    # 一条把毛利率从 40% 压到 39.2% 的发现，撞上 −2pp 的兜底后**一点作用都没有**，
    # 消融实验测出的"证据增量"是 0 —— 等于又把证据静默丢掉了。
    #
    # 因此取"证据优先"：证据动过的参数由证据决定，兜底让位；
    # 证据没覆盖的参数才由兜底填补。既无双重计算，也不吞掉证据。
    floor_applied = []
    evidence_touched = {a.get('parameter') for a in applied}
    for key, (base_v, candidate, description) in floor_targets.items():
        if key in evidence_touched:
            floor_applied.append({
                'source': STRESS_FLOOR_SOURCE, 'parameter': key,
                'base': _s(base_v), 'floor_target': _s(candidate),
                'evidence_value': _s(params.get(key)), 'applied_value': _s(params.get(key)),
                'binding': False,
                'note': '该参数已由证据驱动的传导决定，结构性兜底让位，避免双重计算。',
            })
            continue
        params[key] = candidate
        touched.add(key)
        floor_applied.append({
            'source': STRESS_FLOOR_SOURCE, 'parameter': key,
            'base': _s(base_v), 'floor_target': _s(candidate),
            'evidence_value': None, 'applied_value': _s(candidate),
            'binding': True, 'path': description,
            'note': description,
        })
    applied = floor_applied + applied

    evidence_driven = [a for a in applied if a.get('source') != STRESS_FLOOR_SOURCE]
    return {'parameters': params, 'applied': applied, 'unmatched_risks': unmatched,
            'touched_parameters': sorted(touched),
            'floor_applied': floor_applied,
            'binding_floor_parameters': sorted(a['parameter'] for a in floor_applied if a['binding']),
            'evidence_driven_adjustments': len(evidence_driven),
            # 证据驱动部分为空时明确标记降级：此时压力情景完全由情景假设构成，
            # 报告必须说明"本轮扫描未产出可传导的发现"，而不是让读者以为
            # 压力情景是从证据推出来的。
            'degraded_to_structural_only': not evidence_driven}


def run_scenarios(base_params: dict, findings: list | None = None) -> dict:
    """跑基准 / 乐观 / 压力三情景，返回各自的预测、估值与敏感性。"""
    findings = findings or []
    base_params = {**DEFAULT_ASSUMPTIONS, **(base_params or {})}
    out = {}
    for name in ('base', 'optimistic', 'stress'):
        mapped = risks_to_parameters(base_params, findings, name)
        params = mapped['parameters']
        proj = project(params)
        val = dcf(proj, params.get('wacc'), params.get('terminal_growth'),
                  params.get('net_debt'), params.get('minority_interest'),
                  params.get('non_operating_assets'), params.get('shares'))
        sens = sensitivity(proj, params.get('net_debt'), params.get('minority_interest'),
                           params.get('non_operating_assets'),
                           _d(params.get('wacc')), _d(params.get('terminal_growth')))
        val['multiples'] = multiples_check(
            val, ebitda=params.get('ebitda_base'),
            parent_net_profit=params.get('parent_net_profit_base'),
            shares=params.get('shares'),
            comparable_ev_ebitda=params.get('comparable_ev_ebitda'),
            comparable_pe=params.get('comparable_pe'))
        out[name] = {
            'label': SCENARIOS[name]['label'],
            'note': SCENARIOS[name]['note'],
            'parameters': {k: _s(v) for k, v in params.items() if k != 'shares'},
            'projection': [{'year': r['year'], 'revenue': _s(r['revenue']), 'ebit': _s(r['ebit']),
                            'fcff': _s(r['fcff'])} for r in proj['rows']],
            'valuation': val,
            'sensitivity': sens,
            'risk_adjustments': mapped['applied'],
            'unmatched_risks': mapped['unmatched_risks'],
            'floor_applied': mapped.get('floor_applied') or [],
            'evidence_driven_adjustments': mapped.get('evidence_driven_adjustments'),
            'degraded_to_structural_only': mapped.get('degraded_to_structural_only'),
        }

    values = [out[k]['valuation'].get('equity_value') for k in out if out[k]['valuation'].get('ok')]
    numeric = sorted(_d(v) for v in values if v)
    out['range'] = {
        'equity_value_low': _s(numeric[0]) if numeric else '',
        'equity_value_high': _s(numeric[-1]) if numeric else '',
        'equity_value_mid': _s(numeric[len(numeric) // 2]) if numeric else '',
        'text': (f"三情景权益价值区间 {_s(numeric[0])} – {_s(numeric[-1])} 元"
                 if len(numeric) >= 2 else '情景不足，无法给出区间。'),
        'disclaimer': '这是按情景分布的合理价值区间，不是目标价，也不构成投资建议。',
    }
    shares = _d(base_params.get('shares'))
    if numeric and shares and shares > 0:
        per_share = sorted(v / shares for v in numeric)
        out['range']['value_per_share_low'] = _s(per_share[0])
        out['range']['value_per_share_high'] = _s(per_share[-1])
        out['range']['value_per_share_text'] = (
            f"三情景每股价值区间 {_s(per_share[0])} – {_s(per_share[-1])} 元/股")

    # ---- 压力情景是否真的构成压力 ----
    #
    # 这一条必须显式报出来。实测踩过：某次运行只命中 1 条低严重度规则，
    # 压力情景与基准情景的权益价值只差 0.5%，但报告照常展示"三情景压力测试"，
    # 读者会误以为风险已经充分量化。差异过小要说明原因，而不是默认它是正常的。
    base_eq = _d((out.get('base') or {}).get('valuation', {}).get('equity_value'))
    stress_eq = _d((out.get('stress') or {}).get('valuation', {}).get('equity_value'))
    spread = None
    if base_eq and stress_eq and base_eq != 0:
        with localcontext() as ctx:
            ctx.prec = 28
            spread = (stress_eq - base_eq) / base_eq
    stress_used = (out.get('stress') or {}).get('risk_adjustments') or []
    floor_used = (out.get('stress') or {}).get('floor_applied') or []
    evidence_used = [item for item in stress_used if item.get('source') != STRESS_FLOOR_SOURCE]
    out['stress_diagnostic'] = {
        'adjustment_count': len(stress_used),
        'adjustments': [f"{item.get('parameter')}: {item.get('from')} → {item.get('to')}"
                        for item in stress_used],
        # 压力情景是两层叠加的，必须分开报：
        #   floor      —— 结构性情景假设（与法证发现无关，永远施加）
        #   evidence   —— 由法证发现传导而来
        # 只报总数会让"哪些压力有证据支持"这个关键判断丢失。
        'structural_floor_adjustments': len(floor_used),
        'evidence_driven_adjustments': len(evidence_used),
        'evidence_backed': bool(evidence_used),
        'equity_value_gap_vs_base': _s(spread) if spread is not None else '',
        'text': (f"压力情景相对基准情景变动 {_pct(spread)}" if spread is not None
                 else '无法比较压力情景与基准情景。'),
    }
    if not evidence_used:
        out['stress_diagnostic']['evidence_note'] = (
            '本轮扫描没有产出可传导的法证发现，压力情景完全由结构性情景假设构成'
            '（增速 −6pp、毛利率 −2pp、回款天数 +15 天等）。'
            '报告必须说明这一点：该压力情景检验的是"假设恶化时结论是否稳健"，'
            '而不是"已识别的具体风险被量化"。')
    if spread is not None and abs(spread) < Decimal('0.02'):
        out['stress_diagnostic']['insufficient'] = True
        out['stress_diagnostic']['reason'] = (
            '压力情景与基准情景的权益价值差异不足 2%，本次压力测试没有构成实质压力。'
            f'结构性下限施加了 {len(floor_used)} 项调整、证据传导施加了 {len(evidence_used)} 项；'
            '出现这种情况说明基础假设本身已接近压力水平，'
            '报告应改用更极端的下行情景，而不是宣称"风险已充分量化"。')
    else:
        out['stress_diagnostic']['insufficient'] = False
    out['ok'] = bool(numeric)
    return out


def from_metrics(metrics: list, findings: list | None = None, overrides: dict | None = None,
                 facts: list | None = None) -> dict:
    """从 analyze_financials 产出的指标与事实里校准建模起点，再跑三情景。

    为什么必须同时接收 `facts`：`metrics` 里只有"算出来的"比率与天数，
    收入、货币资金、借款这些**水平值**是事实、不在 metrics 里。
    只读 metrics 的话 revenue_base 永远取不到，估值分支必然以"缺关键输入"收场
    （实测踩过：三情景全部 ok=False）。缺什么就报什么，不用默认值偷偷填平。

    假设的优先级（高到低）：overrides（人工指定） > 校准值（来自事实/指标）
    > DEFAULT_ASSUMPTIONS（教科书默认，一律标注为假设）。

    返回里带 `data_quality`：逐项说明每个驱动变量到底是"取自公司数据"
    还是"沿用默认假设"。这是"可验证"的具体落实 —— 评审能一眼看出
    哪些数字有依据、哪些是拍的。
    """
    calibration = calibrate_from_facts(facts, metrics)
    params = dict(DEFAULT_ASSUMPTIONS)
    params.update(calibration['params'])

    overrides = overrides or {}
    for key, value in overrides.items():
        converted = _d(value)
        params[key] = converted if converted is not None else value

    params = _normalize_params(params)

    # ---- 关键输入检查 ----
    # revenue_base 缺失 → 整个模型不成立，直接判失败。
    blocking = [k for k in ('revenue_base',) if not params.get(k)]
    # 其余为"降级输入"：模型能跑，但结果可信度下降，必须显式列出。
    degradations = []
    for key, label in (
            ('net_debt', '净债务（有息负债 − 类现金）'),
            ('minority_interest', '少数股东权益'),
            ('non_operating_assets', '非经营性资产'),
            ('payable_days', '应付账款周转天数'),
            ('ebitda_base', 'EBITDA'),
            ('parent_net_profit_base', '归母净利润'),
            ('shares', '总股本')):
        if key in overrides:
            continue
        if key not in calibration['params']:
            degradations.append({'parameter': key, 'label': label,
                                 'impact': '沿用默认假设或保持为空，结果可信度下降。'})

    result = run_scenarios(params, findings)
    result['inputs_used'] = calibration['provenance']
    result['assumption_sources'] = {
        key: calibration['provenance'][key]['source']
        for key in calibration['provenance']
    }
    result['missing_inputs'] = blocking
    result['degraded_inputs'] = degradations
    result['calibration_notes'] = calibration['notes']
    result['calibration_warnings'] = calibration['warnings']
    result['calibration_checks'] = calibration['checks']
    result['data_quality'] = {
        'from_company_data': sorted(calibration['provenance']),
        'from_default_assumptions': sorted(
            key for key in DEFAULT_ASSUMPTIONS
            if key not in calibration['provenance'] and key not in overrides
            and DEFAULT_ASSUMPTIONS[key] is not None),
        'overridden_by_user': sorted(overrides),
        'blocking_missing': blocking,
        'degraded_count': len(degradations),
        'text': (f"驱动变量中 {len(calibration['provenance'])} 项取自公司已核验数据，"
                 f"{len(degradations)} 项降级或沿用默认假设，"
                 f"{len(overrides)} 项由使用者覆盖。"),
    }
    if blocking:
        result['note'] = ('缺少营业收入基数等关键输入，情景结果不成立；'
                          '请先运行 analyze_financial_report 取得已核验事实。')
    return result
