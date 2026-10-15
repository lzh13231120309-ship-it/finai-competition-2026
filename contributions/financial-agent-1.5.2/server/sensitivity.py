"""敏感性分析工具：二维矩阵、龙卷风、情景并列。与具体模型解耦——调用方传入 fn(杠杆字典)->权益价值。

为什么解耦：竞赛要求"改收入增速/毛利率/资本开支后利润表、FCF、估值同时重算"。
因此四张矩阵里，凡涉及经营驱动（增速×EBIT 率、capex 率×营运资本天数）的格子，fn 都是**整套三表预测 + DCF 的完整重算**，
不是对终值做线性近似；只有 WACC×g、WACC×退出倍数这两张不改变现金流的，fn 才只需重新折现。
每张表都带单位、基期、币种、轴含义，避免"一张没有量纲的数字表"。
"""
from __future__ import annotations

from decimal import Decimal

from .dcf_core import dec, ser

D = Decimal


def axis(center, step, n=2):
    """以 center 为中心、步长 step、两侧各 n 点的对称轴（共 2n+1 点）。"""
    c, s = dec(center), dec(step)
    return [c + s * i for i in range(-n, n + 1)]


def two_way(fn, row_values, col_values, row_name, col_name, base=None, meta=None):
    """二维矩阵。fn({row_name: r, col_name: c, **base}) -> Decimal | None（None 表示该组合被拒绝，如 g≥WACC）。

    返回 values[i][j] 对应 row_values[i]、col_values[j]；被拒绝的格子为 None 并在 rejected 里记录原因位置。
    """
    base = dict(base or {})
    values, rejected = [], []
    for i, r in enumerate(row_values):
        line = []
        for j, c in enumerate(col_values):
            v = fn({**base, row_name: r, col_name: c})
            line.append(v)
            if v is None:
                rejected.append({'row': ser(r), 'col': ser(c)})
        values.append(line)
    flat = [v for line in values for v in line if v is not None]
    return {'row_name': row_name, 'col_name': col_name,
            'rows': [ser(x) for x in row_values], 'cols': [ser(x) for x in col_values],
            'values': values, 'rejected': rejected,
            'min': min(flat) if flat else None, 'max': max(flat) if flat else None,
            'meta': meta or {}}


def tornado(fn, base_levers, specs, base_value=None):
    """龙卷风：逐个杠杆取 low/high，其余保持基准；按摆幅 |high−low| 降序。

    specs: [{'name': 杠杆键, 'label': 展示名, 'low': 值, 'high': 值, 'unit': '...'}]。
    某一端模型拒绝（None）时该杠杆记 rejected 并排在最后，不静默丢弃。
    """
    base_levers = dict(base_levers or {})
    base_value = fn(base_levers) if base_value is None else dec(base_value)
    rows = []
    for sp in specs:
        lo = fn({**base_levers, sp['name']: sp['low']})
        hi = fn({**base_levers, sp['name']: sp['high']})
        swing = abs(hi - lo) if (lo is not None and hi is not None) else None
        rows.append({'name': sp['name'], 'label': sp.get('label', sp['name']), 'unit': sp.get('unit', ''),
                     'low_input': ser(dec(sp['low'])), 'high_input': ser(dec(sp['high'])),
                     'value_at_low': lo, 'value_at_high': hi, 'swing': swing,
                     'swing_pct_of_base': (swing / abs(base_value)) if (swing is not None and base_value) else None,
                     'rejected': swing is None})
    rows.sort(key=lambda r: (r['rejected'], -(r['swing'] or D(0))))
    for rank, r in enumerate(rows, start=1):
        r['rank'] = rank
    return {'base_value': base_value, 'rows': rows}


def scenarios(fn, levers_by_scenario):
    """三情景并列：每个情景给一组杠杆，fn 完整重算。"""
    return {name: fn(levers) for name, levers in levers_by_scenario.items()}
