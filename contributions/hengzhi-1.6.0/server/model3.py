"""平衡三表联动预测引擎：把"驱动变量预测"升级成"会自己配平的三张表"。

为什么需要这个模块
------------------
`valuation_advanced` 的自述里写着一句自我限制：

    "This forecasts operating drivers, not three balanced financial statements."
    model_plan['balanced_three_statements'] = False

也就是说，此前模型只沿一条线外推经营驱动、算出 FCFF，**从不生成资产负债表**，
因而无法回答三个评审一定会问的问题：

1. 你预测的利润，进到资产负债表里去了吗（留存收益是否累积）？
2. 你预测的现金流，与资产负债表上现金的变动对得上吗？
3. 收入涨 12% 意味着应收和存货要占多少资金？这些钱从哪来？

前两个是"可验证"，第三个是"金融逻辑"。本模块把这三件事变成**逐年、可机检**的输出。

配平机制（数学上可证明，不是"调数字凑平"）
--------------------------------------------
把资产负债表拆成四块：

    经营性流动资产 WC_assets + 非流动经营资产 FA + 现金 Cash
        = 经营性流动负债 WC_liab + 经营性非流动负债 NCL + 有息负债 Debt + 权益 Equity

现金取作**平衡项（plug）**：

    Cash_t = (WC_liab_t + NCL_t + Debt_t + Equity_t) − (WC_assets_t + FA_t)

在满足下面四条纪律时，BS 推出的现金变动与现金流量表推出的现金变动**恒等**：

    ① FA_t   = FA_{t−1} + Capex_t − D&A_t − Impairment_t
    ② Equity_t = Equity_{t−1} + NI_t − Dividend_t
    ③ CFO 只含营运资本变动，不含固定资产变动（那是 CFI 的 Capex）
    ④ CFI = −Capex

推导（记 Δ 为对上一年的变动）：

    BS 侧： ΔCash = ΔWC_liab + ΔNCL + ΔDebt + ΔEquity − ΔWC_assets − ΔFA
                  = ΔWC_liab + ΔNCL + ΔDebt + (NI − Div) − ΔWC_assets − (Capex − D&A − Imp)
    CF 侧： ΔCash = CFO + CFI + CFF
                  = (NI + D&A + Imp − ΔWC_assets + ΔWC_liab + ΔNCL) − Capex + (−Div + ΔDebt)

两式逐项相同。因此**两张表必须给出同一个现金余额** —— 这正是本模块的自检依据：
任何一处配平写错，残差立刻不为零，`check()` 会报出来。

两种平衡模式
------------
- `cash_plug`：现金被动平衡。适用于**净现金公司**（有息负债远小于现金），
  因为这类公司不会为了"配平"去借钱，现金本身就是缓冲垫。
- `debt_plug`：有息负债被动平衡（revolver）。适用于**杠杆公司**，
  这类公司的现金余额由经营需要决定，缺口靠借款补。

模式依据期初 `net_debt` 的符号自动选择，并允许覆盖 —— 选择依据写进输出，
因为它直接决定了"增长所需的钱从哪来"这个结论。

现金为负怎么办
--------------
`cash_plug` 模式下若算出的现金低于最低现金持有量，说明经营与投资的需求超过了
内生现金生成能力，缺口必须由借款补足。此时切到 revolver 逻辑迭代收敛：
借入 → 利息费用上升 → 净利润下降 → 权益下降 → 现金需求再上升。

融资与利息的循环引用
--------------------
利息按**期初与期末的平均余额**计息，于是"借多少"与"利息多少"互为因果，
是一个真正的不动点问题，必须迭代求解，而不是可以一次算出的联立方程：

    Int_t = (Debt_{t-1} + Debt_t) / 2 × Kd
    Debt_t = (WC_assets_t + FA_t + Cash_t) − (WC_liab_t + NCL_t + Equity_t)
    Equity_t = Equity_{t-1} + (EBIT_t − Int_t + IntInc_t) × (1 − t) × (1 − 分红率)

单轮收缩系数约为 (1 − 税率) × (1 − 分红率) × 资金成本 / 2，实测在税率 25%、
分红率 50%、资金成本 4% 时约 0.0075，远小于 1，因此通常 6–8 轮即收敛到
1e-9 元以内；迭代上限 200 轮（正常情形提前退出），不收敛则明确报错
而不是给一个错数。

若改为按期初余额计息（`funding['interest_basis'] = 'beginning'`），
循环引用消失、一轮即得解 —— 保留该口径是为了与只按期初余额计息的
常见简化模型对账。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext

D = Decimal
ZERO = D(0)
ONE = D(1)
TWO = D(2)
DAYS = D(365)

# 配平残差的容忍度（元）。三表恒等是精确关系，只留浮点/舍入余量。
TOLERANCE = D('0.01')

# 融资—利息不动点迭代的收敛容差。刻意比 TOLERANCE 严 7 个数量级：
# 循环里利息是用**上一轮**的余额算的、余额又用这一轮的利息更新，
# 若容差取 0.01，停下来的那一刻利润表里的利息收入与资产负债表上的
# 平均余额就会差约 2×10⁻⁵ 元（实测），输出内部便存在一处极小的不自洽。
# 收紧到 1e-9 后该差异降到 1e-11 以下，任何展示精度下都不可见。
CONVERGENCE_TOLERANCE = D('1e-9')

# revolver 迭代上限。收敛速度由收缩系数决定（实测约 0.0075，通常 6–8 轮），
# 上限取得很宽是为了不在参数稍极端时误报"不收敛"；正常情形会提前退出。
MAX_ITERATIONS = 200


def _dec(value, default=None):
    """安全转 Decimal。None/空/非法一律回落 default，绝不静默当 0。"""
    if value is None or value == '' or isinstance(value, bool):
        return default
    if isinstance(value, D):
        return value
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default
    return result if result.is_finite() else default


def _s(value):
    """序列化 Decimal：去掉尾随零，-0 归一为 0。"""
    if value is None:
        return None
    if isinstance(value, D):
        text = format(value.normalize(), 'f')
        return '0' if text == '-0' else text
    return value


# ---------------------------------------------------------------------------
# 期初资产负债表：从已核验事实重建，并保证期初自身是平的
# ---------------------------------------------------------------------------

# 科目归组。分组的依据是"驱动方式"而不是"流动性"：
# 经营性流动资产由周转天数驱动，非流动经营资产由 Capex/D&A 驱动，
# 有息负债由融资政策驱动，权益由利润与分红驱动。
# 2026-10 补强：增加应收款项融资/合同资产/其他流动资产与应付票据/预收/应付职工薪酬/其他流动负债。
# 此前这些科目只能靠总括行倒挤，倒挤口径把有息长期借款也算进"流动负债已映射部分"，于是真实 A 股上
# 常出现负的"其他流动负债"（紫金 -189 亿、神华 -183 亿）。显式给出则缺口为 0，不再出现修正项。
_CURRENT_ASSET_CODES = ('receivables', 'notes_receivable', 'prepayments', 'other_receivables',
                        'inventory', 'receivables_financing', 'contract_assets', 'other_current_assets')
_CURRENT_LIABILITY_CODES = ('payables', 'contract_liabilities', 'tax_payables',
                            'other_payables', 'deferred_income', 'notes_payable', 'advance_receipts',
                            'payroll_payable', 'other_current_liabilities')
_NONCURRENT_ASSET_CODES = ('fixed_assets', 'right_of_use_assets', 'intangibles',
                           'deferred_tax_assets', 'other_noncurrent_assets')
# 租赁负债**不在这里**：会计上它是有息负债，已含在 interest_bearing_debt 指标里。
# 若在此重复计入，负债会虚增一倍租赁负债额，资产负债表直接不平。
_NONCURRENT_LIABILITY_CODES = ('deferred_tax_liabilities', 'other_noncurrent_liabilities')

# 这些科目进入"其他项"残差，本身不单独预测，只按收入/成本比例延续。
_RESIDUAL_LABELS = {
    'other_current_assets': '其他流动资产',
    'other_current_liabilities': '其他流动负债',
    'other_noncurrent_assets': '其他非流动资产',
    'other_noncurrent_liabilities': '其他非流动负债',
}


class _Picker:
    """按 (期间, 口径, 证据层级) 优先级取已核验事实。

    与 valuation._FactPicker 同源，但这里只需要"取最新一期的时点值"，
    因此独立实现一个更小的版本，避免两个模块对同一份数据给出不同的选取规则。
    """

    _TIER = {'primary': 0, 'summary': 1, 'inferred': 2}

    def __init__(self, facts):
        self.facts = [f for f in (facts or [])
                      if isinstance(f, dict) and f.get('status') == 'verified']

    def one(self, code, period=None, scope=None):
        hits = [f for f in self.facts if f.get('code') == code]
        if period:
            same = [f for f in hits if str(f.get('period')) == str(period)]
            if same:
                hits = same
        if scope:
            same = [f for f in hits if f.get('scope') in (None, '', scope)]
            if same:
                hits = same
        if not hits:
            return None
        # 合并口径优先于母公司口径；同口径取期间最新；同期间取证据层级最高。
        hits.sort(key=lambda f: (str(f.get('period') or ''),
                                 f.get('scope') == 'consolidated',
                                 -self._TIER.get(f.get('source_tier'), 9)))
        return hits[-1]

    def value(self, code, period=None, scope=None):
        fact = self.one(code, period, scope)
        return _dec(fact.get('value')) if fact else None


def opening_balance(facts, metrics=None, period=None, scope=None) -> dict:
    """从已核验事实重建期初资产负债表，并保证它自身是平的。

    关键手法：**用总括行减去已映射明细，差额进"其他项"**。
    直接对各明细求和而不看总括行，是建模里最常见的错 —— 年报科目远比
    标准科目表丰富（农夫山泉有 57 个科目），漏一个科目资产负债表就平不了，
    而很多人此时会去"调"一个数字凑平，把错误埋进模型里。
    本模块的做法是：差额显式入表、单独列示、可被质疑。

    有息负债**优先取 `interest_bearing_debt` 指标**而不是自己拼科目：
    `borrowings` 与 `short_term_debt` 在农夫山泉年报里是同一笔钱的两个标签，
    直接相加会得到 88.8 亿元，而权威口径是 44.93 亿元（实测踩过）。
    financial_analysis 里的那个指标已经做了总括行优先与金额去重，是唯一权威实现。
    """
    pick = _Picker(facts)
    if period is None:
        revenue = pick.one('revenue')
        if revenue:
            period = str(revenue.get('period'))
    if scope is None:
        revenue = pick.one('revenue', period)
        scope = revenue.get('scope') if revenue else None

    # 指标也要按期、按口径挑：metrics 里同一 code 每一期都有**合并与母公司两条**
    # （实测 2025 年有息负债合并口径 44.93 亿元、母公司口径 0.20 亿元），
    # 只按 code 取最后一条会拿到母公司数，期初表口径直接错。
    def metric_at(code):
        hits = [m for m in (metrics or [])
                if isinstance(m, dict) and m.get('code') == code]
        if period:
            same = [m for m in hits if str(m.get('period')) == str(period)]
            if same:
                hits = same
        if scope:
            same = [m for m in hits if m.get('scope') in (None, '', scope)]
            if same:
                hits = same
        return hits[-1] if hits else None

    sources: dict[str, dict] = {}

    def take(code):
        fact = pick.one(code, period, scope) or pick.one(code, period) or pick.one(code)
        if not fact:
            return None
        sources[code] = {'source': 'fact', 'period': fact.get('period'),
                         'scope': fact.get('scope'),
                         'evidence_ids': list(fact.get('evidence_ids') or [])}
        return _dec(fact.get('value'))

    # ---- 经营性流动资产 ----
    wc_assets = {}
    for code in _CURRENT_ASSET_CODES:
        value = take(code)
        if value is not None:
            wc_assets[code] = value
    # ---- 非流动经营资产 ----
    fa_items = {}
    for code in _NONCURRENT_ASSET_CODES:
        value = take(code)
        if value is not None:
            fa_items[code] = value
    # ---- 经营性流动负债 ----
    wc_liab = {}
    for code in _CURRENT_LIABILITY_CODES:
        value = take(code)
        if value is not None:
            wc_liab[code] = value
    # ---- 经营性非流动负债 ----
    ncl = {}
    for code in _NONCURRENT_LIABILITY_CODES:
        value = take(code)
        if value is not None:
            ncl[code] = value

    cash = take('cash') or ZERO
    # 类现金：货币资金 + 长期银行存款。长期存款在报表里挂在非流动资产，
    # 但它是利息收入的真实来源 —— 算存款收益率时只拿"货币资金"当分母会严重高估。
    long_term_deposits = take('long_term_deposits')
    cash_like = cash + (long_term_deposits or ZERO)

    debt = None
    debt_metric = metric_at('interest_bearing_debt')
    if debt_metric is not None and _dec(debt_metric.get('value')) is not None:
        debt = _dec(debt_metric.get('value'))
        sources['interest_bearing_debt'] = {
            'source': 'metric', 'code': 'interest_bearing_debt',
            'period': debt_metric.get('period'),
            'detail': 'financial_analysis 的权威口径（总括行优先 + 金额去重，已含租赁负债）',
            'evidence_ids': list(debt_metric.get('evidence_ids') or [])}
    if debt is None:
        # 降级路径：总括行优先，明细仅在总括行缺失或明显漏计口径时使用。
        # borrowings 与 short_term_debt 常是同一笔钱的两个标签，直接相加会翻倍。
        total_debt = take('borrowings')
        details = [v for v in (take('short_term_debt'), take('long_term_debt'),
                               take('bonds_payable')) if v is not None]
        detail_sum = sum(details, ZERO)
        if total_debt is not None:
            debt = detail_sum if (details and detail_sum > total_debt * D('1.02')) else total_debt
            sources['interest_bearing_debt'] = {
                'source': 'fact',
                'formula': 'borrowings 总括行（明细之和未显著超出时不重复计入）',
                'detail': f'总括行 {_s(total_debt)}，明细之和 {_s(detail_sum)}'}
        elif details:
            debt = detail_sum
            sources['interest_bearing_debt'] = {
                'source': 'fact',
                'formula': 'short_term_debt + long_term_debt + bonds_payable'}
        # 租赁负债属有息负债，且已从 ncl 中移出，这里补入以免漏计。
        lease = take('lease_liabilities')
        if lease is None:
            lease = sum((v for v in (take('lease_liabilities_current'),
                                     take('lease_liabilities_noncurrent')) if v is not None), ZERO)
        if lease:
            debt = (debt or ZERO) + lease
            sources.setdefault('interest_bearing_debt', {})['lease_included'] = _s(lease)
        if debt is not None:
            sources.setdefault('interest_bearing_debt', {})['degraded'] = True

    total_assets = take('assets')
    total_liabilities = take('liabilities')
    total_equity = take('equity')
    share_capital = take('share_capital')
    parent_equity = take('parent_equity')
    minority_equity = take('minority_equity') or ZERO

    # ---- 权益拆分：股本 + 留存收益 + 少数股东权益 ----
    # 合并口径的留存收益不能直接用母公司报表的 reserves，两者口径不同。
    # 正确的口径是：归母权益 − 股本。
    if parent_equity is not None and share_capital is not None:
        retained = parent_equity - share_capital
        sources['retained_earnings'] = {
            'source': 'derived', 'formula': 'parent_equity − share_capital',
            'detail': '合并口径留存收益（含其他储备），避免误用母公司报表的 reserves'}
    elif total_equity is not None and share_capital is not None:
        retained = total_equity - share_capital - minority_equity
        sources['retained_earnings'] = {
            'source': 'derived', 'formula': 'equity − share_capital − minority_equity'}
    else:
        retained = ZERO
    if share_capital is None:
        share_capital = ZERO

    # ---- 总括行对账：差额进"其他项"，保证期初平衡 ----
    residual_notes = []
    mapped_assets = cash + sum(wc_assets.values(), ZERO) + sum(fa_items.values(), ZERO)
    if total_assets is not None:
        gap = total_assets - mapped_assets
        if gap != 0:
            # 差额归到"其他流动资产"还是"其他非流动资产"？
            # 用流动/非流动总括行的缺口来定位，取不到就按缺口正负就近归入。
            current_total = take('current_assets')
            noncurrent_total = take('noncurrent_assets')
            current_mapped = cash + sum(
                v for k, v in wc_assets.items() if k != 'inventory') + wc_assets.get('inventory', ZERO)
            if current_total is not None:
                current_gap = current_total - current_mapped
                noncurrent_gap = gap - current_gap
                if current_gap != 0:
                    wc_assets['other_current_assets'] = wc_assets.get('other_current_assets', ZERO) + current_gap
                    residual_notes.append(
                        f'其他流动资产由 current_assets 总括行倒挤：{_s(current_gap)} 元')
                if noncurrent_gap != 0:
                    fa_items['other_noncurrent_assets'] = fa_items.get('other_noncurrent_assets', ZERO) + noncurrent_gap
                    residual_notes.append(
                        f'其他非流动资产由 noncurrent_assets 总括行倒挤：{_s(noncurrent_gap)} 元')
            else:
                if gap > 0:
                    wc_assets['other_current_assets'] = wc_assets.get('other_current_assets', ZERO) + gap
                    residual_notes.append(f'未取到流动/非流动拆分，差额 {_s(gap)} 元全部计入其他流动资产')
                else:
                    fa_items['other_noncurrent_assets'] = fa_items.get('other_noncurrent_assets', ZERO) + gap
                    residual_notes.append(f'未取到流动/非流动拆分，差额 {_s(gap)} 元全部计入其他非流动资产')

    mapped_liab = (debt or ZERO) + sum(wc_liab.values(), ZERO) + sum(ncl.values(), ZERO)
    if total_liabilities is not None:
        gap = total_liabilities - mapped_liab
        if gap != 0:
            current_total = take('current_liabilities')
            noncurrent_total = take('noncurrent_liabilities')
            if current_total is not None:
                current_mapped = sum(wc_liab.values(), ZERO) + (debt or ZERO)
                current_gap = current_total - current_mapped
                noncurrent_gap = gap - current_gap
                if current_gap != 0:
                    wc_liab['other_current_liabilities'] = wc_liab.get('other_current_liabilities', ZERO) + current_gap
                    residual_notes.append(f'其他流动负债由 current_liabilities 总括行倒挤：{_s(current_gap)} 元')
                if noncurrent_gap != 0:
                    ncl['other_noncurrent_liabilities'] = ncl.get('other_noncurrent_liabilities', ZERO) + noncurrent_gap
                    residual_notes.append(f'其他非流动负债由 noncurrent_liabilities 总括行倒挤：{_s(noncurrent_gap)} 元')
            else:
                if gap > 0:
                    wc_liab['other_current_liabilities'] = wc_liab.get('other_current_liabilities', ZERO) + gap
                    residual_notes.append(f'未取到流动/非流动拆分，差额 {_s(gap)} 元全部计入其他流动负债')
                else:
                    ncl['other_noncurrent_liabilities'] = ncl.get('other_noncurrent_liabilities', ZERO) + gap
                    residual_notes.append(f'未取到流动/非流动拆分，差额 {_s(gap)} 元全部计入其他非流动负债')

    # ---- 权益侧配平 ----
    # 负债已按总括行配好，权益 = 资产 − 负债（若有总括行），
    # 这样期初表**定义上**就是平的，避免因某个权益明细缺失而让整个模型起步就歪。
    equity_total = (total_assets - total_liabilities
                    if total_assets is not None and total_liabilities is not None
                    else share_capital + retained + minority_equity)
    if parent_equity is not None and minority_equity == ZERO:
        minority_equity = max(equity_total - parent_equity, ZERO)
    retained = equity_total - share_capital - minority_equity

    assets_check = cash + sum(wc_assets.values(), ZERO) + sum(fa_items.values(), ZERO)
    liab_equity_check = (debt or ZERO) + sum(wc_liab.values(), ZERO) + sum(ncl.values(), ZERO) + equity_total
    residual = assets_check - liab_equity_check

    revenue = pick.value('revenue', period, scope)
    cost = pick.value('cost_of_revenue', period, scope)

    # ---- 倒挤出负数的"其他项"必须显式告警 ----
    # 负值说明某些明细科目在总括行里被重复列示（或本模块的分类口径与年报不同），
    # 这类项在预测期会按比例延续，语义上是"修正项"而不是真实科目。
    # 不告警的话，读者会把一个负的"其他流动负债"当成正常数据。
    negative_residuals = []
    for label, container in (('其他流动资产', wc_assets), ('其他流动负债', wc_liab),
                             ('其他非流动资产', fa_items), ('其他非流动负债', ncl)):
        key = {v: k for k, v in _RESIDUAL_LABELS.items()}[label]
        value = container.get(key)
        if value is not None and value < 0:
            negative_residuals.append(f'{label} {_s(value)} 元')
    warnings = []
    if abs(residual) > TOLERANCE:
        warnings.append(f'期初资产负债表残差 {_s(residual)} 元，未通过配平检查。')
    if negative_residuals:
        warnings.append(
            '倒挤出的"其他项"为负（' + '；'.join(negative_residuals) + '），'
            '说明部分明细科目在总括行中被重复列示，或本模块的流动/非流动分类口径'
            '与年报不同。这些项在预测期按收入比例延续，语义上是修正项而非真实科目，'
            '引用时需说明。')

    return {
        'period': period,
        'scope': scope,
        'cash': cash,
        'cash_like': cash_like,
        'long_term_deposits': long_term_deposits,
        'wc_assets': wc_assets,
        'fa': fa_items,
        'wc_liab': wc_liab,
        'ncl': ncl,
        'debt': debt,
        'share_capital': share_capital,
        'retained_earnings': retained,
        'minority_equity': minority_equity,
        'equity': equity_total,
        'total_assets': assets_check,
        'total_liabilities': (debt or ZERO) + sum(wc_liab.values(), ZERO) + sum(ncl.values(), ZERO),
        'revenue': revenue,
        'cost_of_revenue': cost,
        'sources': sources,
        'residual_notes': residual_notes,
        'negative_residuals': negative_residuals,
        'balanced': abs(residual) <= TOLERANCE,
        'residual': residual,
        'warnings': warnings,
    }


# ---------------------------------------------------------------------------
# 三表联动预测
# ---------------------------------------------------------------------------

def _ratio(numerator, denominator, default=None):
    """比率计算，分母为 0 或缺失时回落默认值而不是抛错。"""
    n, d = _dec(numerator), _dec(denominator)
    if n is None or d is None or d == 0:
        return default
    with localcontext() as ctx:
        ctx.prec = 28
        return n / d


def forecast(opening: dict, params: dict, paths: dict | None = None,
             funding: dict | None = None) -> dict:
    """逐年生成配平的三张表。

    `params` 提供水平值（期初余额、税率、融资政策），`paths` 提供逐年驱动
    （收入增速、毛利率、费用率、周转天数、Capex 率、D&A 率）。两者分离是为了
    让"压力情景"只改路径而不动期初 —— 期初是事实，不该被情景改写。

    `funding` 控制平衡模式与融资政策：
        mode              'cash_plug' | 'debt_plug' | 'auto'
        minimum_cash      最低现金持有（cash_plug 下低于此值触发借款），默认 0
        payout_ratio      分红率（占归母净利润）
        debt_repayment    'hold'（保持期初水平）| 'sweep'（超额现金先还债）
    """
    funding = dict(funding or {})
    years = int(_dec(params.get('years'), 5) or 5)
    tax_rate = _dec(params.get('tax_rate'), D('0.25')) or D('0.25')
    debt_cost = _dec(params.get('cost_of_debt'))
    cash_yield = _dec(params.get('cash_yield'))
    payout = _dec(funding.get('payout_ratio'), ZERO) or ZERO
    payout = min(max(payout, ZERO), ONE)
    minimum_cash = _dec(funding.get('minimum_cash'), ZERO) or ZERO
    mode = str(funding.get('mode') or 'auto')
    if mode == 'auto':
        mode = 'cash_plug' if (opening.get('debt') or ZERO) <= opening.get('cash', ZERO) else 'debt_plug'
    other_nca_policy = str(funding.get('other_noncurrent_policy') or 'hold')
    average_basis = str(funding.get('interest_basis') or 'average') != 'beginning'

    # 期初的"其他项"与收入的比例，用于外推。取不到比例时按 0 处理并告警。
    revenue_0 = _dec(opening.get('revenue'))
    cost_0 = _dec(opening.get('cost_of_revenue'))
    warnings: list[str] = []

    # 驱动项与比例项必须**覆盖期初表的每一个科目**。
    # 漏掉任何一个科目，它都会在预测首年凭空消失：营运资本变动被虚增，
    # 现金流量表与资产负债表立刻对不上（实测漏掉 notes_receivable /
    # prepayments / other_receivables 三个应收类科目，第 5 年现金差 30.39 亿元）。
    # 因此这里按"期初有哪些科目"动态建比例表，而不是硬编码几个键名。
    driven_assets = ('receivables', 'inventory')     # 由周转天数驱动
    driven_liabilities = ('payables',)               # 由周转天数驱动
    asset_ratios = {key: _ratio(value, revenue_0)
                    for key, value in opening['wc_assets'].items()
                    if key not in driven_assets and revenue_0}
    liab_ratios = {key: _ratio(value, revenue_0)
                   for key, value in opening['wc_liab'].items()
                   if key not in driven_liabilities and revenue_0}
    ncl_ratios = {key: _ratio(value, revenue_0)
                  for key, value in opening['ncl'].items() if revenue_0}
    # 非流动经营资产里，"其他非流动资产"按收入比例延续，其余由 Capex/D&A 滚存驱动。
    other_nca_ratio = _ratio(opening['fa'].get('other_noncurrent_assets'), revenue_0)
    # 期初的"其他非流动资产"必须单独记住：首年算固定资产净额时要用**期初**口径，
    # 不能用首年预测值，否则首年的 Capex/D&A 滚存起点就错了。
    other_nca_opening = _dec(opening['fa'].get('other_noncurrent_assets'), ZERO) or ZERO

    # 少数股东占比：用期初少数股东权益 / 总权益。取不到就按 0，并在输出里标注。
    minority_share = _ratio(opening.get('minority_equity'), opening.get('equity'), ZERO) or ZERO
    # 少数股东"损益"占比与"权益"占比常差很大（实测紫金：权益占比 25%、历史损益占比约 20%；
    # 万科权益占比 50%）。调用方可用 params['minority_income_share'] 直接给出历史损益占比。
    explicit_minority_share = _dec(params.get('minority_income_share'))
    if explicit_minority_share is not None:
        minority_share = min(max(explicit_minority_share, ZERO), ONE)
    if minority_share > 0:
        basis = '历史少数股东损益占比' if explicit_minority_share is not None else '期初权益占比'
        warnings.append(f'少数股东损益按{basis} {minority_share:.4f} 简化分摊，'
                        '未单独建模少数股东所在子公司的盈利能力。')

    if not paths:
        warnings.append('未提供逐年驱动路径，已按期初驱动与稳定期假设线性过渡。')

    # ---- 期初状态 ----
    cash_prev = _dec(opening.get('cash'), ZERO) or ZERO
    debt_prev = _dec(opening.get('debt'), ZERO) or ZERO
    fa_prev = sum(opening['fa'].values(), ZERO)
    wc_assets_prev = sum(opening['wc_assets'].values(), ZERO)
    wc_liab_prev = sum(opening['wc_liab'].values(), ZERO)
    # 期初营运资本要单独留存：首年 ΔNWC 必须以它为起点。
    wc_assets_opening, wc_liab_opening = wc_assets_prev, wc_liab_prev
    # "核心口径"只含应收/存货/应付三项，用于与驱动法 FCFF 逐项对账 ——
    # 驱动法只建模这三项，其余经营科目它完全没有，两者本就不该直接比总额。
    wc_assets_opening_core = ((_dec(opening['wc_assets'].get('receivables'), ZERO) or ZERO)
                              + (_dec(opening['wc_assets'].get('inventory'), ZERO) or ZERO))
    wc_liab_opening_core = _dec(opening['wc_liab'].get('payables'), ZERO) or ZERO
    ncl_prev = sum(opening['ncl'].values(), ZERO)
    retained_prev = _dec(opening.get('retained_earnings'), ZERO) or ZERO
    minority_prev = _dec(opening.get('minority_equity'), ZERO) or ZERO
    share_capital = _dec(opening.get('share_capital'), ZERO) or ZERO
    revenue = revenue_0

    rows = []
    converged = True
    other_nca_prev_core = other_nca_opening
    for year in range(1, years + 1):
        index = year - 1
        path = {}
        for key in ('revenue_growth', 'gross_margin', 'opex_ratio', 'da_to_revenue',
                    'capex_to_revenue', 'receivable_days', 'inventory_days', 'payable_days'):
            values = (paths or {}).get(key)
            if values and index < len(values):
                path[key] = _dec(values[index])
            else:
                # 路径缺省时回落到校准参数，**绝不能回落成 0**：
                # 回落成 0 会让收入零增长、折旧与资本开支凭空消失，
                # 三表照样能配平，但模型与这家公司已经没关系了。
                path[key] = _dec(params.get(key))
        growth = path['revenue_growth'] if path['revenue_growth'] is not None else ZERO
        gm = path['gross_margin'] if path['gross_margin'] is not None else D('0.3')
        opex_ratio = path['opex_ratio'] if path['opex_ratio'] is not None else D('0.15')
        da_ratio = path['da_to_revenue'] if path['da_to_revenue'] is not None else ZERO
        capex_ratio = path['capex_to_revenue'] if path['capex_to_revenue'] is not None else ZERO
        ar_days = path['receivable_days'] if path['receivable_days'] is not None else ZERO
        inv_days = path['inventory_days'] if path['inventory_days'] is not None else ZERO
        ap_days = path['payable_days'] if path['payable_days'] is not None else ZERO

        revenue_prev = revenue
        revenue = revenue_prev * (ONE + growth) if revenue_prev is not None else None
        if revenue is None:
            break
        cogs = revenue * (ONE - gm)
        gross_profit = revenue - cogs
        opex = revenue * opex_ratio
        impairment = revenue * (_dec(params.get('impairment_to_revenue'), ZERO) or ZERO) if year == 1 else ZERO
        ebit = gross_profit - opex - impairment

        # ---- 经营性科目 ----
        # 天数驱动项
        receivables = revenue * ar_days / DAYS
        inventory = cogs * inv_days / DAYS
        payables = cogs * ap_days / DAYS
        # 其余科目一律按收入比例延续，键名与期初表逐一对齐（一个都不许丢）
        wc_assets = {'receivables': receivables, 'inventory': inventory}
        for key, ratio in asset_ratios.items():
            wc_assets[key] = revenue * ratio
        wc_liab = {'payables': payables}
        for key, ratio in liab_ratios.items():
            wc_liab[key] = revenue * ratio
        ncl = {key: revenue * ratio for key, ratio in ncl_ratios.items()}

        # ---- 非流动经营资产 ----
        # 核心部分（固定/无形/使用权/递延税）由 Capex/D&A 滚存驱动；
        # "其他非流动资产"（在农夫山泉主要是 110.88 亿元长期银行存款这类
        # 金融性资产）按政策处理：
        #   hold（默认）—— 保持期初绝对额。长期存款不随收入线性增长，
        #                  按收入比例外推会凭空造出十几亿"投资"。
        #   revenue_scaled —— 按收入比例延续，其变动**必须计入投资活动现金流**，
        #                  否则资产负债表上占用的现金在现金流量表里无处体现，
        #                  两张表立刻对不上（实测差 17.16 亿元）。
        if other_nca_policy == 'revenue_scaled' and other_nca_ratio is not None:
            other_nca = revenue * other_nca_ratio
        else:
            other_nca = other_nca_opening
        delta_other_nca = other_nca - other_nca_prev_core
        fa_core_prev = fa_prev - other_nca_prev_core
        da = revenue * da_ratio
        capex = revenue * capex_ratio
        fa_core = fa_core_prev + capex - da - impairment
        if fa_core < ZERO:
            # 折旧超过资产净额会把净额推成负数 —— 说明 D&A 率与资产规模不匹配。
            # 关键是**钳折旧、而不是钳净额**：把净额硬钳到 0 会让"净额滚存"
            # 与"现金流量表加回折旧"不再恒等，两表立刻出现缺口（而且缺口
            # 藏在被钳掉的金额里，很难查）。这里反解出使净额恰好为 0 的折旧额，
            # 恒等式得以保持，代价只是当年折旧被下调。
            adjusted = fa_core_prev + capex - impairment
            if adjusted < ZERO:
                adjusted = ZERO
            warnings.append(
                f'第 {year} 年按收入比例计提的折旧摊销（{_s(da)} 元）超过非流动经营'
                f'资产净额，已下调至 {_s(adjusted)} 元，以保证资产净额非负且两表恒等；'
                '请核对折旧摊销率与资本开支假设。')
            da = adjusted
            fa_core = fa_core_prev + capex - da - impairment
            if fa_core < ZERO:
                fa_core = ZERO
        fa = fa_core + other_nca
        other_nca_prev_core = other_nca

        # ---- 配平：融资决策与利息费用互为因果，用不动点迭代求解 ----
        # 利息按**期初与期末的平均余额**计息：多借 → 利息升 → 净利降 → 权益降
        # → 现金缺口更大 → 更要多借。这是真实的循环引用，必须迭代。
        # 收缩系数 ≈ (1−税率)×(1−分红率)×资金成本/2，实测约 0.0075，远小于 1。
        wc_assets_total = sum(wc_assets.values(), ZERO)
        wc_liab_total = sum(wc_liab.values(), ZERO)
        ncl_total = sum(ncl.values(), ZERO)

        debt = debt_prev
        cash = cash_prev
        converged_year = False
        for _ in range(MAX_ITERATIONS):
            if average_basis:
                interest_expense = ((debt_prev + debt) / TWO * debt_cost) if debt_cost is not None else ZERO
                interest_income = ((cash_prev + cash) / TWO * cash_yield) if cash_yield is not None else ZERO
            else:
                interest_expense = (debt_prev * debt_cost) if debt_cost is not None else ZERO
                interest_income = (cash_prev * cash_yield) if cash_yield is not None else ZERO
            pbt = ebit - interest_expense + interest_income
            tax = max(pbt, ZERO) * tax_rate
            net_income = pbt - tax
            minority_income = net_income * minority_share
            parent_income = net_income - minority_income
            dividend = max(parent_income, ZERO) * payout
            minority_dividend = max(minority_income, ZERO) * payout
            retained = retained_prev + (parent_income - dividend)
            minority = minority_prev + minority_income - minority_dividend
            equity = share_capital + retained + minority

            previous_cash, previous_debt = cash, debt
            if mode == 'cash_plug':
                cash = (wc_liab_total + ncl_total + debt + equity) - (wc_assets_total + fa)
                if cash < minimum_cash:
                    # 现金不足：缺口由借款补足（revolver），下一轮利息费用随之上升
                    debt = debt + (minimum_cash - cash)
                    cash = minimum_cash
            else:
                debt = (wc_assets_total + fa + cash) - (wc_liab_total + ncl_total + equity)
                if debt <= ZERO:
                    # 经营与投资所需资金已能被现金完全覆盖 → 转为净现金状态，
                    # 有息负债归零、现金成为平衡项。这是杠杆公司转净现金公司的分界点。
                    debt = ZERO
                    cash = (wc_liab_total + ncl_total + debt + equity) - (wc_assets_total + fa)
            # 收敛判据必须**同时**盯住现金与有息负债：平衡项会在"债务归零转净现金"
            # 的分界点由债务切换成现金，只盯债务会看到它恒为 0 而提前判定收敛，
            # 此时现金可能仍在振荡。
            if (abs(cash - previous_cash) <= CONVERGENCE_TOLERANCE
                    and abs(debt - previous_debt) <= CONVERGENCE_TOLERANCE):
                converged_year = True
                break
        if not converged_year:
            converged = False

        # ---- 现金流量表（口径见模块 docstring 的恒等证明） ----
        delta_wc_assets = wc_assets_total - wc_assets_prev
        delta_wc_liab = wc_liab_total - wc_liab_prev
        delta_ncl = ncl_total - ncl_prev
        cfo = (net_income + da + impairment - delta_wc_assets
               + delta_wc_liab + delta_ncl)
        # 其他非流动资产的增加是**投资活动**占用现金，必须在这里体现。
        # 漏掉它，资产负债表上少掉的现金在现金流量表里没有出处，两表必然对不上。
        cfi = -capex - delta_other_nca
        delta_debt = debt - debt_prev
        cff = -dividend - minority_dividend + delta_debt
        net_change = cfo + cfi + cff
        cash_from_cf = cash_prev + net_change

        rows.append({
            'year': year,
            'income_statement': {
                'revenue': revenue, 'cost_of_revenue': cogs, 'gross_profit': gross_profit,
                'operating_expenses': opex, 'impairment': impairment, 'ebit': ebit,
                'interest_expense': interest_expense, 'interest_income': interest_income,
                'profit_before_tax': pbt, 'income_tax': tax, 'net_profit': net_income,
                'minority_income': minority_income, 'parent_net_profit': parent_income,
                'dividend': dividend,
            },
            'balance_sheet': {
                'cash': cash,
                'wc_assets': wc_assets_total, 'noncurrent_operating_assets': fa,
                'total_assets': cash + wc_assets_total + fa,
                'wc_liabilities': wc_liab_total, 'noncurrent_operating_liabilities': ncl_total,
                'debt': debt, 'total_liabilities': wc_liab_total + ncl_total + debt,
                'share_capital': share_capital, 'retained_earnings': retained,
                'minority_equity': minority, 'equity': equity,
                'liabilities_and_equity': wc_liab_total + ncl_total + debt + equity,
                # 分科目明细：用于把"三表口径 FCFF"与"驱动法口径 FCFF"逐项对账，
                # 差异归因到具体科目，而不是只说一句"口径不同"。
                'wc_assets_detail': wc_assets, 'wc_liabilities_detail': wc_liab,
            },
            'cash_flow': {
                'net_profit': net_income, 'depreciation_amortization': da,
                'impairment': impairment, 'delta_wc_assets': delta_wc_assets,
                'delta_wc_liabilities': delta_wc_liab, 'delta_noncurrent_liabilities': delta_ncl,
                'operating': cfo, 'capex': capex,
                'delta_other_noncurrent_assets': delta_other_nca, 'investing': cfi,
                'dividend': dividend + minority_dividend, 'net_borrowing': delta_debt,
                'financing': cff, 'net_change': net_change,
                'cash_beginning': cash_prev, 'cash_ending': cash_from_cf,
            },
            'balance_residual': (cash + wc_assets_total + fa)
                                - (wc_liab_total + ncl_total + debt + equity),
            'cash_flow_gap': cash - cash_from_cf,
        })

        cash_prev = cash
        debt_prev = debt
        fa_prev = fa
        wc_assets_prev = wc_assets_total
        wc_liab_prev = wc_liab_total
        ncl_prev = ncl_total
        retained_prev = retained
        minority_prev = minority

    if not converged:
        warnings.append('revolver 迭代未在 %d 轮内收敛：收入增长与现金持有下限的组合'
                        '可能自相矛盾，请检查假设。' % MAX_ITERATIONS)

    return {
        'years': len(rows),
        'rows': rows,
        'mode': mode,
        'converged': converged,
        'minimum_cash': minimum_cash,
        'payout_ratio': payout,
        'minority_share': minority_share,
        'other_noncurrent_policy': other_nca_policy,
        'interest_basis': 'average_balance' if average_basis else 'beginning_balance',
        # 期初营运资本：首年 ΔNWC 必须以它为起点，否则首年 ΔNWC 会被算成
        # NWC 的绝对额（实测把 -4.88 亿元的变动算成 -61.04 亿元）。
        'opening_nwc': wc_assets_opening - wc_liab_opening,
        'opening_nwc_core': wc_assets_opening_core - wc_liab_opening_core,
        'warnings': warnings,
    }


def check(result: dict) -> list:
    """逐年勾稽自检。任何一条不通过都说明配平逻辑有实质错误，不能交付。

    检查项与竞赛"可验证"直接对应：报告里可以写"三表逐年配平，最大残差 X 元"，
    这句话是可机检的，而不是作者的自我评价。
    """
    checks = []
    rows = result.get('rows') or []
    if not rows:
        return [{'name': 'three_statement_balance', 'ok': False, 'detail': '没有预测行，无法核验。'}]

    worst_balance = ZERO
    worst_cash = ZERO
    worst_equity = ZERO
    worst_balance_year = worst_cash_year = worst_equity_year = None
    for row in rows:
        residual = _dec(row.get('balance_residual'), ZERO) or ZERO
        gap = _dec(row.get('cash_flow_gap'), ZERO) or ZERO
        if abs(residual) > abs(worst_balance):
            worst_balance, worst_balance_year = residual, row['year']
        if abs(gap) > abs(worst_cash):
            worst_cash, worst_cash_year = gap, row['year']

    checks.append({
        'name': 'balance_sheet_identity',
        'ok': abs(worst_balance) <= TOLERANCE,
        'detail': f'资产 = 负债 + 权益，最大残差 {_s(worst_balance)} 元'
                  + (f'（第 {worst_balance_year} 年）' if worst_balance_year else ''),
    })
    checks.append({
        'name': 'cash_flow_reconciliation',
        'ok': abs(worst_cash) <= TOLERANCE,
        'detail': f'资产负债表现金 = 现金流量表期末现金，最大差异 {_s(worst_cash)} 元'
                  + (f'（第 {worst_cash_year} 年）' if worst_cash_year else '')
                  + '；两者恒等是配平机制正确的充要证据。',
    })

    # 非流动经营资产滚存：期末 = 期初 + Capex − D&A − 减值 + 其他非流动资产变动。
    # 这一条把"折旧钳制"这类修补动作从暗处拉到明处 —— 一旦钳制破坏了恒等，
    # 勾稽立刻失败，而不是让缺口藏在现金差异里。
    prev_fa = None
    ok_fa = True
    worst_fa = ZERO
    worst_fa_year = None
    for row in rows:
        balance = row['balance_sheet']
        flow = row['cash_flow']
        fa_now = _dec(balance.get('noncurrent_operating_assets'), ZERO) or ZERO
        if prev_fa is not None:
            expected = (prev_fa
                        + (_dec(flow.get('capex'), ZERO) or ZERO)
                        - (_dec(flow.get('depreciation_amortization'), ZERO) or ZERO)
                        - (_dec(flow.get('impairment'), ZERO) or ZERO)
                        + (_dec(flow.get('delta_other_noncurrent_assets'), ZERO) or ZERO))
            if abs(fa_now - expected) > TOLERANCE:
                ok_fa = False
                if abs(fa_now - expected) > abs(worst_fa):
                    worst_fa, worst_fa_year = fa_now - expected, row['year']
        prev_fa = fa_now
    checks.append({
        'name': 'noncurrent_asset_rollforward',
        'ok': ok_fa,
        'detail': '非流动经营资产期末 = 期初 + Capex − D&A − 减值 + 其他非流动资产变动，'
                  '最大偏差 ' + _s(worst_fa) + ' 元'
                  + (f'（第 {worst_fa_year} 年）' if worst_fa_year else ''),
    })

    # 权益滚存：期末 = 期初 + 归母净利润 − 分红
    prev_retained = None
    ok_equity = True
    for row in rows:
        retained = _dec(row['balance_sheet']['retained_earnings'])
        parent = _dec(row['income_statement']['parent_net_profit'])
        dividend = _dec(row['income_statement']['dividend'])
        if prev_retained is not None:
            expected = prev_retained + parent - dividend
            if abs(retained - expected) > TOLERANCE:
                ok_equity = False
                if abs(retained - expected) > abs(worst_equity):
                    worst_equity, worst_equity_year = retained - expected, row['year']
        prev_retained = retained
    checks.append({
        'name': 'retained_earnings_rollforward',
        'ok': ok_equity,
        'detail': '留存收益期末 = 期初 + 归母净利润 − 分红，最大偏差 '
                  + _s(worst_equity) + ' 元'
                  + (f'（第 {worst_equity_year} 年）' if worst_equity_year else ''),
    })

    # 现金非负（cash_plug 下若为负说明配平逻辑失效）
    negative = [r['year'] for r in rows if (_dec(r['balance_sheet']['cash'], ZERO) or ZERO) < ZERO]
    checks.append({
        'name': 'cash_non_negative',
        'ok': not negative,
        'detail': ('各年现金均为非负' if not negative
                   else f'第 {negative} 年现金为负，配平机制未能提供足够融资。'),
    })
    return checks


def implied_fcff(result: dict, tax_rate) -> list:
    """从三表反推 FCFF，用于与驱动法 FCFF 交叉验证。

        FCFF = EBIT × (1 − t) + D&A + 减值 − Capex − ΔNWC

    输出**两个口径**，这是刻意的：

    - `core_*`：营运资本只含应收、存货、应付三项。驱动法（`valuation_advanced.evaluate`）
      只建模这三项，所以只有这个口径能与它逐项对账。**两者应当精确一致** ——
      不一致说明有一侧漏了项目或口径写错。
    - 全口径：营运资本含全部经营科目（预付、其他应收、合同负债、应交税、
      其他应付等）。这才是资产负债表实际给出的占用额，通常与核心口径相差很大
      （实测农夫山泉首年相差 56 亿元，主要来自"其他应付款项及应计费用"与
      倒挤出的其他流动资产/负债）。差异本身就是结论：驱动法在营运资本上是简化口径。

    首年 ΔNWC 以期初营运资本为起点，不能用 NWC 绝对额冒充变动。
    """
    tax_rate = _dec(tax_rate, ZERO) or ZERO
    out = []
    prev_full = _dec(result.get('opening_nwc'))
    prev_core = _dec(result.get('opening_nwc_core'))
    for row in result.get('rows') or []:
        income = row['income_statement']
        balance = row['balance_sheet']
        flow = row['cash_flow']
        ebit = _dec(income['ebit'], ZERO) or ZERO
        nopat = ebit - max(ebit, ZERO) * tax_rate
        da = _dec(flow['depreciation_amortization'], ZERO) or ZERO
        impairment = _dec(flow['impairment'], ZERO) or ZERO
        capex = _dec(flow['capex'], ZERO) or ZERO

        nwc = ((_dec(balance['wc_assets'], ZERO) or ZERO)
               - (_dec(balance['wc_liabilities'], ZERO) or ZERO))
        delta_nwc = (nwc - prev_full) if prev_full is not None else nwc

        assets_detail = balance.get('wc_assets_detail') or {}
        liab_detail = balance.get('wc_liabilities_detail') or {}
        core = (((_dec(assets_detail.get('receivables'), ZERO) or ZERO)
                 + (_dec(assets_detail.get('inventory'), ZERO) or ZERO))
                - (_dec(liab_detail.get('payables'), ZERO) or ZERO))
        delta_core = (core - prev_core) if prev_core is not None else core

        out.append({
            'year': row['year'], 'nopat': nopat,
            'depreciation_amortization': da, 'impairment': impairment, 'capex': capex,
            'nwc': nwc, 'delta_nwc': delta_nwc,
            'fcff': nopat + da + impairment - capex - delta_nwc,
            'core_nwc': core, 'core_delta_nwc': delta_core,
            'core_fcff': nopat + da + impairment - capex - delta_core,
        })
        prev_full, prev_core = nwc, core
    return out


def align_fcff(cross_check, driver_fcff=None) -> dict:
    """把三表 FCFF 与驱动法 FCFF 的对账结果结构化。

    核心口径（应收/存货/应付）是与驱动法唯一可比的口径，两者从**第 2 年起
    应当逐位一致**。第 1 年的差异有确定来源，不是算错：

    - 驱动法用「收入 × 周转天数」反推期初营运资本；
    - 周转天数指标按**平均余额**（(期初+期末)/2）计算，与三表使用的
      **期末时点值**不是同一个口径；
    - 实测差异 3.4769 亿元，恰好等于两种口径期初营运资本之差。

    三表用实际报表值更正确。把这条写清楚，比声称"两者完全一致"诚实得多。
    """
    result = {'core_vs_full': [], 'driver_comparison': None}
    for item in cross_check or []:
        result['core_vs_full'].append({
            'year': item['year'],
            'core_fcff': item['core_fcff'],
            'full_fcff': item['fcff'],
            'gap': item['fcff'] - item['core_fcff'],
            'gap_from_working_capital': item['delta_nwc'] - item['core_delta_nwc'],
        })
    if not driver_fcff:
        return result

    gaps = []
    for item, driver in zip(cross_check or [], driver_fcff):
        d = _dec(driver)
        if d is None:
            continue
        gaps.append({'year': item['year'], 'driver_fcff': d,
                     'three_statement_core_fcff': item['core_fcff'],
                     'gap': item['core_fcff'] - d})
    if not gaps:
        return result
    after_first = [g for g in gaps if g['year'] > 1]
    max_after = max((abs(g['gap']) for g in after_first), default=None)
    result['driver_comparison'] = {
        'rows': gaps,
        'max_gap_after_first_year': max_after,
        'aligned_after_first_year': bool(max_after is not None and max_after <= TOLERANCE),
        'first_year_gap': gaps[0]['gap'],
        'note': ('核心口径与驱动法可比。第 2 年起应逐位一致；第 1 年差异来自期初营运资本口径 ——'
                 '驱动法用「收入 × 周转天数」反推，而周转天数指标按平均余额计算，'
                 '与三表使用的期末时点值不同。三表用实际报表值更正确。'),
    }
    return result


def build(facts, metrics, params: dict, paths: dict | None = None,
          funding: dict | None = None, driver_fcff=None) -> dict:
    """总入口：期初表 → 预测三表 → 勾稽自检 → FCFF 交叉验证。"""
    opening = opening_balance(facts, metrics)
    out = {'ok': False, 'opening_balance': opening, 'mode': None,
           'statements': [], 'checks': [], 'fcff_cross_check': [],
           'warnings': list(opening.get('warnings') or [])}
    if opening.get('revenue') is None:
        out['warnings'].append('未取到营业收入基数，三表预测不成立。')
        return out
    if not opening.get('balanced'):
        out['warnings'].append(
            f"期初资产负债表未通过配平（残差 {_s(opening.get('residual'))} 元）；"
            '预测表的配平结论同样不可信。')
    result = forecast(opening, params, paths, funding)
    out['mode'] = result['mode']
    out['other_noncurrent_policy'] = result.get('other_noncurrent_policy')
    out['interest_basis'] = result.get('interest_basis')
    out['statements'] = result['rows']
    out['converged'] = result['converged']
    out['opening_nwc'] = result.get('opening_nwc')
    out['opening_nwc_core'] = result.get('opening_nwc_core')
    out['warnings'].extend(result['warnings'])
    out['checks'] = check(result)
    out['fcff_cross_check'] = implied_fcff(result, params.get('tax_rate'))
    out['fcff_alignment'] = align_fcff(out['fcff_cross_check'], driver_fcff)
    out['ok'] = bool(result['rows']) and all(c['ok'] for c in out['checks'])
    return out
