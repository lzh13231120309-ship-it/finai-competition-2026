"""财务法证规则引擎：把「勾稽通过的标准事实」翻译成「按影响度排序的待核验风险」。

为什么单独做一层：
- 提取层（financial_analysis）只负责把数字变准，不负责判断"这个数字意味着什么"。
- 判断必须可解释、可复现、可反驳，所以规则全部写成确定性代码，不含 LLM 参与取数。
- 每条发现都要同时给出：支持证据、**反证检索方向**、未解问题、估值传导路径。
  只给支持证据的规则会把分析师带进确认偏误，这正是通用大模型读年报最大的问题。

分数定义（与竞赛方案一致，仅用于排序，不等同于舞弊概率）::

    risk_score = 0.30 * magnitude            # 偏离阈值的幅度
               + 0.25 * persistence          # 连续发生的期数
               + 0.20 * peer_extremeness     # 相对同行的极端程度
               + 0.15 * cash_impact          # 是否直接侵蚀现金流
               + 0.10 * disclosure_uncertainty  # 证据链是否薄弱

五个分项各自归一到 0–100。没有同行数据时 peer_extremeness 取 50 并在结果里
标记 `peer_data_missing`，绝不用 0 伪造"无异常"。

所有金额是 Decimal 字符串（单位：元），比例是分数（0.2 = 20%）。
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal, InvalidOperation

from .financial_analysis import _key, _rounding, _valid

# ---------------------------------------------------------------- 规则元数据

RULE_META = {
    'R_AR_01': ('应收账款增速显著快于收入且周转恶化', '收入质量'),
    'R_INV_01': ('存货增速显著快于收入，存在积压与减值风险', '存货质量'),
    'R_CFO_01': ('利润与经营现金流背离，盈利现金含量不足', '利润现金匹配'),
    'R_MARGIN_01': ('毛利率同比大幅变动且缺少可验证解释', '毛利异常'),
    'R_NREC_01': ('非经常性损益支撑利润，主营盈利能力弱', '非经常项目'),
    'R_GW_01': ('商誉占净资产比重高且盈利能力下滑', '资产减值'),
    'R_DEBT_01': ('短期债务超过货币资金且缺口扩大', '偿债与流动性'),
    'R_SETTLE_01': ('销售收现比偏低，收入含金量不足', '收入质量'),
    'R_EXP_01': ('期间费用率异常下降，存在费用递延嫌疑', '费用与资本化'),
    'R_OCF_01': ('收入增长但经营现金流为负', '利润现金匹配'),
    'R_CAP_01': ('资本开支强度异常上升，存在费用资本化或激进扩张', '投资与资本化'),
    'R_RPT_01': ('其他应收款与预付款项异常膨胀，警惕关联方资金占用', '关联方与资金占用'),
    'R_DISC_01': ('关键科目披露不完整，指标与勾稽基础不足', '披露质量'),
    'R_TIEOUT_01': ('报表内部勾稽不成立', '会计一致性'),
}

# 每条规则的估值传导路径：风险怎么进模型，是"风险联动估值"的落点
VALUATION_LINKS = {
    'R_AR_01': ['应收账款周转天数 → 营运资本占用 → FCFF 下降'],
    'R_INV_01': ['存货周转天数 → 营运资本占用 + 存货跌价 → FCFF 与 EBIT 双降'],
    'R_CFO_01': ['经营现金流质量 → 折现率风险溢价 / 终值增长率下调'],
    'R_MARGIN_01': ['毛利率 → EBIT → 终值'],
    'R_NREC_01': ['可持续净利润 → 估值基数下调（扣非口径）'],
    'R_GW_01': ['商誉减值 → 非经常损失 → 净资产与 PB'],
    'R_DEBT_01': ['净债务 → EV 转权益价值 → 利息费用'],
    'R_SETTLE_01': ['收现比 → 营运资本 → FCFF'],
    'R_EXP_01': ['费用化比例上调 → 当期利润下降'],
    'R_OCF_01': ['经营现金流 → 自由现金流 → DCF 近端'],
    'R_CAP_01': ['资本开支 / 收入 → FCFF 分子与折现期假设'],
    'R_RPT_01': ['体外资金占用 → 可分配利润低于账面 → 估值基数与治理折价'],
    'R_DISC_01': ['关键科目缺失 → 全部下游指标与勾稽的可信度降级'],
    'R_TIEOUT_01': ['数据可信度 → 全部下游计算结果降级'],
}

# 反证检索方向：每条规则都强制带一份，避免只找支持自己的证据
COUNTER_GUIDANCE = {
    'R_AR_01': ['业务季节性（旺季集中在期末）', '结算方式变化（现款转月结）', '票据重分类（应收票据/应收款项融资）',
                '并购或新客户导致合并范围变化', '信用政策放宽但有明确合同依据'],
    'R_INV_01': ['为预期订单提前备货且有在手订单支持', '原材料价格上涨导致的战略性囤货',
                 '产品结构调整导致单位成本上升', '合并范围变化'],
    'R_CFO_01': ['收入和回款存在跨期错配（如政府客户年末结算）', '票据结算比例上升',
                 '大额预付款或押金占用', '季节性因素需看全年而非单季'],
    'R_MARGIN_01': ['产品结构升级且均价可验证', '原材料价格下行传导', '产能利用率提升摊薄固定成本',
                    '会计政策或成本归集口径变化'],
    'R_NREC_01': ['非经常损益来自可持续的政府补助（产业政策支持）', '处置收益一次性但有明确披露'],
    'R_GW_01': ['被并购主体业绩承诺期末且已充分计提', '商誉对应资产组现金流预测有第三方评估支持'],
    'R_DEBT_01': ['期末存在未使用的银行授信额度', '债务期限结构在期后已置换为长期',
                  '受限资金有明确披露但仍有可用额度'],
    'R_SETTLE_01': ['销售模式以票据或分期结算为主', '预收款模式导致收现比天然偏低',
                    '收入确认时点与实际收款存在制度性错配'],
    'R_EXP_01': ['费用下降来自规模效应或降本措施（有明确说明）', '会计政策变更（如研发资本化）需检查政策披露'],
    'R_OCF_01': ['收入以票据结算未收到现金', '处于产能扩张期需要铺底营运资金'],
    'R_CAP_01': ['处于行业景气周期中的产能扩张期（有募投项目与进度披露）',
                 '在建工程集中转固导致折旧基数跳升', '并购整合带来的资产重估'],
    'R_RPT_01': ['集团内正常资金池调剂且已披露', '已按期偿还的历史往来款',
                 '预付款项对应真实的大宗采购锁价条约'],
    'R_DISC_01': ['该主体按简化披露要求列报', '关键科目在附注或管理层讨论中另有说明',
                  '报告为摘要或摘录而非完整年报'],
    'R_TIEOUT_01': ['四舍五入差异在容差内', '重述口径未统一', '合并与母公司口径混用'],
}

OPEN_QUESTIONS = {
    'R_AR_01': ['应收账款账龄结构如何？一年以上占比是否上升？', '前五大欠款方是否与收入增长来源一致？',
                '期末后回款情况（期后事项）如何？'],
    'R_INV_01': ['存货构成中在产品/库存商品占比变化如何？', '存货跌价准备计提比例是否同步上升？',
                 '在手订单能否覆盖期末存货？'],
    'R_CFO_01': ['营运资本各科目对经营现金流的贡献是多少？', '是否存在期末突击回款或期初集中支付？',
                 '连续几期的现金含量趋势如何？'],
    'R_MARGIN_01': ['毛利率变动的量价拆解是什么？', '同行同期毛利率方向是否一致？',
                    '成本归集口径或会计政策是否发生变化？'],
    'R_NREC_01': ['非经常性损益明细的具体构成与金额？', '其中哪些项目在未来三年内可延续？'],
    'R_GW_01': ['商誉对应的资产组是哪些？历史减值测试假设是否变化？', '被并购主体实际经营业绩与承诺的差距？'],
    'R_DEBT_01': ['受限货币资金金额是多少？', '未使用授信额度与到期结构如何？'],
    'R_SETTLE_01': ['销售商品收到现金与收入的差额由哪些科目占用？', '应收票据及应收款项融资的变动如何？'],
    'R_EXP_01': ['费用率下降是收入基数效应还是绝对额下降？', '是否伴随会计政策或费用资本化变化？'],
    'R_OCF_01': ['经营性应收项目的增加金额是多少？', '预付款项是否异常增加？'],
    'R_CAP_01': ['资本开支的具体投向与在建工程进度如何？', '研发支出资本化比例是否变化？',
                 '资本开支与折旧摊销的长期比值是否可持续？'],
    'R_RPT_01': ['其他应收款的前五大对象是谁？是否与控股股东或关联方相关？',
                 '预付款项对应的供应商是否存在关联关系？', '是否有非经营性资金占用的决议与还款安排？'],
    'R_DISC_01': ['缺失的科目是否能在附注中补全？', '该报告是否只是摘要版？',
                  '缺失是否导致关键勾稽无法验证？'],
    'R_TIEOUT_01': ['差额能否用某一个科目的单位口径变化解释？', '是否存在重述但未标记的期间？'],
}

CASH_IMPACT_RULES = {'R_AR_01', 'R_INV_01', 'R_CFO_01', 'R_SETTLE_01', 'R_OCF_01', 'R_DEBT_01',
                     'R_CAP_01', 'R_RPT_01'}

# 规则 → 同行可比指标。
#
# 为什么必须显式列表：`peer_benchmark` 的输出是按**指标代码**索引的
# （gross_margin / inventory_days …），而 `_peer_score` 的调用方只知道
# **规则号**（R_MARGIN_01 …）。两边键不同名，早期版本里 `self.peer.get(rule_id)`
# 永远取不到值，于是 peer_extremeness 恒定返回中性 50 分 —— 表面上"接了同行数据"，
# 实际上同行分位对风险排序毫无影响，同行消融实验也就无从做起。
#
# 只映射**方向一致**的指标：observed 偏低代表风险时，映射到"越低越差"的指标
# （如 cash_conversion、current_ratio）；observed 偏高代表风险时，映射到
# "越高越差"的指标（如 receivables_days、inventory_days）。方向相反的指标不硬凑，
# 宁可不给同行分位（进 peer_data_missing），也不给出反向的伪信号。
RULE_PEER_METRIC = {
    'R_AR_01': 'receivables_days',        # 应收恶化 → 应收周转天数高于同行
    'R_INV_01': 'inventory_days',         # 存货积压 → 存货周转天数高于同行
    'R_CFO_01': 'cash_conversion',        # 盈利现金含量 → 低于同行
    'R_MARGIN_01': 'gross_margin',        # 毛利异常 → 偏离同行区间（双侧）
    'R_DEBT_01': 'current_ratio',         # 短债缺口 → 流动比率低于同行
    'R_SETTLE_01': 'operating_cash_margin',   # 收现比低 → 经营现金利润率低于同行
    'R_OCF_01': 'operating_cash_margin',      # 经营现金流恶化 → 同上
    # 期间费用率异常低会直接抬高净利率，因此同行净利率是它的自然对照：
    # 如果净利率同时显著高于同行，说明"费用下降"更可能来自会计处理而非真实降本。
    'R_EXP_01': 'net_margin',
}


# ---------------------------------------------------------------- 工具函数

def _dec(value):
    """把任意输入安全转成 Decimal；不可解析返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _excess_score(observed, threshold, span):
    """偏离阈值一个 span 记满分 100，只算超出阈值的部分。"""
    excess = abs(observed - threshold)
    return _clip(float(excess / span) * 100.0) if span else 0.0


