"""原始财报（巨潮 PDF）-> 事实库，并与研报声明（claim）对账。

输入：公司一季报 / 半年报 / 年报 PDF（文字版）。
输出：SourceDoc，含逐页表格行、"科目 + 数值列 + 页码 + 单位 + 报表口径"事实，
      以及 find_value / lookup 等查询接口，供 errorcheck_service 做来源定位。

设计原则（与项目哲学一致）：
- 数值一律 Decimal；单位显式（元/千元/万元/百万元/亿元），统一换算到"元"再比较；
- 口径显式：合并 / 母公司；找不到口径就降级为"口径待确认"，不猜；
- 每个事实都带页码和原文行，能在三次点击内回到原文。
"""
from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .report_parse import parse_pdf, is_numeric_cell

UNIT_SCALE = {'元': Decimal(1), '千元': Decimal(1000), '万元': Decimal(10000), '百万元': Decimal(1000000),
              '亿元': Decimal(100000000), '亿': Decimal(100000000), '万': Decimal(10000), '百万': Decimal(1000000),
              '千万元': Decimal(10000000), '十亿元': Decimal(1000000000)}
_UNIT_DECL = re.compile(r'单位(?:为|[:：])?\s*[:：]?\s*(?:人民币)?\s*(千万元|百万元|十亿元|万元|千元|亿元|元)')
_UNIT_DECL2 = re.compile(r'[（(]\s*(?:单位[:：]?\s*)?(?:人民币)?(百万元|万元|千元|亿元|元)\s*[）)]')
_STATEMENT = re.compile(r'(合并|母公司)?\s*(资产负债表|利润表|现金流量表|所有者权益变动表|股东权益变动表|利润及利润分配表|损益表|综合收益表)')
_NUM = re.compile(r'^[（(]?[-−–]?\s*\d[\d,，]*(?:\.\d+)?[）)]?$')


def to_decimal(s):
    """'1,234.56' / '(1,234)' / '-12.3' / '12.3%' -> Decimal；失败返回 None。"""
    if s is None:
        return None
    t = str(s).strip().replace('，', ',').replace('−', '-').replace('–', '-').replace(' ', '')
    neg = False
    if re.match(r'^[（(].*[）)]$', t):
        neg, t = True, t[1:-1]
    t = t.rstrip('%％').replace(',', '')
    if t.startswith('-'):
        neg, t = (not neg) or True, t[1:]
    try:
        d = Decimal(t)
    except (InvalidOperation, ValueError):
        return None
    return -d if neg else d


@dataclass
class Fact:
    label: str
    nums: list
    texts: list
    page: int
    unit: str
    scale: Decimal
    statement: str      # 例：合并利润表 / 主要会计数据 / ''
    scope: str          # 合并 / 母公司 / ''
    row_text: str
    doc: str = ''

    def values_yuan(self):
        return [n * self.scale if n is not None else None for n in self.nums]

    def to_dict(self):
        return {'label': self.label, 'texts': self.texts, 'page': self.page, 'unit': self.unit, 'statement': self.statement,
                'scope': self.scope, 'row_text': self.row_text, 'doc': self.doc}


def norm_label(s):
    # 先去掉标签末尾的"单位括号"：'营业收入（元）'、'归属于上市公司股东的净利润（元）'
    # 与不带单位的写法是同一个科目。不去掉这一步，主要会计数据表里几乎所有科目
    # 都会因为多了个"（元）"而匹配不上规则，整个年度的来源事实就都丢了。
    s = re.sub(r'[（(]\s*(?:人民币)?\s*(?:千万元|百万元|十亿元|千元|万元|亿元|百万|万|亿|元|%)\s*[）)]\s*$', '', s or '')
    s = re.sub(r'[\s　:：（）()\[\]【】,，、]', '', s or '')
    s = re.sub(r'^[一二三四五六七八九十]+[、.]', '', s)
    s = re.sub(r'^[0-9]+[、.]', '', s)
    s = s.replace('“', '').replace('”', '')
    s = re.sub(r'^其中', '', s)
    s = re.sub(r'^(减|加)', '', s)
    return s


