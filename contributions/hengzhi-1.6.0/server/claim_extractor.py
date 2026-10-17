"""规则版研报声明（claim）抽取：指标 + 期间 + 数值 + 单位 + 口径修饰词 + 位置。

输入：Document（server.report_parse）或纯文本。输出：list[Claim]。
覆盖中文研报常见写法：
  - 正文："2026 年第一季度公司实现营业收入90.29 亿元，同比增长8.57%""归母净利润分别为361.4/370.2/380.3 亿元"
  - 表格：行标签带单位"营业收入(百万元)"、表头年份"2024 | 2025 | 2026E"、"(+/-%)"增速行、PE/PB/EPS 行
  - 口径修饰：归母/扣非/合并/母公司/单季/累计/调整后 等
确定性规则，无 LLM；指标词有歧义（如"收入"可能是分部收入）时降级为 unverifiable，不猜。
LLM 接口位：extract_claims(..., llm=None)——传入 llm(callable) 时，可对规则抽不出的句子做补抽（本期未接入，见 docs/prompt-additions/errorcheck.md）。

对外字段（Claim.to_dict）：id, metric, label, kind, value, unit, scale, decimals, period, is_forecast, growth_basis,
  modifiers, sentence, quote, block_id, page, span, where, row_label, col_header, raw, flags
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .report_parse import Document, parse_report, split_sentences

# ---------------------------------------------------------------- 指标词典（长词优先）
METRIC_WORDS = [
    ('扣非归母净利润', 'adj_parent_net_profit'), ('扣非后归母净利润', 'adj_parent_net_profit'), ('扣非归母净利', 'adj_parent_net_profit'),
    ('扣除非经常性损益后的归属于上市公司股东的净利润', 'adj_parent_net_profit'), ('扣除非经常性损益的净利润', 'adj_parent_net_profit'),
    ('扣非净利润', 'adj_parent_net_profit'), ('扣非后净利润', 'adj_parent_net_profit'),
    ('归属于上市公司股东的净利润', 'parent_net_profit'), ('归属于母公司股东的净利润', 'parent_net_profit'), ('归属于母公司所有者的净利润', 'parent_net_profit'),
    ('归属母公司股东净利润', 'parent_net_profit'), ('归属母公司净利润', 'parent_net_profit'), ('归母净利润', 'parent_net_profit'), ('归母净利', 'parent_net_profit'),
    ('归母利润', 'parent_net_profit'), ('归属净利润', 'parent_net_profit'), ('归母口径净利润', 'parent_net_profit'),
    ('母公司营业收入', 'pc_revenue'), ('母公司净利润', 'pc_net_profit'),
    ('营业总收入', 'revenue'), ('营业收入', 'revenue'), ('营收', 'revenue'),
    ('经营活动产生的现金流量净额', 'ocf'), ('经营性现金流净额', 'ocf'), ('经营活动现金流净额', 'ocf'), ('经营现金流净额', 'ocf'), ('经营活动现金流', 'ocf'), ('经营现金流', 'ocf'),
    ('销售毛利率', 'gross_margin'), ('综合毛利率', 'gross_margin'), ('毛利率', 'gross_margin'),
    ('归母净利率', 'net_margin_parent'), ('归母净利润率', 'net_margin_parent'), ('销售净利率', 'net_margin_parent'), ('净利率', 'net_margin_parent'), ('净利润率', 'net_margin_parent'),
    ('销售费用率', 'selling_ratio'), ('管理费用率', 'admin_ratio'), ('研发费用率', 'rd_ratio'), ('财务费用率', 'finance_ratio'), ('期间费用率', 'period_expense_ratio'),
    ('资产负债率', 'debt_ratio'), ('加权平均净资产收益率', 'roe'), ('净资产收益率', 'roe'), ('ROE', 'roe'),
    ('基本每股收益', 'basic_eps'), ('摊薄每股收益', 'eps'), ('每股收益', 'eps'), ('EPS', 'eps'),
    ('市盈率', 'pe'), ('P/E', 'pe'), ('PE', 'pe'), ('市净率', 'pb'), ('P/B', 'pb'), ('PB', 'pb'), ('EV/EBITDA', 'ev_ebitda'), ('PEG', 'peg'),
    ('总市值', 'market_cap'), ('营业成本', 'cost_of_revenue'), ('营业利润', 'operating_profit'), ('利润总额', 'total_profit'),
    ('资产总计', 'total_assets'), ('总资产', 'total_assets'), ('负债合计', 'total_liabilities'), ('总负债', 'total_liabilities'),
    ('归属于母公司所有者权益', 'parent_equity'), ('归母净资产', 'parent_equity'), ('归母权益', 'parent_equity'),
    ('销售费用', 'selling_expense'), ('管理费用', 'admin_expense'), ('研发费用', 'rd_expense'), ('财务费用', 'finance_expense'),
    ('净利润', 'net_profit'), ('收入', 'revenue_generic'),
]
_MW_RE = re.compile('|'.join(re.escape(k) for k, _ in sorted(METRIC_WORDS, key=lambda x: -len(x[0]))))
_MW_MAP = dict(METRIC_WORDS)
SEGMENT_HINT = re.compile(r'分部|业务|板块|地区|海外|国内|境外|境内|线上|线下|渠道|产品|品类|其中|单品|门店|客户|电池|储能|新能源车|整车|[东南西北中]部|酱油|蚝油|调味酱|华东|华南|华北|国际|家用|工业|商用')

RATIO_METRICS = {'gross_margin', 'net_margin_parent', 'selling_ratio', 'admin_ratio', 'rd_ratio', 'finance_ratio', 'period_expense_ratio', 'debt_ratio', 'roe'}
MULTIPLE_METRICS = {'pe', 'pb', 'ev_ebitda', 'peg'}
PER_SHARE = {'eps', 'basic_eps'}

UNIT_SCALE = {'亿元': Decimal('1e8'), '亿': Decimal('1e8'), '百万元': Decimal('1e6'), '百万': Decimal('1e6'), '万元': Decimal('1e4'), '万': Decimal('1e4'),
              '千元': Decimal('1e3'), '元': Decimal(1), '十亿元': Decimal('1e9'), '亿美元': Decimal('1e8'), '美元': Decimal(1), '千万元': Decimal('1e7')}

# ---------------------------------------------------------------- 正则
_NUM = r'\d[\d,]*(?:\.\d+)?'
UNIT_RE = r'亿元|亿美元|亿股|亿|百万元|百万|万元|万吨|万辆|万|元/股|元|pct|个百分点|bp|BP|%|％|倍|x|X'
TOKEN = re.compile(r'(?<![0-9.])(?P<sign>[+＋\-−–])?(?P<num>' + _NUM + r')\s*(?P<unit>' + UNIT_RE + r')?')
LIST_TOKEN = re.compile(r'(?<![0-9.])(?P<first>[+＋\-−–]?' + _NUM + r'\s*[%％]?)(?P<rest>(?:\s*[/、]\s*[+＋\-−–]?' + _NUM + r'\s*[%％]?){1,5})\s*(?P<unit>' + UNIT_RE + r')?')
GROWTH_WORD = re.compile(r'同比|环比|YoY|yoy|QoQ|增长|增加|增速|提升|提高|上升|下降|减少|下滑|回落|下行|上行|\+/-')
UP_WORDS = re.compile(r'增长|增加|提升|提高|上升|上行|扩大')
DOWN_WORDS = re.compile(r'下降|减少|下滑|回落|下行|收窄|缩小|降低')
MODIFIERS = [('合并', '合并'), ('母公司', '母公司'), ('单季', '单季'), ('单季度', '单季'), ('累计', '累计'), ('调整后', '调整后'), ('调整前', '调整前'), ('扣非', '扣非'),
             ('归母', '归母'), ('少数股东', '少数股东'), ('LTM', 'LTM'), ('TTM', 'TTM'), ('滚动', 'TTM')]

PERIOD_PATTERNS = [
    (re.compile(r'(?P<y>20\d{2})\s*年?\s*(?:第一季度|一季度|1季度|Q1|1Q|一季报|第一季度报告|一季度报告)'), lambda m: f"{m.group('y')}Q1"),
    (re.compile(r'(?P<y>20\d{2})\s*年?\s*(?:第二季度|二季度|2季度|Q2|2Q)'), lambda m: f"{m.group('y')}Q2"),
    (re.compile(r'(?P<y>20\d{2})\s*年?\s*(?:第三季度|三季度|3季度|Q3|3Q|三季报|第三季度报告|三季度报告)(?!\s*累)'), lambda m: f"{m.group('y')}Q3"),
    (re.compile(r'(?P<y>20\d{2})\s*年?\s*(?:第四季度|四季度|4季度|Q4|4Q)'), lambda m: f"{m.group('y')}Q4"),
    (re.compile(r'(?P<y>20\d{2})\s*年?\s*(?:前三季度|1-3Q|1-3季度|Q1-Q3|9M)'), lambda m: f"{m.group('y')}Q1-Q3"),
    (re.compile(r'(?P<y>20\d{2})\s*年?\s*(?:上半年|半年度|半年|H1|1H|中报|中期|半年报|半年度报告)'), lambda m: f"{m.group('y')}H1"),
    (re.compile(r'(?P<y>20\d{2})\s*年?\s*(?:全年|年度|年报|年度报告|FY)'), lambda m: f"{m.group('y')}FY"),
    (re.compile(r'(?P<y>20\d{2})\s*[AＡ]'), lambda m: f"{m.group('y')}FY"),
    (re.compile(r'(?P<y>20\d{2})\s*[EＥ]'), lambda m: f"{m.group('y')}E"),
    (re.compile(r'(?<!\d)(?P<y>\d{2})\s*Q(?P<q>[1-4])'), lambda m: f"20{m.group('y')}Q{m.group('q')}"),
    (re.compile(r'(?<!\d)(?P<q>[1-4])Q\s*(?P<y>\d{2})(?!\d)'), lambda m: f"20{m.group('y')}Q{m.group('q')}"),
    (re.compile(r'(?<!\d)(?P<y>\d{2})\s*年\s*(?:全年|年报|年度报告)'), lambda m: f"20{m.group('y')}FY"),
    (re.compile(r'(?<!\d)(?P<y>2[0-9])\s*年\s*(?:上半年|半年度|半年|H1|1H|中报|中期|半年报)'), lambda m: f"20{m.group('y')}H1"),
    (re.compile(r'(?<!\d)(?P<y>\d{2})\s*[AＡ](?!\w)'), lambda m: f"20{m.group('y')}FY"),
    (re.compile(r'(?<!\d)(?P<y>\d{2})\s*[EＥ](?!\w)'), lambda m: f"20{m.group('y')}E"),
    (re.compile(r'(?P<y>20\d{2})\s*年(?!\s*\d)(?!.{0,3}(?:月|日))'), lambda m: f"{m.group('y')}Y?"),
    (re.compile(r'(?<!\d)(?P<y>2[0-9])\s*年(?!\s*\d)(?!.{0,3}(?:月|日|度|限|期|龄|初|末|底|来|内|前|后|间|均|化|轻|纪))'), lambda m: f"20{m.group('y')}Y?"),
]
BARE_SUBPERIOD_PATTERNS = [
    (re.compile(r'上半年|半年度'), 'H1'),
    (re.compile(r'全年'), 'FY'),
    (re.compile(r'前三季度'), 'Q1-Q3'),
    (re.compile(r'第[一1]季度|一季度|(?<![0-9A-Za-z])Q1(?![0-9A-Za-z])'), 'Q1'),
    (re.compile(r'第[二2]季度|二季度|(?<![0-9A-Za-z])Q2(?![0-9A-Za-z])'), 'Q2'),
    (re.compile(r'第[三3]季度|三季度|(?<![0-9A-Za-z])Q3(?![0-9A-Za-z])'), 'Q3'),
    (re.compile(r'第[四4]季度|四季度|(?<![0-9A-Za-z])Q4(?![0-9A-Za-z])'), 'Q4'),
]
YEAR_RANGE = re.compile(r'(?P<a>20\d{2})\s*[-~–—至到]\s*(?P<b>20\d{2})\s*年?\s*(?P<e>[EＥ])?')
FORECAST_WORD = re.compile(r'预计|预测|预期|我们预估|盈利预测|有望|将达|目标')


@dataclass
class Claim:
    id: str
    metric: str | None
    label: str
    kind: str                  # amount | growth | ratio | multiple | per_share | pct_point | price | count | other
    value: Decimal
    unit: str
    scale: Decimal             # 乘以 value 得到"元"(金额) 或 小数(比率:0.01)
    decimals: int
    period: str | None
    is_forecast: bool
    growth_basis: str | None
    modifiers: list
    sentence: str
    quote: str
    block_id: int
    page: int | None
    span: tuple
    where: str                 # prose | table
    row_label: str = ''
    col_header: str = ''
    raw: str = ''
    flags: list = field(default_factory=list)   # 例：list_item、inherited_period、segment、unit_inferred
    list_pos: int = -1
    list_len: int = 1
    negative_word: bool = False

    def to_dict(self):
        d = dict(self.__dict__)
        d['value'] = str(self.value)
        d['scale'] = str(self.scale)
        d['span'] = list(self.span)
        return d


def D(s):
    try:
        return Decimal(str(s).replace(',', '').replace('，', ''))
    except InvalidOperation:
        return None


def decs(numstr):
    return len(numstr.split('.')[1]) if '.' in numstr else 0


def norm_period(label, default_year=None):
    return label


# ---------------------------------------------------------------- 期间解析
def find_periods(text, default_year=None):
    """返回 [(start, end, period_id)] 按出现位置排序。"""
    out = []
    taken = []
    for pat, fn in PERIOD_PATTERNS:
        for m in pat.finditer(text):
            if any(m.start() < e and m.end() > s for s, e in taken):
                continue
            p = fn(m)
            out.append((m.start(), m.end(), p))
            taken.append((m.start(), m.end()))
    out.sort()
    if out or default_year:
        base_out = list(out)
        for pat, sub in BARE_SUBPERIOD_PATTERNS:
            for m in pat.finditer(text):
                if any(m.start() < e and m.end() > s for s, e in taken):
                    continue
                prev = [p for s, e, p in base_out if e <= m.start() and re.match(r'^20\d{2}', p)]
                yr = prev[-1][:4] if prev else (str(default_year) if default_year else None)
                if yr:
                    out.append((m.start(), m.end(), f'{yr}{sub}'))
                    taken.append((m.start(), m.end()))
        out.sort()
    return out


def year_range_periods(m):
    a, b = int(m.group('a')), int(m.group('b'))
    return [f'{y}E' for y in range(a, b + 1)]


# ---------------------------------------------------------------- 表格
_HDR_CELL = re.compile(r'^(?P<y>20\d{2})\s*(?P<s>[AaEe]?)$|^(?P<y2>\d{2})\s*(?P<s2>[AaEe])$|^(?P<y3>20\d{2})\s*(?P<q>Q[1-4]|H1|H2)$|^(?P<q4>[1-4])Q(?P<y4>\d{2})$|^(?P<y5>20\d{2})\s*(?P<s5>[AEae])\s*$')


def parse_header_cell(c, report_year=None):
    c = c.strip().replace('Ａ', 'A').replace('Ｅ', 'E')
    m = _HDR_CELL.match(c)
    if not m:
        return None
    if m.group('y'):
        y, s = int(m.group('y')), m.group('s').upper()
        if s == 'E':
            return f'{y}E', True
        if s == 'A':
            return f'{y}FY', False
        # 无后缀：同年或以后视为预测/当期不明确，早于报告年视为实际
        return (f'{y}FY', False) if (report_year and y < report_year) else (f'{y}Y?', report_year is not None and y >= report_year)
    if m.group('y2'):
        y = 2000 + int(m.group('y2'))
        return (f'{y}E', True) if m.group('s2').upper() == 'E' else (f'{y}FY', False)
    if m.group('y3'):
        return f"{m.group('y3')}{m.group('q')}", False
    if m.group('y4'):
        return f"20{m.group('y4')}Q{m.group('q4')}", False
    return None


def parse_cell_number(c):
    t = c.strip().replace('，', ',').replace('−', '-').replace('–', '-')
    if t in ('', '-', '--', '—', 'N/A', 'n/a', 'NA', 'nan'):
        return None
    neg = False
    pct = False
    if re.match(r'^\(.*\)$', t) or re.match(r'^（.*）$', t):
        neg, t = True, t[1:-1]
    if t.endswith(('%', '％')):
        pct, t = True, t[:-1]
    t = t.replace(',', '')
    if t.startswith('-'):
        neg, t = True, t[1:]
    if not re.fullmatch(r'\d+(?:\.\d+)?', t):
        return None
    v = Decimal(t)
    return (-v if neg else v), pct, decs(t)


LABEL_UNIT = re.compile(r'[（(]\s*(?:单位[:：]?)?\s*(百万元|亿元|万元|千元|元/股|元|百万|亿|万|%|％|倍|X|x|亿股|百万股|万股)\s*[）)]')
GROWTH_LABEL = re.compile(r'^\s*(?:[（(]\s*\+/-\s*%\s*[）)]|增速|增长率|同比|同比增长|同比增速|YoY|yoy|\(%\)|（%）)\s*$|(?:增长率|同比|增速|yoy|YoY)\s*(?:[（(]%[）)])?$')


def row_metric(label):
    lab = LABEL_UNIT.sub('', label).strip()
    lab = re.sub(r'[（(][^）)]*[）)]$', '', lab).strip() if not re.search(r'EV/EBITDA|P/E|P/B', lab) else lab
    m = _MW_RE.search(lab)
    if m and (m.start() <= 4):
        return _MW_MAP[m.group(0)]
    return None


# ---------------------------------------------------------------- 主入口
def extract_claims(doc_or_text, llm=None, report_year=None):
    doc = doc_or_text if isinstance(doc_or_text, Document) else parse_report(doc_or_text)
    if report_year is None:
        report_year = detect_report_year(doc)
    claims: list[Claim] = []
    ctx = {'header': None, 'unit': None, 'last_metric': None, 'last_unit': None, 'last_values': None}
    default_period = detect_default_period(doc)
    carry_period = None
    for b in doc.blocks:
        if b.kind == 'row':
            carry_period = None
            claims += _table_claims(b, ctx, report_year, len(claims))
        else:
            ctx['header'] = ctx['header'] if b.kind == 'caption' else ctx['header']
            p_in = carry_period or default_period
            sc, p_out = _prose_claims(b, p_in, report_year, len(claims))
            claims += sc
            t = (b.text or '').strip()
            if b.kind == 'para' and t and t[-1] not in '。！？!?':
                carry_period = p_out
            else:
                carry_period = None
    for i, c in enumerate(claims):
        c.id = f'C{i + 1:04d}'
    return claims


def detect_report_year(doc):
    for b in doc.blocks[:40]:
        m = re.search(r'(20\d{2})\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日', b.text)
        if m:
            return int(m.group(1))
    for b in doc.blocks[:40]:
        m = re.search(r'(20\d{2})[-/.]\d{1,2}[-/.]\d{1,2}', b.text)
        if m:
            return int(m.group(1))
    return None


def detect_default_period(doc):
    """全文最常出现的"报告期"提法（取前几段）。"""
    from collections import Counter
    cnt = Counter()
    for b in doc.blocks[:60]:
        if b.kind == 'row':
            continue
        for s, e, p in find_periods(b.text):
            if p[-1] not in ('E', '?') and 'Y?' not in p:
                cnt[p] += 1
    return cnt.most_common(1)[0][0] if cnt else None


def _modifiers(text):
    return [v for k, v in MODIFIERS if k in text]


# ---------------------------------------------------------------- 表格声明
def _table_claims(b, ctx, report_year, n0):
    cells = b.cells
    if not cells:
        return []
    out = []
    hdr_cells = [parse_header_cell(c, report_year) for c in cells[1:]]
    if sum(1 for h in hdr_cells if h) >= 3 and sum(1 for h in hdr_cells if h) >= len(cells[1:]) * 0.6:
        ctx['header'] = [None] + hdr_cells
        ctx['hdr_raw'] = cells
        m = LABEL_UNIT.search(cells[0])
        ctx['unit'] = m.group(1) if m else ctx.get('unit') if 'EBIT' in cells[0] else (m.group(1) if m else None)
        ctx['last_metric'] = None
        return out
    header = ctx.get('header')
    label = cells[0]
    if len(cells) < 2:
        return out
    um = LABEL_UNIT.search(label)
    unit = um.group(1) if um else None
    metric = row_metric(label)
    is_growth_row = bool(GROWTH_LABEL.search(label.strip()))
    if not header or len(header) != len(cells):
        # 表头与数据列数不一致：从右对齐
        if header and len(cells) < len(header):
            header = [None] * (len(header) - len(cells)) + header[len(header) - len(cells):]
        elif not header:
            header = None
    if is_growth_row:
        gm = row_metric(label) or ctx.get('last_metric')
        metric = gm
    vals = []
    for i, c in enumerate(cells[1:], start=1):
        pn = parse_cell_number(c)
        if pn is None:
            continue
        v, pct, d = pn
        hp = header[i] if header and i < len(header) else None
        period, fc = (hp if hp else (None, False))
        vals.append((i, v, pct, d, period, fc, c))
    if not vals:
        return out
    # 行标签里没写指标词、也不是增速行：不抽取（如股东表、数量表），仅保留已有指标词的行
    if not metric:
        return out
    base_metric = metric
    kind_default = 'amount'
    if is_growth_row:
        kind_default = 'growth'
    elif metric in RATIO_METRICS:
        kind_default = 'ratio'
    elif metric in MULTIPLE_METRICS:
        kind_default = 'multiple'
    elif metric in PER_SHARE:
        kind_default = 'per_share'
    tunit = unit or ctx.get('unit')
    flags = []
    if unit is None and kind_default == 'amount':
        flags.append('unit_from_table_header' if tunit else 'unit_unknown')
    if not is_growth_row and kind_default == 'amount':
        ctx['last_metric'] = metric
        ctx['last_unit'] = tunit
    for (i, v, pct, d, period, fc, raw) in vals:
        kind = kind_default
        u = ''
        scale = Decimal(1)
        if kind == 'growth' or pct:
            u = '%'
            scale = Decimal('0.01')
            if kind != 'growth':
                kind = 'ratio' if kind in ('ratio', 'amount') else kind
        elif kind == 'amount':
            u = tunit or ''
            scale = UNIT_SCALE.get(tunit, Decimal(0)) if tunit else Decimal(0)
        elif kind == 'ratio':
            u = '%'
            scale = Decimal('0.01')
        elif kind in ('multiple',):
            u = '倍'
        elif kind == 'per_share':
            u = '元'
        c = Claim('', base_metric, label.strip(), kind, v, u, scale, d, period, bool(fc), 'yoy' if kind == 'growth' else None, _modifiers(label), b.text, b.text,
                  b.id, b.page, (0, len(b.text)), 'table', row_label=label.strip(), col_header=(ctx.get('hdr_raw') or [''] * (i + 1))[i] if ctx.get('hdr_raw') and i < len(ctx['hdr_raw']) else '',
                  raw=raw, flags=list(flags))
        c.col_index = i
        out.append(c)
    return out


# ---------------------------------------------------------------- 正文
def _prose_claims(b, default_period, report_year, n0):
    text = b.text
    out = []
    para_period = default_period
    for s, e in split_sentences(text):
        sent = text[s:e]
        if len(sent) < 6:
            continue
        sc = _sentence_claims(b, sent, s, para_period, report_year)
        for c in sc:
            if c.period and not c.is_forecast and 'inherited_period' not in c.flags and not c.period.endswith('Y?'):
                para_period = c.period
        if not sc:
            ps = find_periods(sent, report_year)
            for _, _, p in ps:
                if p and not p.endswith('E') and not p.endswith('Y?'):
                    para_period = p
        out += sc
    return out, para_period


def _clauses(sent):
    """按，；: 切分子句，返回 [(start,end)]（相对 sent）。列表里的顿号/斜杠不切。"""
    spans, st = [], 0
    for m in re.finditer(r'[，,；;：:]\s*', sent):
        # 数字内部逗号（1,234）不切
        i = m.start()
        if sent[i] == ',' and i > 0 and sent[i - 1].isdigit() and i + 1 < len(sent) and sent[i + 1].isdigit():
            continue
        spans.append((st, m.end()))
        st = m.end()
    spans.append((st, len(sent)))
    return spans


def _sentence_claims(b, sent, offset, default_period, report_year):
    out = []
    used = []
    items = []   # (start, end, raw, unit, list_pos, list_len, list_id)
    for li, m in enumerate(LIST_TOKEN.finditer(sent)):
        unit = (m.group('unit') or '').strip()
        raw_all = m.group(0)
        nms = list(re.finditer(r'[+＋\-−–]?\d[\d,]*(?:\.\d+)?', raw_all))
        for k, nm in enumerate(nms):
            after = raw_all[nm.end():nm.end() + 2].lstrip()
            iu = '%' if after[:1] in ('%', '％') else unit
            items.append((m.start() + nm.start(), m.start() + nm.end(), nm.group(0), iu, k, len(nms), li))
        used.append((m.start(), m.end()))
    for m in TOKEN.finditer(sent):
        if any(m.start() >= s0 and m.end() <= e0 for s0, e0 in used):
            continue
        unit = (m.group('unit') or '').strip()
        items.append((m.start(), m.end(), (m.group('sign') or '') + m.group('num'), unit, -1, 1, -1))
    items.sort()
    pers = find_periods(sent)
    yr_ranges = list(YEAR_RANGE.finditer(sent))
    forecast_sentence = bool(FORECAST_WORD.search(sent))
    last_amount_metric = None
    last_list = None            # {'metric','periods','len','fc'}
    list_ctx = {}
    last_pp_metric = None
    for (st, en, raw, unit, lpos, llen, lid) in items:
        sign = -1 if raw[:1] in '-−–' else 1
        numtxt = raw.lstrip('+＋-−–')
        val = D(numtxt)
        if val is None:
            continue
        if not unit:
            if re.fullmatch(r'20\d{2}', numtxt) and re.match(r'\s*[年/-]', sent[en:en + 2]):
                continue
            if re.match(r'\s*(?:年|月|日|号|个|家|款|项|条|名|位|只|座|期|次|轮|代|页|周|天|季|[A-Za-z]{1,3}\b)', sent[en:en + 3]):
                continue
        before = sent[max(0, st - 1):st]
        if before in ('-', '/', '.') and re.match(r'20\d{2}', sent[max(0, st - 5):st]):
            continue
        left_lo = max(0, st - 60)
        left = sent[left_lo:st]
        if lpos > 0 and lid in list_ctx:
            base = list_ctx[lid]
            c = _clone_list_item(base, b, sent, offset, st, en, raw, numtxt, sign, val, lpos, llen, unit, yr_ranges, last_list)
            if c:
                out.append(c)
                if c.kind == 'amount' and c.metric:
                    last_amount_metric = c.metric
                    last_list = last_list or {}
                    last_list.setdefault('periods', []).append(c.period)
            continue
        cut = 0
        for mm in re.finditer(r'[，,；;：:]|\)[，,]', left):
            cut = mm.end()
        clause_left = left[cut:]
        mw = None
        for m2 in _MW_RE.finditer(clause_left):
            mw = m2
        label = mw.group(0) if mw else ''
        metric = _MW_MAP.get(label) if label else None
        gap = len(clause_left) - (mw.end() if mw else 0)
        if mw and re.search(r'\d', clause_left[mw.end():]):
            metric, label = None, ''
        if mw and gap > 14 + (12 if GROWTH_WORD.search(clause_left[mw.end():]) else 0):
            metric, label = None, ''
        seg = bool(SEGMENT_HINT.search(clause_left[:mw.start()] if mw else clause_left)) if mw else False
        prev_fc_list = bool(re.search(r'前预测值|原预测|此前预测|调整前|前值|前次预测|原预计', clause_left))
        pctlike = unit in ('%', '％')
        gm = GROWTH_WORD.search(clause_left)
        is_growth = False
        growth_basis = None
        if pctlike and gm:
            is_growth = True
            growth_basis = 'qoq' if '环比' in clause_left else 'yoy'
        if pctlike and re.search(r'同比|环比', clause_left):
            is_growth = True
            growth_basis = 'qoq' if '环比' in clause_left else 'yoy'
        # "同比+2.0pct 至42.0%"
        to_ratio = bool(pctlike and re.search(r'(?:至|达到?|为)\s*$', clause_left) and last_pp_metric)
        if to_ratio:
            is_growth = False
        if unit in ('pct', '个百分点', 'bp', 'BP'):
            kind = 'pct_point'
        elif is_growth:
            kind = 'growth'
        elif pctlike:
            kind = 'ratio'
        elif unit == '倍' or unit in ('x', 'X') or (metric in MULTIPLE_METRICS):
            kind = 'multiple'
        elif metric in PER_SHARE and unit in ('元', '元/股', ''):
            kind = 'per_share'
        elif unit in UNIT_SCALE:
            kind = 'amount'
        else:
            kind = 'other'
        if not unit and not (metric in MULTIPLE_METRICS or metric in PER_SHARE or metric in RATIO_METRICS):
            continue
        if kind == 'other' and not metric:
            continue
        if to_ratio:
            metric = metric or last_pp_metric
        if kind == 'growth' and not metric:
            metric = last_amount_metric
            if metric:
                label = ''
        if kind == 'ratio' and not metric:
            continue
        if kind == 'amount' and not metric:
            continue
        # 期间
        period = None
        inherited = False
        left_pers = [p for p in pers if p[1] <= st]
        if left_pers:
            period = left_pers[-1][2]
        rng_used = None
        for yr in yr_ranges:
            if yr.end() <= st and (not left_pers or yr.end() >= left_pers[-1][1]):
                rng_used = yr
        if rng_used is not None:
            ys = year_range_periods(rng_used)
            if lpos >= 0:
                period = ys[lpos] if (lpos < len(ys) and llen == len(ys)) else f'?COUNT{len(ys)}vs{llen}'
            elif not left_pers or rng_used.end() >= left_pers[-1][1]:
                period = ys[0] if len(ys) == 1 else period
        elif lpos >= 0 and last_list and last_list.get('len') == llen and last_list.get('periods') and kind in ('growth', 'multiple', 'per_share', 'ratio', 'amount'):
            if not (left_pers and left_pers[-1][0] >= left_lo):
                period = last_list['periods'][lpos]
        if period is None:
            period = default_period
            inherited = True
        elif period.endswith('Y?'):
            y = int(period[:4])
            period = (f'{y}E' if forecast_sentence else period) if (report_year and y >= report_year) else (f'{y}FY' if report_year else period)
        is_fc = bool(period and period.endswith('E'))
        if prev_fc_list:
            is_fc = True
        flags = []
        if inherited:
            flags.append('inherited_period')
        if seg or metric == 'revenue_generic':
            flags.append('segment')
        if lpos >= 0:
            flags.append('list_item')
        if prev_fc_list:
            flags.append('prev_forecast')
        sc = Decimal(1)
        u = unit
        if kind in ('growth', 'ratio'):
            sc, u = Decimal('0.01'), '%'
        elif kind == 'pct_point':
            sc = Decimal('0.01')
        elif kind == 'amount':
            sc = UNIT_SCALE.get(unit, Decimal(0))
        value = val * sign
        up_word = bool(UP_WORDS.search(clause_left[-14:])) if kind == 'growth' else False
        down_word = bool(DOWN_WORDS.search(clause_left[-14:])) if kind == 'growth' else False
        c = Claim('', metric, label, kind, value, u, sc, decs(numtxt), period, is_fc, growth_basis if kind == 'growth' else None,
                  _modifiers(clause_left[max(0, (mw.start() if mw else len(clause_left)) - 8):(mw.end() if mw else len(clause_left))]), sent, '', b.id, b.page,
                  (offset + st, offset + en), 'prose', raw=(raw + (unit or '')), flags=flags, list_pos=lpos, list_len=llen, negative_word=down_word)
        c.quote = sent[max(0, st - 36):min(len(sent), en + 12)]
        c.up_word = up_word
        c.period_in_clause = bool(left_pers and left_pers[-1][0] >= left_lo) or rng_used is not None
        if kind == 'pct_point':
            last_pp_metric = metric
        if kind == 'amount':
            last_amount_metric = metric
        if lpos == 0:
            list_ctx[lid] = c
            last_list = {'metric': metric, 'len': llen, 'periods': [period], 'kind': kind}
        out.append(c)
    return out


def _clone_list_item(base, b, sent, offset, st, en, raw, numtxt, sign, val, lpos, llen, unit, yr_ranges, last_list):
    """列表中第 2 项起：沿用首项的指标/单位/口径，期间按位置顺延。"""
    period = base.period
    m = re.match(r'(\d{4})(E|FY|Q\d|H1)$', base.period or '')
    ys = None
    for yr in yr_ranges:
        if yr.end() <= base.span[0] - 0 + 1000:
            ys = year_range_periods(yr) if (yr.end() <= (base.span[0] - offset if False else 10 ** 9)) else ys
    if base.period and base.period.endswith('E') and m:
        period = f'{int(m.group(1)) + lpos}E'
    elif base.period and base.period.endswith('FY') and m and base.is_forecast:
        period = f'{int(m.group(1)) + lpos}E'
    sc = base.scale
    c = Claim('', base.metric, base.label, base.kind, val * sign, base.unit, sc, decs(numtxt), period, base.is_forecast, base.growth_basis, list(base.modifiers),
              sent, '', b.id, b.page, (offset + st, offset + en), 'prose', raw=(raw + (unit or base.unit or '')), flags=list(base.flags), list_pos=lpos, list_len=llen,
              negative_word=base.negative_word)
    c.quote = sent[max(0, st - 36):min(len(sent), en + 12)]
    c.up_word = getattr(base, 'up_word', False)
    c.period_in_clause = getattr(base, 'period_in_clause', False)
    return c
