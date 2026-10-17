"""相对估值：同行选择规则 → 倍数 → IQR 异常值剔除（前后对比）→ 中位数/四分位 → 隐含权益价值。

竞赛要求："相对估值展示同行选择规则、中位数、四分位区间和异常值剔除前后差异"。本模块逐条落实，
并且每一步都可人工干预后重算（manual_exclude / 自定义 rules），不是黑箱。

设计要点
--------
1. **选择规则显式**：行业、规模带（收入为目标的 a–b 倍）、剔除 ST/退市、最少家数、最多家数；
   每家被剔除的同行都带原因；`manual_exclude` 让使用者人工剔除后重算。
2. **倍数口径**：PE=市值/归母净利润；PB=市值/归母净资产；PS=市值/营业收入；
   EV/EBITDA、EV/Sales 的 EV=市值+有息债务+租赁+少数股东权益+优先股−现金(类现金)（与 DCF 桥同口径）；
   PEG=PE/(利润增速×100)。分母 ≤ 0 的倍数"无意义"，直接剔出（不是异常值，而是不可定义）。
3. **IQR 异常值**：对每个倍数，值落在 [Q1−1.5·IQR, Q3+1.5·IQR] 之外者剔除（Tukey 围栏）。
   分位数用线性插值（等同 Excel QUARTILE.INC / numpy 默认），保证 Excel 里可复算。同时输出剔除前后的
   n、均值、Q1、中位数、Q3，让读者看到异常值把均值拉偏了多少。1.5 倍是 Tukey 惯例（启发式）。
4. **适用性闸门**：金融企业不用 EV 类与 PS；亏损企业不用 PE/EV-EBITDA（用 PS/EV-Sales/PB 并警告）；
   周期股用"正常化盈利"（多年均值）代替当年盈利，并警告峰谷风险。
5. 样本 < 5 家标"低可信"，< 3 家不给四分位，只列倍数。
"""
from __future__ import annotations

import math
from decimal import Decimal

from .dcf_core import dec, ser

D = Decimal
ZERO = D(0)

MULTIPLES = ('pe', 'pb', 'ps', 'ev_ebitda', 'ev_sales', 'peg')
LABELS = {'pe': 'PE（市盈率）', 'pb': 'PB（市净率）', 'ps': 'PS（市销率）', 'ev_ebitda': 'EV/EBITDA',
          'ev_sales': 'EV/Sales', 'peg': 'PEG'}
IQR_K = D('1.5')
MIN_PEERS_CONFIDENT = 5
MIN_PEERS_QUARTILE = 3

DEFAULT_RULES = {
    'industry': None,            # 目标所属行业（字符串）；None 表示不按行业过滤（不推荐）
    'size_band': (D('0.2'), D(5)),   # 同行收入 / 目标收入 ∈ [0.2, 5]
    'exclude_st': True,
    'min_peers': MIN_PEERS_CONFIDENT,
    'max_peers': 12,
    'manual_exclude': [],        # 人工剔除的代码或名称
}


def quantile(sorted_values, q):
    """线性插值分位数（Excel QUARTILE.INC / numpy 默认）：位置 = (n-1)·q。"""
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return sorted_values[0]
    pos = (n - 1) * dec(q)
    lo = int(pos.to_integral_value(rounding='ROUND_FLOOR'))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def median(values):
    s = sorted(values)
    return quantile(s, D('0.5'))


# ---------------------------------------------------------------------------
# 同行选择
# ---------------------------------------------------------------------------

