"""Deterministic financial assertions: identity, date, scope, amount and citations.

No model or number-only lookup is used. Unsupported/ambiguous assertions stay
unverifiable; this module never turns absence of evidence into a contradiction.
Values in the result use yuan for amounts and fractions for percentage ratios.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

ALIASES = {
    'revenue': ('营业收入', '营收', '收入'),
    'cost_of_revenue': ('营业成本',),
    'parent_net_profit': ('归属于母公司股东的净利润', '归属于上市公司股东的净利润', '归母净利润'),
    'net_profit': ('净利润',),
    'nonrecurring_adj_parent_profit': ('归属于上市公司股东的扣除非经常性损益的净利润', '扣除非经常性损益后的归属于上市公司股东的净利润', '扣非归母净利润', '扣非净利润'),
    'operating_cash_flow': ('经营活动产生的现金流量净额', '经营活动现金流净额', '经营现金流净额'),
    'assets': ('资产总计', '资产总额', '总资产'),
    'liabilities': ('负债合计', '负债总额', '总负债'),
    'parent_equity': ('归属于母公司股东的权益', '归母权益'),
    'equity': ('所有者权益合计', '股东权益合计', '所有者权益', '净资产'),
    'operating_profit': ('营业利润',), 'profit_before_tax': ('利润总额',),
    'income_tax': ('所得税费用',), 'gross_profit': ('毛利润', '毛利'),
    'cash': ('货币资金',), 'inventory': ('存货',), 'receivables': ('应收账款',),
    'payables': ('应付账款',), 'capex': ('资本开支', '资本支出'),
    'gross_margin': ('销售毛利率', '毛利率'), 'net_margin': ('净利率', '净利润率'),
    'roe': ('净资产收益率', 'ROE'), 'roa': ('总资产收益率', 'ROA'),
    'debt_ratio': ('资产负债率',), 'current_ratio': ('流动比率',),
    'quick_ratio': ('速动比率',), 'basic_eps': ('基本每股收益',),
}
SCALES = {'亿元': Decimal('1e8'), '亿': Decimal('1e8'), '百万元': Decimal('1e6'),
          '万元': Decimal('1e4'), '万': Decimal('1e4'), '千元': Decimal('1e3'),
          '元': Decimal(1), '%': Decimal('.01'), '％': Decimal('.01'), '倍': Decimal(1)}
NUMBER = re.compile(r'(?<![\d.])(?P<open>[（(])?(?P<num>[+\-−]?\d+(?:,\d{3})*(?:\.\d+)?)(?P<close>[）)])?\s*(?P<unit>亿元|百万元|万元|千元|元|亿|万|%|％|倍)?')
EVIDENCE = re.compile(r'(?<![0-9a-z])[0-9a-f]{8,64}:p\d+:[bt]\d+(?![0-9a-z])')
DATE = re.compile(r'(20\d{2})(?:\s*年)?(?:[-/](\d{1,2})[-/](\d{1,2})|\s*(?:年)?(上半年|半年|中期|前三季度|一季度|三季度|第四季度|四季度|第二季度|第三季度|第一季度|年度|全年|年末))?')


def _decimal(value):
    try:
        number = Decimal(str(value).replace(',', '').replace('−', '-'))
        return number if number.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def _str(value):
    return format(value.normalize(), 'f') if value is not None else None


def _evidence_matches(left, right):
    """Accept full/short document hashes only for the same page and block."""
    left_parts, right_parts = str(left).split(':', 1), str(right).split(':', 1)
    if len(left_parts) != 2 or len(right_parts) != 2 or left_parts[1] != right_parts[1]:
        return False
    a, b = left_parts[0], right_parts[0]
    return min(len(a), len(b)) >= 8 and (a.startswith(b) or b.startswith(a))


def _scope(text):
    # “归属于母公司股东” denotes a field, not parent-only statements.
    parent = bool(re.search(r'(?<!于)母公司|单体', text)) or text.strip() == '母公司'
    consolidated = bool(re.search(r'合并|集团口径', text))
    return 'ambiguous' if parent and consolidated else ('parent' if parent else 'consolidated' if consolidated else None)


def _period(text):
    hits = list(DATE.finditer(EVIDENCE.sub('', text)))
    if not hits:
        return None
    m = hits[-1]
    year, month, day, suffix = m.groups()
    if month:
        return f'{year}-{int(month):02d}-{int(day):02d}'
    ends = {'上半年': '06-30', '半年': '06-30', '中期': '06-30', '前三季度': '09-30',
            '三季度': '09-30', '第三季度': '09-30', '第二季度': '06-30', '第一季度': '03-31', '一季度': '03-31'}
    return f'{year}-{ends.get(suffix, "12-31")}'


def _aliases(pool):
    items = [(alias, code) for code, aliases in ALIASES.items() for alias in aliases]
    items += [(str(p['label']), p['code']) for p in pool if p.get('label') and p.get('code')]
    # Longest alternative prevents 净利润 inside 归母净利润 stealing the identity.
    lookup = {}
    for alias, code in items:
        lookup.setdefault(alias, code)
    pattern = re.compile('|'.join(re.escape(a) for a in sorted(lookup, key=len, reverse=True)), re.I)
    return lookup, pattern


def _currency(text):
    found = {code for pattern, code in ((r'人民币|CNY|RMB', 'CNY'), (r'美元|USD', 'USD'),
             (r'港元|港币|HKD', 'HKD'), (r'欧元|EUR', 'EUR')) if re.search(pattern, text, re.I)}
    return next(iter(found)) if len(found) == 1 else 'ambiguous' if found else None


def _duration(text):
    if re.search(r'单季|第四季度|四季度|第二季度|第三季度|第一季度', text):
        return 'quarter'
    if re.search(r'前三季度|上半年|半年|中期', text):
        return 'ytd'
    if re.search(r'年度|全年|年末', text):
        return 'annual'
    return None


def _trusted(pool):
    out = []
    for original in pool or []:
        p = dict(original)
        if p.get('status') not in (None, 'verified', 'calculated'):
            continue
        if p.get('source') == 'valuation':
            continue  # scenario assumptions do not prove historical assertions
        value = _decimal(p.get('value'))
        if value is None:
            continue
        p['value'] = value
        p['kind'] = p.get('kind') or ('ratio' if p.get('unit') == 'ratio' else 'amount')
        out.append(p)
    return out


def _tokens(raw, default_unit=None):
    masked = EVIDENCE.sub(lambda m: ' ' * len(m[0]), raw)
    masked = DATE.sub(lambda m: ' ' * len(m[0]), masked)
    for m in NUMBER.finditer(masked):
        unit = m['unit'] or default_unit
        if not unit:
            continue
        value = _decimal(m['num'])
        if m['open'] and m['close']:
            value = -abs(value)
        if value is not None:
            decimals = len(m['num'].split('.')[-1]) if '.' in m['num'] else 0
            yield m, value * SCALES[unit], unit, Decimal(1).scaleb(-decimals) * SCALES[unit] / 2


def _derived(code, period, scope, pool):
    """Only compute YoY from comparable annual verified values, never assumptions."""
    if not code or not code.endswith('_yoy') or not period or not period.endswith('-12-31'):
        return []
    base = code[:-4]
    prior = str(int(period[:4]) - 1) + period[4:]
    current = [p for p in pool if p.get('code') == base and p.get('period') == period and p.get('scope') == scope]
    previous = [p for p in pool if p.get('code') == base and p.get('period') == prior and p.get('scope') == scope]
    if len(current) != 1 or len(previous) != 1 or previous[0]['value'] <= 0:
        return []
    ids = list(dict.fromkeys(current[0].get('evidence_ids', []) + previous[0].get('evidence_ids', [])))
    return [{'code': code, 'period': period, 'scope': scope, 'kind': 'ratio',
             'value': current[0]['value'] / previous[0]['value'] - 1, 'evidence_ids': ids}]


def review_claims(text: str, pool: list[dict], run: dict | None = None) -> dict:
    """Extract reviewable numeric assertions and validate semantic evidence support.

    ``coverage`` is determinate assertions / all extracted assertions, from 0 to 1.
    Source rows with explicit non-verified status cannot support a claim. A pool
    produced by report_audit.build_pool is already vetted and has no status field.
    """
    original_pool = pool
    pool = _trusted(pool)
    lookup, fields = _aliases(pool)
    run = run or {}
    quality = run.get('quality') or {}
    context_scope = run.get('reporting_scope') or quality.get('reporting_scope')
    context_period = run.get('reporting_period') or quality.get('reporting_period')
    expected_currency = run.get('reporting_currency') or run.get('currency') or quality.get('reporting_currency') or quality.get('currency')
    currencies = {p.get('currency') for p in pool if p.get('currency')}
    expected_currency = expected_currency or (next(iter(currencies)) if len(currencies) == 1 else None)
    expected_company = run.get('company') or quality.get('company')
    context_currency = None
    context_company = None
    context_duration = None
    default_unit = None
    headers = None
    claims = []
    evidence_index = {eid for p in pool for eid in p.get('evidence_ids', [])}

    def add(raw, line, code, period, scope, value, unit, tolerance):
        claim = {'id': f'C{len(claims) + 1:04d}', 'line': line, 'raw': raw,
                 'code': code, 'period': period, 'scope': scope, 'value': _str(value),
                 'unit': unit, 'display_tolerance': str(tolerance), 'status': 'unverifiable', 'expected_value': None,
                 'evidence_ids': [], 'reason': '', 'advice': ''}
        candidates = [p for p in pool if p.get('code') == code and p.get('period') == period
                      and p.get('scope') == scope and p['kind'] in (('ratio',) if unit in ('%', '％') else ('ratio', 'other') if unit == '倍' else ('amount',))]
        if not candidates:
            candidates = _derived(code, period, scope, pool)
        report_currency = _currency(raw) or context_currency
        duration = _duration(raw) or context_duration
        if report_currency and (report_currency == 'ambiguous' or report_currency != expected_currency):
            claim['reason'] = '报告币种与已核验数据币种不一致或数据币种未明确。'
            claim['advice'] = '核对币种，汇率换算需单独提供可追溯计算证据。'
        elif context_company and expected_company and context_company != expected_company:
            claim['reason'] = '报告明确公司名称与分析运行主体不一致。'
            claim['advice'] = '切换到对应公司数据后重新核验。'
        elif duration and candidates and any(p.get('period_type') != duration for p in candidates) and (duration == 'quarter' or any(p.get('period_type') for p in candidates)):
            claim['reason'] = '报告期间长度与数据不一致，或无法确认单季/累计口径。'
            claim['advice'] = '补充明确 period_type 的同口径数据；同一截止日不能证明期间长度一致。'
        elif not code or not period or scope not in ('parent', 'consolidated'):
            claim['reason'] = '科目、期间或报表口径未明确，不能仅凭相同数字核验。'
            claim['advice'] = '补充明确科目、报告期间及合并/母公司口径。'
        elif len({p.get('company') for p in candidates if p.get('company')}) > 1:
            claim['reason'] = '候选事实包含多个公司主体，不能把同额跨公司数据视为证明。'
            claim['advice'] = '按明确公司主体选择或重新分析来源。'
        elif expected_company and any(p.get('company') and p.get('company') != expected_company for p in candidates):
            claim['reason'] = '候选事实主体与指定分析公司不一致。'
            claim['advice'] = '核对公司主体与分析依据。'
        elif not candidates:
            claim['reason'] = '没有同科目、同期间、同口径的已核验数据。'
            claim['advice'] = '补充对应原文证据并完成事实核验。'
        elif len({p['value'] for p in candidates}) > 1:
            claim['reason'] = '同一科目期间口径存在多值，须先消除冲突。'
            claim['advice'] = '核对公司主体、重述版本和报表期间。'
        else:
            expected = candidates[0]['value']
            ids = list(dict.fromkeys(e for p in candidates for e in p.get('evidence_ids', [])))
            cited = set(EVIDENCE.findall(raw))
            claim['expected_value'], claim['evidence_ids'] = _str(expected), ids
            correction = f'{_str(expected / SCALES[unit])}{unit}'
            if not ids:
                claim['reason'] = '匹配数据缺少可回溯证据。'
                claim['advice'] = '补充原文页码及证据 ID。'
            elif cited and not any(_evidence_matches(c, e) for c in cited for e in ids):
                known = {c for c in cited if any(_evidence_matches(c, e) for e in evidence_index)}
                claim['status'] = 'contradicted' if known else 'unverifiable'
                claim['reason'] = '引用存在但不支持该科目期间口径。' if known else '引用 ID 未出现在已核验证据中。'
                claim['advice'] = '改用支持此断言的证据：' + '、'.join(ids)
            elif abs(expected - value) <= tolerance:
                claim['status'] = 'verified'
                claim['reason'] = '科目、期间、口径和数值一致（允许显示精度内四舍五入）。'
                claim['advice'] = ''
            else:
                claim['status'] = 'contradicted'
                claim['reason'] = '同科目、同期间、同口径的数值不一致。'
                claim['advice'] = f'将此数值更正为 {correction}，并保留支持证据。'
        claims.append(claim)

    for lineno, raw in enumerate((text or '').splitlines(), 1):
        explicit_scope = _scope(raw)
        company_header = re.fullmatch(r'\s*#*\s*(?:公司名称|分析公司|公司)\s*[:：]\s*(.+?)\s*', raw)
        if company_header:
            context_company = company_header[1]
        if len(raw) < 100 and (raw.lstrip().startswith('#') or not list(_tokens(raw))):
            context_currency = _currency(raw) or context_currency
            context_duration = _duration(raw) or context_duration
        # Short report headings establish context, regular sentences do not leak it.
        if len(raw) < 100 and (raw.lstrip().startswith('#') or not list(_tokens(raw))):
            context_scope = explicit_scope or context_scope
            context_period = _period(raw) or context_period
        unit_match = re.search(r'单位\s*[:：]?\s*(亿元|百万元|万元|千元|元|%|％)', raw)
        if unit_match:
            default_unit = unit_match[1]
        cells = [c.strip() for c in raw.strip().strip('|').split('|')] if '|' in raw else None
        if cells and any(_period(c) for c in cells) and not fields.search(cells[0]):
            headers = cells
            continue
        if cells and all(re.fullmatch(r'[:\-\s]*', c) for c in cells):
            continue
        if cells and headers:
            label = cells[0]
            if MODEL_FIELD.search(label):
                continue
            field = next(fields.finditer(label), None)
            code = lookup.get(field[0]) if field else None
            if code and '同比' in label:
                code += '_yoy'
            for col, cell in enumerate(cells[1:], 1):
                header = headers[col] if col < len(headers) else ''
                header_unit = re.search(r'亿元|百万元|万元|千元|元|%|％|倍', header)
                label_unit = re.search(r'亿元|百万元|万元|千元|元|%|％|倍', label)
                unit = label_unit[0] if label_unit else header_unit[0] if header_unit else default_unit
                for _, value, unit, tolerance in _tokens(cell, unit):
                    add(raw, lineno, code, _period(header) or context_period,
                        explicit_scope or context_scope, value, unit, tolerance)
            continue
        if not cells:
            headers = None
        for sentence in re.split(r'[。；;]', raw):
            found = list(fields.finditer(sentence))
            tokens = list(_tokens(sentence))
            respective = '分别' in sentence and len(found) == len(tokens)
            for token_index, (token, value, unit, tolerance) in enumerate(tokens):
                preceding = [f for f in found if f.end() <= token.start()]
                model_preceding = [f for f in MODEL_FIELD.finditer(sentence) if f.end() <= token.start()]
                if model_preceding and (not preceding or model_preceding[-1].end() > preceding[-1].end()):
                    continue
                field = found[token_index] if respective else preceding[-1] if preceding else None
                code = lookup.get(field[0]) if field else None
                between = sentence[field.end():token.start()] if field else ''
                if code and unit in ('%', '％') and re.search(r'同比|增长|下降|减少', between):
                    code += '_yoy'
                    if re.search(r'下降|减少', between):
                        value = -abs(value)
                claim_period = _period(sentence[:token.start()]) or context_period
                if field and code and code.endswith('_yoy'):
                    claim_period = _period(sentence[:field.start()]) or claim_period
                add(sentence, lineno, code, claim_period,
                    _scope(sentence) or context_scope, value, unit, tolerance)
    claims.extend(review_model_claims(text, original_pool)['claims'])
    for index, claim in enumerate(claims, 1):
        claim['id'] = f'C{index:04d}'
    stats = {key: sum(c['status'] == key for c in claims) for key in ('verified', 'contradicted', 'unverifiable')}
    stats['checked'] = len(claims)
    stats['coverage'] = (stats['verified'] + stats['contradicted']) / len(claims) if claims else 0.0
    return {'claims': claims, 'stats': stats}


MODEL_ALIASES = {'企业价值': 'enterprise_value', '股权价值': 'equity_value',
                 '每股价值': 'value_per_share', '每股估值': 'value_per_share',
                 'WACC': 'wacc', '加权平均资本成本': 'wacc',
                 '永续增长率': 'terminal_growth', '终值增长率': 'terminal_growth'}
MODEL_FIELD = re.compile('|'.join(map(re.escape, MODEL_ALIASES)), re.I)
SCENARIOS = {'基准': 'base', '基础': 'base', 'base': 'base', '乐观': 'optimistic',
             'optimistic': 'optimistic', '压力': 'stress', '悲观': 'stress', 'stress': 'stress'}
SCENARIO_RE = re.compile('|'.join(SCENARIOS), re.I)


def _scenario(text):
    hits = list(SCENARIO_RE.finditer(text))
    return SCENARIOS[hits[-1][0].lower()] if hits else None


def review_model_claims(text: str, pool: list[dict]) -> dict:
    """Check explicitly named conditional model results without proving fair value.

    A matching scenario gets financial status ``unverifiable`` and separate
    ``model_status=conditional_model_result_match``. A different reported result
    is contradicted relative to that saved model, not relative to market value.
    """
    claims = []
    context_scenario = None
    context_currency = None
    default_unit = None
    headers = None

    def add(raw, line, code, scenario, value, unit, tolerance):
        claim = {'id': '', 'line': line, 'raw': raw, 'code': code, 'period': None,
                 'scope': None, 'scenario': scenario, 'display_tolerance': str(tolerance), 'value': _str(value), 'unit': unit,
                 'status': 'unverifiable', 'model_status': 'unverifiable',
                 'expected_value': None, 'evidence_ids': [],
                 'reason': '情景未明确或没有对应情景模型结果；估值模型结果不能证明真实公允价值。',
                 'advice': '明确基准/乐观/压力情景，并回溯模型参数及计算结果。'}
        matching = [p for p in pool if p.get('source') == 'valuation' and p.get('code') == code
                    and (p.get('scenario') or _scenario(str(p.get('label') or ''))) == scenario
                    and scenario is not None and _decimal(p.get('value')) is not None]
        values = {_decimal(p['value']) for p in matching}
        report_currency = _currency(raw) or context_currency
        if report_currency and matching and any(p.get('currency') != report_currency for p in matching):
            claim['reason'] = '报告币种与保存模型结果币种不一致，或模型币种未明确。'
            claim['advice'] = '核对模型币种；换算金额须提供汇率及计算依据。'
        elif len(values) == 1:
            expected = next(iter(values))
            claim['expected_value'] = _str(expected)
            if abs(expected - value) <= tolerance:
                claim['model_status'] = 'conditional_model_result_match'
                claim['reason'] = '与指定情景的保存模型结果一致，仅表示条件模型一致，不构成独立财务事实核验。'
                claim['advice'] = '保留情景假设、模型公式及参数来源。'
            else:
                claim['status'] = 'contradicted'
                claim['model_status'] = 'model_result_mismatch'
                claim['reason'] = '报告数值与指定情景的保存模型结果不一致。'
                claim['advice'] = f'按此情景模型更正为 {_str(expected / SCALES[unit])}{unit}；同时披露模型假设。'
        claims.append(claim)

    for lineno, raw in enumerate((text or '').splitlines(), 1):
        if len(raw) < 100 and (raw.lstrip().startswith('#') or not list(_tokens(raw))):
            context_currency = _currency(raw) or context_currency
        if raw.lstrip().startswith('#'):
            context_scenario = _scenario(raw) or context_scenario
        unit_match = re.search(r'单位\s*[:：]?\s*(亿元|百万元|万元|千元|元|%|％)', raw)
        if unit_match:
            default_unit = unit_match[1]
        cells = [c.strip() for c in raw.strip().strip('|').split('|')] if '|' in raw else None
        if cells and any(_scenario(c) for c in cells):
            headers = cells
            continue
        if cells and headers:
            field = MODEL_FIELD.search(cells[0])
            if field:
                code = MODEL_ALIASES.get(field[0].upper() if field[0].upper() == 'WACC' else field[0])
                label_unit = re.search(r'亿元|百万元|万元|千元|元|%|％', cells[0])
                for col, cell in enumerate(cells[1:], 1):
                    for _, value, unit, tolerance in _tokens(cell, label_unit[0] if label_unit else default_unit):
                        add(raw, lineno, code, _scenario(headers[col]) if col < len(headers) else None, value, unit, tolerance)
            continue
        if not cells:
            headers = None
        for sentence in re.split(r'[。；;]', raw):
            fields = list(MODEL_FIELD.finditer(sentence))
            for token, value, unit, tolerance in _tokens(sentence):
                preceding = [f for f in fields if f.end() <= token.start()]
                if not preceding:
                    continue
                field = preceding[-1]
                # A later historical field starts its own assertion.
                historical = [a for aliases in ALIASES.values() for a in aliases
                              if a in sentence[field.end():token.start()]]
                if historical:
                    continue
                code = MODEL_ALIASES.get(field[0].upper() if field[0].upper() == 'WACC' else field[0])
                add(sentence, lineno, code, _scenario(sentence[:token.start()]) or context_scenario,
                    value, unit, tolerance)
    for index, claim in enumerate(claims, 1):
        claim['id'] = f'M{index:04d}'
    stats = {key: sum(c['status'] == key for c in claims) for key in ('verified', 'contradicted', 'unverifiable')}
    stats.update(checked=len(claims), coverage=0.0)
    return {'claims': claims, 'stats': stats}
