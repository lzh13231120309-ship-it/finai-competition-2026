"""WACC 的财报侧校准：把折现率从"硬编码 10%"变成"能算出多少就算多少、算不出的明说"。

为什么必须做这件事
------------------
`valuation_advanced.derive_discount_rate` 在拿不到完整市场输入（无风险利率、
股权风险溢价、β、债务成本、债务权重全齐且有来源与日期）时，直接返回
`method='default_assumption'`，WACC 沿用 `DEFAULT_ASSUMPTIONS['wacc'] = 0.10`。

问题在于：**这五项里有三项其实能从年报直接算出来**。一家净现金、几乎不借钱、
ROE 44% 的消费品公司，和一家高杠杆制造业，共用同一个 10% 折现率，
是对公司基本面的无视。竞赛的"可验证"要求恰恰指向这里 —— 一个说不出依据的
折现率，天然不可验证。

本模块把折现率的每一项拆成三种来源，逐项标注：

    E1 财报实算   利息支出/有息负债、有息负债/权益、存款收益率 —— 本模块直接算
    E2 派生       加杠杆后的 β（Hamada）、税后债务成本
    E3 外部参数   无风险利率、股权风险溢价、行业去杠杆 β —— 必须由使用者提供或核验
    E4 默认假设   取不到时的兜底值，一律显式标注，不冒充测算结果

**最重要的产出不是那个 WACC 数字，而是"哪个参数真正重要"的判断。**
净现金公司的债务权重可能只有 1%，此时债务成本从 3% 调到 5% 对 WACC 的影响
不到 0.02 个百分点 —— 与其花力气去找一个"精确"的债务成本，不如把这份力气
花在 β 和股权风险溢价上。本模块用敏感度归因把这个判断量化出来。

口径陷阱（实测踩过的）
----------------------
1. **债务成本不能用"财务费用 ÷ 有息负债"直接算。** 港股/IFRS 报表里
   财务费用常是**净额**（已抵减利息收入）。农夫山泉 2025 年财务费用 6,690 万元、
   利息收入 5.80 亿元、有息负债 44.93 亿元 —— 直接相除得 1.62%，
   把存款利息收入当成了借款成本；把利息收入加回去又得 15.69%，
   因为那 5.8 亿元是 222 亿元银行存款赚的，跟 44.93 亿元借款无关。
   本模块的做法：给出 [下界, 上界] 区间，再乘以债务权重，**说明它对结论是否有影响**。
2. **国别风险溢价可能与股权风险溢价重复计算。** 若使用者给的是"中国市场 ERP"，
   再加国别溢价就是加了两遍。本模块用 `erp_basis` 显式区分，默认按"成熟市场 ERP +
   国别溢价"处理，并要求使用者确认。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext

D = Decimal
ZERO = D(0)
ONE = D(1)

# 行业去杠杆 β（unlevered beta）。来源：Damodaran 行业 beta 数据集
# （pages.stern.nyu.edu/~adamodar/），取整到两位小数。
# 这是**外部参数**，不是从财报算出来的；标注出来是为了让使用者能替换成
# 自己的可比公司估计值。找不到行业时用 'default'。
INDUSTRY_UNLEVERED_BETA: dict[str, Decimal] = {
    '食品饮料': D('0.55'), '软饮料': D('0.55'), '饮料': D('0.55'), '白酒': D('0.60'),
    '乳制品': D('0.50'), '食品': D('0.60'), '农业': D('0.70'), '农林牧渔': D('0.70'),
    '医药': D('0.75'), '生物制药': D('0.80'), '医疗器械': D('0.85'),
    '银行': D('0.35'), '保险': D('0.60'), '证券': D('0.80'),
    '房地产': D('0.60'), '建筑': D('0.80'), '建材': D('0.85'),
    '零售': D('0.70'), '商业贸易': D('0.75'), '纺织服装': D('0.75'),
    '家电': D('0.70'), '汽车': D('0.85'), '机械': D('0.90'),
    '化工': D('0.90'), '钢铁': D('0.95'), '有色金属': D('1.00'), '煤炭': D('0.85'),
    '电力设备': D('0.90'), '新能源': D('1.00'), '公用事业': D('0.40'),
    '交通运输': D('0.75'), '物流': D('0.80'), '航空': D('0.90'),
    '科技': D('1.00'), '半导体': D('1.15'), '软件': D('1.05'), '互联网': D('1.10'),
    '通信': D('0.80'), '传媒': D('0.95'), '教育': D('0.90'),
    'default': D('0.85'),
}

# 各参数做敏感度归因时的扰动幅度。选择依据是"这个参数在真实研究中的典型
# 争议范围"，而不是随便取 1%。
_PARAM_SHOCKS: dict[str, Decimal] = {
    'risk_free_rate': D('0.005'),        # ±50bp：10 年期国债年内波动量级
    'equity_risk_premium': D('0.005'),   # ±50bp：各家 ERP 估计的分歧量级
    'beta': D('0.10'),                   # ±0.10：β 估计的典型标准误
    'cost_of_debt': D('0.005'),          # ±50bp：信用利差波动
    'debt_weight': D('0.05'),            # ±5pp：资本结构口径分歧
}


def _dec(value, default=None):
    if value is None or value == '' or isinstance(value, bool):
        return default
    if isinstance(value, D):
        return value
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default
    return result if result.is_finite() else default


def _ratio(numerator, denominator, default=None):
    n, d = _dec(numerator), _dec(denominator)
    if n is None or d is None or d == 0:
        return default
    with localcontext() as ctx:
        ctx.prec = 28
        return n / d


def _fmt_pct(value, digits=2):
    v = _dec(value)
    if v is None:
        return '—'
    return f"{(v * 100).quantize(D(1).scaleb(-digits))}%"


def _fmt_pp(value, digits=2):
    """把比例差渲染成"个百分点"。impact=0.00155 → '0.16 个百分点'。

    直接复用 _fmt_pct 会得到 "0.155% 个百分点" 这种自相矛盾的表述。
    """
    v = _dec(value)
    if v is None:
        return '—'
    return f"{(v * 100).quantize(D(1).scaleb(-digits))} 个百分点"


def _facts_index(facts, period=None, scope=None):
    """按 (期间, 口径) 取已核验事实的最后一个值。"""
    out: dict[str, D] = {}
    candidates = [f for f in (facts or [])
                  if isinstance(f, dict) and f.get('status') == 'verified']
    if scope:
        same = [f for f in candidates if f.get('scope') in (None, '', scope)]
        if same:
            candidates = same
    for fact in candidates:
        code = fact.get('code')
        if not code:
            continue
        if period and str(fact.get('period')) != str(period):
            continue
        value = _dec(fact.get('value'))
        if value is not None:
            out[code] = value
    return out


def _metrics_index(metrics, period=None, scope=None):
    out: dict[str, D] = {}
    for metric in metrics or []:
        if not isinstance(metric, dict) or not metric.get('code'):
            continue
        if period and str(metric.get('period')) != str(period):
            continue
        if scope and metric.get('scope') not in (None, '', scope):
            continue
        value = _dec(metric.get('value'))
        if value is not None:
            out[metric['code']] = value
    return out


def cost_of_debt(facts, metrics=None, period=None, scope=None) -> dict:
    """从财报推导税前债务成本的**区间**。

    只有净财务费用时无法得到单点值，这是数据本身的限制，不是实现偷懒。
    本函数给出 [下界, 上界] 并说明各自口径：

    - 下界：财务费用 ÷ 平均有息负债。财务费用为净额时，这是**被利息收入
      抵减后**的成本，系统性偏低。
    - 上界：(财务费用 + 利息收入) ÷ 平均有息负债。把存款利息也当作借款成本，
      系统性偏高。

    真实值通常落在这个区间里。区间宽度过大时，结论是"该参数无法从财报确定"，
    而不是"取个中间值假装知道"。
    """
    fact = _facts_index(facts, period, scope)
    metric = _metrics_index(metrics, period, scope)
    finance_cost = fact.get('financial_expenses')
    interest_income = fact.get('interest_income')
    if interest_income is not None and interest_income < 0:
        interest_income = -interest_income

    debt = metric.get('interest_bearing_debt')
    if debt is None:
        debt = fact.get('borrowings') or fact.get('short_term_debt')
    debt = _dec(debt)
    if debt is None or debt <= 0:
        return {'ok': False, 'reason': '未取到有息负债，无法推算债务成本。',
                'lower': None, 'upper': None, 'basis': 'unavailable'}

    if finance_cost is None:
        return {'ok': False, 'reason': '未取到财务费用/利息支出科目，无法推算债务成本。',
                'lower': None, 'upper': None, 'basis': 'unavailable',
                'debt': debt}

    lower = _ratio(abs(finance_cost), debt)
    upper = _ratio(abs(finance_cost) + (interest_income or ZERO), debt)
    notes = []
    reliable = True
    if interest_income:
        notes.append(
            f'财务费用为净额口径（已抵减利息收入 {_fmt_pct(_ratio(interest_income, debt))}'
            f' 的有息负债占比），直接相除会系统性低估债务成本。')
    # 上下界比值过大 ⇒ 这不是"真实不确定性"，而是口径错误造成的伪区间：
    # 利息收入来自存款、利息支出来自借款，两者分母不同，硬加回去算不出借款成本。
    # 实测农夫山泉得到 [1.49%, 14.39%]，比值 9.7 倍，毫无信息量。
    if lower is not None and upper is not None and lower > 0 and upper > lower * D('2.5'):
        reliable = False
        notes.append(
            f'上下界相差 {float(upper / lower):.2f} 倍（{_fmt_pct(lower)} vs {_fmt_pct(upper)}），'
            '说明财务费用口径与有息负债规模不匹配（典型于净现金公司：利息收入来自存款而非借款）。'
            '这不是真实的不确定性，而是口径错误造成的伪区间，不能取中点使用。')
    return {
        'ok': True,
        'reliable': reliable,
        'lower': lower, 'upper': upper,
        'midpoint': (lower + upper) / 2 if (lower is not None and upper is not None and reliable) else None,
        'basis': 'net_finance_cost' if interest_income else 'finance_cost',
        'debt': debt, 'finance_cost': abs(finance_cost),
        'interest_income': interest_income,
        'spread': (upper - lower) if lower is not None and upper is not None else None,
        'notes': notes,
    }


def cash_yield(facts, metrics=None, period=None, scope=None) -> dict:
    """存款收益率 = 利息收入 ÷ 平均现金及存款。

    这一项**能从财报扎实地算出来**，而且它直接决定三表预测里的利息收入。
    分母必须用「货币资金 + 长期银行存款」而不是只取货币资金 ——
    农夫山泉的利息收入主要来自 110.88 亿元长期银行存款，只拿货币资金当分母
    会把收益率算高近一倍。
    """
    fact = _facts_index(facts, period, scope)
    metric = _metrics_index(metrics, period, scope)
    interest_income = fact.get('interest_income')
    if interest_income is None:
        return {'ok': False, 'reason': '未取到利息收入科目，无法推算存款收益率。'}
    interest_income = abs(interest_income)
    base = metric.get('cash_and_deposits')
    if base is None:
        cash = fact.get('cash')
        deposits = fact.get('long_term_deposits')
        if cash is None and deposits is None:
            return {'ok': False, 'reason': '未取到货币资金或长期银行存款，无法推算存款收益率。'}
        base = (cash or ZERO) + (deposits or ZERO)
    base = _dec(base)
    if not base:
        return {'ok': False, 'reason': '现金及存款余额为 0，无法推算存款收益率。'}
    rate = _ratio(interest_income, base)
    return {
        'ok': True, 'rate': rate, 'interest_income': interest_income,
        'base': base, 'basis': 'cash_and_deposits',
        'note': '利息收入 ÷ (货币资金 + 长期银行存款)；该值可直接用作三表预测的存款收益率。',
    }


def capital_structure(equity, debt, market_capitalisation=None) -> dict:
    """资本结构权重。

    有市值时用市值权重（理论正确）；只有账面权益时用账面权重，
    并明确说明**账面口径会低估权益权重**（成熟公司的市值通常是账面的数倍），
    因而会高估债务在 WACC 中的分量。
    """
    equity = _dec(equity)
    debt = _dec(debt) or ZERO
    cap = _dec(market_capitalisation)
    if equity is None or equity < 0:
        return {'ok': False, 'reason': '权益账面价值不可用，无法确定资本结构。'}
    base = cap if (cap and cap > 0) else equity
    basis = 'market_value' if (cap and cap > 0) else 'book_value'
    total = base + debt
    if total <= 0:
        return {'ok': False, 'reason': '权益与债务合计不为正，无法确定资本结构。'}
    weight_debt = debt / total
    notes = []
    if basis == 'book_value':
        notes.append('未提供市值，债务权重按账面权益计算；账面口径会低估权益权重、'
                     '高估债务在 WACC 中的分量，请在有市值时替换。')
    if debt <= 0:
        notes.append('有息负债为 0 或缺失，债务权重按 0 处理；此时 WACC 等于股权成本。')
    return {
        'ok': True, 'weight_debt': weight_debt, 'weight_equity': ONE - weight_debt,
        'equity_basis': base, 'debt': debt, 'basis': basis, 'notes': notes,
    }


def lever_beta(beta_unlevered, tax_rate, d_over_e) -> dict:
    """Hamada 加杠杆：β_L = β_U × [1 + (1 − t) × D/E]。

    净现金公司 D/E 为负，加杠杆后 β_L < β_U —— 这是**合理**的：净现金降低
    权益风险。但要设下限（0.30），否则极端净现金结构会把 β 压到接近 0，
    得到低于无风险利率的股权成本，这在经济上不成立。
    """
    beta_u = _dec(beta_unlevered)
    tax = _dec(tax_rate, ZERO) or ZERO
    de = _dec(d_over_e)
    if beta_u is None or de is None:
        return {'ok': False, 'reason': '缺少去杠杆 β 或 D/E，无法加杠杆。'}
    factor = ONE + (ONE - tax) * de
    beta_l = beta_u * factor
    floored = False
    if beta_l < D('0.30'):
        beta_l = D('0.30')
        floored = True
    return {
        'ok': True, 'beta_levered': beta_l, 'beta_unlevered': beta_u,
        'adjustment_factor': factor, 'd_over_e': de,
        'floored': floored,
        'note': ('净现金结构使 β 低于去杠杆 β，这是合理的；'
                 '已按 0.30 设下限以避免得到低于无风险利率的股权成本。'
                 if floored else 'D/E 为正，加杠杆后 β 高于去杠杆 β。'),
    }


def industry_beta(industry) -> dict:
    """按行业取去杠杆 β。这是**外部参数**，必须在使用时标注来源。"""
    text = str(industry or '').strip()
    if text:
        for key, value in INDUSTRY_UNLEVERED_BETA.items():
            if key != 'default' and key in text:
                return {'ok': True, 'beta_unlevered': value, 'matched': key,
                        'source': 'Damodaran 行业 β 数据集（外部参数，非财报实算）',
                        'external': True}
    return {'ok': True, 'beta_unlevered': INDUSTRY_UNLEVERED_BETA['default'],
            'matched': None,
            'source': 'Damodaran 行业 β 数据集 default 值（外部参数，非财报实算）',
            'external': True,
            'note': '未能从行业名称匹配到具体行业，已使用通用默认值；请核对行业归属。'}


def attribute(components: dict) -> dict:
    """WACC 敏感度归因：哪个参数真正影响结论。

    对每个参数做一次 ±扰动，重算 WACC，看它变化多少。输出按影响幅度排序，
    并明确区分"需要外部数据才能确定的参数"和"财报里能算出来的参数"。

    这个表的价值在于把研究力气导向正确的地方：若债务权重只有 1%，
    债务成本从 3% 调到 5% 对 WACC 的影响不到 0.02 个百分点，
    此时纠结债务成本毫无意义。
    """
    rf = _dec(components.get('risk_free_rate'))
    erp = _dec(components.get('equity_risk_premium'))
    beta = _dec(components.get('beta'))
    kd = _dec(components.get('cost_of_debt'))
    tax = _dec(components.get('tax_rate'), ZERO) or ZERO
    wd = _dec(components.get('weight_debt'))
    if None in (rf, erp, beta, kd, wd):
        return {'ok': False, 'reason': '参数不完整，无法做敏感度归因。', 'rows': []}
    we = ONE - wd

    def wacc_of(rf_, erp_, beta_, kd_, wd_):
        ke = rf_ + beta_ * erp_
        return ke * (ONE - wd_) + kd_ * (ONE - tax) * wd_

    base = wacc_of(rf, erp, beta, kd, wd)
    rows = []
    for key, shock in _PARAM_SHOCKS.items():
        if key == 'risk_free_rate':
            up = wacc_of(rf + shock, erp, beta, kd, wd)
        elif key == 'equity_risk_premium':
            up = wacc_of(rf, erp + shock, beta, kd, wd)
        elif key == 'beta':
            up = wacc_of(rf, erp, beta + shock, kd, wd)
        elif key == 'cost_of_debt':
            up = wacc_of(rf, erp, beta, kd + shock, wd)
        elif key == 'debt_weight':
            wd_up = min(wd + shock, ONE)
            up = wacc_of(rf, erp, beta, kd, wd_up)
        else:
            continue
        rows.append({
            'parameter': key, 'shock': shock,
            'wacc_change': up - base,
            'abs_change': abs(up - base),
            'external': key in ('risk_free_rate', 'equity_risk_premium', 'beta'),
        })
    rows.sort(key=lambda r: r['abs_change'], reverse=True)
    total = sum((r['abs_change'] for r in rows), ZERO)
    for row in rows:
        row['share'] = _ratio(row['abs_change'], total) if total else None
    return {
        'ok': True, 'base_wacc': base, 'rows': rows,
        'dominant': rows[0]['parameter'] if rows else None,
        'external_share': _ratio(sum((r['abs_change'] for r in rows if r['external']), ZERO),
                                 total) if total else None,
        'note': '每个参数单独做一次 ±扰动后重算 WACC；share 是该参数影响占全部参数影响之和的比重。',
    }


def derive(facts, metrics=None, overrides=None, industry=None, market=None,
           equity=None, period=None, scope=None, tax_rate=None) -> dict:
    """WACC 总入口：能算的算、算不出的标注来源，最后给出归因。

    `overrides` 里给的任何参数都优先，并标注为使用者提供。
    `market` 若含完整 CAPM 输入（由 valuation_service.normalize_market 校验过），
    则直接采用市场口径，本模块只做补充与归因。
    """
    overrides = dict(overrides or {})
    market = dict(market or {})
    facts_index = _facts_index(facts, period, scope)
    metric_index = _metrics_index(metrics, period, scope)

    sources: dict[str, dict] = {}
    notes: list[str] = []
    warnings: list[str] = []

    # ---- 税率 ----
    if tax_rate is None:
        pbt = facts_index.get('profit_before_tax')
        tax = facts_index.get('income_tax')
        if pbt and tax and pbt > 0:
            tax_rate = _ratio(tax, pbt)
            sources['tax_rate'] = {'source': 'E1_财报实算', 'formula': 'income_tax / profit_before_tax'}
        else:
            tax_rate = D('0.25')
            sources['tax_rate'] = {'source': 'E4_默认假设', 'note': '未取到税前利润与所得税，按 25% 法定税率。'}
    else:
        tax_rate = _dec(tax_rate, D('0.25'))
        sources['tax_rate'] = {'source': 'E1_使用者提供'}
    tax_rate = min(max(tax_rate, ZERO), D('0.5'))

    # ---- 债务成本 ----
    kd_info = cost_of_debt(facts, metrics, period, scope)
    if 'cost_of_debt' in overrides:
        kd = _dec(overrides['cost_of_debt'])
        sources['cost_of_debt'] = {'source': 'E1_使用者提供', 'value': kd}
        kd_band = None
    elif _dec(market.get('cost_of_debt')) is not None:
        kd = _dec(market['cost_of_debt'])
        sources['cost_of_debt'] = {'source': 'E3_外部市场输入',
                                   'source_detail': market.get('source')}
        kd_band = None
    elif kd_info.get('ok') and kd_info.get('reliable'):
        kd = kd_info['midpoint']
        kd_band = (kd_info['lower'], kd_info['upper'])
        sources['cost_of_debt'] = {
            'source': 'E1_财报实算', 'basis': kd_info['basis'],
            'lower': kd_info['lower'], 'upper': kd_info['upper'],
            'note': '财务费用口径给出可信区间，取中点。'}
        notes.extend(kd_info.get('notes') or [])
    elif kd_info.get('ok'):
        # 口径不可靠时**不取伪区间的中点**。财务费用是净额且被存款利息抵减，
        # 硬取中点等于把一个口径错误包装成"测算结果"。改用行业默认借款成本，
        # 区间按信用利差的合理范围设定 —— 这才是真实的不确定性。
        kd = D('0.04')
        kd_band = (D('0.03'), D('0.05'))
        sources['cost_of_debt'] = {
            'source': 'E4_默认假设',
            'note': '财务费用口径无法推算借款成本（净额且被存款利息抵减），'
                    '改用 AA 级信用债利率量级 4%，区间按信用利差取 [3%, 5%]。'}
        notes.extend(kd_info.get('notes') or [])
    else:
        kd = D('0.04')
        sources['cost_of_debt'] = {'source': 'E4_默认假设',
                                   'note': kd_info.get('reason') or '无法从财报推算债务成本。'}
        kd_band = None

    # ---- 资本结构 ----
    debt = metric_index.get('interest_bearing_debt') or facts_index.get('borrowings') or ZERO
    equity_book = (equity
                   or facts_index.get('parent_equity')
                   or facts_index.get('equity'))
    cap = _dec(market.get('market_capitalisation')) or _dec(overrides.get('market_capitalisation'))
    structure = capital_structure(equity_book, debt, cap)
    if not structure.get('ok'):
        warnings.append(structure.get('reason') or '资本结构不可用。')
    weight_debt = _dec(overrides.get('debt_weight')) or structure.get('weight_debt') or ZERO
    if 'debt_weight' in overrides:
        sources['weight_debt'] = {'source': 'E1_使用者提供'}
    elif structure.get('ok'):
        sources['weight_debt'] = {'source': 'E1_财报实算', 'basis': structure.get('basis')}
    else:
        # 资本结构算不出来时权重被兜底成 0（即"全权益"）。这是默认假设，
        # 不是财报实算 —— 若标成 E1，调用方会以为折现率做过财报校准，
        # 而实际上没有任何一项来自报表（实测空事实表下就会这样）。
        sources['weight_debt'] = {
            'source': 'E4_默认假设', 'value': ZERO,
            'note': '未取到权益与有息负债，债务权重按 0（全权益）处理，属默认假设而非财报实算。'}
    notes.extend(structure.get('notes') or [])

    # ---- 无风险利率 ----
    if _dec(overrides.get('risk_free_rate')) is not None:
        rf = _dec(overrides['risk_free_rate'])
        sources['risk_free_rate'] = {'source': 'E1_使用者提供'}
    elif _dec(market.get('risk_free_rate')) is not None:
        rf = _dec(market['risk_free_rate'])
        sources['risk_free_rate'] = {'source': 'E3_外部市场输入',
                                     'as_of': market.get('as_of'),
                                     'source_detail': market.get('source')}
    else:
        rf = D('0.018')
        sources['risk_free_rate'] = {
            'source': 'E4_默认假设', 'note': '中国 10 年期国债收益率的量级值；'
                                             '该值随时间变化，必须由使用者按估值日核验后替换。'}
        warnings.append('无风险利率用的是默认量级值而非估值日实测值，'
                        '它同时影响股权成本与终值增长率上限，必须核验。')
        notes.append(
            '中国 10 年期国债收益率处于历史低位（约 1.8%），直接代入 CAPM 会得到'
            '明显低于长期均衡水平的折现率。若改用规范化无风险利率（例如按长期均值'
            '2.5%–3%），必须在报告里写明依据；本引擎不替使用者做这个选择，'
            '但会在折现率偏低时提示。')

    # ---- 股权风险溢价 ----
    erp_basis = str(overrides.get('erp_basis') or market.get('erp_basis') or 'mature_plus_country')
    if _dec(overrides.get('equity_risk_premium')) is not None:
        erp = _dec(overrides['equity_risk_premium'])
        sources['equity_risk_premium'] = {'source': 'E1_使用者提供'}
    elif _dec(market.get('equity_risk_premium')) is not None:
        erp = _dec(market['equity_risk_premium'])
        sources['equity_risk_premium'] = {'source': 'E3_外部市场输入',
                                          'as_of': market.get('as_of'),
                                          'source_detail': market.get('source')}
    else:
        erp = D('0.055')
        sources['equity_risk_premium'] = {
            'source': 'E4_默认假设', 'note': '成熟市场 ERP 的量级值；须由使用者核验后替换。'}
        warnings.append('股权风险溢价用的是默认量级值而非有来源的估计，必须核验。')

    country = _dec(overrides.get('country_risk_premium')) or _dec(market.get('country_risk_premium')) or ZERO
    if erp_basis == 'china_total':
        # 使用者给的已是中国市场总 ERP，再加国别溢价就是加两遍
        if country:
            notes.append('使用者声明 ERP 已是中国市场总口径，国别风险溢价不再叠加，避免重复计算。')
        country = ZERO
    elif country:
        sources['country_risk_premium'] = {'source': 'E3_外部市场输入'}

    # ---- β ----
    if _dec(overrides.get('beta')) is not None:
        beta = _dec(overrides['beta'])
        beta_info = {'ok': True, 'beta_levered': beta, 'external': False,
                     'note': '使用者直接提供已加杠杆的 β。'}
        sources['beta'] = {'source': 'E1_使用者提供'}
    elif _dec(market.get('beta')) is not None:
        beta = _dec(market['beta'])
        beta_info = {'ok': True, 'beta_levered': beta, 'external': True,
                     'note': '外部市场输入的已加杠杆 β。'}
        sources['beta'] = {'source': 'E3_外部市场输入', 'as_of': market.get('as_of'),
                           'source_detail': market.get('source')}
    else:
        industry_info = industry_beta(industry)
        de = _ratio(debt, equity_book, ZERO) or ZERO
        beta_info = lever_beta(industry_info['beta_unlevered'], tax_rate, de)
        beta_info['industry'] = industry_info.get('matched')
        beta_info['source'] = industry_info.get('source')
        beta = beta_info.get('beta_levered') if beta_info.get('ok') else D('0.85')
        sources['beta'] = {
            'source': 'E2_派生（行业去杠杆 β 经 Hamada 加杠杆）',
            'industry': industry_info.get('matched'),
            'beta_unlevered': industry_info.get('beta_unlevered'),
            'd_over_e': de,
            'source_detail': industry_info.get('source')}
        notes.append('β 由行业去杠杆 β 按 Hamada 公式加杠杆得到，属 E2 派生值；'
                     '行业 β 本身是外部参数，请用可比公司估计值替换以提升可信度。')

    # ---- 组装 ----
    ke = rf + beta * erp + country
    wacc = ke * (ONE - weight_debt) + kd * (ONE - tax_rate) * weight_debt

    # ---- 使用者直接给定折现率 ----
    # 本函数的文档承诺"「overrides」里给的任何参数都优先"，但此前 `wacc`
    # 被静默忽略：调用方以为自己的折现率生效了，实际用的还是组件重算值。
    # 这类"参数给了不生效"是最难查的失效之一，因为输出看起来完全正常。
    wacc_override = _dec(overrides.get('wacc'))
    if wacc_override is not None:
        if wacc_override <= ZERO:
            raise ValueError('wacc 覆盖值必须为正数')
        notes.append(
            f'折现率由使用者直接给定为 {_fmt_pct(wacc_override)}；'
            f'按组件重算的值为 {_fmt_pct(wacc)}。下面的参数归因描述的是'
            '"组件重算值"对各参数的敏感度，与最终采用的折现率不完全对应。')
        sources['wacc'] = {'source': 'E1_使用者提供', 'value': wacc_override,
                           'computed_from_components': wacc}
        wacc = wacc_override

    attribution = attribute({
        'risk_free_rate': rf, 'equity_risk_premium': erp, 'beta': beta,
        'cost_of_debt': kd, 'tax_rate': tax_rate, 'weight_debt': weight_debt,
    })

    # ---- 债务成本是否真的重要 ----
    # 这是本模块最有价值的一句判断：把"该参数无法从财报确定"这个坏消息，
    # 转化为"该参数对本结论没有实质影响"这个可用的结论。
    impact = None
    if kd_band:
        impact = abs((kd_band[1] - kd_band[0]) * (ONE - tax_rate) * weight_debt)
    cost_of_debt_material = bool(impact is None or impact >= D('0.002'))
    if kd_band and not cost_of_debt_material:
        notes.append(
            f'债务成本在 [{_fmt_pct(kd_band[0])}, {_fmt_pct(kd_band[1])}] 区间内变动时，'
            f'对 WACC 的影响不超过 {_fmt_pp(impact, 3)}'
            f'（债务权重仅 {_fmt_pct(weight_debt)}），该参数不影响结论，无需外部数据。')
    elif kd_band:
        warnings.append(
            f'债务成本区间 [{_fmt_pct(kd_band[0])}, {_fmt_pct(kd_band[1])}] 对 WACC 的影响'
            f'达 {_fmt_pp(impact, 3)}（债务权重 {_fmt_pct(weight_debt)}），'
            '需由使用者提供或核验后再定稿。')

    # ---- 边界校验 ----
    checks = []
    checks.append({'name': 'wacc_above_risk_free', 'ok': wacc > rf,
                   'detail': f'WACC {_fmt_pct(wacc)} 应高于无风险利率 {_fmt_pct(rf)}。'})
    checks.append({'name': 'equity_cost_above_risk_free', 'ok': ke > rf,
                   'detail': f'股权成本 {_fmt_pct(ke)} 应高于无风险利率 {_fmt_pct(rf)}。'})
    checks.append({
        'name': 'cost_of_debt_resolved',
        'ok': bool(not kd_band or not cost_of_debt_material),
        'detail': (f'税前债务成本 {_fmt_pct(kd)}'
                   + (f'，区间 [{_fmt_pct(kd_band[0])}, {_fmt_pct(kd_band[1])}] 对 WACC 的影响为 '
                      f'{_fmt_pp(impact, 3)}' if kd_band else '，来自外部输入')
                   + ('，不影响结论。' if not cost_of_debt_material
                      else '，需核验后再定稿。')),
    })
    if wacc < D('0.06'):
        notes.append(
            f'推导出的 WACC 为 {_fmt_pct(wacc)}，低于 6%，属低利率环境下的常见结果。'
            '这不必然是错误（当前无风险利率确实很低），但报告里应当说明：'
            '折现率偏低会让估值对终值假设更敏感，且更容易得出"显著低估"的结论。')

    external = sorted(k for k, v in sources.items() if str(v.get('source', '')).startswith('E3')
                      or '外部' in str(v.get('source', '')))
    assumed = sorted(k for k, v in sources.items() if str(v.get('source', '')).startswith('E4'))

    # ---- 这个 WACC 到底有多少来自财报？ ----
    # 必须显式判断，不能只因为"函数跑完了"就宣称做了财报侧校准。
    # 实测：把空事实表传进来时本函数照样返回 ok=True、WACC 6.475% ——
    # 每一项都是默认值，资本结构甚至因为取不到权益而失败、债务权重被当成 0。
    # 如果调用方据此把折现率的方法标签从「默认假设」改写成「财报侧校准」，
    # 报告就会声称一个从未发生的校准，这是最典型的"可追溯性"造假。
    #
    # 只认 E1_财报实算：E2 派生值（如行业 β 经 Hamada 加杠杆）的输入本身
    # 就是外部参数，拿它当"财报校准"同样不成立。
    statement_sources = sorted(
        k for k, v in sources.items()
        if str(v.get('source', '')).startswith('E1_财报实算'))
    derived_sources = sorted(
        k for k, v in sources.items()
        if str(v.get('source', '')).startswith('E2_'))
    statement_calibrated = bool(statement_sources) and bool(structure.get('ok'))
    if not statement_calibrated:
        warnings.append(
            '本次 WACC 没有任何一项来自财报实算'
            f'（财报实算项：{statement_sources or "无"}；派生项：{derived_sources or "无"}；'
            f'资本结构：{"可用" if structure.get("ok") else "不可用"}）——'
            '该折现率是纯默认假设，不得在报告中描述为"由财报校准"。')

    return {
        'ok': True,
        'statement_calibrated': statement_calibrated,
        'statement_sources': statement_sources,
        'derived_sources': derived_sources,
        'wacc': wacc, 'cost_of_equity': ke, 'after_tax_cost_of_debt': kd * (ONE - tax_rate),
        'cost_of_debt': kd, 'cost_of_debt_band': kd_band,
        'cost_of_debt_material': cost_of_debt_material,
        'cost_of_debt_wacc_impact': impact,
        'weight_debt': weight_debt, 'weight_equity': ONE - weight_debt,
        'tax_rate': tax_rate, 'beta': beta, 'beta_info': beta_info,
        'risk_free_rate': rf, 'equity_risk_premium': erp,
        'country_risk_premium': country, 'erp_basis': erp_basis,
        'capital_structure': structure,
        'sources': sources, 'attribution': attribution,
        'checks': checks,
        'notes': notes, 'warnings': warnings,
        'external_parameters': external, 'assumed_parameters': assumed,
        'cash_yield': cash_yield(facts, metrics, period, scope),
        'summary': (f"WACC {_fmt_pct(wacc)} = 股权成本 {_fmt_pct(ke)} × {_fmt_pct(ONE - weight_debt)}"
                    f" + 税后债务成本 {_fmt_pct(kd * (ONE - tax_rate))} × {_fmt_pct(weight_debt)}；"
                    f"{len(external)} 项来自外部输入、{len(assumed)} 项沿用默认假设。"),
    }
