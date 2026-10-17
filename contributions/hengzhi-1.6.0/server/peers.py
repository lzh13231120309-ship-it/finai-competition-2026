"""同行可比公司分位计算。

竞赛 MVP 硬指标里有一条「**五家**同行可比公司」，消融实验里也有一条
「同行消融：只看公司历史 → 同行分位减少对行业周期的误判」。
这个模块把多份分析运行摊成一张分位表，输出可直接喂给 `forensic_scan`
的 `peer` 参数，形成"取数 → 分位 → 法证加权"的闭环。

三条设计纪律
------------
1. **期间必须对齐。** 拿一家的 2025 年去比另一家的 2024 年，差异里混着行业周期，
   正是消融实验要消除的那种误判。找不到共同期间时，结果里明确标出 `aligned=False`。
2. **只比可比指标。** 收入、资产总额这类规模指标不做同行比较 —— 规模差异不说明
   经营质量，把大公司的绝对额和小公司比毫无意义。比的是比率、天数、周转。
3. **样本不足就说不比。** 某个指标只有一家有值时不硬算分位，进 `missing` 列表。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# 默认比较的指标：跨公司可比的比率与天数类。
DEFAULT_METRICS: list[str] = [
    'gross_margin', 'net_margin', 'roe_total', 'roa', 'asset_turnover',
    'receivables_days', 'inventory_days', 'payables_days', 'cash_conversion_cycle',
    'current_ratio', 'quick_ratio', 'debt_ratio',
    'operating_cash_margin', 'cash_conversion', 'revenue_yoy',
]

# 指标中文名，用于输出可读表格。
METRIC_LABELS: dict[str, str] = {
    'gross_margin': '毛利率', 'net_margin': '净利率', 'roe_total': 'ROE(总权益)',
    'roe': 'ROE(母公司权益)', 'roa': 'ROA', 'asset_turnover': '总资产周转率',
    'receivables_days': '应收周转天数', 'inventory_days': '存货周转天数',
    'payables_days': '应付周转天数', 'cash_conversion_cycle': '现金转换周期',
    'current_ratio': '流动比率', 'quick_ratio': '速动比率', 'cash_ratio': '现金比率',
    'debt_ratio': '资产负债率', 'operating_cash_margin': '经营现金利润率',
    'cash_conversion': '现金含量(OCF/净利)', 'revenue_yoy': '收入同比',
    'net_profit_yoy': '净利润同比', 'free_cash_flow': '自由现金流',
    'ebitda': 'EBITDA',
}


# 天数类指标按 2 位小数呈现，其余比率/倍数按 4 位。
_DAY_METRICS = {'receivables_days', 'inventory_days', 'payables_days',
                'cash_conversion_cycle'}


def _fmt(code: str, value: Decimal) -> str:
    """按指标类型定小数位。

    原始数据的有效精度本来就只有 2–4 位；把 28 位小数的分位表丢给读者，
    既读不了，也会让人误以为数据有那么高的精度。
    """
    places = Decimal('0.01') if code in _DAY_METRICS else Decimal('0.0001')
    return str(value.quantize(places, rounding=ROUND_HALF_UP))


def _dec(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _clean(value: Decimal) -> Decimal:
    """去掉 Decimal 运算产生的尾随零（0.420 → 0.42），但不改变量级。

    插值算 0.42 + 0.03 × 0.0 会得到 Decimal('0.420')。直接 str() 出来就是三位小数，
    和原始数据位数不一致，人看着别扭，下游做字符串比较也容易对不上。
    """
    if value == value.to_integral_value():
        return value.quantize(Decimal(1))
    return value.normalize()


def _quantile(sorted_values: list[Decimal], q) -> Decimal:
    """线性插值分位，与 numpy.percentile 默认口径一致。输入须已升序且非空。"""
    n = len(sorted_values)
    if n == 1:
        return _clean(sorted_values[0])
    pos = Decimal(str(q)) * (n - 1)
    low = int(pos)
    high = min(low + 1, n - 1)
    frac = pos - Decimal(low)
    return _clean(sorted_values[low] + (sorted_values[high] - sorted_values[low]) * frac)


def percentile_rank(values: list[Decimal], target: Decimal):
    """target 在样本中的分位位置（0–1）。并列值按中点计，避免全等时给出 0 或 1。"""
    if not values:
        return None
    below = sum(1 for v in values if v < target)
    equal = sum(1 for v in values if v == target)
    return ((Decimal(below) + Decimal(equal) / 2) / Decimal(len(values))).quantize(
        Decimal('0.0001'))


def _index_run(run: dict) -> dict:
    """把一份运行摊成 {(code, period, scope): Decimal}。"""
    out: dict[tuple[str, str, str], Decimal] = {}
    for metric in run.get('metrics') or []:
        code, period = metric.get('code'), metric.get('period')
        if not code or not period:
            continue
        value = _dec(metric.get('value'))
        if value is None:
            continue
        out.setdefault((str(code), str(period), str(metric.get('scope') or 'consolidated')),
                       value)
    return out


def _company_name(run: dict) -> str:
    for collection in ('metrics', 'facts'):
        for item in run.get(collection) or []:
            if item.get('company'):
                return str(item['company'])
    return str(run.get('run_id') or '')[:8]


def _common_period(indexes: list[dict], metrics: list[str]):
    """找所有公司都齐的最新期间。返回 (period, aligned)。"""
    counts: dict[str, int] = {}
    for index in indexes:
        periods = {period for (code, period, _scope) in index if code in metrics}
        for period in periods:
            counts[period] = counts.get(period, 0) + 1
    if not counts:
        return None, False
    full = sorted(p for p, c in counts.items() if c == len(indexes))
    if full:
        return full[-1], True
    return max(counts), False


def rule_payload(payload: dict) -> dict:
    """把"按指标代码索引"的分位表转成"按规则号索引"，直接喂给 `forensic_scan`。

    法证规则只知道规则号（R_AR_01…），同行引擎只产出指标代码（receivables_days…），
    中间缺一层翻译。这里按 `forensics.RULE_PEER_METRIC` 建反向索引，一个指标可以
    对应多条规则（如 operating_cash_margin 同时服务 R_SETTLE_01 与 R_OCF_01）。
    """
    from .forensics import RULE_PEER_METRIC      # 延迟导入，避免模块级循环依赖
    out: dict[str, dict] = {}
    for rule_id, code in RULE_PEER_METRIC.items():
        entry = (payload or {}).get(code)
        if isinstance(entry, dict):
            out[rule_id] = entry
    return out


def benchmark(runs: list, metrics=None, period: str | None = None,
              target_run_id: str | None = None) -> dict:
    """算出同行分位表。

    `runs` 是各公司 `analyze_financial_report` 的运行字典。
    `target_run_id` 指定目标公司，输出里会标出它相对同行的位置。
    """
    chosen = [str(m) for m in (metrics or DEFAULT_METRICS)]
    runs = [r for r in (runs or []) if isinstance(r, dict)]
    if len(runs) < 2:
        return {'ok': False, 'error': '同行比较至少需要两家公司的分析运行',
                'companies': [], 'metrics': {}, 'peer_payload': {}}

    indexes = [_index_run(r) for r in runs]
    names = [_company_name(r) for r in runs]
    run_ids = [str(r.get('run_id') or '') for r in runs]

    aligned = True
    if period:
        chosen_period = str(period)
    else:
        chosen_period, aligned = _common_period(indexes, chosen)

    target_position = None
    if target_run_id:
        for position, run_id in enumerate(run_ids):
            if run_id == str(target_run_id):
                target_position = position
                break
        if target_position is None:
            return {'ok': False, 'error': f'目标运行不在传入的 run 列表里：{target_run_id}',
                    'companies': [], 'metrics': {}, 'peer_payload': {}}

    table: dict[str, dict] = {}
    payload: dict[str, dict] = {}
    missing: list[dict] = []
    outliers: list[dict] = []

    for code in chosen:
        values: dict[str, Decimal] = {}
        for name, index in zip(names, indexes):
            value = index.get((code, chosen_period, 'consolidated'))
            if value is None:
                continue
            values[name] = value
        if len(values) < 2:
            missing.append({'code': code, 'label': METRIC_LABELS.get(code, code),
                            'available': len(values),
                            'reason': '可比样本不足两家，不做分位计算'})
            continue

        ordered = sorted(values.values())
        median, p25, p75 = (_quantile(ordered, 0.5), _quantile(ordered, 0.25),
                            _quantile(ordered, 0.75))
        entry = {
            'label': METRIC_LABELS.get(code, code),
            'n': len(ordered),
            'median': _fmt(code, median), 'p25': _fmt(code, p25), 'p75': _fmt(code, p75),
            'min': _fmt(code, ordered[0]), 'max': _fmt(code, ordered[-1]),
            'values': {name: _fmt(code, value) for name, value in
                       sorted(values.items(), key=lambda kv: kv[1])},
        }
        if target_position is not None:
            target_name = names[target_position]
            target_value = values.get(target_name)
            if target_value is not None:
                entry['target'] = _fmt(code, target_value)
                entry['target_percentile'] = str(percentile_rank(ordered, target_value))
                if target_value < p25:
                    side = 'below'
                elif target_value > p75:
                    side = 'above'
                else:
                    side = 'within'
                entry['target_position'] = side
                if side != 'within':
                    outliers.append({
                        'code': code, 'label': METRIC_LABELS.get(code, code),
                        'target': _fmt(code, target_value), 'p25': _fmt(code, p25),
                        'p75': _fmt(code, p75), 'median': _fmt(code, median),
                        'side': side,
                        'target_percentile': entry['target_percentile']})
        table[code] = entry
        # 这一份可以直接作为 forensic_scan 的 peer 参数
        payload[code] = {'code': code, 'median': _fmt(code, median),
                         'p25': _fmt(code, p25), 'p75': _fmt(code, p75),
                         'n': len(ordered)}

    result = {
        'ok': bool(table),
        'period': chosen_period,
        'aligned': aligned,
        'companies': [{'run_id': run_id, 'company': name} for run_id, name in
                      zip(run_ids, names)],
        'metrics': table,
        'peer_payload': payload,
        # 同一份数据的第二种索引方式：按法证规则号。喂给 forensic_scan 的是这个，
        # 因为规则按 rule_id 查同行分位。
        'rule_payload': rule_payload(payload),
        'outliers': outliers,
        'missing': missing,
    }
    if not aligned:
        result['warning'] = (
            f'各公司没有共同报告期，已改用 {chosen_period}；'
            '期间不一致的同行分位会把行业周期混进差异里，结论需谨慎。')
    if target_position is None:
        result['note'] = ('未指定 target_run_id，只输出分位基准。'
                          '把其中一家指定为目标，即可得到它在同行中的位置。')
    return result