class SourceDoc:
    def __init__(self, path, kind=None, company=None):
        self.path = str(path)
        self.kind = kind or ''
        self.company = company
        self.facts: list[Fact] = []
        self.page_text: dict[int, str] = {}
        self.period_title = ''

    # ------------------------------------------------------------ 构建
    @classmethod
    def from_pdf(cls, path, kind=None, cache_dir=None):
        path = Path(path)
        cache = Path(cache_dir) / (path.stem + '.facts.json.gz') if cache_dir else None
        if cache and cache.is_file():
            return cls.load(cache)
        doc = parse_pdf(path)
        sd = cls(path, kind)
        sd._extract(doc)
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            sd.save(cache)
        return sd

    def _fallback_period(self):
        txt = ''.join(self.page_text.get(p, '') for p in (1, 2, 3, 4, 5))
        txt = txt.translate(str.maketrans('零〇一二三四五六七八九', '00123456789')) if '二零' in txt else txt
        m = re.search(r'(20\d{2})\s*年\s*(?:年度报告|年报|年年度报告)', txt)
        if m:
            return m.group(1) + ' 年年度'
        m = re.search(r'(20\d{2})\s*年\s*(半年度|中期)', txt)
        if m:
            return m.group(1) + ' 年半年度'
        from collections import Counter
        yrs = Counter(re.findall(r'(20\d{2})\s*年', txt))
        if yrs:
            y = max(yrs, key=lambda k: (yrs[k], k))
            return f'{y} 年年度'
        return ''

    def _extract(self, doc):
        blocks = doc.blocks
        unit, statement, scope = '元', '', ''
        cur_page = None
        pending_label = []
        for i, b in enumerate(blocks):
            if b.page != cur_page:
                cur_page = b.page
                pending_label = []
            self.page_text[b.page] = self.page_text.get(b.page, '') + b.text + '\n'
            if i < 40 and not self.period_title:
                tt = b.text.translate(str.maketrans('零〇一二三四五六七八九', '00123456789')) if '二零' in b.text or '二〇' in b.text else b.text
                tt = re.sub(r'二(?=0[0-9]{2}年)', '2', tt) if False else tt
                m = re.search(r'(20\d{2})\s*年\s*(第一季度|一季度|第三季度|三季度|半年度|中期|中报|年度|年)', tt)
                if m and ('报告' in tt or '中报' in tt):
                    self.period_title = m.group(0)
            t = b.text
            m = _UNIT_DECL.search(t) or _UNIT_DECL2.search(t)
            if m and len(t) < 160:
                unit = m.group(1)
            ms = _STATEMENT.search(t)
            if ms and len(t) < 40 and b.kind != 'row' or (ms and b.kind == 'row' and len(b.cells) <= 3 and len(t) < 40):
                statement = (ms.group(1) or '') + ms.group(2)
                if ms.group(1):
                    scope = ms.group(1)
                else:
                    scope = ''
            elif re.search(r'分季度', t) and len(t) < 40:
                statement = '分季度'
                scope = '合并'
            elif re.search(r'主要会计数据|主要财务数据|主要财务指标', t) and len(t) < 40:
                statement = '主要会计数据'
                scope = '合并'
            if b.kind != 'row':
                if len(t) <= 40 and not re.search(r'[。；]$', t):
                    pending_label.append(t)
                    pending_label = pending_label[-3:]
                else:
                    pending_label = []
                continue
            cells = [c for c in b.cells if c != '']
            if not cells:
                continue
            numeric_idx = [j for j, c in enumerate(cells) if _NUM.match(c.replace(' ', '')) or re.match(r'^[-−–]?\d[\d,.]*\s*%?$', c.strip())]
            if not numeric_idx:
                pending_label = []
                continue
            first = numeric_idx[0]
            if first == 0:
                label = ''.join(pending_label)
            else:
                label = ' '.join(cells[:first])
            pending_label = []
            # 附注编号列（如 "七、1"）会让第一个数字成为附注号，这里保留，由查询端容忍
            nums = [to_decimal(cells[j]) for j in numeric_idx]
            texts = [cells[j] for j in numeric_idx]
            # 行内带 % 的列单独保留在 texts 里
            self.facts.append(Fact(label=label.strip(), nums=nums, texts=texts, page=b.page, unit=unit,
                                   scale=UNIT_SCALE.get(unit, Decimal(1)), statement=statement, scope=scope,
                                   row_text=b.text, doc=self.kind))
        if not self.period_title:
            self.period_title = self._fallback_period()

    # ------------------------------------------------------------ 持久化
    def save(self, path):
        data = {'path': self.path, 'kind': self.kind, 'period_title': self.period_title,
                'pages': {str(k): v for k, v in self.page_text.items()},
                'facts': [dict(f.to_dict(), nums=[None if n is None else str(n) for n in f.nums], scale=str(f.scale)) for f in self.facts]}
        with gzip.open(path, 'wt', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False)

    @classmethod
    def load(cls, path):
        with gzip.open(path, 'rt', encoding='utf-8') as fh:
            data = json.load(fh)
        sd = cls(data['path'], data.get('kind'))
        sd.period_title = data.get('period_title', '')
        sd.page_text = {int(k): v for k, v in data['pages'].items()}
        for f in data['facts']:
            sd.facts.append(Fact(label=f['label'], nums=[None if n is None else Decimal(n) for n in f['nums']], texts=f['texts'],
                                 page=f['page'], unit=f['unit'], scale=Decimal(f['scale']), statement=f['statement'],
                                 scope=f['scope'], row_text=f['row_text'], doc=f.get('doc', '')))
        return sd

    # ------------------------------------------------------------ 查询
    def find_value(self, label_keys, value, decimals=2, unit_scale=Decimal('100000000'), tol_steps=Decimal('0.5001'), pct=False, cols=None):
        """在事实库中找"标签含 label_keys 之一、某列数值按 unit_scale 折算后四舍五入到 decimals 位等于 value"的行。
        返回命中的 Fact 列表（按页码排序）。pct=True 时按百分数比较（列文本里带小数的比率）。"""
        hits = []
        step = Decimal(1).scaleb(-decimals)
        for f in self.facts:
            if label_keys and not any(k in norm_label(f.label) or k in f.label for k in label_keys):
                continue
            vals = f.nums if pct else [n * f.scale / unit_scale if n is not None else None for n in f.nums]
            for j, v in enumerate(vals):
                if v is None or (cols is not None and j not in cols):
                    continue
                if abs(v - value) <= step * tol_steps:
                    hits.append((f, j))
                    break
        return sorted(hits, key=lambda x: x[0].page)

    def find_label(self, keys, exact=False):
        out = []
        for f in self.facts:
            nl = norm_label(f.label)
            if (exact and any(nl == k for k in keys)) or (not exact and any(k in nl for k in keys)):
                out.append(f)
        return out


