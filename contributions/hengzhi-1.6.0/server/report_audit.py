"""报告交付前的确定性自检引擎。

历史项目内部诊断量表（不是竞赛官方评分规则）：

    事实正确 / 证据充分 / 金融逻辑 / 风险审慎 / 可操作性   —— 平均分不低于 4/5
    一票否决：关键数字错误、伪造引用、三表不平或因果倒置、
              收益保证或确定性荐股、只有摘要没有决策支持

设计原则
--------
1. **只做确定性判断。** 数字溯源、计数核对、引用核验、关键词扫描都是可复现的
   字符串与数值运算，不调用模型、不做语义猜测。这是"核心数值不依赖模型"的直接体现。
2. **双向检查，两头都防。**
   - 正向（防编造）：报告里出现的金额，必须在工具产出的事实/指标里找到对应值。
   - 反向（防漏读）：工具已核验的关键科目，报告里必须出现。
   只做正向会漏掉"该写的没写"，只做反向会漏掉"凭空多出来的数"。
3. **分级报错。** 硬错误（未溯源金额、计数不符、伪造引用、一票否决）与提示
   （派生值不在指标表、章节缺失）分开列出，避免噪音淹没真问题。

为什么需要它
------------
实测踩过的两类事故，都靠人工肉眼发现的：
  * 报告第八节标题写"待复核 19 项"，明细 7+3+1+2+13=26 项，工具返回也是 26；
  * 第三节 ROE 行 2024 年写"—"，而原文该页有值（提取器静默丢数）。
第一类是本模块的 `counts` 检查，第二类对应 `coverage` 检查。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------- 数值解析

# 金额单位表。长单位必须排在短单位之前，否则「亿元」会被「元」抢先匹配掉尾巴。
_AMOUNT_UNITS: list[tuple[str, Decimal]] = [
    ('亿元', Decimal('1e8')), ('亿', Decimal('1e8')),
    ('百万元', Decimal('1e6')), ('百万', Decimal('1e6')),
    ('万元', Decimal('1e4')), ('万', Decimal('1e4')),
    ('千元', Decimal('1e3')), ('千', Decimal('1e3')),
    ('元', Decimal('1')),
]
_UNIT_ALT = '|'.join(re.escape(u) for u, _ in _AMOUNT_UNITS)
_SCALE_OF = dict(_AMOUNT_UNITS)

# 数字本体：带千分位或不带，可带小数。
_NUMBER = r'\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?'

# 金额 / 比率 token。捕获符号、数字、紧邻单位。
_TOKEN_RE = re.compile(
    r'(?<![\w.,])'
    r'(?P<sign>[-−+])?'
    r'(?P<num>' + _NUMBER + r')'
    r'\s*'
    r'(?P<unit>' + _UNIT_ALT + r'|倍|%|％|pp|个百分点)?'
)

# 计数 token：报告里"19 项""248 条""23 个"这类，用于和工具字段对账。
_COUNT_RE = re.compile(r'(?<![\d.,])(?P<num>\d+)\s*(?P<unit>条|项|个)')

# 证据 ID：<文档短哈希>:p<页>:b<t/b><序号>。文档哈希可能是 16 位短前缀或 64 位全量。
_EVIDENCE_RE = re.compile(r'\b[0-9a-f]{8,64}:p\d+:[bt]\d+\b')

# run_id：32 位十六进制。报告开头通常写「分析运行 run_id=xxx；法证运行 run_id=yyy」。
_RUN_ID_RE = re.compile(r'\b[0-9a-f]{32}\b')

# 预处理时要抹掉的"不是财务数字"的串，否则页码、哈希、日期会大量误报。
_MASK_PATTERNS = [
    _EVIDENCE_RE,                                    # 证据 ID
    re.compile(r'\b[0-9a-f]{16,}\b'),                # 文档哈希 / run_id
    re.compile(r'\d{4}-\d{1,2}-\d{1,2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?'),   # ISO 日期时间
    re.compile(r'\b\d{4}\s*年'),                      # 年份
    re.compile(r'\d+\.\d+\.\d+'),                    # 版本号
    re.compile(r'\bp\d+:[bt]\d+\b'),                 # 报告里简写的页码证据
]

# ---------------------------------------------------------------- 计数关键词

# (正则, 取值函数, 中文名)。取值函数接收 run / forensic，返回期望的数字或 None。
_COUNT_TARGETS: list[tuple[str, str, str]] = [
    (r'verified|已核验|核验通过|通过核验', 'verified_facts', '已核验事实数'),
    (r'事实|facts', 'facts', '事实总数'),
    (r'指标|metrics', 'metrics', '程序计算指标数'),
    (r'勾稽|检查|tieout', 'checks', '勾稽检查数'),
    (r'待复核|复核|review', 'reviews', '待复核事项数'),
    (r'信号|法证|命中|发现|findings', 'findings', '法证发现数'),
]

# ---------------------------------------------------------------- 一票否决

# (正则, 说明, 是否允许被否定语境豁免)
_VETO_PATTERNS: list[tuple[str, str]] = [
    (r'目标价(?:位)?(?:为|是|在)?\s*[\d,]+', '给出了具体目标价'),
    (r'(?:给予|维持|首次覆盖).{0,8}(?:买入|增持|推荐|强烈推荐)评级', '给出了投资评级'),
    (r'(?:建议|应当|可以)\s*(?:买入|增持|加仓|重仓)', '给出了买入建议'),
    (r'保证(?:收益|回报|盈利)|稳赚|包赚|必赚|无风险(?:收益|套利)', '收益保证表述'),
    (r'必(?:将|定)(?:上涨|涨停|大涨|翻倍)|肯定(?:会)?(?:上涨|涨停)|一定会(?:上涨|涨)', '确定性涨跌预测'),
    (r'翻(?:一|两|三|十)倍|至少(?:上涨|翻)\s*\d+', '收益幅度承诺'),
]

# 否定语境：命中这些词的句子不算一票否决（免责声明里本来就该出现这些字眼）。
_NEGATION = re.compile(r'不构成|不是|并非|不提供|不承诺|不得|禁止|不给予|不作|免责|不推荐|'
                       r'不作为|不用于|不应当|勿|请勿|不预测|不设')

# ---------------------------------------------------------------- 覆盖率关键科目

# 竞赛与日常分析都绕不开的核心科目。第三列 True 表示"必需"：
# 这类科目的数值不出现，读者就无法复算报告里的任何比率。
# 只对必需档打分，建议档列提示——避免"分析报告没抄全三张表"被当成硬伤。
_KEY_CODES: list[tuple[str, str, bool]] = [
    ('revenue', '营业收入', True),
    ('net_profit', '净利润', True),
    ('parent_net_profit', '归母净利润', True),
    ('operating_cash_flow', '经营活动现金流净额', True),
    ('assets', '资产总计', True),
    ('equity', '所有者权益合计', True),
    ('cost_of_revenue', '营业成本', False),
    ('gross_profit', '毛利', False),
    ('operating_profit', '营业利润', False),
    ('profit_before_tax', '利润总额', False),
    ('income_tax', '所得税费用', False),
    ('capex', '资本开支', False),
    ('cash', '货币资金', False),
    ('inventory', '存货', False),
    ('receivables', '应收账款', False),
    ('payables', '应付账款', False),
    ('liabilities', '负债合计', False),
    ('parent_equity', '归母权益', False),
]

# 五维度量表：每项给分依据是客观计数，不是主观印象。
_DIMENSION_NAMES = ['事实正确', '证据充分', '金融逻辑', '风险审慎', '可操作性']


def extract_run_ids(text: str) -> list[str]:
    """找出报告里出现的 32 位十六进制串（run_id 的形态）。

    为什么要这个：报告是**当时**基于某个运行写的，而自检可能在之后跑。
    若拿新生成的运行去核对旧报告，会把"数据版本不同"误报成"计数抄错"。
    报告开头一般会写明「分析运行 run_id=xxx；法证运行 run_id=yyy」，
    据此定位到同一批数据，对账才有意义。
    """
    return list(dict.fromkeys(_RUN_ID_RE.findall(text or '')))


# ================================================================ 工具函数

def _to_decimal(text: str) -> Decimal | None:
    try:
        return Decimal(str(text).replace(',', '').strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None


def _mask(text: str) -> str:
    """把非财务数字（证据 ID、哈希、日期、版本号）替换成等长空白，保持位置稳定。"""
    out = text
    for pattern in _MASK_PATTERNS:
        out = pattern.sub(lambda m: ' ' * len(m.group(0)), out)
    return out


def _half_step(num_text: str, scale: Decimal) -> Decimal:
    """半格容差：报告把 52,552,910,000 写成 525.53 亿时，允许的绝对偏差。

    四舍五入到小数点后 k 位的最大误差是 0.5×10^-k。乘上单位尺度就是绝对容差。
    这个口径比"固定百分比"合理：写 0.75 亿（两位有效数字）本来就允许更大的相对误差。
    """
    decimals = len(num_text.split('.')[-1]) if '.' in num_text else 0
    step = Decimal(1).scaleb(-decimals) * scale
    return step / 2


def _clamp_tolerance(value: Decimal, tol: Decimal) -> Decimal:
    """容差上下限：不低于 1e-6（防浮点噪音），不高于 1%（防小额数字匹配过宽）。"""
    low = abs(value) * Decimal('1e-6')
    high = abs(value) * Decimal('0.01')
    return max(min(tol, high), low)


# ================================================================ 数值池

def build_pool(facts: list | None, metrics: list | None,
               valuation: dict | None = None) -> list[dict]:
    """把工具产出摊平成一个可检索的数值池。

    每一项都带 `code` / `period` / `scope` / `evidence_ids`，这样报告里的数字一旦
    匹配上，就能立刻回答"这是哪个科目的哪一期，证据是什么"——这正是"三次点击以内
    看到原文页码"要求的底层数据。
    """
    pool: list[dict] = []

    for fact in facts or []:
        if fact.get('status') != 'verified':
            continue          # 待复核事实不作为"可信池"，否则等于自证
        value = _to_decimal(fact.get('value'))
        if value is None:
            continue
        pool.append({'value': value, 'kind': 'amount', 'code': fact.get('code'),
                     'label': fact.get('raw_label') or fact.get('code'),
                     'period': fact.get('period'), 'scope': fact.get('scope'),
                     'period_type': fact.get('period_type'), 'currency': fact.get('currency'),
                     'company': fact.get('company'),
                     'evidence_ids': list(fact.get('evidence_ids') or []),
                     'source': 'fact'})

    for metric in metrics or []:
        if metric.get('status') not in ('calculated', 'verified'):
            continue
        value = _to_decimal(metric.get('value'))
        if value is None:
            continue
        unit = str(metric.get('unit') or '')
        # ratio 类指标在报告里以百分数出现（0.2251 → 22.51%），必须单独归类，
        # 否则 0.2251 会和金额池里的 0.2251 元混淆。
        kind = 'ratio' if unit == 'ratio' else ('amount' if unit in ('CNY', 'currency', '')
                                                else 'other')
        pool.append({'value': value, 'kind': kind, 'code': metric.get('code'),
                     'label': metric.get('code'), 'period': metric.get('period'),
                     'scope': metric.get('scope'), 'period_type': metric.get('period_type'),
                     'currency': metric.get('currency'), 'company': metric.get('company'),
                     'evidence_ids': list(metric.get('evidence_ids') or []),
                     'source': 'metric'})

    if isinstance(valuation, dict):
        for name in ('base', 'optimistic', 'stress'):
            scenario = (valuation.get('scenarios') or {}).get(name) or valuation.get(name)
            if not isinstance(scenario, dict):
                continue
            val = scenario.get('valuation') or {}
            params = scenario.get('parameters') or {}
            for key, code in (('enterprise_value', 'enterprise_value'),
                              ('equity_value', 'equity_value'),
                              ('value_per_share', 'value_per_share')):
                value = _to_decimal(val.get(key))
                if value is not None:
                    pool.append({'value': value, 'kind': 'amount', 'code': code,
                                 'label': f'{code}({name})', 'period': None, 'scope': None,
                                 'evidence_ids': [], 'source': 'valuation',
                                 'currency': valuation.get('currency') or (valuation.get('model_plan') or {}).get('currency')})
            for key in ('wacc', 'terminal_growth', 'revenue_growth', 'gross_margin',
                        'opex_ratio', 'tax_rate'):
                value = _to_decimal(params.get(key))
                if value is not None:
                    pool.append({'value': value, 'kind': 'ratio', 'code': key,
                                 'label': f'{key}({name})', 'period': None, 'scope': None,
                                 'evidence_ids': [], 'source': 'valuation',
                                 'currency': valuation.get('currency') or (valuation.get('model_plan') or {}).get('currency')})
    return pool


# ================================================================ 正向：数字溯源

def _find_in_pool(value: Decimal, tol: Decimal, pool: list[dict],
                  kinds: tuple[str, ...]) -> list[dict]:
    hits = []
    for item in pool:
        if item['kind'] not in kinds:
            continue
        if abs(item['value'] - value) <= tol:
            hits.append(item)
    return hits


def _find_amount(value: Decimal, tol: Decimal, pool: list[dict]) -> list[dict]:
    """金额匹配，**符号不敏感**。

    现金流量表里的资本开支在工具产出里是负数（现金流出），报告里通常写成
    "资本开支 64.81 亿" 的正数。只按原值匹配会把这一类全部误报成"凭空多出来的数"，
    实测 3 处。符号方向本身由报告的文字表述决定，不是数字错误。
    """
    hits = _find_in_pool(value, tol, pool, ('amount',))
    if hits or value == 0:
        return hits
    return _find_in_pool(-value, tol, pool, ('amount',))


def _is_conversion_factor(masked: str, start: int) -> bool:
    """数字前面紧邻乘号时是单位换算系数（「千元」×1,000），不是财务数字。"""
    return masked[max(0, start - 2):start].strip().endswith(('×', 'x', 'X', '*', '乘'))


def _find_by_scale(num: Decimal, pool: list[dict]) -> list[dict]:
    """无单位数字的尺度变体匹配。

    报告公式区常写「EBITDA = ... = 24,377,050 千元」，但千元可能离得很远、
    或者干脆被折行隔开。此时数字本身不带单位，只能逐个试常见量级。
    """
    hits = []
    for item in pool:
        if item['kind'] != 'amount':
            continue
        for scale in (Decimal('1'), Decimal('1e3'), Decimal('1e4'),
                      Decimal('1e6'), Decimal('1e8')):
            for signed in (num, -num):
                candidate = signed * scale
                tol = _clamp_tolerance(
                    item['value'], abs(candidate) * Decimal('1e-6') + Decimal('0.5') * scale)
                if abs(item['value'] - candidate) <= tol:
                    hits.append({**item, 'assumed_scale': str(scale)})
                    break
            else:
                continue
            break
    return hits


def _find_by_sum(value: Decimal, tol: Decimal, pool: list[dict]) -> list[dict]:
    """组合匹配：报告里的「现金及长期银行存款合计 222.65 亿」是两个科目之和。

    不做这一步的话，所有合理的小计都会被误判成"凭空多出来的数"，
    噪音会淹没真正的错误。

    容差放宽到两倍（一格）：每个加数各自允许半格舍入误差，两个数相加就是一格。
    实测：cash 111.78 亿 + long_term_deposits 110.88 亿 = 222.66 亿，
    报告写 222.65 亿（截断而非四舍五入），半格容差刚好卡掉。
    """
    amounts = [item for item in pool if item['kind'] == 'amount']
    wide = tol * 2
    hits = []
    for target in (value, -value):
        for i, first in enumerate(amounts):
            for second in amounts[i + 1:]:
                if (first['code'] == second['code']
                        and first['period'] == second['period']):
                    continue
                if abs(first['value'] + second['value'] - target) <= wide:
                    hits.append({'parts': [first, second]})
                    if len(hits) >= 3:
                        return hits
        if hits:
            break
    return hits


def audit_numbers(text: str, pool: list[dict]) -> dict:
    """报告里每个金额/比率是否能在工具产出里找到出处。"""
    masked = _mask(text)
    traced, untraced, soft = [], [], []
    checked = 0

    for match in _TOKEN_RE.finditer(masked):
        num_text = match.group('num')
        unit = match.group('unit') or ''
        sign = -1 if match.group('sign') in ('-', '−') else 1
        num = _to_decimal(num_text)
        if num is None:
            continue
        if _is_conversion_factor(masked, match.start('num')):
            continue          # 「千元」×1,000 这类换算说明，不是财务数字
        num *= sign
        context = masked[max(0, match.start() - 30):match.end() + 10].replace('\n', ' ')

        if unit in ('%', '％'):
            # 百分数：池里的 ratio 是小数形式（0.2251），报告写 22.51%。
            checked += 1
            target = num / 100
            tol = _clamp_tolerance(target, _half_step(num_text, Decimal('0.01')) / 100)
            hits = _find_in_pool(target, tol, pool, ('ratio',))
            record = {'raw': f'{num_text}{unit}', 'context': context.strip()}
            if hits:
                traced.append({**record, 'matched': _describe(hits[0])})
            else:
                soft.append({**record, 'reason': '未在工具指标中找到对应比例值'})
            continue

        if unit == '倍':
            checked += 1
            tol = _clamp_tolerance(num, _half_step(num_text, Decimal('1')))
            hits = _find_in_pool(num, tol, pool, ('ratio', 'amount', 'other'))
            record = {'raw': f'{num_text}倍', 'context': context.strip()}
            if hits:
                traced.append({**record, 'matched': _describe(hits[0])})
            else:
                soft.append({**record, 'reason': '未在工具产出中找到对应倍数'})
            continue

        if unit == 'pp' or unit == '个百分点':
            # 百分点是两期之差，天然不在指标池里；只做提示，不算错。
            continue

        if unit in _SCALE_OF:
            checked += 1
            scale = _SCALE_OF[unit]
            value = num * scale
            tol = _clamp_tolerance(value, _half_step(num_text, scale))
            hits = _find_amount(value, tol, pool)
            record = {'raw': f'{num_text}{unit}', 'value': str(value), 'context': context.strip()}
            if hits:
                traced.append({**record, 'matched': _describe(hits[0])})
                continue
            sums = _find_by_sum(value, tol, pool)
            if sums:
                parts = sums[0]['parts']
                traced.append({**record, 'matched': ' + '.join(_describe(p) for p in parts)})
                continue
            untraced.append({**record, 'reason': '带金额单位的数字未在已核验事实中找到'})
            continue

        # 无单位。含千分位 → 基本可断定是金额；否则当小数字处理，不参与硬检查。
        if ',' in num_text:
            checked += 1
            hits = _find_by_scale(num, pool)
            record = {'raw': num_text, 'context': context.strip()}
            if hits:
                traced.append({**record, 'matched': _describe(hits[0])})
            else:
                untraced.append({**record, 'reason': '带千分位的金额未在已核验事实中找到'})
        else:
            continue          # 页码、条数、序号等，交给 counts 检查

    total = len(traced) + len(untraced)
    rate = (Decimal(len(traced)) / Decimal(total)) if total else Decimal('1')
    return {'checked': checked, 'traced': len(traced), 'untraced': untraced,
            'soft_untraced': soft, 'trace_rate': str(rate.quantize(Decimal('0.0001'))),
            'examples': traced[:5]}


def _describe(item: dict) -> str:
    bits = [str(item.get('code') or item.get('label') or '?')]
    if item.get('period'):
        bits.append(str(item['period']))
    if item.get('scope'):
        bits.append(str(item['scope']))
    if item.get('assumed_scale'):
        bits.append(f'尺度×{item["assumed_scale"]}')
    if item.get('source'):
        bits.append(f"[{item['source']}]")
    return ' '.join(bits)


# ================================================================ 反向：漏读检查

def audit_coverage(text: str, pool: list[dict]) -> dict:
    """工具已核验的关键科目，报告里是否出现。

    只做正向溯源会漏掉"该写的没写"：报告把 ROE 那一格写成「—」而原文有值，
    正向检查永远发现不了。覆盖率是反向的网。
    """
    masked = _mask(text)
    latest: dict[str, dict] = {}
    for item in pool:
        if item['kind'] != 'amount' or not item.get('period'):
            continue
        code = item.get('code')
        if not code:
            continue
        current = latest.get(code)
        if current is None or str(item['period']) > str(current['period']):
            latest[code] = item

    missing, covered = [], []
    for code, label, required in _KEY_CODES:
        item = latest.get(code)
        if not item:
            continue
        hit = False
        for match in _TOKEN_RE.finditer(masked):
            num_text, unit = match.group('num'), match.group('unit') or ''
            num = _to_decimal(num_text)
            if num is None or _is_conversion_factor(masked, match.start('num')):
                continue
            if unit in _SCALE_OF:
                value = num * _SCALE_OF[unit]
                tol = _clamp_tolerance(value, _half_step(num_text, _SCALE_OF[unit]))
                if _find_amount(value, tol, [item]):
                    hit = True
                    break
            elif ',' in num_text:
                if _find_by_scale(num, [item]):
                    hit = True
                    break
        record = {'code': code, 'label': label, 'required': required,
                  'period': item['period'], 'value': str(item['value']),
                  'evidence_ids': item['evidence_ids'][:2]}
        (covered if hit else missing).append(record)

    required_missing = [m for m in missing if m['required']]
    required_total = len([c for c in covered if c['required']]) + len(required_missing)
    total = len(covered) + len(missing)
    rate = (Decimal(len(covered)) / Decimal(total)) if total else Decimal('1')
    required_rate = (Decimal(required_total - len(required_missing)) / Decimal(required_total)
                     if required_total else Decimal('1'))
    return {'checked': total, 'covered': len(covered), 'missing': missing,
            'required_missing': required_missing,
            'required_checked': required_total,
            'coverage_rate': str(rate.quantize(Decimal('0.0001'))),
            'required_coverage_rate': str(required_rate.quantize(Decimal('0.0001')))}


# ================================================================ 计数对账

def audit_counts(text: str, run: dict | None, forensic: dict | None = None) -> dict:
    """报告里「N 条 / N 项」是否与工具字段一致。

    这是实测抓到过的硬错误：报告标题写"待复核 19 项"，明细与工具都是 26。
    模型自己数一遍就会错，所以一律要求从工具字段原样抄。
    """
    if not run and not forensic:
        return {'available': False, 'checked': 0, 'mismatches': [], 'matches': []}

    def expected(key: str):
        facts = (run or {}).get('facts') or []
        if key == 'facts':
            return len(facts)
        if key == 'verified_facts':
            return sum(1 for f in facts if f.get('status') == 'verified')
        if key == 'metrics':
            return len((run or {}).get('metrics') or [])
        if key == 'checks':
            return len((run or {}).get('checks') or [])
        if key == 'reviews':
            reviews = (run or {}).get('review_queue')
            if reviews is None:
                reviews = (forensic or {}).get('review_queue')
            return len(reviews or [])
        if key == 'findings':
            findings = (run or {}).get('findings')
            if findings is None:
                findings = (forensic or {}).get('findings')
            return len(findings or [])
        return None

    masked = _mask(text)
    matches, mismatches = [], []
    seen: set[tuple[int, str]] = set()
    for match in _COUNT_RE.finditer(masked):
        number = int(match.group('num'))
        # 数字紧跟在"失败/通过/不符"之后时是子计数（"失败 0 项"），不是总数。
        head = masked[max(0, match.start() - 3):match.start()]
        if re.search(r'(失败|通过|不符|异常|超出|缺失|剩余)\s*$', head):
            continue
        window = masked[max(0, match.start() - 14):match.start()]
        best = None
        best_pos = -1
        for pattern, key, label in _COUNT_TARGETS:
            for found in re.finditer(pattern, window, re.I):
                if found.start() > best_pos:      # 取最靠近数字的那个关键词
                    best_pos, best = found.start(), (key, label)
        if not best:
            continue
        key, label = best
        want = expected(key)
        if want is None:
            continue
        if (number, key) in seen:
            continue
        seen.add((number, key))
        record = {'report_says': number, 'unit': match.group('unit'), 'field': label,
                  'tool_says': want, 'context': window.strip()[-24:]}
        (matches if number == want else mismatches).append(record)
    return {'available': True, 'checked': len(matches) + len(mismatches),
            'mismatches': mismatches, 'matches': matches}


# ================================================================ 引用核验

def audit_citations(text: str, run: dict | None, forensic: dict | None = None) -> dict:
    """报告引用的证据 ID 是否真实存在 —— 直接对应"伪造引用"这条一票否决。"""
    cited = set(_EVIDENCE_RE.findall(text))
    if not cited:
        return {'cited': 0, 'known': 0, 'unknown': [], 'citation_coverage': '0',
                'note': '报告未出现任何形如 <文档哈希>:p<页>:b<序号> 的证据引用。'}
    known: set[str] = set()
    for source in (run, forensic):
        if not isinstance(source, dict):
            continue
        for collection in ('facts', 'metrics', 'checks', 'signals', 'findings',
                           'review_queue'):
            for item in source.get(collection) or []:
                if not isinstance(item, dict):
                    continue
                # findings 用的字段名是 support_evidence_ids（支持证据），
                # 不是 evidence_ids。只认后者会让"只传法证运行"的调用
                # 把全部引用判成伪造 —— 实测踩过：报告引用精确率恒为 0。
                for field in ('evidence_ids', 'support_evidence_ids'):
                    known.update(str(i) for i in (item.get(field) or []))

    # 报告里常用 16 位短前缀，池里可能是 64 位全量哈希，所以**只放宽文档哈希段**。
    # 页码与块号必须完整匹配 —— 否则 <doc>:p999:b99 会被 <doc>:p11:b32 的存在蒙混过关，
    # 而"伪造引用"恰恰是一票否决项，不能靠前缀相似放过。
    known_suffixes: dict[str, set[str]] = {}
    for item in known:
        doc, _, suffix = item.partition(':')
        known_suffixes.setdefault(doc, set()).add(suffix)

    unknown = []
    for item in sorted(cited):
        doc, _, suffix = item.partition(':')
        if any((kdoc.startswith(doc) or doc.startswith(kdoc)) and suffix in suffixes
               for kdoc, suffixes in known_suffixes.items()):
            continue
        unknown.append(item)
    rate = (Decimal(len(cited) - len(unknown)) / Decimal(len(cited)))
    return {'cited': len(cited), 'known': len(cited) - len(unknown), 'unknown': unknown[:20],
            'citation_coverage': str(rate.quantize(Decimal('0.0001')))}


# ================================================================ 一票否决

def audit_veto(text: str) -> list[dict]:
    """扫描收益保证、确定性荐股等一票否决表述。

    带否定语境的句子（"本报告不构成投资建议""不给出目标价"）自动豁免，
    否则规范的免责声明反而会被判成违规。
    """
    findings = []
    sentences = re.split(r'[。；\n]', text)
    for pattern, reason in _VETO_PATTERNS:
        regex = re.compile(pattern)
        for sentence in sentences:
            match = regex.search(sentence)
            if not match:
                continue
            if _NEGATION.search(sentence):
                continue
            findings.append({'type': reason, 'sentence': sentence.strip()[:120],
                             'matched': match.group(0)})
            break
    return findings


# ================================================================ 章节完整性

_SECTION_HINTS = [
    (r'结论|摘要', '结论与适用边界'),
    (r'公司|行业|概览|业务', '公司与行业'),
    (r'盈利|收入|利润', '盈利分析'),
    (r'现金流|现金', '现金流分析'),
    (r'偿债|负债|流动性', '偿债能力'),
    (r'营运|周转', '营运效率'),
    (r'法证|风险|异常|信号', '风险与法证发现'),
    (r'估值|DCF|情景|预测', '估值与预测'),
    (r'证据|出处|来源|口径|边界', '证据与口径说明'),
    (r'待核验|复核|未解', '待核验事项'),
]


def audit_sections(text: str) -> dict:
    """报告是否覆盖项目建议的主题。只做提示，不判错。"""
    present, absent = [], []
    for pattern, label in _SECTION_HINTS:
        (present if re.search(pattern, text) else absent).append(label)
    return {'present': present, 'absent': absent,
            'completeness': str((Decimal(len(present)) / Decimal(len(_SECTION_HINTS)))
                                .quantize(Decimal('0.0001')))}


# ================================================================ 五维度打分

def _score(condition_5: bool, condition_4: bool, condition_3: bool) -> int:
    return 5 if condition_5 else (4 if condition_4 else (3 if condition_3 else 2))


def grade(numbers: dict, coverage: dict, counts: dict, citations: dict,
          veto: list, sections: dict, run: dict | None, text: str = '') -> dict:
    """按项目旧版内部量表给五维度打分。每一分的依据都写出来，便于申诉与复核。"""
    untraced = len(numbers.get('untraced') or [])
    missing = len(coverage.get('missing') or [])
    required_missing = len(coverage.get('required_missing') or [])
    mismatches = len(counts.get('mismatches') or [])
    unknown_cites = len(citations.get('unknown') or [])
    absent = len(sections.get('absent') or [])

    dims = []

    fact = _score(untraced == 0 and mismatches == 0, untraced <= 2 and mismatches == 0,
                  untraced <= 5)
    dims.append({'name': '事实正确', 'score': fact,
                 'basis': f'未溯源金额 {untraced} 处，计数不符 {mismatches} 处'})

    evidence = _score(
        unknown_cites == 0 and citations.get('cited', 0) > 0 and required_missing == 0,
        unknown_cites == 0 and required_missing <= 2,
        unknown_cites <= 2)
    dims.append({'name': '证据充分', 'score': evidence,
                 'basis': (f'引用证据 {citations.get("cited", 0)} 条，'
                           f'其中无法核对 {unknown_cites} 条；'
                           f'必需科目未覆盖 {required_missing} 个、'
                           f'建议科目未覆盖 {missing - required_missing} 个')})

    checks = (run or {}).get('checks') or []
    failed_checks = [c for c in checks if str(c.get('status') or '').lower() not in ('pass', 'ok', 'passed')]
    logic = _score(not failed_checks and (run is not None), len(failed_checks) <= 1,
                   len(failed_checks) <= 3)
    dims.append({'name': '金融逻辑', 'score': logic,
                 'basis': f'三表勾稽 {len(checks)} 项，未通过 {len(failed_checks)} 项'})

    risk = _score(not veto and bool(re.search(r'不构成|不提供|不承诺|免责|区间|不是目标价', text)),
                   not veto, len(veto) <= 1)
    dims.append({'name': '风险审慎', 'score': risk,
                 'basis': f'一票否决表述 {len(veto)} 处'})

    action = _score(absent == 0 and bool(re.search(r'待核验|复核|敏感', text)),
                    absent <= 2, absent <= 4)
    dims.append({'name': '可操作性', 'score': action,
                 'basis': f'缺失章节主题 {absent} 个（{", ".join(sections.get("absent") or []) or "无"}）'})

    total = (Decimal(sum(d['score'] for d in dims)) / Decimal(len(dims))).quantize(Decimal('0.01'))
    verdict = ('可交付' if total >= Decimal('4.5') else
               '基本可交付，需按下列问题修订' if total >= Decimal('4') else
               '需修订后重新自检' if total >= Decimal('3') else '不可交付')
    return {'total': str(total), 'verdict': verdict, 'dimensions': dims,
            'veto_items': veto, 'rubric_scope': '项目内部旧版诊断，非竞赛官方评分'}


# ================================================================ 主入口

def audit_report(text: str, run: dict | None = None, valuation: dict | None = None,
                 forensic: dict | None = None) -> dict:
    """跑完整自检，返回结构化结果 + 按严重度排序的问题清单。"""
    text = text or ''
    pool = build_pool((run or {}).get('facts'), (run or {}).get('metrics'), valuation)

    numbers = audit_numbers(text, pool)
    coverage = audit_coverage(text, pool)
    counts = audit_counts(text, run, forensic)
    citations = audit_citations(text, run, forensic)
    veto = audit_veto(text)
    sections = audit_sections(text)
    report_grade = grade(numbers, coverage, counts, citations, veto, sections, run, text)

    issues: list[dict] = []
    for item in numbers.get('untraced') or []:
        issues.append({'severity': 'hard', 'type': '未溯源金额', 'detail': item['raw'],
                       'context': item.get('context', ''), 'advice': item.get('reason', '')})
    for item in counts.get('mismatches') or []:
        issues.append({'severity': 'hard', 'type': '计数不符',
                       'detail': f"{item['field']}：报告写 {item['report_says']}，工具为 {item['tool_says']}",
                       'context': item.get('context', ''),
                       'advice': '计数类数字一律从工具字段原样抄，不要自己数'})
    for item in citations.get('unknown') or []:
        issues.append({'severity': 'hard', 'type': '引用不存在',
                       'detail': item, 'context': '',
                       'advice': '证据 ID 必须来自工具返回，不能凭记忆或改写'})
    for item in veto:
        issues.append({'severity': 'hard', 'type': '一票否决', 'detail': item['type'],
                       'context': item.get('sentence', ''), 'advice': '删除该表述或改为条件式、区间式'})
    for item in coverage.get('missing') or []:
        issues.append({'severity': 'medium' if item.get('required') else 'soft',
                       'type': '必需科目未出现' if item.get('required') else '关键科目未出现',
                       'detail': f"{item['label']}({item['code']}) {item['period']} = {item['value']}",
                       'context': '', 'advice': '已核验的关键科目应在报告里给出，写"—"属漏读'})
    for item in numbers.get('soft_untraced') or []:
        issues.append({'severity': 'soft', 'type': '派生值未在指标表', 'detail': item['raw'],
                       'context': item.get('context', ''),
                       'advice': '若为自行计算，请在公式区给出操作数'})
    for item in sections.get('absent') or []:
        issues.append({'severity': 'soft', 'type': '缺少章节主题', 'detail': item,
                       'context': '', 'advice': '按实际任务需求补充主题'})

    order = {'hard': 0, 'medium': 1, 'soft': 2}
    issues.sort(key=lambda x: order.get(x['severity'], 2))
    hard = [i for i in issues if i['severity'] == 'hard']

    return {
        'ok': True,
        'grade': report_grade,
        'competition_metrics': competition_metrics(report_grade, numbers, coverage,
                                                   citations, veto),
        'numbers': {'checked': numbers['checked'], 'traced': numbers['traced'],
                    'untraced_count': len(numbers['untraced']),
                    'trace_rate': numbers['trace_rate'],
                    'untraced': numbers['untraced'][:20],
                    'soft_untraced_count': len(numbers['soft_untraced'])},
        'coverage': coverage,
        'counts': counts,
        'citations': citations,
        'sections': sections,
        'issues': issues[:40],
        'hard_issue_count': len(hard),
        'summary': _summary_text(report_grade, numbers, coverage, counts, citations,
                                 len(hard)),
        'note': ('本自检只做确定性核对：数字是否能在工具产出中找到、计数是否与工具字段一致、'
                 '引用是否真实存在、是否出现一票否决表述。它不评价文风、不判断业务逻辑是否深刻，'
                 '也不替代人工复核。'),
    }


def competition_metrics(report_grade: dict, numbers: dict, coverage: dict,
                        citations: dict, veto: list) -> list[dict]:
    """把自检结果映射到项目内部目标值（不是赛事官方标准）。

    这样答辩时不必口头解释"我们做得不错"，直接给一张达标对照表。
    只列报告层面能测的指标；表格抽取、科目映射那些要黄金答案才能算的，不在本模块。
    """
    rows: list[dict] = []

    def add(name, target, actual, ok, note=''):
        rows.append({'指标': name, '目标': target, '实测': actual,
                     '达标': '是' if ok else '否', '说明': note})

    try:
        precision = Decimal(str(citations.get('citation_coverage') or 0))
    except InvalidOperation:
        precision = Decimal('0')
    add('引用精确率', '≥ 0.95', str(precision),
        precision >= Decimal('0.95'),
        f"报告引用证据 {citations.get('cited', 0)} 条，"
        f"无法核对 {len(citations.get('unknown') or [])} 条")

    try:
        cov = Decimal(str(coverage.get('required_coverage_rate') or 0))
    except InvalidOperation:
        cov = Decimal('0')
    add('必需科目覆盖率', '≥ 0.90', str(cov), cov >= Decimal('0.90'),
        f"必需科目 {coverage.get('required_checked', 0)} 个，"
        f"未出现 {len(coverage.get('required_missing') or [])} 个")

    try:
        trace = Decimal(str(numbers.get('trace_rate') or 0))
    except InvalidOperation:
        trace = Decimal('0')
    add('数值可溯源率', '= 1.00', str(trace), trace == Decimal('1'),
        f"金额 {numbers.get('traced', 0)}/{numbers.get('traced', 0) + len(numbers.get('untraced') or [])} 可回溯到工具产出")

    try:
        total = Decimal(str(report_grade.get('total') or 0))
    except InvalidOperation:
        total = Decimal('0')
    add('报告质量平均分', '≥ 4/5', str(total), total >= Decimal('4'),
        report_grade.get('verdict', ''))

    add('一票否决项', '0 处', str(len(veto)), not veto,
        '；'.join(sorted({v['type'] for v in veto})) or '无')
    return rows


def _summary_text(report_grade, numbers, coverage, counts, citations, hard_count) -> str:
    return (
        f"五维度平均 {report_grade['total']}/5（{report_grade['verdict']}）。"
        f"金额溯源 {numbers['traced']}/{numbers['traced'] + len(numbers['untraced'])}"
        f"（{numbers['trace_rate']}）；"
        f"关键科目覆盖 {coverage['covered']}/{coverage['checked']}"
        f"（{coverage['coverage_rate']}）；"
        f"引用证据 {citations.get('cited', 0)} 条、无法核对 {len(citations.get('unknown') or [])} 条；"
        f"硬问题 {hard_count} 处。"
    )


# ================================================================ 文件入口

def read_report_text(path: str) -> str:
    """读报告正文。支持 .md/.txt/.docx（docx 走 zip 兜底解析）。"""
    from pathlib import Path
    target = Path(path).expanduser()
    if not target.is_file():
        raise ValueError(f'文件不存在：{target}')
    if target.suffix.lower() == '.pdf':
        from .financial_documents import parse_document
        doc = parse_document(target)
        content = '\n\n'.join(str(page.get('text') or '') for page in doc.get('pages', []))
        if not content.strip():
            raise ValueError('PDF未提取到可读文本，请提供清晰文件或文本版本')
        return content
    if target.suffix.lower() not in {'.md', '.txt', '.docx'}:
        raise ValueError('研报核查支持PDF、Word、Markdown或纯文本')
    if target.suffix.lower() == '.docx':
        return _read_docx(target)
    return target.read_text(encoding='utf-8', errors='replace')


def audit_file(path: str, run: dict | None = None, valuation: dict | None = None,
               forensic: dict | None = None) -> dict:
    """从本地文件读报告正文再自检。"""
    from pathlib import Path
    text = read_report_text(path)
    result = audit_report(text, run, valuation, forensic)
    result['source_file'] = str(Path(path).expanduser())
    result['text_length'] = len(text)
    return result


def _read_docx(path) -> str:
    """不依赖 python-docx 的兜底读取：直接解 zip 取 document.xml。

    实测教训：某些 docx 用 python-docx 读出来的表格单元格是空的，
    只有解析原始 XML 才拿得到全文。
    """
    import re as _re
    import zipfile
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.endswith('document.xml')]
        if not names:
            raise ValueError('docx 内没有 document.xml')
        xml = zf.read(names[0]).decode('utf-8', errors='replace')
    xml = _re.sub(r'</w:p>', '\n', xml)
    xml = _re.sub(r'</w:tc>', ' | ', xml)
    xml = _re.sub(r'</w:tr>', '\n', xml)
    xml = _re.sub(r'<[^>]+>', '', xml)
    text = (xml.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
            .replace('&quot;', '"').replace('&apos;', "'"))
    return _re.sub(r'\n{3,}', '\n\n', text)
