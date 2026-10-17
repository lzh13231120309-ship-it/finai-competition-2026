"""公告字段的确定性预解析。

为什么需要这一层：公告抽取里有一半以上的失分点**不是理解问题，是机械问题** ——
中文大写金额换算、万元/亿元/万股的量级、日期格式归一化、表格里 0.05 与 5.00%
的互换、把"以司法机关文书为准"当成已知事实编出法院名称、
把表格里的"—"当成 0、把"合计"行当成一个股东。

这些都可以用确定性代码做掉，而且**必须**用代码做：
模型每次凭手感判断，"结果稳定性"就无从谈起。本模块只做机械变换，
不承担语义判断；它输出的每个候选都带原文 span，供上层引用与复核。

设计原则：
- 只归一化，不臆测。解析不出来就返回 None 并记 note，不猜。
- 每个结果带 raw（原文）与 span（在文本中的位置），保证可追溯。
- 归一化值与原值同时返回，不做破坏性替换。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# ----------------------------------------------------------------- 中文数字

_CN_DIGITS = {
    '零': 0, '〇': 0, '洞': 0,
    '一': 1, '壹': 1, '幺': 1,
    '二': 2, '贰': 2, '两': 2,
    '三': 3, '叁': 3,
    '四': 4, '肆': 4,
    '五': 5, '伍': 5,
    '六': 6, '陆': 6,
    '七': 7, '柒': 7,
    '八': 8, '捌': 8,
    '九': 9, '玖': 9,
}
_CN_SMALL_UNIT = {'十': 10, '拾': 10, '百': 100, '佰': 100, '千': 1000, '仟': 1000}
_CN_BIG_UNIT = {'万': 10 ** 4, '萬': 10 ** 4, '亿': 10 ** 8, '億': 10 ** 8}
_CN_NUM_CHARS = set(_CN_DIGITS) | set(_CN_SMALL_UNIT) | set(_CN_BIG_UNIT)


def cn_number(text: str) -> int | None:
    """把中文数字串转成整数，如 '壹亿贰仟伍佰万' → 125000000。

    采用「万 / 亿 分级累加」的经典算法，可处理 '一亿零五万'、'十二万三千' 这类写法。
    无法解析时返回 None。
    """
    if not text:
        return None
    s = ''.join(ch for ch in str(text) if ch in _CN_NUM_CHARS)
    if not s:
        return None
    total = 0        # 已结算的「亿」级
    section = 0      # 当前「万」级累加器
    number = 0
    for ch in s:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_SMALL_UNIT:
            section += (number or 1) * _CN_SMALL_UNIT[ch]
            number = 0
        elif ch in _CN_BIG_UNIT:
            unit = _CN_BIG_UNIT[ch]
            if unit == 10 ** 8:
                total = (total + section + number) * unit
                section = 0
            else:
                section = (section + number) * unit
            number = 0
    return total + section + number


# ----------------------------------------------------------------- 数值与单位

_MONEY_UNIT = {'元': 1, '千元': 1000, '万元': 10000, '萬': 10000, '百万元': 10 ** 6,
               '亿元': 10 ** 8, '億': 10 ** 8, '十亿元': 10 ** 9}
_SHARE_UNIT = {'股': 1, '千股': 1000, '万股': 10000, '百股': 100, '亿股': 10 ** 8}

# 允许前导负号（减持数量、负的金额调整），但不吞掉范围写法里的连字符：
# "3,000-5,000 万元" 中 "-" 前是数字，不视为负号。
_NUM = r'(?:(?<![\d,.])[-−－])?\d[\d,]*(?:\.\d+)?'
# 「5,000 万元」「88,567,500.00 元」「1,250.36万元」
_RE_NUM_UNIT = re.compile(rf'({_NUM})\s*(亿元|億|百万元|十亿元|万元|萬|千元|元|亿股|億|万股|千股|百股|股)')
# 中文大写金额：壹亿贰仟伍佰万元整 / 人民币叁仟万元
_RE_CN_MONEY = re.compile(
    r'(?:人民币|人民幣|RMB)?\s*([零〇一壹二贰两兩三叁四肆五伍六陆陸七柒八捌九玖十拾百佰千仟万萬亿億]+)'
    r'\s*(亿元|億|万元|萬|千元|元)?\s*整?')
_RE_CURRENCY_SYMBOL = re.compile(r'[¥￥]\s*(' + _NUM + r')')


def _cn_money_value(text: str) -> Decimal | None:
    """匹配中文大写金额。**必须带单位或含万/亿量级**才认。

    否则"一、"、"二、"这类章节编号会被当成 1 元、2 元 —— 实测踩过：
    司法冻结公告的"一、本次司法冻结基本情况"被解析成一笔 1 元金额。
    """
    for m in _RE_CN_MONEY.finditer(text):
        numeral, unit = m.group(1), m.group(2)
        if not any(ch in _CN_DIGITS for ch in numeral):
            continue
        if not unit and not any(ch in _CN_BIG_UNIT for ch in numeral):
            continue
        value = cn_number(numeral)
        if value is None:
            continue
        factor = _MONEY_UNIT.get(unit or '元')
        if factor:
            return Decimal(value) * factor
    return None

UNDISCLOSED_PATTERNS = [
    (r'以[^，。；]{0,20}(?:出具的)?文书为准', '原文声明以外部文书为准，字段应填 null'),
    (r'以[^，。；]{0,20}为准', '原文声明以外部/后续文件为准，字段应填 null'),
    (r'(?:尚待|待)[^，。；]{0,12}(?:确认|披露|公告)', '原文声明尚未确认，字段应填 null'),
    (r'另行(?:公告|披露|通知)', '原文声明将另行披露，当前应填 null'),
    (r'未(?:予)?披露', '原文明确未披露'),
    (r'以(?:相关|有权)(?:部门|机关|机构)[^，。；]{0,12}为准', '以主管机关为准，字段应填 null'),
]
NULL_SYMBOLS = {'—', '–', '-', '－', '―', '/', '不适用', 'N/A', 'n/a', 'NA', '—　'}
_TOTAL_ROW_RE = re.compile(r'^\s*(?:合\s*计|总\s*计|小\s*计|合\s*總|Total|Sub-?total)\b', re.I)

# 常见 OCR / 输入笔误 → 标准字段名
FIELD_ALIASES = {
    '质押权人': '质权人', '质押权人名称': '质权人', '质权银行': '质权人',
    '出质方': '出质人', '出质股东': '出质人', '质押人': '出质人',
    '解除质押日期': '解押日期', '解押日': '解押日期',
    '被担保方': '被担保人', '中标方': '中标人',
}

_DATE_RE = re.compile(
    r'((?:19|20)\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?')
_PERCENT_RE = re.compile(rf'({_NUM})\s*(%|％|个百分点)')


def _dec(raw: str) -> Decimal | None:
    try:
        s = str(raw).replace(',', '').replace('，', '').replace('−', '-').replace('－', '-')
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def fmt_number(value) -> str:
    """规范化输出：整数不带小数尾巴（'150000000' 而非 '150000000.0'），
    小数去掉多余的尾随零。仅用于展示与 JSON，不参与计算。"""
    if value is None:
        return ''
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    if d == d.to_integral_value():
        return str(int(d))
    return format(d.normalize(), 'f')


def money_yuan(text: str) -> Decimal | None:
    """从含单位的金额文本解析出「元」。支持 '5,000 万元'、'88,567,500.00 元'、
    '壹亿贰仟伍佰万元整'、'¥125,000,000.00'。

    解析不出返回 None —— 不猜。
    """
    if not text:
        return None
    s = str(text)
    m = _RE_CURRENCY_SYMBOL.search(s)
    if m:
        return _dec(m.group(1))
    # 中文大写优先（同一句里可能既有大写又有阿拉伯数字）
    cn_value = _cn_money_value(s)
    if cn_value is not None:
        return cn_value
    m = _RE_NUM_UNIT.search(s)
    if m:
        value = _dec(m.group(1))
        unit = m.group(2)
        factor = _MONEY_UNIT.get(unit)
        if factor is None:      # 股数单位，不是金额
            return None
        return value * factor if value is not None else None
    return None


def shares(text: str) -> Decimal | None:
    """从含单位的股数文本解析出「股」。支持 '18,750,000 股'、'1,500 万股'。"""
    if not text:
        return None
    s = str(text)
    for m in _RE_NUM_UNIT.finditer(s):
        unit = m.group(2)
        factor = _SHARE_UNIT.get(unit)
        if factor is None:
            continue
        value = _dec(m.group(1))
        if value is not None:
            return value * factor
    return None


# 中文大写日期：二〇二六年十月九日 / 二零二六年十二月三十一日
_CN_DATE_RE = re.compile(
    r'([〇零一二三四五六七八九]{4})\s*年\s*([十一二三四五六七八九〇零]{1,3})\s*月\s*'
    r'([十一二三四五六七八九〇零]{1,3})\s*日')
_CN_YEAR_DIGITS = {c: str(_CN_DIGITS[c]) for c in _CN_DIGITS if c in '〇零一二三四五六七八九'}


def _iter_dates(text: str) -> list[tuple[int, str, str | None]]:
    """返回 [(起始位置, 原文, ISO日期或None)]，阿拉伯与中文大写两种写法都识别，按位置排序。"""
    out = []
    for m in _DATE_RE.finditer(text):
        out.append((m.start(), m.group(0), _make_iso(m.group(1), m.group(2), m.group(3))))
    for m in _CN_DATE_RE.finditer(text):
        year = ''.join(_CN_YEAR_DIGITS[c] for c in m.group(1))
        mo = cn_number(m.group(2))
        day = cn_number(m.group(3))
        out.append((m.start(), m.group(0), _make_iso(year, mo, day)))
    return sorted(out, key=lambda t: t[0])


def _make_iso(y, mo, d) -> str | None:
    try:
        y, mo, d = int(y), int(mo), int(d)
    except (TypeError, ValueError):
        return None
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f'{y:04d}-{mo:02d}-{d:02d}'


def iso_date(text: str) -> str | None:
    """把 2026年9月18日 / 2026-9-18 / 2026.09.18 / 二〇二六年九月十八日 归一化为 YYYY-MM-DD。"""
    if not text:
        return None
    hits = _iter_dates(str(text))
    return hits[0][2] if hits else None


def percent(text: str, *, decimal_form: bool | None = None) -> dict | None:
    """解析百分数。

    返回 {'value': 百分数值, 'form': 'percent_sign'|'decimal', 'raw': 原文}。

    两种写法都要处理，因为它们在公告与表格里混用：
    - 带 % 号：'6.25%' → 6.25
    - 小数形式：表格里写 '0.05' 表示 5%，必须 ×100

    `decimal_form` 显式指定时以其为准；未指定时，若原文没有 % 号且
    绝对值 ≤ 1，判为小数形式并在 form 里标明（调用方仍可复核）。
    """
    if not text:
        return None
    s = str(text)
    m = _PERCENT_RE.search(s)
    if m:
        value = _dec(m.group(1))
        if value is None:
            return None
        return {'value': value, 'form': 'percent_sign', 'raw': m.group(0)}
    value = _dec(s.strip())
    if value is None:
        return None
    is_decimal = decimal_form if decimal_form is not None else abs(value) <= 1
    if is_decimal:
        return {'value': value * 100, 'form': 'decimal', 'raw': s.strip()}
    return {'value': value, 'form': 'percent_sign', 'raw': s.strip()}


def is_null_symbol(text: str) -> bool:
    """'—' / '-' / '不适用' 等表示"无数值"，**不是 0**。"""
    return str(text).strip() in NULL_SYMBOLS


def is_total_row(text: str) -> bool:
    """'合计 / 总计 / 小计 / Total' 行是汇总行，不是数据条目（如"不是股东"）。

    同时兼容 markdown 表格行（`| 合计 | 53,000,000 | ...`）与
    全角/半角竖线、多余空格。
    """
    s = str(text).strip()
    s = s.strip('|').strip()          # markdown 表格的边界竖线
    s = re.split(r'[|｜]', s)[0].strip() if re.split(r'[|｜]', s) else s
    s = s.strip('*` 　')
    return bool(_TOTAL_ROW_RE.match(s))


def normalize_field_name(name: str) -> str:
    """把常见笔误/别名归一到标准字段名（如 '质押权人' → '质权人'）。"""
    key = str(name).strip()
    return FIELD_ALIASES.get(key, key)


def undisclosed_signals(text: str) -> list[dict]:
    """找出"该字段原文未给出、必须填 null"的短语。

    为什么单列：这是幻觉的高发区。公告写"以司法机关出具的文书为准"时，
    模型极易顺手编出一个法院名称 —— 而金标准要求 null，编造触发零容忍红旗。
    """
    out, seen = [], set()
    for pattern, reason in UNDISCLOSED_PATTERNS:
        for m in re.finditer(pattern, str(text)):
            span = m.group(0)
            if span in seen:
                continue
            seen.add(span)
            out.append({'phrase': span, 'start': m.start(), 'reason': reason})
    return out


def negation_signals(text: str, keyword: str) -> list[dict]:
    """检测"不存在 … {keyword} …"式的否定表述。

    典型用途：公告写"本次冻结不存在被冻结股份数量超过其所持公司股份数量 50% 的情形"，
    对应的判断题答案是"否"。这里只给出信号与原文，不下结论。
    """
    out = []
    for m in re.finditer(r'[^，。；]{0,40}(?:不存在|未|没有|无)[^，。；]{0,60}', str(text)):
        span = m.group(0)
        if keyword and keyword not in span:
            continue
        out.append({'span': span, 'negated': True})
    return out


def parse_table_rows(text: str) -> list[dict]:
    """解析 markdown 表格行（含 markdown 与纯竖线两种写法）。

    每行返回 {'cells': [...], 'is_total': bool, 'null_cells': [列号...]}。
    为什么需要：多级表头表格里，"合计"行不是数据条目、"—"不是 0，
    这两点都靠这一层显式标出来，避免模型按行数硬套。
    """
    rows = []
    for line in str(text or '').splitlines():
        s = line.strip()
        if not s.startswith('|'):
            continue
        if re.fullmatch(r'\|[\s:\-|｜]+', s.replace('｜', '|')):
            continue                      # 表头分隔行 |---|---|
        cells = [c.strip() for c in s.strip('|').split('|')]
        if not any(cells):
            continue
        null_cells = [i for i, c in enumerate(cells) if is_null_symbol(c)]
        rows.append({'cells': cells, 'is_total': is_total_row(s),
                     'null_cells': null_cells})
    return rows


_PCT_HEADER_RE = re.compile(r'比例|占比|percent|百分', re.I)


def table_percents(rows: list[dict]) -> list[dict]:
    """按**表头**识别比例列，并把该列的小数写法归一成百分数。

    这是 T8 那类陷阱的正解：正文写 `5%`，同一张表里写 `0.05`，
    两者是同一事实。只按 `%` 号扫描会漏掉表格里的 `0.05`，
    直接输出 `0.05` 又会被判错（要求 5.00）。
    """
    out = []
    if not rows:
        return out
    header = rows[0]['cells']
    pct_cols = [i for i, h in enumerate(header) if _PCT_HEADER_RE.search(h)]
    for row in rows[1:]:
        if row.get('is_total'):
            continue
        cells = row['cells']
        for i in pct_cols:
            if i >= len(cells):
                continue
            cell = cells[i]
            if is_null_symbol(cell) or not cell:
                continue
            parsed = percent(cell, decimal_form=('%' not in cell and '％' not in cell))
            if parsed:
                out.append({'column': header[i] if i < len(header) else '',
                            'row_label': cells[0] if cells else '',
                            **parsed})
    return out


def scan(text: str) -> dict:
    """对一段公告原文做整体机械扫描，返回各类候选与提示。

    输出的是**候选**而非结论：金额/股数/比例可能有多处出现（本次 vs 累计），
    由上层结合语境选择，本函数只保证"原文里到底有哪些写法、各自等于多少"。
    """
    text = str(text or '')
    amounts, shares_list, dates, percents = [], [], [], []
    seen_spans: set[int] = set()
    for m in _RE_CURRENCY_SYMBOL.finditer(text):
        seen_spans.add(m.start())
        amounts.append({'raw': m.group(0).strip(),
                        'value_yuan': fmt_number(_dec(m.group(1))), 'start': m.start()})
    for m in _RE_NUM_UNIT.finditer(text):
        unit = m.group(2)
        if unit in _MONEY_UNIT:
            amounts.append({'raw': m.group(0).strip(),
                            'value_yuan': fmt_number(money_yuan(m.group(0))),
                            'start': m.start()})
        elif unit in _SHARE_UNIT:
            shares_list.append({'raw': m.group(0).strip(),
                                'value_shares': fmt_number(shares(m.group(0))),
                                'start': m.start()})
    for m in _RE_CN_MONEY.finditer(text):
        value = _cn_money_value(m.group(0))
        if value is not None:
            amounts.append({'raw': m.group(0).strip(),
                            'value_yuan': fmt_number(value), 'start': m.start()})
    for start, raw, iso in _iter_dates(text):
        dates.append({'raw': raw.strip(), 'iso': iso, 'start': start})
    for m in _PERCENT_RE.finditer(text):
        parsed = percent(m.group(0))
        if parsed:
            percents.append({**parsed, 'start': m.start()})
    return {
        'amounts': amounts,
        'shares': shares_list,
        'dates': dates,
        'percents': percents,
        'table_rows': parse_table_rows(text),
        'table_percents': table_percents(parse_table_rows(text)),
        'undisclosed': undisclosed_signals(text),
        'total_rows': [ln.strip() for ln in text.splitlines() if is_total_row(ln)],
        'null_symbols': sorted({t for t in re.findall(r'[—–\-－―]', text)}),
        'note': ('以上为确定性机械解析结果，供核对与引用；'
                 '出现多处金额/股数时（如"本次"与"累计"）需按语境选择，'
                 '本结果不代替语义判断。null_symbols 表示原文用该符号表示无数值，'
                 '应输出 null 而不是 0。table_rows 里 is_total=true 的行是汇总行，'
                 '不得当作数据条目；null_cells 标出该行哪些列是"—"。'),
    }
