"""财报分析的三个确定性缺口补齐（交接文档第 5 项）。

引擎原有能力已覆盖：同比（`_yoy`）、可推导单季、勾稽、现金转化率（`cash_conversion`）
与非经常性损益**汇总**占比（`nonrecurring_profit_share`）。本模块只补真正缺的部分：

1. **环比（qoq）** —— 引擎无 `_qoq` 指标。环比必须建立在**单季**值上，
   不能拿累计值直接比（Q3 累计 vs Q2 累计 是"累计增速"，不是环比）。
2. **会计口径变化** —— 引擎能发现 `restatement_mismatch` 但不给出具体差异。
   这里把同一 `(科目, 期间, 口径, 币种)` 的**多个不同值**摊开，给出差额、来源文档与重述标记。
3. **业绩变化原因定位** —— 纯文本任务，但"定位"是确定性的：找出报告里
   业绩变动原因相关的段落并给页码与证据 ID。**归纳由模型完成，且必须标为推断**——
   本模块不产出任何归因结论。

设计纪律（与项目一致）：
- 只做确定性计算，缺数据返回 `None` 并说明原因，不用默认值填平。
- 基数为零或负数时**不报百分比**，只报绝对增减额（与引擎 yoy 的规则一致）。
- 每条结果都带 `formula`、`dependency_fact_ids`、`evidence_ids`，可复算。
- 本模块产出的指标都带 `derived: True` 与 `note`，与引擎原生指标区分开。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

D = Decimal
ZERO = D('0')

# A 股定期报告期末日 → 季度序号
_QUARTER_BY_MMDD = {'03-31': 1, '06-30': 2, '09-30': 3, '12-31': 4}
_PERIOD_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')

# 业绩变动原因相关的定位关键词。只用于**定位段落**，不用于判断内容对错。
# 顺序=优先级：同一段落命中多个时，取最靠前的（最具体的）那个作为 keyword 标签。
# 后面的通用词（变动原因/主要原因）是为了兜住 A 股年报「主营业务分析」表的
# 「变动原因」列，以及附注里"本期××增加的主要原因系…"这类逐科目说明。
#
# **故意不收章节标题**（如"管理层讨论与分析"）：这类标题会作为页眉出现在每一页上，
# 收进来会产出几十条重复噪声，而它本身不含任何原因内容。章节定位靠目录与
# disclosure_map，不靠这里。
REASON_KEYWORDS = (
    '业绩变动原因', '业绩变化原因', '业绩变动的主要原因是', '经营业绩变动',
    '业绩变动说明', '业绩变动的主要原因', '变动原因说明', '主要变动原因',
    '经营业绩说明', '业绩驱动因素', '业绩变动因素',
    '营业收入变动原因', '净利润变动原因', '主要原因是',
    # 定期报告的**标准小节标题**：季报/半年报里这一节常被勾成「□适用 ☑不适用」。
    # 定位到标题本身有价值——模型能据此写"公司标注为不适用"，而不是误写成"未披露"。
    '变动的情况及原因', '变动情况及原因', '财务指标发生变动', '增减变动及原因',
    '变动原因', '主要原因', '原因说明',
    # 繁体（港式年报/繁体披露）与简体不是同一串字符，必须单列，否则一篇港式年报会漏光。
    # 「主要原因」在简繁中同形，已在上面列出，这里不重复。
    '業績變動原因', '業績變化原因', '變動原因', '原因說明',
)

NOTE = '本项目补充计算（非引擎原生指标）：公式与操作数已给出，可复算。'


def _dec(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return D(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _fid(fact):
    return fact.get('fact_id') or ''


def _period_of(period):
    """'2025-09-30' -> (2025, 3)。无法解析返回 None。"""
    m = _PERIOD_RE.match((period or '').strip())
    if not m:
        return None
    quarter = _QUARTER_BY_MMDD.get(f'{m.group(2)}-{m.group(3)}')
    return (int(m.group(1)), quarter) if quarter else None


def _group_key(fact):
    return (fact.get('code') or '', fact.get('scope') or '', fact.get('currency') or '',
            fact.get('company') or '')


# ------------------------------------------------------------------ 1. 环比

def single_quarter_values(facts):
    """把累计（ytd）事实折算成单季值；已是单季（quarter）的直接采用。

    返回 {(code, scope, currency, company, year, quarter): {...}}。
    余额类（instant）不参与——时点值没有"环比增速"的语义。
    """
    ytd = {}       # (group, year, quarter) -> fact
    single = {}    # (group, year, quarter) -> fact
    for f in facts or []:
        if f.get('status') not in (None, 'verified', 'calculated'):
            continue
        if _dec(f.get('value')) is None:
            continue
        pk = _period_of(f.get('period'))
        if not pk:
            continue
        year, quarter = pk
        ptype = (f.get('period_type') or '').lower()
        if ptype == 'instant':
            continue
        if ptype == 'quarter':
            single.setdefault((_group_key(f), year, quarter), f)
        elif ptype in ('ytd', 'year', 'annual', ''):
            ytd.setdefault((_group_key(f), year, quarter), f)

    out = {}
    for (group, year, quarter), f in single.items():
        out[(group, year, quarter)] = {
            'value': _dec(f['value']), 'method': 'disclosed_single_quarter',
            'facts': [f], 'evidence_ids': list(f.get('evidence_ids') or []),
        }
    for (group, year, quarter), f in ytd.items():
        key = (group, year, quarter)
        if key in out:
            continue
        if quarter == 1:
            out[key] = {'value': _dec(f['value']), 'method': 'ytd_is_q1',
                        'facts': [f], 'evidence_ids': list(f.get('evidence_ids') or [])}
            continue
        prev = ytd.get((group, year, quarter - 1))
        if not prev:
            continue      # 缺上一期累计 → 不推算，留空（宁缺毋滥）
        diff = _dec(f['value']) - _dec(prev['value'])
        out[key] = {
            'value': diff, 'method': 'ytd_difference',
            'facts': [f, prev],
            'evidence_ids': list(f.get('evidence_ids') or []) + list(prev.get('evidence_ids') or []),
        }
    return out


def qoq_metrics(facts):
    """相邻单季的环比。返回 (metrics, reviews)。

    - 上一单季基数 > 0 时给出 `_qoq` 比率；
    - 基数 <= 0 时**不给百分比**，只给绝对增减额，并入 reviews 说明原因；
    - 跨年相邻（Q4 上年 → Q1 本年）同样计算，但公式里写明跨年。
    """
    singles = single_quarter_values(facts)
    metrics, reviews = [], []
    by_group = {}
    for (group, year, quarter), info in singles.items():
        by_group.setdefault(group, []).append((year, quarter, info))

    for group, items in sorted(by_group.items()):
        code, scope, currency, company = group
        items.sort(key=lambda x: (x[0], x[1]))
        for (py, pq, prev), (cy, cq, cur) in zip(items, items[1:]):
            # 只连相邻季度：同一年内差 1，或跨年 Q4→Q1
            adjacent = (cy == py and cq == pq + 1) or (cy == py + 1 and pq == 4 and cq == 1)
            if not adjacent:
                continue
            base, value = prev['value'], cur['value']
            change = value - base
            deps = [_fid(f) for f in cur['facts'] + prev['facts']]
            ev = cur['evidence_ids'] or prev['evidence_ids']
            formula_ops = (f'{value} - {base}')
            cross = '（跨年：上年 Q4 → 本年 Q1）' if cy != py else ''
            if base > ZERO:
                ratio = change / base
                metrics.append({
                    'code': f'{code}_qoq', 'value': str(ratio), 'unit': 'ratio',
                    'company': company, 'period': f'{cy}Q{cq}', 'period_type': 'quarter',
                    'scope': scope, 'currency': currency, 'status': 'calculated',
                    'formula': f'({code}_single_quarter - prior_quarter) / prior_quarter{cross}',
                    'dependency_fact_ids': deps, 'evidence_ids': ev,
                    'derived': True, 'note': NOTE,
                    'operands': {'current_single_quarter': str(value), 'prior_single_quarter': str(base),
                                 'current_method': cur['method'], 'prior_method': prev['method']},
                })
            else:
                metrics.append({
                    'code': f'{code}_qoq_change', 'value': str(change), 'unit': 'amount',
                    'company': company, 'period': f'{cy}Q{cq}', 'period_type': 'quarter',
                    'scope': scope, 'currency': currency, 'status': 'calculated',
                    'formula': f'{code}_single_quarter - prior_quarter{cross}',
                    'dependency_fact_ids': deps, 'evidence_ids': ev,
                    'derived': True, 'note': NOTE,
                    'operands': {'current_single_quarter': str(value), 'prior_single_quarter': str(base)},
                })
                reviews.append({
                    'code': 'qoq_base_nonpositive',
                    'message': (f'{code} {cy}Q{cq} 的环比基数为 {base}（非正数）：'
                                f'{"上一单季为上年 Q4" if cy != py else f"上一单季为 {py}Q{pq}"}，'
                                '不报告环比百分比，只给绝对增减额。'),
                    'facts': deps, 'evidence_ids': ev,
                })
    return metrics, reviews


# ------------------------------------------------------------------ 2. 会计口径变化

# 口径已知的两个值才有可比性。scope='unknown' 时无法排除「合并 vs 母公司」混用，
# 把它写成"口径变化"就是误报（项目纪律：宁可漏报也不误报），所以单独走 suspects。
KNOWN_SCOPES = ('consolidated', 'parent')


def _restatement_groups(facts):
    """按 (科目, 期间, 口径, 币种, 单位, 量级, 公司) 分组，只留出现多个不同值的组。

    单位与量级（scale）必须进 key：把 0.2825（%）和 110,553,006,650.89（元）放进同一组
    比较出来的"差额"没有任何意义，那不是重述，是取数错误，由别的检查负责。
    """
    groups = {}
    for f in facts or []:
        v = _dec(f.get('value'))
        if v is None:
            continue
        key = (f.get('code') or '', f.get('period') or '', f.get('scope') or '',
               f.get('currency') or '', f.get('unit') or '', f.get('scale') or 1,
               f.get('company') or '')
        groups.setdefault(key, []).append((v, f))
    out = []
    for key, items in sorted(groups.items(), key=lambda kv: [str(x) for x in kv[0]]):
        distinct = {}
        for v, f in items:
            distinct.setdefault(v, []).append(f)
        if len(distinct) < 2:
            continue
        out.append((key, sorted(distinct.items(), key=lambda kv: kv[0])))
    return out


def _restatement_record(key, ordered):
    code, period, scope, currency, unit, scale, company = key
    base_value, _base_facts = ordered[0]
    latest_value, _latest_facts = ordered[-1]
    delta = latest_value - base_value
    pct = (delta / abs(base_value)) if base_value != ZERO else None
    rebuilt = any((f.get('source') or {}).get('rebuilt_table')
                  for _v, fs in ordered for f in fs)
    note = ('同一科目、同一期间、同一口径与单位下出现多个值，通常来自追溯重述、'
            '同一控制下企业合并或列报口径调整。本记录只给出差异，成因须人工读附注确认，'
            '不得据此推断为差错。')
    if rebuilt:
        note += ('注意：其中至少一个值来自 OCR 坐标重建表（rebuilt_table=true），'
                 '差异更可能是重建时的行列错位，请先抽查原文单元格再判断。')
    return {
        'code': code, 'period': period, 'scope': scope, 'currency': currency,
        'unit': unit, 'scale': scale, 'company': company,
        'rebuilt_table': rebuilt,
        'values': [{
            'value': str(v), 'restated': bool((fs[0] or {}).get('restated')),
            'documents': sorted({(f.get('source') or {}).get('document_id') or '' for f in fs} - {''}),
            'evidence_ids': [e for f in fs for e in (f.get('evidence_ids') or [])],
            'fact_ids': [_fid(f) for f in fs],
        } for v, fs in ordered],
        'delta': str(delta),
        'delta_ratio': None if pct is None else str(pct),
        'cause': None,
        'note': note,
    }


def restatement_changes(facts):
    """同一 (科目, 期间, 口径, 币种, 单位, 量级, 公司) 出现多个不同值 → 口径变化/重述候选。

    返回记录列表。**只报告差异，不判断成因**（政策变更 / 差错更正 / 重分类的判定
    需要人工读附注），成因一栏固定为 None。口径未知（scope='unknown'）的组不进本清单，
    见 `restatement_suspects`——那种差异更可能是合并/母公司混用，报成"口径变化"会误报。
    """
    return [_restatement_record(key, ordered) for key, ordered in _restatement_groups(facts)
            if key[2] in KNOWN_SCOPES]


def restatement_suspects(facts):
    """口径未确定（scope='unknown'）但同期间出现多值的组。

    单独返回、单独说明：它们**不是**口径变化的证据，只是"取数可能混了口径"的线索，
    需要回原文确认该行属于合并表还是母公司表。
    """
    out = []
    for key, ordered in _restatement_groups(facts):
        if key[2] in KNOWN_SCOPES:
            continue
        record = _restatement_record(key, ordered)
        record['note'] = ('该组事实的口径（合并/母公司）未确定：多值更可能来自口径混用，'
                          '而不是重述。**不得**当作口径变化引用；如需使用，先回原文确认口径。')
        record['reason'] = 'scope_unknown'
        out.append(record)
    return out



# ------------------------------------------------------------------ 3b. 年报「分季度主要财务指标」

# 单季值的期末日
_MMDD_BY_Q = {1: '03-31', 2: '06-30', 3: '09-30', 4: '12-31'}

# 「八、分季度主要财务指标」表的行名 → 标准科目代码。
# 只收「期间流量类」科目：资产负债类（总资产/归母净资产）是时点值，没有环比语义，一律不收。
# 每股收益也不收：季度 EPS 之和与全年 EPS 因股数变动/四舍五入对不上，勾稽会把整表判为未通过。
QUARTER_ROW_CODES = {
    '营业收入': 'revenue',
    '归属于上市公司股东的净利润': 'parent_net_profit',
    '归属于上市公司股东的扣除非经常性损益的净利润': 'nonrecurring_adj_parent_profit',
    '归属于上市公司股东的扣除非经常性损益后的净利润': 'nonrecurring_adj_parent_profit',
    '扣除非经常性损益后的净利润': 'nonrecurring_adj_parent_profit',
    '扣除非经常性损益的净利润': 'nonrecurring_adj_parent_profit',
    '净利润': 'net_profit',
    '经营活动产生的现金流量净额': 'operating_cash_flow',
}

_QUARTER_HEADER_RE = re.compile(r'^第([一二三四])季度$')
_Q_HEADER_RE = re.compile(r'^[Qq]([1-4])$')
_QCN = '一二三四'


def _norm_label(text):
    """去空白（含换行）+ 去掉尾部的单位括号。'营业收入（元）' → '营业收入'。"""
    s = re.sub(r'\s+', '', text or '')
    while True:
        m = re.search(r'[（(][^（()）]*[)）]$', s)
        if not m:
            break
        s = s[:m.start()]
    return s


def _num(text):
    """'41,506,860,074.79' → Decimal；'(1,234)' → 负数；空/破折号/不适用 → None。"""
    s = (text or '').strip()
    if not s or s in ('--', '—', '–', '-', '/', '不适用', 'N/A'):
        return None
    negative = s[0] in '(（'
    s = s.strip('()（）').replace(',', '').replace(' ', '').replace('\u00a0', '')
    try:
        value = D(s)
    except (InvalidOperation, ValueError):
        return None
    return -value if negative else value


def _quarter_columns(rows):
    """在前几行里找「第一季度…第四季度」表头。返回 {列下标: 季度号}；不足 4 列返回 {}。"""
    for row in (rows or [])[:4]:
        found = {}
        for index, cell in enumerate(row or []):
            label = _norm_label(cell)
            m = _QUARTER_HEADER_RE.match(label)
            if m:
                found[index] = _QCN.index(m.group(1)) + 1
                continue
            m = _Q_HEADER_RE.match(label)
            if m:
                found[index] = int(m.group(1))
        if len(found) == 4:
            return found
    return {}


def _qfact_id(document_id, code, period):
    """确定性 ID：同输入同输出（便于复现与去重）。前缀 fq_ 与引擎的 f_ 区分开。"""
    import hashlib
    raw = f'quarter|{document_id}|{code}|{period}'.encode('utf-8')
    return 'fq_' + hashlib.sha256(raw).hexdigest()[:12]


def quarter_facts_from_document(doc, facts, *, limit_tables=4):
    """从一份定期报告里抽取「分季度主要财务指标」表的四个单季值。

    返回 (quarter_facts, reviews, notes)。

    年份不靠猜：从**同一份文档**里该科目的年度事实（期末为 12-31）取，并用
    「四季度之和 == 年度值」做勾稽。勾稽通过 → status='verified'（可参与环比）；
    不通过或找不到年度事实 → status='needs_review'（只记录、不参与环比，进复核队列）。
    """
    if not isinstance(doc, dict):
        return [], [], []
    document_id = doc.get('document_id') or ''
    doc_facts = [f for f in (facts or [])
                 if (f.get('source') or {}).get('document_id') == document_id]

    # 该文档里的年度事实（用于定年份 + 勾稽）
    annual = {}
    for f in doc_facts:
        period = f.get('period') or ''
        if not period.endswith('-12-31'):
            continue
        if (f.get('period_type') or '').lower() not in ('annual', 'ytd', 'year'):
            continue
        if f.get('status') not in (None, 'verified', 'calculated'):
            continue
        if _dec(f.get('value')) is None:
            continue
        key = (f.get('code'), f.get('scope') or '', f.get('currency') or '')
        annual.setdefault(key, f)
    if not annual:
        return [], [], ['未在该文档里找到年度（12-31）事实，无法确定分季度表的年份，故不抽取。']

    tables = [t for t in (doc.get('tables') or []) if t.get('rows')]
    out, reviews, notes = [], [], []
    used = 0
    for table in tables:
        if used >= limit_tables:
            break
        rows = table.get('rows') or []
        cols = _quarter_columns(rows)
        if not cols:
            continue
        table_id = table.get('table_id') or ''
        page = table.get('page')
        rows_emitted = 0
        for row_index, row in enumerate(rows):
            code = QUARTER_ROW_CODES.get(_norm_label((row or [None])[0] if row else None))
            if not code:
                continue
            # 优先取合并口径、本币的年度事实
            ref = None
            for key, f in annual.items():
                if key[0] == code and (key[1] == 'consolidated' or ref is None):
                    ref = f
                    if key[1] == 'consolidated':
                        break
            if ref is None:
                notes.append(f'分季度表 {table_id} 的「{_norm_label(row[0])}」在本文档没有对应年度事实，'
                             f'无法定年份与勾稽，已跳过。')
                continue
            year = (ref.get('period') or '')[:4]
            scale = ref.get('scale') or 1
            values = {}
            for col, quarter in sorted(cols.items()):
                raw = row[col] if col < len(row) else None
                value = _num(raw)
                if value is None:
                    continue
                values[quarter] = (value, raw, col)
            if len(values) < 4:
                notes.append(f'分季度表 {table_id} 的「{_norm_label(row[0])}」只有 {len(values)} 个季度值，'
                             f'不足四季度，已跳过。')
                continue
            total = sum(v for v, _raw, _col in values.values())
            target = _dec(ref.get('value'))
            tolerance = max(D('0.5') * D(str(scale)), D('1'))
            diff = total - target if target is not None else None
            reconciled = diff is not None and abs(diff) <= tolerance
            for quarter, (value, raw, col) in values.items():
                period = f'{year}-{_MMDD_BY_Q[quarter]}'
                out.append({
                    'fact_id': _qfact_id(document_id, code, period),
                    'code': code,
                    'raw_label': _norm_label(row[0]),
                    'company': ref.get('company') or '',
                    'period': period,
                    'period_type': 'quarter',
                    'scope': ref.get('scope') or 'consolidated',
                    'currency': ref.get('currency') or '',
                    'value': str(value),
                    'raw_value': str(raw),
                    'unit': ref.get('unit') or '元',
                    'scale': scale,
                    'value_unit': ref.get('value_unit') or ref.get('currency') or '',
                    'evidence_ids': [f'{table_id}:r{row_index + 1}:c{col + 1}',
                                     f'{table_id}:r1:c{col + 1}'],
                    'source': {'document_id': document_id, 'filename': doc.get('filename') or '',
                               'page': page, 'table_id': table_id,
                               'row': row_index, 'column': col, 'rebuilt_table': bool(table.get('rebuilt'))},
                    'status': 'verified' if reconciled else 'needs_review',
                    'cross_check': {
                        'against_fact_id': ref.get('fact_id'),
                        'against_tier': ref.get('source_tier'),
                        'difference': None if diff is None else str(diff),
                        'result': 'consistent_within_rounding' if reconciled else 'quarter_sum_mismatch',
                    },
                    'derived': True,
                    'note': ('来自年报「分季度主要财务指标」表的直接披露值（单季，非累计）。'
                             '已用「四季度之和 == 年度值」勾稽。'),
                })
                rows_emitted += 1
            if not reconciled and target is not None:
                reviews.append({
                    'code': 'quarter_sum_mismatch',
                    'message': (f'{table_id} 的「{_norm_label(row[0])}」四季度之和 {total} 与年度值 {target} '
                                f'相差 {diff}（容差 {tolerance}）：已标记为 needs_review，**不参与环比计算**，'
                                '请核对原表列报口径或单位。'),
                    'fact_ids': [_qfact_id(document_id, code, f'{year}-{_MMDD_BY_Q[q]}') for q in sorted(values)],
                    'evidence_ids': [f'{table_id}:r{row_index + 1}:c{c + 1}' for c in sorted(cols)],
                })
        if rows_emitted:
            used += 1
    if not out:
        notes.append('未在本文档定位到可用的「分季度主要财务指标」表：可能未披露，或表头不是'
                     '「第一季度…第四季度」，或行名不在已知映射内。')
    return out, reviews, notes


def quarter_facts_from_documents(docs, facts, *, limit_tables=4):
    """多份文档的汇总入口。返回 (quarter_facts, reviews, notes)。"""
    out, reviews, notes = [], [], []
    for doc in (docs or []):
        one, one_reviews, one_notes = quarter_facts_from_document(doc, facts, limit_tables=limit_tables)
        out.extend(one)
        reviews.extend(one_reviews)
        notes.extend(one_notes)
    return out, reviews, notes


# ------------------------------------------------------------------ 4. 业绩变化原因定位

def _blocks_of(page):
    blocks = page.get('blocks') or []
    if blocks:
        return [(b.get('text') or '', b.get('evidence_id') or '') for b in blocks]
    return [(page.get('text') or '', '')]


def reason_candidates(doc, *, limit=40, context_chars=220):
    """在报告里定位"业绩变动原因"相关段落。

    返回候选段落（页码 + 证据 ID + 命中关键词 + 原文摘录）。
    **不做任何归纳或判断**——归因属于模型工作，且必须标为推断。

    同一段原文只保留首次出现（页眉/页脚会在每页重复，不去重会刷满整个清单）。
    """
    if not isinstance(doc, dict):
        return []
    pages = doc.get('pages') or []
    out = []
    seen = set()
    for page in pages:
        page_no = page.get('page')
        for text, evidence_id in _blocks_of(page):
            if not text:
                continue
            hit = next((k for k in REASON_KEYWORDS if k in text), None)
            if not hit:
                continue
            snippet = re.sub(r'\s+', ' ', text).strip()
            if len(snippet) > context_chars:
                idx = snippet.find(hit)
                start = max(0, idx - context_chars // 2)
                snippet = ('…' if start else '') + snippet[start:start + context_chars] + '…'
            # 同一段原文（去掉页码相关差异后）只留一次；页眉页脚正是靠这一步消掉。
            fingerprint = re.sub(r'\s+', '', snippet)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            out.append({
                'page': page_no, 'evidence_id': evidence_id, 'keyword': hit, 'quote': snippet,
                'source_document': (doc.get('filename') or doc.get('document_id') or ''),
            })
            if len(out) >= limit:
                return out
    return out


def _as_docs(doc):
    """把单份/多份文档统一成列表。每处候选都会带 source_document，故多文档不会混淆出处。"""
    if doc is None:
        return []
    if isinstance(doc, dict):
        return [doc]
    return [d for d in doc if isinstance(d, dict)]


def gap_analysis(facts, doc=None, *, reason_limit=40, extra_facts=()):
    """三个缺口的统一入口。返回 (metrics, reviews, changes, reasons, notes)。

    doc 可以是单份解析结果，也可以是列表（多份输入时按文件名分别定位，候选带 source_document）。
    extra_facts 只参与**环比**：分季度表抽出的单季值与引擎的累计值在同一期末会并存
    （例如 2025-12-31 既有 Q4 单季 33.27 亿、又有全年累计 1704.47 亿），
    那是口径不同而不是重述，所以 extra_facts **不进入** restatement_changes。
    """
    metrics, reviews = qoq_metrics(list(facts or []) + list(extra_facts or []))
    changes = restatement_changes(facts)
    suspects = restatement_suspects(facts)
    docs = _as_docs(doc)
    reasons = []
    for one in docs:
        if len(reasons) >= reason_limit:
            break
        reasons.extend(reason_candidates(one, limit=reason_limit - len(reasons)))
    notes = []
    if not metrics:
        notes.append('未产出环比指标：需要同一科目至少两个相邻季度（或"上一期累计+本期累计"）的可核验事实；'
                     '缺任一期就不推算。')
    if not changes:
        notes.append('未发现口径已知且同科目同期间的多值情况：没有重述/口径变化的证据。'
                     '这不代表没有口径变化，只代表本次输入里没出现冲突值。')
    if suspects:
        notes.append(f'另有 {len(suspects)} 组同科目同期间出现多值、但口径（合并/母公司）未确定，'
                     '已单独归入 restatement_suspects，**不计入口径变化清单**：'
                     '这类差异更可能来自口径混用，报成重述会误报。')
    if docs and not reasons:
        notes.append('未在报告中定位到"业绩变动原因"相关段落：可能是报告未披露，或该部分未被解析到。'
                     '两者必须区分，不得写成"公司未披露"。')
    if reasons:
        notes.append(f'已定位 {len(reasons)} 处业绩变动原因候选段落。'
                     '段落内容需由模型阅读后归纳，且必须标注为推断（非引擎结论）。')
    return metrics, reviews, changes, reasons, notes