# ====================================================================== 指标层
# 规范指标码 -> (标签匹配正则, 允许的报表)；先到先得，越靠前优先级越高
_METRIC_RULES = [
    ('revenue', r'^(?:其中)?营业总?收入$|^营业收入合计$|^收入合计$|^营业总收入$', ('合并利润表', '主要会计数据', '利润表', '')),
    ('cost_of_revenue', r'^(?:其中)?营业总?成本$|^营业成本$', ('合并利润表', '利润表')),
    ('operating_profit', r'^营业利润', ('合并利润表', '主要会计数据')),
    ('total_profit', r'^利润总额', ('合并利润表', '主要会计数据')),
    ('net_profit', r'^净利润(?:（净亏损以.*号填列）)?$|^五、净利润|^四、净利润|^净利润合计', ('合并利润表',)),
    ('parent_net_profit', r'归属于(?:上市公司股东|母公司股东|母公司所有者|本行股东|母公司)的净利润$|^1\.?归属于母公司(?:所有者|股东)的净利润|归属于母公司股东的净利润|归属于上市公司股东的净利润|归属于本行股东的净利润|^归属于本公司股东的净利润', ('合并利润表', '主要会计数据', '利润表')),
    ('adj_parent_net_profit', r'扣除非经常性损益(?:后)?的?(?:归属于.*)?净利润|扣除非经常性损益后归属|归属于.*扣除非经常性损益', ('主要会计数据',)),
    ('minority_profit', r'少数股东损益', ('合并利润表',)),
    ('ocf', r'^经营活动产生的现金流量净额|^经营活动现金流量净额', ('合并现金流量表', '主要会计数据')),
    ('total_assets', r'^资产总计$|^总资产$|^资产总额$', ('合并资产负债表', '主要会计数据')),
    ('total_liabilities', r'^负债合计$|^负债总计$', ('合并资产负债表',)),
    ('total_equity', r'^所有者权益(?:（或股东权益）)?合计$|^股东权益合计$', ('合并资产负债表',)),
    ('parent_equity', r'^归属于(?:母公司|上市公司股东|本行股东|母公司股东)的?(?:所有者权益|股东权益)(?:（或股东权益）)?(?:合计)?$|^归属于母公司所有者权益', ('合并资产负债表', '主要会计数据')),
    ('cash', r'^货币资金$', ('合并资产负债表',)),
    ('inventory', r'^存货$', ('合并资产负债表',)),
    ('receivables', r'^应收账款$', ('合并资产负债表',)),
    ('selling_expense', r'^销售费用$', ('合并利润表',)),
    ('admin_expense', r'^管理费用$', ('合并利润表',)),
    ('rd_expense', r'^研发费用$', ('合并利润表',)),
    ('finance_expense', r'^财务费用$', ('合并利润表',)),
    ('basic_eps', r'^基本每股收益', ('主要会计数据', '合并利润表')),
    ('diluted_eps', r'^稀释每股收益', ('主要会计数据', '合并利润表')),
    ('roe', r'^加权平均净资产收益率', ('主要会计数据',)),
]
_FLOW = {'revenue', 'cost_of_revenue', 'operating_profit', 'total_profit', 'net_profit', 'parent_net_profit', 'adj_parent_net_profit',
         'minority_profit', 'ocf', 'selling_expense', 'admin_expense', 'rd_expense', 'finance_expense', 'basic_eps', 'diluted_eps', 'roe'}