def select_peers(candidates, target, rules=None):
    """按显式规则筛同行。candidates: [{code,name,industry,revenue,...}]；target 同结构。

    返回 {'included': [...], 'excluded': [{'code','name','reason'}], 'rules': {...}, 'rule_text': str,
          'warnings': [...]}。排序规则：按 |ln(收入/目标收入)| 升序（规模最近优先），取前 max_peers。
    """
    r = {**DEFAULT_RULES, **(rules or {})}
    lo, hi = (dec(x) for x in r['size_band'])
    manual = {str(x) for x in (r.get('manual_exclude') or [])}
    t_rev = dec(target.get('revenue'))
    included, excluded = [], []
    for c in candidates:
        code, name = str(c.get('code', '')), str(c.get('name', ''))
        reason = None
        if code in manual or name in manual:
            reason = '人工剔除'
        elif r['industry'] and c.get('industry') != r['industry']:
            reason = f"行业不同（{c.get('industry')} ≠ {r['industry']}）"
        elif r['exclude_st'] and ('ST' in name.upper() or '退' in name):
            reason = 'ST/退市风险'
        elif code and code == str(target.get('code', '')):
            reason = '目标公司本身'
        elif t_rev and dec(c.get('revenue')) is not None and dec(c.get('revenue')) > 0:
            ratio = dec(c['revenue']) / t_rev
            if not (lo <= ratio <= hi):
                reason = f'规模不可比（收入为目标 {ratio:.2f} 倍，规则 {lo}–{hi} 倍）'
        elif dec(c.get('revenue')) is None:
            reason = '缺收入数据'
        if reason:
            excluded.append({'code': code, 'name': name, 'reason': reason})
        else:
            included.append(c)
    def dist(c):
        rv = dec(c.get('revenue'))
        return abs(math.log(float(rv / t_rev))) if (t_rev and rv and rv > 0) else 0.0
    included.sort(key=dist)
    cut = included[int(r['max_peers']):]
    included = included[:int(r['max_peers'])]
    for c in cut:
        excluded.append({'code': str(c.get('code', '')), 'name': str(c.get('name', '')),
                         'reason': f"超过最大家数 {r['max_peers']}（按规模接近度截断）"})
    warnings = []
    if len(included) < MIN_PEERS_QUARTILE:
        warnings.append(f'同行仅 {len(included)} 家（< {MIN_PEERS_QUARTILE}），不输出四分位区间，倍数结论仅供参考。')
    elif len(included) < int(r['min_peers']):
        warnings.append(f"同行仅 {len(included)} 家（< 最少 {r['min_peers']} 家），相对估值可信度低。")
    text = (f"行业={r['industry'] or '不限'}；收入规模带 {lo}–{hi} 倍；"
            f"{'剔除 ST/退市；' if r['exclude_st'] else ''}按规模接近度取前 {r['max_peers']}；"
            f"人工剔除 {sorted(manual) or '无'}")
    return {'included': included, 'excluded': excluded, 'rules': {**r, 'size_band': (ser(lo), ser(hi))},
            'rule_text': text, 'warnings': warnings}


# ---------------------------------------------------------------------------
# 倍数与统计
# ---------------------------------------------------------------------------

def compute_multiples(p):
    """由同行原始字段算倍数；分母 ≤ 0 的返回 None（不可定义，非异常值）。

    p 需含：market_cap；可选 ev（缺省则 market_cap + net_debt_like）、net_income、book_equity、revenue、
    ebitda、np_growth（小数）。
    """
    mc = dec(p.get('market_cap'))
    ev = dec(p.get('ev'))
    if ev is None and mc is not None and dec(p.get('net_debt_like')) is not None:
        ev = mc + dec(p['net_debt_like'])
    out = {}

    def ratio(num, den):
        den = dec(den)
        return (num / den) if (num is not None and den is not None and den > 0) else None

    out['pe'] = ratio(mc, p.get('net_income'))
    out['pb'] = ratio(mc, p.get('book_equity'))
    out['ps'] = ratio(mc, p.get('revenue'))
    out['ev_ebitda'] = ratio(ev, p.get('ebitda'))
    out['ev_sales'] = ratio(ev, p.get('revenue'))
    g = dec(p.get('np_growth'))
    out['peg'] = (out['pe'] / (g * 100)) if (out['pe'] is not None and g is not None and g > 0) else None
    return out


def _stats(vals):
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return {'n': 0}
    return {'n': n, 'mean': sum(s) / n, 'q1': quantile(s, D('0.25')), 'median': quantile(s, D('0.5')),
            'q3': quantile(s, D('0.75')), 'min': s[0], 'max': s[-1]}


