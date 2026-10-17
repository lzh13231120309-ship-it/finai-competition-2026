"""稳定期纪律校验与估值方法三角。

两件事，都是"模型算完之后必须有人管"的部分。

一、稳定期（终值）纪律
----------------------
终值通常占 DCF 企业价值的 60%–80%，却最容易被随手填一个增长率了事。
`valuation_advanced.validate` 已经管了两条硬约束（g < WACC、g ≤ 稳定期 ROIC），
但还缺三条同样硬、且**能靠常识判定对错**的上界：

1. **g 不应超过长期名义 GDP 增速。** 一家公司永续增长快于整个经济体，
   等于说它最终会大于经济本身。这不是"乐观"，是逻辑错误。
   中国长期名义 GDP 增速按 4% 设上限（实际 GDP 约 3%+ 通胀约 1%，取保守值）。
2. **g 不应超过无风险利率。** 长期看，永续增长率高于名义无风险利率意味着
   该资产的久期趋于无穷、价值趋于无穷 —— 数学上终值公式仍在，经济上已失真。
3. **g 与显式期末年的增速应当衔接。** 若显式期最后一年增速 12%，
   稳定期却一步跳到 3%，中间缺少过渡，终值会低估；反之则高估。
   本模块检查这个落差并给出提示，而不是自动"修正"。

这三条都输出为**可机检的检查项**，不抛异常、不阻断建模 ——
因为偶尔确实存在合理的高增长假设，此时需要的是"写清理由"，而不是"算不出来"。

二、估值方法三角
----------------
DCF、相对估值、资产基础法给出的往往是三个差很远的数。多数报告的处理方式是
"选一个看起来最好的写上去"，或者"取个平均"而不说为什么。本模块的做法是：

- 每个方法带**权重依据**（数据质量、方法适配性），权重可追溯；
- 数据不可用的方法**退出加权**并把权重按比例分给其余方法，而不是用 0 值填；
- 输出**方法间离散度**：三个方法相差 2 倍以上时，结论是"估值区间宽，
  不宜作为单点判断依据"，而不是"取中位数"。

本模块只做汇总与诊断，不改变任何单个方法的结果。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext

D = Decimal
ZERO = D(0)
ONE = D(1)

# 中国长期名义 GDP 增速上限。实际 GDP 增速约 3%–4%、通胀目标约 2%，
# 取 4% 作为"公司永续增长不应超过"的保守上界。
LONG_RUN_NOMINAL_GDP = D('0.04')

# 显式期末年增速与稳定期增速的最大可接受落差。超过此值说明两条腿没接上。
MAX_FADE_GAP = D('0.06')

# 终值占企业价值比例超过此值时，结论几乎完全由永续假设决定。
TERMINAL_SHARE_ALERT = D('0.75')

# 方法间离散度（(最高 − 最低) / 中位）超过此值时判定为"结论不稳"。
DISPERSION_ALERT = D('0.50')


def _dec(value, default=None):
    if value is None or value == '' or isinstance(value, bool):
        return default
    if isinstance(value, D):
        return value
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default
    return result if result.is_finite() else None


def _pct(value, digits=2):
    v = _dec(value)
    if v is None:
        return '—'
    return f"{(v * 100).quantize(D(1).scaleb(-digits))}%"


def terminal_growth_check(params: dict, risk_free_rate=None,
                          explicit_growth_path=None) -> dict:
    """稳定期假设的纪律校验。返回检查项列表，不抛异常、不修改参数。

    `explicit_growth_path` 传显式期逐年的收入增速，用于检查与稳定期的衔接。
    """
    g = _dec(params.get('terminal_growth'))
    wacc = _dec(params.get('wacc'))
    roic = _dec(params.get('terminal_roic'))
    rf = _dec(risk_free_rate)
    checks: list[dict] = []
    notes: list[str] = []

    if g is None:
        return {'ok': False, 'checks': [
            {'name': 'terminal_growth_present', 'ok': False,
             'detail': '未提供永续增长率，无法校验稳定期假设。'}], 'notes': []}

    checks.append({
        'name': 'terminal_growth_below_wacc',
        'ok': bool(wacc is None or g < wacc),
        'detail': (f'永续增长率 {_pct(g)} 应低于折现率 {_pct(wacc)}；'
                   '否则终值公式不成立（分母非正）。'
                   if wacc is not None else '未提供折现率，无法校验该项。'),
    })
    checks.append({
        'name': 'terminal_growth_within_economy',
        'ok': g <= LONG_RUN_NOMINAL_GDP,
        'detail': (f'永续增长率 {_pct(g)} 未超过长期名义 GDP 上限 {_pct(LONG_RUN_NOMINAL_GDP)}。'
                   if g <= LONG_RUN_NOMINAL_GDP else
                   f'永续增长率 {_pct(g)} 超过长期名义 GDP 上限 {_pct(LONG_RUN_NOMINAL_GDP)}，'
                   '等于假设该公司最终大于整个经济体 —— 这不是乐观，是逻辑错误。'
                   '若确有依据（如行业集中度持续提升），必须在报告中写明理由。'),
    })
    if rf is not None and g > rf:
        # 这条**不判失败**，只作提示。理由：当期无风险利率可能处于历史低位
        # （中国 10 年期国债约 1.8%），拿它约束长期永续增长等于用短期利率管
        # 长期增长。真正的硬约束是"不超过长期名义 GDP 增速"，那条在上面。
        notes.append(
            f'永续增长率 {_pct(g)} 高于当期无风险利率 {_pct(rf)}。'
            '若当期无风险利率处于历史低位，这属正常现象，不构成逻辑错误；'
            '但报告里应当说明所用的无风险利率是估值日当期值还是长期均衡值 —— '
            '这个选择会同时影响折现率与稳定期上限。')
    if roic is not None:
        checks.append({
            'name': 'terminal_growth_within_roic',
            'ok': g <= roic,
            'detail': (f'永续增长率 {_pct(g)} 未超过稳定期 ROIC {_pct(roic)}；'
                       f'对应再投资率 {_pct(g / roic) if roic else "—"}。'
                       if g <= roic else
                       f'永续增长率 {_pct(g)} 超过稳定期 ROIC {_pct(roic)}：'
                       '再投资率会超过 100%，即每赚一元要投超过一元，不可持续。'),
        })

    # 显式期与稳定期的衔接
    if explicit_growth_path:
        path = [_dec(v) for v in explicit_growth_path]
        path = [v for v in path if v is not None]
        if path:
            last = path[-1]
            gap = last - g
            checks.append({
                'name': 'growth_fade_continuity',
                'ok': abs(gap) <= MAX_FADE_GAP,
                'detail': (f'显式期末年增速 {_pct(last)} 与稳定期 {_pct(g)} 相差 {_pct(gap)}，'
                           f'在 {_pct(MAX_FADE_GAP)} 的可接受落差内。'
                           if abs(gap) <= MAX_FADE_GAP else
                           f'显式期末年增速 {_pct(last)} 与稳定期 {_pct(g)} 相差 {_pct(gap)}，'
                           f'超过 {_pct(MAX_FADE_GAP)} 的可接受落差：两条腿没接上，'
                           '终值会系统性偏离。请调整显式期路径末段或说明理由。'),
            })
            if all(v > g for v in path):
                notes.append('显式期各年增速均高于稳定期增速，属正常的收敛形态。')

    # 终值占比
    share = _dec(params.get('terminal_value_share') or params.get('_terminal_value_share'))
    if share is not None:
        checks.append({
            'name': 'terminal_value_share',
            'ok': share <= TERMINAL_SHARE_ALERT,
            'detail': (f'终值占企业价值 {_pct(share)}'
                       + ('，在 75% 以内。' if share <= TERMINAL_SHARE_ALERT else
                          '，超过 75%：结论几乎完全由永续假设决定，'
                          '必须展示敏感性矩阵并说明稳定期假设的依据。')),
        })

    failed = [c for c in checks if not c['ok']]
    return {
        'ok': not failed,
        'terminal_growth': g, 'wacc': wacc, 'terminal_roic': roic,
        'risk_free_rate': rf,
        'checks': checks,
        'failed': [c['name'] for c in failed],
        'notes': notes,
        'summary': ('稳定期假设通过全部纪律校验。' if not failed else
                    f'稳定期假设有 {len(failed)} 项未通过：' + '、'.join(c['name'] for c in failed)),
    }


# ---------------------------------------------------------------------------
# 估值方法三角
# ---------------------------------------------------------------------------

# 各方法的默认权重与依据。权重不是"拍出来的"，而是按"该方法的输入有多少
# 来自可核验数据"设定；数据不可用时整块权重退出，按比例分给其余方法。
_METHOD_SPEC = {
    'dcf': {'label': '现金流折现（FCFF）', 'weight': D('0.45'),
            'basis': '驱动变量逐项锚定到已核验事实，是最可追溯的方法'},
    'residual_income': {'label': '剩余收益（权益法）', 'weight': D('0.30'),
                        'basis': '适用于金融与高分红企业；账面权益为起点，对终值假设依赖较低'},
    'comparables': {'label': '相对估值（可比倍数）', 'weight': D('0.25'),
                    'basis': '需要真实可比公司的市场倍数；缺此数据时权重退出'},
    'asset_based': {'label': '资产基础法', 'weight': D('0.10'),
                    'basis': '需提供独立资产评估与负债调整；账面值仅作参照，不代表价值下限'},
}


def method_triangle(methods: dict, shares=None) -> dict:
    """把各估值方法汇总成加权价值区间，并诊断方法间分歧。

    `methods` 形如::

        {'dcf': {'equity_value': Decimal(...), 'ok': True},
         'comparables': {'equity_value': ..., 'ok': True, 'n': 5},
         'asset_based': {'equity_value': ..., 'ok': True}}

    只汇总 `ok` 为真且权益价值为正的方法。全部不可用时明确报"无法给出区间"，
    不用默认值填一个看起来像结论的数。
    """
    available: dict[str, dict] = {}
    excluded: list[dict] = []
    for key, spec in _METHOD_SPEC.items():
        entry = (methods or {}).get(key)
        value = _dec(entry.get('equity_value')) if isinstance(entry, dict) else None
        if not isinstance(entry, dict) or not entry.get('ok') or value is None or value <= 0:
            excluded.append({
                'method': key, 'label': spec['label'],
                'reason': ('未提供该方法的输入' if not isinstance(entry, dict)
                           else entry.get('reason') or '结果不可用或权益价值非正'),
            })
            continue
        if entry.get('basis') == 'book_value_reference':
            excluded.append({'method': key, 'label': '账面净资产参照', 'reason': '会计账面值不作为独立资产估值加入加权', 'book_value': value})
            continue
        available[key] = {'label': spec['label'], 'weight': spec['weight'],
                          'basis': spec['basis'], 'equity_value': value,
                          'detail': {k: v for k, v in entry.items()
                                     if k not in ('equity_value', 'ok')}}

    if not available:
        return {'ok': False, 'reason': '没有任何可用方法，无法给出估值区间。',
                'excluded': excluded, 'methods': []}

    total_weight = sum((m['weight'] for m in available.values()), ZERO)
    rows = []
    weighted = ZERO
    for key, item in available.items():
        normalised = item['weight'] / total_weight
        weighted += item['equity_value'] * normalised
        rows.append({'method': key, 'label': item['label'],
                     'equity_value': item['equity_value'],
                     'weight': normalised, 'weight_basis': '项目预设权重，未作统计校准；' + item['basis'],
                     'detail': item['detail']})
    values = sorted(item['equity_value'] for item in available.values())
    low, high = values[0], values[-1]
    mid = values[len(values) // 2]
    dispersion = ((high - low) / mid) if mid else None

    result = {
        'ok': True,
        'methods': rows,
        'excluded': excluded,
        'weighted_value': weighted,
        'range': {'low': low, 'high': high, 'mid': mid,
                  'text': f'各方法权益价值区间 {low} – {high} 元'},
        'dispersion': dispersion,
        'note': ('权重为项目预设建模假设，并非按数据质量自动测得；不可作为独立目标价。'
                 '不可用方法的权重按比例分给其余方法，不用 0 值填。'),
    }
    if dispersion is not None and dispersion > DISPERSION_ALERT:
        result['dispersion_alert'] = True
        result['dispersion_note'] = (
            f'各方法结果离散度 {_pct(dispersion)}（最高与最低相差 '
            f'{float(high / low):.2f} 倍），超过 {_pct(DISPERSION_ALERT)} 的阈值。'
            '这说明结论对方法选择高度敏感，应当**呈现区间并说明各方法的适用条件**，'
            '而不是取中位数当作单点结论。')
    else:
        result['dispersion_alert'] = False
    share_count = _dec(shares)
    if share_count and share_count > 0:
        result['per_share'] = {
            'weighted': weighted / share_count,
            'low': low / share_count, 'high': high / share_count,
        }
    return result