_STOCK = {'total_assets', 'total_liabilities', 'total_equity', 'parent_equity', 'cash', 'inventory', 'receivables'}


@dataclass
class Cand:
    metric: str
    period: str         # 2026Q1 / 2025Q1 / 2025FY / 2026-03-31 ...
    value: Decimal      # 金额：元；比率：小数（0.0857）；每股：元
    kind: str           # amount | ratio | per_share
    page: int
    quote: str
    doc: str
    scope: str = '合并'
    derived: str = ''   # 非空表示由公式推得，内容为公式说明
    col: str = ''       # cur / prior / prior_adj / pct

    def to_dict(self):
        return {'metric': self.metric, 'period': self.period, 'value': str(self.value), 'kind': self.kind, 'page': self.page,
                'quote': self.quote, 'doc': self.doc, 'scope': self.scope, 'derived': self.derived, 'col': self.col}


def doc_periods(title, kind):
    """由标题/类别推出 (当期id, 上年同期id, 期末日期当期, 期末上年同期)。"""
    m = re.search(r'(20\d{2})', title or '')
    y = int(m.group(1)) if m else 0
    if kind and kind.endswith('q1') or '一季度' in (title or ''):
        return (f'{y}Q1', f'{y-1}Q1', f'{y}-03-31', f'{y-1}-12-31', 'Q1')
    if kind and kind.endswith('h1') or '半年度' in (title or '') or '中期' in (title or ''):
        return (f'{y}H1', f'{y-1}H1', f'{y}-06-30', f'{y-1}-12-31', 'H1')
    if kind and kind.endswith('q3') or '三季度' in (title or ''):
        return (f'{y}Q1-Q3', f'{y-1}Q1-Q3', f'{y}-09-30', f'{y-1}-12-31', 'Q3')
    return (f'{y}FY', f'{y-1}FY', f'{y}-12-31', f'{y-1}-12-31', 'FY')