def iqr_filter(pairs, k=IQR_K):
    """pairs: [(code, value)]。返回 (kept_values, outliers[(code,value)], fence(low,high))。"""
    vals = sorted(v for _, v in pairs)
    if len(vals) < 4:
        return [v for _, v in pairs], [], (None, None)       # 样本过少，不做异常值剔除
    q1, q3 = quantile(vals, D('0.25')), quantile(vals, D('0.75'))
    iqr = q3 - q1
    low, high = q1 - k * iqr, q3 + k * iqr
    kept = [(c, v) for c, v in pairs if low <= v <= high]
    out = [(c, v) for c, v in pairs if not (low <= v <= high)]
    return [v for _, v in kept], out, (low, high)


def peer_statistics(peers):
    """对每个倍数输出：有效样本、IQR 剔除前后的统计、被剔除名单与围栏。"""
    table = [{'code': str(p.get('code', '')), 'name': str(p.get('name', '')), **{
        m: v for m, v in compute_multiples(p).items()}} for p in peers]
    stats = {}
    for m in MULTIPLES:
        pairs = [(row['code'] or row['name'], row[m]) for row in table if row.get(m) is not None]
        before = _stats([v for _, v in pairs])
        kept, outliers, fence = iqr_filter(pairs)
        after = _stats(kept)
        stats[m] = {'label': LABELS[m], 'n_defined': len(pairs), 'n_undefined': len(table) - len(pairs),
                    'before': before, 'after': after,
                    'outliers': [{'code': c, 'value': v} for c, v in outliers],
                    'fence': {'low': fence[0], 'high': fence[1]},
                    'quartile_ok': after.get('n', 0) >= MIN_PEERS_QUARTILE}
    return {'table': table, 'stats': stats}


# ---------------------------------------------------------------------------
# 适用性与隐含价值
# ---------------------------------------------------------------------------

def classify_target(target):
    """给目标公司贴相对估值口径标签：financial / loss / cyclical / normal。"""
    flags = []
    if target.get('is_financial'):
        flags.append('financial')
    ni = dec(target.get('net_income'))
    if ni is not None and ni <= 0:
        flags.append('loss')
    if target.get('is_cyclical'):
        flags.append('cyclical')
    return flags or ['normal']