def _severity(score: float) -> str:
    if score >= 75:
        return 'critical'
    if score >= 55:
        return 'high'
    if score >= 35:
        return 'medium'
    return 'low'


def _s(value) -> str:
    """输出用字符串，保证 JSON 可序列化且不丢精度。"""
    if value is None:
        return ''
    if isinstance(value, Decimal):
        normalized = value.normalize()
        text = format(normalized, 'f')
        return text if text != '-0' else '0'
    return str(value)


def _prev_year(period: str) -> str:
    try:
        d = date.fromisoformat(period)
    except (TypeError, ValueError):
        return ''
    return f'{d.year - 1}-{d.month:02d}-{min(d.day, calendar.monthrange(d.year - 1, d.month)[1]):02d}'


# ---------------------------------------------------------------- 核心引擎

class ForensicEngine:
    """在一份（或多份）已核验财报事实上跑全部法证规则。"""

    def __init__(self, facts, checks=None, metrics=None, peer=None, top_k=10):
        self.raw_facts = list(facts or [])
        self.checks = list(checks or [])
        self.metrics = list(metrics or [])
        self.peer = peer or {}           # {code: {'median':..,'p25':..,'p75':..,'n':..}}
        self.top_k = max(int(top_k or 10), 1)
        self.index: dict[tuple, dict] = {}
        self.groups: list[tuple] = []
        for f in self.raw_facts:
            if _valid(f):
                self.index.setdefault(_key(f), f)
        self.groups = sorted({_key(f)[:5] for f in self.index.values()})

    # -------------------------------------------------- 取值

    def get(self, group, code, period=None, kind=None):
        company, end, duration, scope, currency = group
        return self.index.get((company, period or end, kind or duration, scope, currency, code))

    def amount(self, fact):
        return _dec(fact.get('value')) if fact else None

    def ratio_of(self, group, num_code, den_code, num_kind=None, den_kind=None, period=None):
        """返回 (比例 Decimal|None, 依赖 facts)。"""
        num = self.get(group, num_code, period, num_kind)
        den = self.get(group, den_code, period, den_kind)
        if not num or not den:
            return None, [f for f in (num, den) if f]
        a, b = self.amount(num), self.amount(den)
        if a is None or b is None or b == 0:
            return None, [num, den]
        return a / b, [num, den]

    def growth(self, group, code, kind=None):
        """同比增速；上年基数 ≤ 0 时返回 None（不制造无意义的百分比）。"""
        cur = self.get(group, code, None, kind)
        prev = self.get(group, code, _prev_year(group[1]), kind)
        if not cur or not prev:
            return None, [f for f in (cur, prev) if f]
        a, b = self.amount(cur), self.amount(prev)
        if a is None or b is None or b <= 0:
            return None, [f for f in (cur, prev) if f]
        return (a - b) / b, [cur, prev]

    def metric_value(self, group, code):
        """从 analyze_financials 产出的指标里取已算好的值。"""
        company, end, duration, scope, currency = group
        for m in self.metrics:
            if (m.get('code') == code and m.get('company') == company and m.get('period') == end
                    and m.get('scope') == scope and m.get('currency') == currency):
                return _dec(m.get('value'))
        return None

    # -------------------------------------------------- 单条规则

    def _r_ar01(self, group):
        """R_AR_01 应收增速 - 收入增速 > 20pp。"""
        rev_g, rev_f = self.growth(group, 'revenue')
        ar_g, ar_f = self.growth(group, 'receivables', kind='instant')
        if rev_g is None or ar_g is None:
            return None
        gap = ar_g - rev_g
        if gap <= Decimal('0.20'):
            return None
        days_now, _ = self.ratio_of(group, 'receivables', 'revenue', 'instant')
        return {'observed': gap, 'threshold': Decimal('0.20'),
                'magnitude': _excess_score(gap, Decimal('0.20'), Decimal('0.30')),
                'deps': rev_f + ar_f,
                'detail': f'应收账款同比 {_pct(ar_g)}，营业收入同比 {_pct(rev_g)}，差值 {_pct(gap)} 个百分点'}

    def _r_inv01(self, group):
        """R_INV_01 存货增速 - 收入增速 > 20pp。"""
        rev_g, rev_f = self.growth(group, 'revenue')
        inv_g, inv_f = self.growth(group, 'inventory', kind='instant')
        if rev_g is None or inv_g is None:
            return None
        gap = inv_g - rev_g
        if gap <= Decimal('0.20'):
            return None
        return {'observed': gap, 'threshold': Decimal('0.20'),
                'magnitude': _excess_score(gap, Decimal('0.20'), Decimal('0.30')),
                'deps': rev_f + inv_f,
                'detail': f'存货同比 {_pct(inv_g)}，营业收入同比 {_pct(rev_g)}，差值 {_pct(gap)} 个百分点'}

    def _r_cfo01(self, group):
        """R_CFO_01 净利润为正但经营现金流/净利润 < 0.8。"""
        profit = self.get(group, 'net_profit')
        cfo = self.get(group, 'operating_cash_flow')
        if not profit or not cfo:
            return None
        p, c = self.amount(profit), self.amount(cfo)
        if p is None or c is None or p <= 0:
            return None
        conversion = c / p
        threshold = Decimal('0.8')
        if conversion >= threshold:
            return None
        return {'observed': conversion, 'threshold': threshold,
                'magnitude': _excess_score(conversion, threshold, Decimal('0.5')),
                'deps': [profit, cfo],
                'detail': f'净利润 {_s(p)} 元，经营现金流 {_s(c)} 元，现金含量 {conversion:.2f}（阈值 0.80）'}

    def _r_margin01(self, group):
        """R_MARGIN_01 毛利率同比变动绝对值 > 5 个百分点。"""
        gm = self.metric_value(group, 'gross_margin')
        prev_group = self._prev_group(group)
        gm_prev = self.metric_value(prev_group, 'gross_margin') if prev_group else None
        if gm is None or gm_prev is None:
            return None
        delta = gm - gm_prev
        if abs(delta) <= Decimal('0.05'):
            return None
        rev = self.get(group, 'revenue')
        cost = self.get(group, 'cost_of_revenue')
        return {'observed': delta, 'threshold': Decimal('0.05'),
                'magnitude': _excess_score(delta, Decimal('0.05'), Decimal('0.10')),
                'deps': [f for f in (rev, cost) if f],
                'detail': f'毛利率由 {_pct(gm_prev)} 变为 {_pct(gm)}，变动 {_pct(delta)} 个百分点'}

    def _r_nrec01(self, group):
        """R_NREC_01 非经常性损益占归母净利润 > 30%。"""
        parent = self.get(group, 'parent_net_profit')
        adjusted = self.get(group, 'nonrecurring_adj_parent_profit')
        if not parent or not adjusted:
            return None
        p, a = self.amount(parent), self.amount(adjusted)
        if p is None or a is None or p <= 0:
            return None
        share = (p - a) / p
        if share <= Decimal('0.30'):
            return None
        return {'observed': share, 'threshold': Decimal('0.30'),
                'magnitude': _excess_score(share, Decimal('0.30'), Decimal('0.30')),
                'deps': [parent, adjusted],
                'detail': f'归母净利润 {_s(p)} 元，扣非后 {_s(a)} 元，非经常性贡献占比 {_pct(share)}'}

    def _r_gw01(self, group):
        """R_GW_01 商誉/归母净资产 > 30% 且归母净利润同比下滑。"""
        goodwill = self.get(group, 'goodwill', kind='instant')
        equity = self.get(group, 'parent_equity', kind='instant')
        if not goodwill or not equity:
            return None
        g, e = self.amount(goodwill), self.amount(equity)
        if g is None or e is None or e <= 0:
            return None
        share = g / e
        if share <= Decimal('0.30'):
            return None
        profit_g, profit_f = self.growth(group, 'parent_net_profit')
        if profit_g is None or profit_g >= 0:
            return None
        return {'observed': share, 'threshold': Decimal('0.30'),
                'magnitude': _excess_score(share, Decimal('0.30'), Decimal('0.30')),
                'deps': [goodwill, equity] + profit_f,
                'detail': f'商誉 {_s(g)} 元占归母净资产 {_pct(share)}，且归母净利润同比 {_pct(profit_g)}'}

    def _r_debt01(self, group):
        """R_DEBT_01 短期借款 > 货币资金。"""
        if group[2] != 'instant':      # 纯资产负债表规则，只在时点组上跑一次，避免重复命中
            return None
        debt = self.get(group, 'short_term_debt', kind='instant')
        cash = self.get(group, 'cash', kind='instant')
        if not debt or not cash:
            return None
        d, c = self.amount(debt), self.amount(cash)
        if d is None or c is None or c <= 0:
            return None
        gap_ratio = d / c
        if gap_ratio <= 1:
            return None
        return {'observed': gap_ratio, 'threshold': Decimal('1'),
                'magnitude': _excess_score(gap_ratio, Decimal('1'), Decimal('1')),
                'deps': [debt, cash],
                'detail': f'短期借款 {_s(d)} 元，货币资金 {_s(c)} 元，覆盖倍数 {gap_ratio:.2f}（<1 为缺口）'}

    def _r_settle01(self, group):
        """R_SETTLE_01 销售收现比 < 0.90。"""
        ratio, deps = self.ratio_of(group, 'cash_from_sales', 'revenue')
        if ratio is None or ratio >= Decimal('0.90'):
            return None
        return {'observed': ratio, 'threshold': Decimal('0.90'),
                'magnitude': _excess_score(ratio, Decimal('0.90'), Decimal('0.30')),
                'deps': deps,
                'detail': f'销售商品收到现金 / 营业收入 = {ratio:.3f}（低于 0.90）'}

    def _r_exp01(self, group):
        """R_EXP_01 研发+销售+管理费用率同比下降超过 2 个百分点。"""
        rate, deps = self._expense_ratio(group)
        prev_group = self._prev_group(group)
        prev_rate, prev_deps = self._expense_ratio(prev_group) if prev_group else (None, [])
        if rate is None or prev_rate is None:
            return None
        delta = rate - prev_rate
        if delta >= Decimal('-0.02'):
            return None
        return {'observed': delta, 'threshold': Decimal('-0.02'),
                'magnitude': _excess_score(delta, Decimal('-0.02'), Decimal('0.05')),
                'deps': deps + prev_deps,
                'detail': f'期间费用率由 {_pct(prev_rate)} 降至 {_pct(rate)}，下降 {_pct(-delta)} 个百分点'}

    def _expense_ratio(self, group):
        revenue = self.get(group, 'revenue')
        if not revenue:
            return None, []
        total = Decimal(0)
        used = []
        for code in ('research_expenses', 'selling_expenses', 'administrative_expenses'):
            f = self.get(group, code)
            if f:
                v = self.amount(f)
                if v is not None:
                    total += v
                    used.append(f)
        if not used:
            return None, []
        rev = self.amount(revenue)
        if rev is None or rev <= 0:
            return None, used
        return total / rev, used + [revenue]

    def _r_ocf01(self, group):
        """R_OCF_01 收入同比增长但经营现金流为负。"""
        cfo = self.get(group, 'operating_cash_flow')
        if not cfo:
            return None
        c = self.amount(cfo)
        if c is None or c >= 0:
            return None
        rev_g, rev_f = self.growth(group, 'revenue')
        if rev_g is None or rev_g <= 0:
            return None
        return {'observed': rev_g, 'threshold': Decimal('0'),
                'magnitude': _excess_score(rev_g, Decimal('0'), Decimal('0.20')),
                'deps': [cfo] + rev_f,
                'detail': f'营业收入同比 {_pct(rev_g)}，但经营现金流为 {_s(c)} 元'}

    def _sum_instant(self, group, codes):
        """把若干时点科目加总；一项都取不到时返回 None（不假装是 0）。"""
        total = Decimal(0)
        used = []
        for code in codes:
            f = self.get(group, code, None, 'instant')
            if not f:
                continue
            v = self.amount(f)
            if v is None:
                continue
            total += v
            used.append(f)
        return (total, used) if used else (None, [])

    def _r_cap01(self, group):
        """R_CAP_01 资本开支强度异常上升。

        两个独立信号，命中任一即可：
        · 资本开支 / 营业收入 比去年同期上升超过 5 个百分点 —— 投资强度跳变；
        · 资本开支 / 折旧摊销 > 4 倍 —— 投资远超维持性支出，
          可能是真实扩张，也可能是把费用包装成资产以平滑利润。
        """
        capex = self.get(group, 'capex')
        if not capex:
            return None
        ratio, deps = self.ratio_of(group, 'capex', 'revenue')
        if ratio is None:
            return None
        prev_group = self._prev_group(group)
        prev_ratio, prev_deps = (self.ratio_of(prev_group, 'capex', 'revenue')
                                 if prev_group else (None, []))
        da_total, da_deps = Decimal(0), []
        for code in ('depreciation', 'amortization'):
            f = self.get(group, code)
            v = self.amount(f) if f else None
            if v is not None and v > 0:
                da_total += v
                da_deps.append(f)
        spike = (ratio - prev_ratio) if prev_ratio is not None else None
        to_da = (self.amount(capex) / da_total) if da_total > 0 else None

        if spike is not None and spike > Decimal('0.05'):
            return {'observed': spike, 'threshold': Decimal('0.05'),
                    'magnitude': _excess_score(spike, Decimal('0.05'), Decimal('0.15')),
                    'deps': deps + prev_deps,
                    'detail': f'资本开支 / 营业收入由 {_pct(prev_ratio)} 升至 {_pct(ratio)}，'
                              f'上升 {_pct(spike)} 个百分点'}
        if to_da is not None and to_da > Decimal('4'):
            return {'observed': to_da, 'threshold': Decimal('4'),
                    'magnitude': _excess_score(to_da, Decimal('4'), Decimal('6')),
                    'deps': deps + da_deps,
                    'detail': f'资本开支 / 折旧摊销 = {to_da:.2f} 倍（高于 4 倍），'
                              '投资强度远超维持性支出'}
        return None

    def _r_rpt01(self, group):
        """R_RPT_01 其他应收款与预付款项异常膨胀（关联方资金占用的代理信号）。

        为什么用代理信号：年报正文不总是把「关联方资金占用」做成可直接取数的科目，
        它通常散在附注的往来款明细里。但「其他应收款 + 预付款项」这两项合计
        增速远快于收入、且在总资产里占到可观比重，是资金体外循环的典型外观 ——
        方向明确、可复算，因此作为筛查起点而不是结论。
        """
        now, deps_now = self._sum_instant(group, ('other_receivables', 'prepayments'))
        if now is None:
            return None
        prev_group = self._prev_group(group)
        prev, deps_prev = (self._sum_instant(prev_group, ('other_receivables', 'prepayments'))
                           if prev_group else (None, []))
        rev_g, rev_f = self.growth(group, 'revenue')
        if prev is None or prev <= 0 or rev_g is None:
            return None
        growth = (now - prev) / prev
        gap = growth - rev_g
        if gap <= Decimal('0.30'):
            return None
        assets = self.get(group, 'assets', None, 'instant')
        share = None
        share_deps = []
        if assets:
            assets_v = self.amount(assets)
            if assets_v and assets_v > 0:
                share = now / assets_v
                share_deps = [assets]
        detail = (f'其他应收款 + 预付款项合计 {_s(now)} 元，同比 {_pct(growth)}，'
                  f'比营业收入增速高 {_pct(gap)} 个百分点')
        if share is not None:
            detail += f'；合计占总资产 {_pct(share)}'
        return {'observed': gap, 'threshold': Decimal('0.30'),
                'magnitude': _excess_score(gap, Decimal('0.30'), Decimal('0.50')),
                'deps': deps_now + deps_prev + rev_f + share_deps,
                'detail': detail}

    def _latest_periods(self):
        """每个 (公司, 口径) 的最新报告期。用于把"披露完整性"限定在最新一期。"""
        if getattr(self, '_latest_cache', None) is None:
            latest: dict[tuple, str] = {}
            for key in self.index:
                k = (key[0], key[3])
                if key[1] > latest.get(k, ''):
                    latest[k] = key[1]
            self._latest_cache = latest
        return self._latest_cache

    def _r_disc01(self, group):
        """R_DISC_01 最新一期关键科目披露不完整，分析基础不足。

        这不是"公司做了什么"，而是"这份材料够不够支撑分析"。
        缺项越多，下游指标与勾稽的可信度越低，必须显式降级而不是沉默地算下去。

        两个必须的限定，否则会大面积误报：
        · **只看最新一期**。历史年份常常只出现在五年摘要里，摘要按定义只列
          收益、毛利、资产负债总额等少数几行，拿它去要求完整的六项关键科目
          等于把"这是摘要"判成"披露不完整"。
        · **要求该组里至少有一条主表来源的事实**。整组都来自摘要页时，
          缺失是排版体例，不是披露缺陷。
        · **有合并口径时只在合并口径上判定**。母公司单体报表按披露惯例
          通常只列一张"本公司财务状况表"，本来就没有营业收入/净利润/
          经营活动现金流这些区间科目 —— 拿合并口径的六项去要求它，
          必然把正常列报判成"披露不完整"。实测踩过：这一条曾误报成
          排名第一的发现，把真正的信号挤下去了。
        """
        # 有合并口径数据时，披露完整性只针对合并口径判定（合并是分析基准）
        if group[3] != 'consolidated' and any(k[3] == 'consolidated' for k in self.index):
            return None
        latest = self._latest_periods().get((group[0], group[3]))
        if latest and group[1] != latest:
            return None
        tier_facts = [self.index[k] for k in self.index
                      if k[:5] == group
                      and self.index[k].get('source_tier', 'primary') == 'primary']
        if not tier_facts:
            return None
        required = (('revenue', '营业收入', 'annual'), ('net_profit', '净利润', 'annual'),
                    ('operating_cash_flow', '经营活动现金流净额', 'annual'),
                    ('assets', '资产总额', 'instant'), ('liabilities', '负债总额', 'instant'),
                    ('equity', '权益总额', 'instant'))
        present, missing = [], []
        for code, name, kind in required:
            f = self.get(group, code, None, kind)
            if f:
                present.append(f)
            else:
                missing.append(name)
        if len(missing) < 2:
            return None
        ratio = Decimal(len(missing)) / Decimal(len(required))
        return {'observed': ratio, 'threshold': Decimal('0.2'),
                'magnitude': _excess_score(ratio, Decimal('0.2'), Decimal('0.5')),
                'deps': present,
                'detail': f'最新一期关键科目缺失 {len(missing)}/{len(required)} 项'
                          f'（{"、".join(missing)}），指标与勾稽的可验证性不足'}

    RULES = {
        'R_AR_01': _r_ar01, 'R_INV_01': _r_inv01, 'R_CFO_01': _r_cfo01,
        'R_MARGIN_01': _r_margin01, 'R_NREC_01': _r_nrec01, 'R_GW_01': _r_gw01,
        'R_DEBT_01': _r_debt01, 'R_SETTLE_01': _r_settle01, 'R_EXP_01': _r_exp01,
        'R_OCF_01': _r_ocf01, 'R_CAP_01': _r_cap01, 'R_RPT_01': _r_rpt01,
        'R_DISC_01': _r_disc01,
    }

    def _prev_group(self, group):
        prev = _prev_year(group[1])
        if not prev:
            return None
        return (group[0], prev, group[2], group[3], group[4])

    def _persistence(self, rule_id, group, max_periods=3):
        """连续命中期数：只往前推年份，且每一年都真实命中才算。"""
        fn = self.RULES[rule_id]
        hits = 1
        cur = group
        for _ in range(max_periods - 1):
            prev = self._prev_group(cur)
            if not prev:
                break
            try:
                if fn(self, prev) is None:
                    break
            except Exception:
                break
            hits += 1
            cur = prev
        return hits

    def _peer_score(self, rule_id, group, code_hint=None):
        """同行极端程度。

        优先按**四分位距（IQR）**归一化：落在 [p25, p75] 内记 0 分（同行区间内不算极端），
        超出区间每 1 个 IQR 记约 33 分，超出 3 个 IQR 及以上记满分。

        为什么不只看中位数：不同科目的离散度差一个量级 —— 毛利率全行业都在 30–50%
        波动，偏离中位数 10 个百分点已经是异类；周转天数本身能差几倍，偏离 10% 什么
        也不算。用 IQR 归一化才叫"相对同行的极端程度"，也才对得上竞赛说的
        "同行分位减少对行业周期的误判"。

        只给了中位数（没有四分位）时退回相对偏离的近似口径，保证老调用不变。
        """
        if not self.peer:
            return 50.0, True
        # 依次尝试：显式 code_hint → 规则号 → 规则映射到的可比指标代码。
        # 最后一项是修复键不匹配的关键：`peer_benchmark` 按指标代码建索引，
        # 只按规则号查会永远落空。
        stats = None
        for key in (code_hint, rule_id, RULE_PEER_METRIC.get(rule_id)):
            if key and isinstance(self.peer.get(key), dict):
                stats = self.peer[key]
                break
        if not stats:
            return 50.0, True
        observed = None
        observed_code = stats.get('code') or RULE_PEER_METRIC.get(rule_id)
        for m in self.metrics:
            if (m.get('company') == group[0] and m.get('period') == group[1]
                    and m.get('code') == observed_code):
                observed = _dec(m.get('value'))
        median = _dec(stats.get('median'))
        if observed is None or median is None or median == 0:
            return 50.0, True

        p25, p75 = _dec(stats.get('p25')), _dec(stats.get('p75'))
        if p25 is not None and p75 is not None and p75 > p25:
            iqr = p75 - p25
            if observed < p25:
                excess = (p25 - observed) / iqr
            elif observed > p75:
                excess = (observed - p75) / iqr
            else:
                return 0.0, False
            return _clip(float(excess) * 33.0), False

        deviation = abs(observed - median) / abs(median)
        return _clip(float(deviation) * 100.0), False

    # -------------------------------------------------- 主入口

    def scan(self):
        raw = []
        for group in self.groups:
            for rule_id, fn in self.RULES.items():
                hit = fn(self, group)
                if not hit:
                    continue
                raw.append(self._build(rule_id, group, hit))

        raw.extend(self._tieout_findings())
        findings = self._merge(raw)

        findings.sort(key=lambda f: f['risk_score'], reverse=True)
        for rank, f in enumerate(findings, start=1):
            f['rank'] = rank

        return {
            'findings': findings[: self.top_k],
            'all_finding_count': len(findings),
            'tieout_diagnosis': self.diagnose_tieouts(),
            'review_queue': self._review_queue(findings),
            'summary': self._summary(findings, raw),
        }

    @staticmethod
    def _merge(items):
        """同一规则在同一公司同一口径上跨年命中，合并成一条发现。

        竞赛口径里一项 RiskFinding 的 `periods` 是列表、`severity` 取最严重的一期。
        不合并的话 Top 10 会被同一规则的三个年份挤满，排序失去信息量。
        """
        merged: dict[tuple, dict] = {}
        for f in items:
            key = (f['rule_id'], f['company'], f['scope'])
            cur = merged.get(key)
            if cur is None:
                merged[key] = f
                continue
            cur['periods'] = sorted(set(cur['periods']) | set(f['periods']), reverse=True)
            cur['consecutive_periods'] = max(cur['consecutive_periods'], f['consecutive_periods'])
            cur['dependency_fact_ids'] = list(dict.fromkeys(
                cur['dependency_fact_ids'] + f['dependency_fact_ids']))
            cur['support_evidence_ids'] = list(dict.fromkeys(
                cur['support_evidence_ids'] + f['support_evidence_ids']))
            cur['metric_codes'] = list(dict.fromkeys(cur['metric_codes'] + f['metric_codes']))
            # 保留风险分最高那一期的取值与描述，其余期数体现在 periods 里
            if f['risk_score'] > cur['risk_score']:
                cur['risk_score'] = f['risk_score']
                cur['severity'] = f['severity']
                cur['detail'] = f['detail']
                cur['observed'] = f['observed']
                cur['threshold'] = f['threshold']
                cur['score_components'] = f['score_components']
                cur['peer_data_missing'] = f['peer_data_missing']
        return list(merged.values())

    def _build(self, rule_id, group, hit):
        title, category = RULE_META[rule_id]
        magnitude = float(hit['magnitude'])
        periods = self._persistence(rule_id, group)
        persistence = _clip(periods / 3.0 * 100.0)
        peer_score, peer_missing = self._peer_score(rule_id, group)
        cash_impact = 100.0 if rule_id in CASH_IMPACT_RULES else 40.0
        deps = [f for f in hit['deps'] if f]
        # 证据薄弱度：没有证据编号的依赖事实最可疑；置信度不足再额外加权。
        weak = 0.0
        for f in deps:
            if not f.get('evidence_ids'):
                weak += 1.0
            conf = _dec(f.get('confidence'))
            if conf is not None and conf < Decimal('0.9'):
                weak += 0.5
        uncertainty = _clip(weak / max(len(deps), 1) * 100.0) if deps else 100.0

        score = (0.30 * magnitude + 0.25 * persistence + 0.20 * peer_score
                 + 0.15 * cash_impact + 0.10 * uncertainty)

        evidence_ids = []
        fact_ids = []
        for f in deps:
            fact_ids.append(f.get('fact_id', ''))
            evidence_ids.extend(f.get('evidence_ids') or [])
        evidence_ids = list(dict.fromkeys(e for e in evidence_ids if e))
        fact_ids = list(dict.fromkeys(f for f in fact_ids if f))

        return {
            'rank': 0,
            'rule_id': rule_id,
            'title': title,
            'category': category,
            'severity': _severity(score),
            'risk_score': round(score, 1),
            'periods': [group[1]],
            'consecutive_periods': periods,
            'company': group[0],
            'scope': group[3],
            'currency': group[4],
            'detail': hit['detail'],
            'observed': _s(hit.get('observed')),
            'threshold': _s(hit.get('threshold')),
            'metric_codes': list(dict.fromkeys(
                c for c in (f.get('code') for f in deps) if c)),
            'dependency_fact_ids': fact_ids,
            'support_evidence_ids': evidence_ids,
            'score_components': {
                'magnitude': round(magnitude, 1),
                'persistence': round(persistence, 1),
                'peer_extremeness': round(peer_score, 1),
                'cash_impact': round(cash_impact, 1),
                'disclosure_uncertainty': round(uncertainty, 1),
            },
            'peer_data_missing': peer_missing,
            'counter_checks': COUNTER_GUIDANCE.get(rule_id, []),
            'open_questions': OPEN_QUESTIONS.get(rule_id, []),
            'valuation_links': VALUATION_LINKS.get(rule_id, []),
            'disclaimer': '这是需要人工核验的风险信号，不是财务造假结论。',
        }

    # -------------------------------------------------- 勾稽反查

    def diagnose_tieouts(self):
        """对失败的勾稽做单变量反查，尝试定位「哪个数字的单位错了」。

        思路来自会计恒等式：如果等式差 10^k 倍，往往不是有人做假账，
        而是某一行把"万元"的表头读成了"元"。逐个把依赖事实按不同数量级
        重新解释，若**恰好一个**改法能让等式成立，就判为单位错位；
        多个改法都成立则判歧义，转人工 —— 绝不自动改数。
        """
        by_id = {f.get('fact_id'): f for f in self.raw_facts}
        out = []
        for check in self.checks:
            if check.get('status') != 'fail':
                continue
            deps = [by_id.get(fid) for fid in (check.get('dependency_fact_ids') or [])]
            deps = [f for f in deps if f]
            coeffs = _coefficients(check.get('formula', ''), [f.get('code') for f in deps])
            if not deps or len(coeffs) != len(deps):
                out.append({'check': check.get('code'), 'period': check.get('period'),
                            'resolution': 'undetermined',
                            'reason': '公式系数无法解析，无法自动反查，转人工复核。'})
                continue
            tolerance = sum((_rounding(f) * abs(c) for f, c in zip(deps, coeffs)), Decimal(0))
            base_diff = sum((_dec(f['value']) * c for f, c in zip(deps, coeffs)), Decimal(0))

            fixes = []
            for f, c in zip(deps, coeffs):
                original = _dec(f['value'])
                for k in range(-4, 5):
                    if k == 0:
                        continue
                    candidate = original * (Decimal(10) ** k)
                    delta = candidate - original
                    if abs(base_diff + delta * c) <= tolerance:
                        fixes.append({'fact_id': f.get('fact_id'), 'code': f.get('code'),
                                      'scale_shift': k,
                                      'from_value': _s(original), 'to_value': _s(candidate)})
            if len(fixes) == 1:
                fx = fixes[0]
                out.append({'check': check.get('code'), 'period': check.get('period'),
                            'resolution': 'unit_misplacement',
                            'difference': _s(base_diff), 'tolerance': _s(tolerance),
                            'fix': fx,
                            'reason': f"把「{fx['code']}」按 10^{fx['scale_shift']} 重新标度后勾稽成立，"
                                      f"疑似该行的单位（元/千元/万元/亿元）识别有误。已标记为可疑，未自动改数。",
                            'suggested_fact_marker': 'suspect'})
            elif len(fixes) > 1:
                out.append({'check': check.get('code'), 'period': check.get('period'),
                            'resolution': 'ambiguous',
                            'difference': _s(base_diff), 'tolerance': _s(tolerance),
                            'candidates': fixes,
                            'reason': f'有 {len(fixes)} 种单位改法都能让等式成立，无法唯一确定，转人工复核。'})
            else:
                out.append({'check': check.get('code'), 'period': check.get('period'),
                            'resolution': 'not_unit_issue',
                            'difference': _s(base_diff), 'tolerance': _s(tolerance),
                            'reason': '单纯的数量级调整无法修复该等式，可能涉及漏项、口径混用或真实错误，需人工复核。'})
        return out

    def _tieout_findings(self):
        out = []
        for item in self.diagnose_tieouts():
            if item.get('resolution') == 'not_unit_issue':
                continue
            check_code = item.get('check', '')
            score = 90.0 if item.get('resolution') == 'unit_misplacement' else 65.0
            out.append({
                'rank': 0,
                'rule_id': 'R_TIEOUT_01',
                'title': RULE_META['R_TIEOUT_01'][0],
                'category': RULE_META['R_TIEOUT_01'][1],
                'severity': _severity(score),
                'risk_score': round(score, 1),
                'periods': [item.get('period', '')],
                'consecutive_periods': 1,
                'company': '',
                'scope': '',
                'currency': '',
                'detail': f"{check_code}：{item.get('reason', '')}",
                'observed': item.get('difference', ''),
                'threshold': item.get('tolerance', ''),
                'metric_codes': [check_code],
                'dependency_fact_ids': [item.get('fix', {}).get('fact_id', '')] if item.get('fix') else [],
                'support_evidence_ids': [],
                'score_components': {'magnitude': 100.0, 'persistence': 33.3,
                                     'peer_extremeness': 50.0, 'cash_impact': 40.0,
                                     'disclosure_uncertainty': 100.0},
                'peer_data_missing': True,
                'counter_checks': COUNTER_GUIDANCE['R_TIEOUT_01'],
                'open_questions': OPEN_QUESTIONS['R_TIEOUT_01'],
                'valuation_links': VALUATION_LINKS['R_TIEOUT_01'],
                'disclaimer': '数据可信度问题会污染全部下游结论，必须优先修复。',
            })
        return out

    def _review_queue(self, findings):
        queue = []
        for f in findings:
            if f['severity'] in ('critical', 'high'):
                queue.append({'rule_id': f['rule_id'], 'period': f['periods'][0],
                              'action': '需人工核对原始报表与附注',
                              'open_questions': f['open_questions']})
        for item in self.diagnose_tieouts():
            if item.get('resolution') == 'ambiguous':
                queue.append({'rule_id': 'R_TIEOUT_01', 'period': item.get('period'),
                              'action': '单位口径存在多种可能，需人工指定正确口径',
                              'open_questions': OPEN_QUESTIONS['R_TIEOUT_01']})
        return queue

    def _summary(self, findings, raw=None):
        by_sev = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
        for f in findings:
            by_sev[f['severity']] = by_sev.get(f['severity'], 0) + 1
        top = findings[0] if findings else None
        return {
            'group_count': len(self.groups),
            'fact_count': len(self.raw_facts),
            'finding_count': len(findings),
            'raw_hit_count': len(raw or findings),
            'severity_breakdown': by_sev,
            'peer_data_available': bool(self.peer),
            'top_finding': (f"{top['rule_id']} {top['title']}（风险分 {top['risk_score']}）" if top else ''),
            'text': (f'在 {len(self.groups)} 个「公司-期间-口径」组合上运行 {len(RULE_META)} 类法证规则，'
                     f'合并后得到 {len(findings)} 条待核验风险信号'
                     f'（严重 {by_sev["critical"]}、高 {by_sev["high"]}、中 {by_sev["medium"]}、低 {by_sev["low"]}）。'
                     + ('' if self.peer else ' 未提供同行数据，peer_extremeness 分项取中性值 50 并已标记。')),
        }


def _pct(value) -> str:
    d = _dec(value)
    if d is None:
        return 'N/A'
    return f'{d * 100:.2f}%'


def _coefficients(formula: str, codes: list[str]):
    """从 '1 * assets + -1 * liabilities + -1 * equity = 0' 解析出系数序列。"""
    if not formula or '=' not in formula:
        return []
    left = formula.split('=', 1)[0]
    out = []
    import re
    for code in codes:
        m = re.search(r'(-?\d+(?:\.\d+)?)\s*\*\s*' + re.escape(str(code)) + r'(?![\w])', left)
        out.append(Decimal(m.group(1)) if m else Decimal(0))
    return out


def scan(facts, checks=None, metrics=None, peer=None, top_k=10) -> dict:
    """对外入口：跑全套法证规则。"""
    return ForensicEngine(facts, checks=checks, metrics=metrics, peer=peer, top_k=top_k).scan()