def _build_candidates(sd):
    cur, prior, end_cur, end_prior, ptype = doc_periods(sd.period_title, sd.kind)
    cands = []
    seen = set()
    for f in sd.facts:
        label = norm_label(f.label)
        if not label or not f.nums:
            continue
        # 去掉附注编号列（如"七、61"被拆成 7、61）：附注列通常在标签后且为小整数，已由 label 吸收
        for metric, pat, stmts in _METRIC_RULES:
            if not re.search(pat, label):
                continue
            stmt = f.statement or ''
            if not any((st == stmt) or (st and st in stmt) or (st == '' and stmt == '') for st in stmts):
                continue
            if f.scope == '母公司' or stmt.startswith('母公司'):
                continue
            nums = [n for n in f.nums]
            scale = f.scale
            kind = 'per_share' if metric.endswith('eps') else ('ratio' if metric == 'roe' else 'amount')
            if stmt == '分季度':
                for qi, n in enumerate(nums[:4], start=1):
                    y = int(cur[:4]) if ptype == 'FY' else int(cur[:4])
                    key = (metric, f'{y}Q{qi}', n, f.page)
                    if key in seen or n is None:
                        continue
                    seen.add(key)
                    cands.append(Cand(metric, f'{y}Q{qi}', n * scale, kind, f.page, f.row_text, sd.kind, '合并', '', 'quarter'))
                break
            if len(nums) < 1:
                break
            is_balance = metric in _STOCK
            pc, pp = (end_cur, end_prior) if is_balance else (cur, prior)
            # 列语义：cur, prior(...)(可能有调整前后两列), pct
            has_pct = len(nums) >= 3 and f.texts[-1].count('.') <= 1 and (abs(nums[-1]) < 1000) and (abs(nums[0]) > 1000 or '%' in f.texts[-1])
            if kind != 'amount':
                has_pct = len(nums) >= 4 and metric.endswith('eps')
            body = nums[:-1] if has_pct else nums
            if metric == 'roe':
                body = nums[:2]
                has_pct = False
            for ci, n in enumerate(body):
                if n is None:
                    continue
                if ci == 0:
                    per, col = pc, 'cur'
                elif ci == 1:
                    per, col = pp, 'prior'
                elif ci == 2 and not is_balance and ptype != 'FY':
                    per, col = pp, 'prior_adj'
                else:
                    break
                v = n * scale if kind == 'amount' else (n / Decimal(100) if kind == 'ratio' else n)
                key = (metric, per, v, f.page)
                if key in seen:
                    continue
                seen.add(key)
                cands.append(Cand(metric, per, v, kind, f.page, f.row_text, sd.kind, f.scope or '合并', '', col))
            if has_pct:
                n = nums[-1]
                cands.append(Cand(metric + '_yoy', cur if not is_balance else pc, n / Decimal(100), 'ratio', f.page, f.row_text, sd.kind,
                                  f.scope or '合并', '', 'pct'))
            break
    return cands


def build_candidates(sd):
    """事实库 -> 规范指标候选（含公式推导的同比、毛利率、净利率）。"""
    cands = _build_candidates(sd)
    by = {}
    for c in cands:
        by.setdefault((c.metric, c.period), []).append(c)
    derived = []
    periods = {c.period for c in cands}
    for (m, per), lst in list(by.items()):
        if m in _FLOW and not m.endswith('eps') and m != 'roe' and per[-2:] in ('Q1', 'H1', 'FY') or (m in _FLOW and per.endswith(('Q1', 'H1', 'FY'))):
            y = int(per[:4])
            prev = f'{y-1}{per[4:]}'
            for c in lst:
                if c.kind not in ('amount', 'per_share'):
                    continue
                for p in by.get((m, prev), []):
                    if p.value and p.value != 0 and c.col in ('cur', 'quarter') and p.value > 0:
                        derived.append(Cand(m + '_yoy', per, c.value / p.value - 1, 'ratio', c.page, c.quote, c.doc, c.scope,
                                            f'{m} {per}/{prev}-1', 'derived'))
    # 毛利率 / 净利率 / 费用率
    for per in periods:
        rev = [c for c in by.get(('revenue', per), []) if c.col in ('cur', 'prior', 'prior_adj', 'quarter')]
        cost = by.get(('cost_of_revenue', per), [])
        for r in rev[:1]:
            for c in cost[:1]:
                if r.value:
                    derived.append(Cand('gross_margin', per, (r.value - c.value) / r.value, 'ratio', r.page, r.quote, r.doc, '合并',
                                        '(营业收入-营业成本)/营业收入', 'derived'))
            for m, tgt in (('parent_net_profit', 'net_margin_parent'), ('net_profit', 'net_margin'), ('selling_expense', 'selling_ratio'),
                           ('admin_expense', 'admin_ratio'), ('rd_expense', 'rd_ratio'), ('finance_expense', 'finance_ratio'),
                           ('operating_profit', 'operating_margin'), ('adj_parent_net_profit', 'adj_net_margin_parent')):
                for c in by.get((m, per), [])[:1]:
                    if r.value:
                        derived.append(Cand(tgt, per, c.value / r.value, 'ratio', c.page, c.quote, c.doc, '合并', f'{m}/revenue', 'derived'))
    # 资产负债率
    for per in periods:
        a = by.get(('total_assets', per), [])
        l = by.get(('total_liabilities', per), [])
        if a and l and a[0].value:
            derived.append(Cand('debt_ratio', per, l[0].value / a[0].value, 'ratio', a[0].page, a[0].quote, a[0].doc, '合并', '负债合计/资产总计', 'derived'))
    return cands + derived


def _candidates(self):
    if not hasattr(self, '_cands') or self._cands is None:
        self._cands = build_candidates(self)
    return self._cands


SourceDoc.candidates = _candidates
SourceDoc._cands = None