def implied_valuation(target, pstats, net_debt_like=None):
    """用同行 Q1/中位/Q3 倍数 × 目标公司指标 → 权益价值区间。

    target 字段：net_income、book_equity、revenue、ebitda、np_growth；周期股另给 normalized_net_income、
    normalized_ebitda；net_debt_like（EV→权益：权益 = EV − net_debt_like，含少数股东/租赁/优先股）。
    """
    flags = classify_target(target)
    nd = dec(net_debt_like if net_debt_like is not None else target.get('net_debt_like'), ZERO)
    ni = dec(target.get('normalized_net_income')) if 'cyclical' in flags and target.get('normalized_net_income') is not None \
        else dec(target.get('net_income'))
    ebitda = dec(target.get('normalized_ebitda')) if 'cyclical' in flags and target.get('normalized_ebitda') is not None \
        else dec(target.get('ebitda'))
    metric = {'pe': ni, 'pb': dec(target.get('book_equity')), 'ps': dec(target.get('revenue')),
              'ev_ebitda': ebitda, 'ev_sales': dec(target.get('revenue'))}
    results, warnings = {}, []
    if 'cyclical' in flags:
        warnings.append('周期股：PE/EV-EBITDA 用正常化盈利（多年均值）而非当年盈利；当年盈利处于峰/谷时倍数法会系统性偏离。')
    if 'loss' in flags:
        warnings.append('目标公司当年归母净利润 ≤ 0：PE 不适用；改用 PS/EV-Sales/PB 并降低结论权重（亏损企业的收入倍数不反映盈利能力）。')
    for m in MULTIPLES:
        st = pstats['stats'][m]
        res = {'label': LABELS[m], 'applicable': True, 'reason': ''}
        if 'financial' in flags and m in ('ev_ebitda', 'ev_sales', 'ps', 'peg'):
            res.update(applicable=False, reason='金融企业负债是经营性的，EV 类/PS 倍数无意义；仅用 PB、PE')
        elif m == 'peg':
            g = dec(target.get('np_growth'))
            if g is None or g <= 0 or ni is None or ni <= 0:
                res.update(applicable=False, reason='目标利润增速或净利润 ≤ 0，PEG 不适用')
        elif 'loss' in flags and m in ('pe', 'ev_ebitda'):
            res.update(applicable=False, reason='目标盈利 ≤ 0，该倍数无意义')
        elif metric.get(m) is None or metric[m] <= 0:
            res.update(applicable=False, reason='目标公司对应指标缺失或 ≤ 0')
        if res['applicable'] and not st['quartile_ok']:
            res.update(applicable=False, reason=f"同行有效样本仅 {st['after'].get('n', 0)} 家（<{MIN_PEERS_QUARTILE}），不给区间")
        if res['applicable']:
            lows = {}
            for tag, key in (('low', 'q1'), ('mid', 'median'), ('high', 'q3')):
                mult = st['after'][key]
                if m == 'peg':
                    pe_equiv = mult * dec(target['np_growth']) * 100
                    equity = pe_equiv * ni
                elif m in ('ev_ebitda', 'ev_sales'):
                    equity = mult * metric[m] - nd
                else:
                    equity = mult * metric[m]
                lows[tag] = {'multiple': mult, 'equity_value': equity}
            res.update(lows)
            res['basis_metric'] = metric.get(m) if m != 'peg' else ni
            if m in ('ev_ebitda', 'ev_sales') and any(lows[t]['equity_value'] <= 0 for t in lows):
                res['warnings'] = ['倍数隐含权益价值 ≤ 0：净债务/少数股东权益相对 EV 过大']
        results[m] = res
    # 综合区间：盈利企业不把 PS/EV-Sales 计入综合（收入倍数忽略盈利能力差异，对高利润率企业系统性偏低）；
    # 亏损企业无盈利倍数可用，才用收入倍数/PB，且已警告降低权重。收入倍数仍单独展示。
    profitable = 'loss' not in flags
    skip = ('ps', 'ev_sales') if profitable else ()
    app_keys = [m for m, r in results.items() if r['applicable'] and m not in skip]
    app = [results[m] for m in app_keys]
    composite = None
    if app:
        composite = {k: median([r[k]['equity_value'] for r in app]) for k in ('low', 'mid', 'high')}
        composite['methods'] = app_keys
        composite['rule'] = ('各适用倍数的 Q1/中位/Q3 隐含权益价值，再分别取中位数（避免单一倍数主导）；'
                             + ('盈利企业不含 PS/EV-Sales' if profitable else '亏损企业含 PS/EV-Sales/PB，权重应降低'))
    return {'flags': flags, 'by_multiple': results, 'composite': composite, 'warnings': warnings}


def run_relative(target, candidates, rules=None, net_debt_like=None):
    """一站式：选同行 → 统计 → 隐含价值。manual_exclude 在 rules 里给，改了即重算。"""
    sel = select_peers(candidates, target, rules)
    stats = peer_statistics(sel['included'])
    val = implied_valuation(target, stats, net_debt_like)
    warnings = list(sel['warnings']) + list(val['warnings'])
    for m, s in stats['stats'].items():
        if s['outliers']:
            b, a = s['before'], s['after']
            warnings.append(f"{LABELS[m]}：IQR 剔除 {len(s['outliers'])} 个异常值（{', '.join(o['code'] for o in s['outliers'])}），"
                            f"中位数 {float(b['median']):.2f}→{float(a['median']):.2f}，均值 {float(b['mean']):.2f}→{float(a['mean']):.2f}")
    return {'selection': sel, 'statistics': stats, 'valuation': val, 'warnings': warnings}
