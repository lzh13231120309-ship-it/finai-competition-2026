"""DCF 数学内核：只含纯函数、Decimal、无任何数据依赖。

定位
----
这是整个估值体系里"必须一眼能手算验证"的一层：现值、终值、WACC、β 调整、
EV→权益价值桥、反向求解。上层（三表预测、同行倍数、敏感性、Excel）都建立在它之上，
而 tests/test_dcf_reference.py 里有一份**独立写的朴素参考实现**与它逐位比对（误差 < 1e-6），
另有三个手算黄金算例（算式写在注释里）。

约定
----
- 金额单位由调用方决定（元/万元/亿元均可，本层不换算；上层统一换成"元"再进来）。
- 比率用小数（0.08 = 8%）；期数 t 从 1 开始，第 t 年末现金流（期末折现），
  可选"期中折现"（mid_year=True：第 t 年现金流按 t-0.5 折现）。
- 永续增长率校验是**硬约束**：g >= WACC 或 g > 长期名义 GDP 增速上限，一律拒绝并给出原因，
  不静默截断。上限默认 4%（见 LONG_RUN_NOMINAL_GDP；是启发式，可由调用方显式放宽并留痕）。

公式（与竞赛要求的"主要假设、公式、可复算"对应）
-------------------------------------------------
    PV(CF_t)      = CF_t / (1+WACC)^t                       （期中折现：指数改为 t-0.5）
    TV_Gordon     = FCFF_{n+1} / (WACC - g)
    FCFF_{n+1}    = NOPAT_n × (1+g) × (1 - g/ROIC_T)        （价值驱动法，默认）
                  | FCFF_n × (1+g)                          （简单 Gordon，作对照）
    TV_exit       = 终年指标 × 退出倍数
    EV            = Σ PV(FCFF_t) + PV(TV)
    Equity        = EV - 有息债务 - 租赁负债 - 优先股/永续债 - 少数股东权益 - 或有负债
                       + 货币类现金 + 联营合营投资 + 其他非经营资产
    β_u = β_L / (1 + (1-τ)·D/E)      β_L = β_u × (1 + (1-τ)·D/E)      （Hamada）
    β_adj = 2/3·β_raw + 1/3          （Blume 调整）
    Ke = rf + β·ERP (+ 其他溢价)     WACC = E/V·Ke + D/V·Kd·(1-τ)
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext

D = Decimal
ZERO = D(0)
ONE = D(1)
HALF = D('0.5')

# 长期名义 GDP 增速上限（启发式）。依据：中国潜在实际增速在 2030 年后趋向 3.5%-4.5%、
# 通胀 1.5%-2.5%，永续期名义增速的合理上界取 4%–5%；这里取偏保守的 4%，
# 因为企业永续增长不应长期超过经济整体（否则终值期企业占 GDP 比例趋向无穷大）。
LONG_RUN_NOMINAL_GDP = D('0.04')
# WACC 与 g 的最小利差（启发式）：利差 < 2pp 时终值对 g 极度敏感（1pp 变动改变终值 >30%）。
MIN_SPREAD = D('0.02')


def dec(value, default=None):
    """安全转 Decimal：None/空串/非法/NaN/Inf 一律回落 default，绝不静默当 0。"""
    if value is None or value == '' or isinstance(value, bool):
        return default
    if isinstance(value, D):
        return value if value.is_finite() else default
    try:
        out = D(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default
    return out if out.is_finite() else default


def ser(value):
    """序列化 Decimal 为字符串（去尾零、-0 归零）；其余原样。"""
    if value is None:
        return None
    if isinstance(value, D):
        text = format(value.normalize(), 'f')
        return '0' if text == '-0' else text
    return value


# ---------------------------------------------------------------------------
# 现值与终值
# ---------------------------------------------------------------------------

def pv_factor(rate, t, mid_year=False):
    """折现因子 1/(1+r)^t；期中折现时 t 取 t-0.5。"""
    rate, t = dec(rate), dec(t)
    exponent = t - HALF if mid_year else t
    with localcontext() as ctx:
        ctx.prec = 40
        return ONE / ((ONE + rate) ** exponent)


def npv(cashflows, rate, mid_year=False):
    """净现值：cashflows[0] 为第 1 年末现金流。"""
    total = ZERO
    for i, cf in enumerate(cashflows, start=1):
        total += dec(cf, ZERO) * pv_factor(rate, i, mid_year)
    return total


def check_terminal_growth(g, wacc, gdp_cap=LONG_RUN_NOMINAL_GDP, allow_above_gdp=False):
    """永续增长率硬约束。返回 {'ok', 'reasons', 'warnings'}；ok=False 时调用方必须拒绝估值。"""
    g, wacc = dec(g), dec(wacc)
    reasons, warnings = [], []
    if g is None or wacc is None:
        return {'ok': False, 'reasons': ['永续增长率或 WACC 缺失'], 'warnings': []}
    if g >= wacc:
        reasons.append(f'永续增长率 {g:.4%} ≥ WACC {wacc:.4%}：Gordon 终值公式不成立（分母 ≤ 0），拒绝计算。')
    cap = dec(gdp_cap, LONG_RUN_NOMINAL_GDP)
    if g > cap:
        msg = (f'永续增长率 {g:.4%} 超过长期名义 GDP 增速上限 {cap:.2%}（启发式）：'
               '企业永续增速长期超过经济整体在逻辑上不可持续。')
        if allow_above_gdp:
            warnings.append(msg + '（使用者显式放宽，已留痕。）')
        else:
            reasons.append(msg + '如确有依据，须显式设置 allow_above_gdp 并说明。')
    if g < wacc and (wacc - g) < MIN_SPREAD:
        warnings.append(f'WACC−g 利差仅 {(wacc - g):.2%}，低于 {MIN_SPREAD:.0%}（启发式）：终值对 g 极度敏感。')
    if g < ZERO:
        warnings.append('永续增长率为负：隐含企业永续萎缩，请确认是否为夕阳/资源耗竭型。')
    return {'ok': not reasons, 'reasons': reasons, 'warnings': warnings}


def gordon_tv(fcf_next, wacc, g):
    """Gordon 终值（第 n 年末时点）：FCFF_{n+1}/(WACC-g)。调用方须先 check_terminal_growth。"""
    wacc, g = dec(wacc), dec(g)
    return dec(fcf_next) / (wacc - g)


def value_driver_fcff(nopat_next, g, roic):
    """价值驱动法终值现金流：NOPAT_{n+1} × (1 − g/ROIC)。

    经济含义：永续增长 g 必须有再投资支持（再投资率 = g/ROIC）。若直接把末年 FCFF×(1+g) 当终值现金流，
    会把"高增长期的大额资本开支"或"低估的资本开支"永久复制进终值。
    """
    roic = dec(roic)
    if roic is None or roic <= ZERO:
        raise ValueError('terminal ROIC 必须为正')
    return dec(nopat_next) * (ONE - dec(g) / roic)


def exit_multiple_tv(metric_n, multiple):
    return dec(metric_n) * dec(multiple)


def tv_discount_periods(n, mid_year, method):
    """终值折现期数。Gordon + 期中折现：按 n-0.5（与期中现金流同口径）；退出倍数：按 n（出售发生在期末）。"""
    n = dec(n)
    return n - HALF if (mid_year and method == 'gordon') else n


def implied_exit_multiple(tv, metric_n):
    m = dec(metric_n)
    return None if not m else dec(tv) / m


def implied_growth_from_tv(tv, fcf_n, wacc):
    """由 n 时点终值反推隐含永续增长率：TV = FCF_n(1+g)/(w-g) → g = (TV·w − FCF_n)/(TV + FCF_n)。"""
    tv, fcf, w = dec(tv), dec(fcf_n), dec(wacc)
    if tv is None or fcf is None or (tv + fcf) == 0:
        return None
    return (tv * w - fcf) / (tv + fcf)


# ---------------------------------------------------------------------------
# 资本成本
# ---------------------------------------------------------------------------

def blume_adjust(beta_raw, weight=D(2) / D(3)):
    """Blume 调整：β_adj = 2/3·β_raw + 1/3（向 1 回归；Bloomberg 沿用该法）。"""
    return dec(beta_raw) * weight + (ONE - weight)


def unlever_beta(beta_levered, tax_rate, debt_to_equity):
    """Hamada：β_u = β_L / (1 + (1−τ)·D/E)。"""
    return dec(beta_levered) / (ONE + (ONE - dec(tax_rate)) * dec(debt_to_equity))


def relever_beta(beta_unlevered, tax_rate, debt_to_equity):
    """Hamada：β_L = β_u × (1 + (1−τ)·D/E)。"""
    return dec(beta_unlevered) * (ONE + (ONE - dec(tax_rate)) * dec(debt_to_equity))


def industry_beta_path(peer_betas, tax_rate_target, target_debt_to_equity):
    """行业 β 路径：同行 [(β_L, 税率, D/E)] → 去杠杆 → 取中位数 → 按目标资本结构再加杠杆。"""
    rows, unl = [], []
    for beta, tax, d_e in peer_betas:
        u = unlever_beta(beta, tax, d_e)
        unl.append(u)
        rows.append({'beta_levered': ser(dec(beta)), 'tax': ser(dec(tax)), 'debt_to_equity': ser(dec(d_e)),
                     'beta_unlevered': ser(u)})
    if not unl:
        return {'unlevered_median': None, 'relevered': None, 'peers': [], 'warnings': ['无同行 β']}
    unl_sorted = sorted(unl)
    n = len(unl_sorted)
    med = unl_sorted[n // 2] if n % 2 else (unl_sorted[n // 2 - 1] + unl_sorted[n // 2]) / 2
    out = {'unlevered_median': med, 'relevered': relever_beta(med, tax_rate_target, target_debt_to_equity),
           'peers': rows, 'warnings': []}
    if n < 3:
        out['warnings'].append(f'同行 β 样本仅 {n} 家，行业 β 稳健性不足')
    return out


def periodic_returns(prices, step=5):
    """由日收盘价 [(date, close)] 每 step 个交易日取一次点（含最后一天），算简单收益。返回 (dates, returns)。"""
    pts = list(prices)[::-1][::step][::-1] if step > 1 else list(prices)
    dates, rets = [], []
    for (d0, p0), (d1, p1) in zip(pts, pts[1:]):
        if p0 and float(p0) > 0:
            dates.append(d1)
            rets.append(D(str(p1)) / D(str(p0)) - ONE)
    return dates, rets


def align_returns(stock_prices, market_prices, step=5):
    """按共同交易日对齐后再算收益（避免停牌/日历错位造成的伪相关偏差）。"""
    mkt = {d: p for d, p in market_prices}
    common = [(d, p) for d, p in stock_prices if d in mkt]
    s_dates, s_ret = periodic_returns(common, step)
    m_dates, m_ret = periodic_returns([(d, mkt[d]) for d, _ in common], step)
    return s_dates, s_ret, m_ret


def ols_beta(stock_returns, market_returns):
    """一元 OLS：r_s = α + β·r_m。返回 beta、alpha、r2、标准误、样本数（Decimal）。"""
    n = min(len(stock_returns), len(market_returns))
    if n < 30:
        return {'ok': False, 'n': n, 'reason': '样本不足 30 个收益观测，β 回归不稳健'}
    xs = [dec(x) for x in market_returns[-n:]]
    ys = [dec(y) for y in stock_returns[-n:]]
    with localcontext() as ctx:
        ctx.prec = 40
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        syy = sum((y - my) ** 2 for y in ys)
        if sxx == 0:
            return {'ok': False, 'n': n, 'reason': '市场收益无方差'}
        beta = sxy / sxx
        alpha = my - beta * mx
        r2 = (sxy * sxy) / (sxx * syy) if syy else ZERO
        resid = sum((y - alpha - beta * x) ** 2 for x, y in zip(xs, ys))
        se = (resid / (n - 2) / sxx).sqrt() if n > 2 else None
    return {'ok': True, 'n': n, 'beta': beta, 'alpha': alpha, 'r2': r2, 'std_error': se}


def capm(rf, beta, erp, extra_premium=ZERO):
    return dec(rf) + dec(beta) * dec(erp) + dec(extra_premium, ZERO)


def wacc_build(cost_of_equity, cost_of_debt_pre_tax, tax_rate, debt_weight):
    """WACC = (1−wd)·Ke + wd·Kd·(1−τ)。debt_weight 为 D/(D+E)（市值/目标权重，不用净债务）。"""
    wd = dec(debt_weight)
    if not (ZERO <= wd < ONE):
        raise ValueError('债务权重须在 [0,1)')
    return (ONE - wd) * dec(cost_of_equity) + wd * dec(cost_of_debt_pre_tax) * (ONE - dec(tax_rate))


# ---------------------------------------------------------------------------
# EV → 权益价值桥
# ---------------------------------------------------------------------------

# 桥项：符号 +1 加、-1 减。口径说明写入输出，评审可逐项质疑。
BRIDGE_ITEMS = [
    ('cash', '货币类现金（货币资金等；交易性金融资产中的类现金部分）', +1),
    ('associates', '联营/合营企业投资（账面价值；FCFF 不含投资收益，须单独加回）', +1),
    ('non_operating_assets', '其他非经营性资产（交易性/其他权益工具/非流动金融资产/投资性房地产等，账面）', +1),
    ('debt', '有息债务（短期借款+一年内到期非流动负债+长期借款+应付债券+短期债券）', -1),
    ('leases', '租赁负债（FCFF 以 EBIT 为起点且 EBIT 已扣使用权资产折旧时，租赁负债仍是融资性债务）', -1),
    ('preferred', '优先股/永续债/其他权益工具（对普通股东而言是优先索偿权）', -1),
    ('minority', '少数股东权益（账面口径；市场口径更合理，见 valuation_sanity 提示）', -1),
    ('contingent', '或有负债/未决诉讼/担保代偿（预计损失，使用者提供，不猜）', -1),
    ('sbc', '股权激励成本现值（若 FCFF 未把 SBC 当费用扣除，则在此扣减；否则填 0）', -1),
]


def equity_bridge(enterprise_value, items=None):
    """EV → 权益价值。items 里的值一律以**正数**给出（负债也给正数），符号由本函数决定。

    缺失项记 0，但在 lines 里标 provided=False，便于审计看出哪些是"未提供"而非"为零"。
    """
    items = items or {}
    ev = dec(enterprise_value)
    lines, equity, net_debt_like = [], ev, ZERO
    for key, label, sign in BRIDGE_ITEMS:
        raw = dec(items.get(key))
        amount = raw if raw is not None else ZERO
        if sign < 0 and amount < ZERO and key != 'minority':   # 少数股东权益可为负(子公司累计亏损)，其余负债项不应为负
            raise ValueError(f'桥项 {key} 应以正数给出（负债性质），收到 {amount}')
        signed = amount * sign
        equity += signed
        net_debt_like -= signed
        lines.append({'key': key, 'label': label, 'sign': sign, 'amount': amount, 'signed': signed,
                      'provided': raw is not None})
    return {'equity_value': equity, 'lines': lines, 'net_debt_like': net_debt_like}


# ---------------------------------------------------------------------------
# 整体 DCF
# ---------------------------------------------------------------------------

def dcf_value(fcff, wacc, terminal, bridge_items=None, mid_year=False, shares=None,
              ebitda_ltm=None, ebitda_terminal=None, gdp_cap=LONG_RUN_NOMINAL_GDP, allow_above_gdp=False):
    """对一串 FCFF 做估值。

    terminal = {
        'method': 'gordon' | 'exit_multiple',
        'g': 永续增长率,                       # gordon 必需；exit_multiple 时仅用于对照
        'fcff_next': 终值现金流(第 n+1 年),    # gordon 必需，由上层按价值驱动法/简单 Gordon 算好
        'multiple': 退出倍数, 'metric_n': 终年指标(如 EBITDA_n)   # exit_multiple 必需
    }
    g 违反约束时 ok=False 且不给出任何价值（拒绝而非截断）。
    """
    wacc = dec(wacc)
    out = {'ok': False, 'warnings': [], 'reasons': [], 'wacc': wacc, 'mid_year': mid_year}
    if not fcff:
        out['reasons'].append('没有可折现的预测期 FCFF')
        return out
    if wacc is None or wacc <= ZERO:
        out['reasons'].append('WACC 必须为正')
        return out
    method = terminal.get('method', 'gordon')
    n = len(fcff)
    with localcontext() as ctx:
        ctx.prec = 40
        rows, pv_explicit = [], ZERO
        for i, cf in enumerate(fcff, start=1):
            df = pv_factor(wacc, i, mid_year)
            pv = dec(cf) * df
            pv_explicit += pv
            rows.append({'t': i, 'fcff': dec(cf), 'discount_factor': df, 'pv': pv})
        out['rows'] = rows
        g = dec(terminal.get('g'))
        if method == 'gordon':
            chk = check_terminal_growth(g, wacc, gdp_cap, allow_above_gdp)
            out['warnings'].extend(chk['warnings'])
            if not chk['ok']:
                out['reasons'].extend(chk['reasons'])
                out['terminal_growth'] = g
                return out
            fcf_next = dec(terminal.get('fcff_next'))
            tv = gordon_tv(fcf_next, wacc, g)
            out['terminal_fcff'] = fcf_next
        elif method == 'exit_multiple':
            tv = exit_multiple_tv(terminal.get('metric_n'), terminal.get('multiple'))
            out['exit_multiple'] = dec(terminal.get('multiple'))
        else:
            out['reasons'].append(f'未知终值方法 {method}')
            return out
        periods = tv_discount_periods(n, mid_year, method)
        pv_tv = tv * (ONE / ((ONE + wacc) ** periods))
        ev = pv_explicit + pv_tv
        out.update({'terminal_method': method, 'terminal_growth': g, 'terminal_value': tv,
                    'pv_terminal_value': pv_tv, 'pv_explicit': pv_explicit, 'enterprise_value': ev,
                    'terminal_discount_periods': periods,
                    'terminal_value_share': (pv_tv / ev) if ev else None})
        bridge = equity_bridge(ev, bridge_items)
        out['bridge'] = bridge
        out['equity_value'] = bridge['equity_value']
        if method == 'gordon':
            if ebitda_terminal:
                out['implied_exit_multiple'] = implied_exit_multiple(tv, ebitda_terminal)
        else:
            out['implied_terminal_growth'] = implied_growth_from_tv(tv, rows[-1]['fcff'], wacc)
        if ebitda_ltm:
            out['implied_ev_ebitda_ltm'] = ev / dec(ebitda_ltm)
        sh = dec(shares)
        if sh and sh > ZERO:
            out['shares'] = sh
            out['value_per_share'] = bridge['equity_value'] / sh
    out['ok'] = True
    if out['terminal_value_share'] is not None and out['terminal_value_share'] > D('0.75'):
        out['warnings'].append('终值占企业价值 > 75%（启发式阈值）：结论主要由永续假设决定，须配合敏感性矩阵阅读。')
    return out


# ---------------------------------------------------------------------------
# 数值求解（反向 DCF 等）
# ---------------------------------------------------------------------------

def bisect(func, lo, hi, target, tol=D('1e-9'), max_iter=200):
    """对单调函数 func 求 func(x)=target。lo/hi 须夹住目标，否则返回 ok=False 与最近边界。"""
    lo, hi, target = dec(lo), dec(hi), dec(target)
    f_lo, f_hi = func(lo), func(hi)
    if f_lo is None or f_hi is None:
        return {'ok': False, 'reason': '边界处模型无解'}
    increasing = f_hi >= f_lo
    low_v, high_v = (f_lo, f_hi) if increasing else (f_hi, f_lo)
    if not (low_v <= target <= high_v):
        return {'ok': False, 'reason': '目标值不在搜索区间的取值范围内', 'f_lo': f_lo, 'f_hi': f_hi,
                'bound': lo if abs(target - f_lo) < abs(target - f_hi) else hi}
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        f_mid = func(mid)
        if f_mid is None:
            return {'ok': False, 'reason': '中间点模型无解'}
        if abs(f_mid - target) <= tol * max(ONE, abs(target)) or (hi - lo) <= tol:
            return {'ok': True, 'x': mid, 'f': f_mid}
        if (f_mid < target) == increasing:
            lo = mid
        else:
            hi = mid
    mid = (lo + hi) / 2
    return {'ok': True, 'x': mid, 'f': func(mid), 'converged': False}


def implied_growth_reverse_dcf(base_cf, years, wacc, g_terminal, target_ev, mid_year=False):
    """最简反向 DCF（单阶段增长，独立参考与快速解释用）：
    FCFF_t = base_cf × (1+x)^t，t=1..years，终值 Gordon(g_terminal)；求 x 使 EV = target_ev。
    完整反向 DCF（驱动三表）在 valuation_pro.reverse_dcf，二者可互验。
    """
    def ev_of(x):
        cfs = [dec(base_cf) * (ONE + x) ** t for t in range(1, years + 1)]
        tv = gordon_tv(cfs[-1] * (ONE + dec(g_terminal)), wacc, g_terminal)
        return npv(cfs, wacc, mid_year) + tv * pv_factor(wacc, tv_discount_periods(years, mid_year, 'gordon'), False)
    return bisect(ev_of, D('-0.5'), D('1.5'), target_ev)


# ---------------------------------------------------------------------------
# 金融企业（不适用 FCFF）：PB-ROE / DDM 简化实现
# ---------------------------------------------------------------------------

def justified_pb(roe, cost_of_equity, g):
    """合理 PB = (ROE − g) / (Ke − g)（稳态、增长靠留存，payout = 1 − g/ROE）。"""
    roe, ke, g = dec(roe), dec(cost_of_equity), dec(g)
    if ke <= g:
        raise ValueError('Ke 必须大于 g')
    return (roe - g) / (ke - g)


def ddm_gordon(dividend_next, cost_of_equity, g):
    """一阶段 Gordon DDM：P = D_1 / (Ke − g)。"""
    ke, g = dec(cost_of_equity), dec(g)
    if ke <= g:
        raise ValueError('Ke 必须大于 g')
    return dec(dividend_next) / (ke - g)
