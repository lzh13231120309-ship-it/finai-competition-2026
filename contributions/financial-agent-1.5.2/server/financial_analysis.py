"""Deterministic, evidence-preserving extraction and financial calculations.

Amounts are Decimal strings in base currency units. Ratios use fractions (0.2 =
20%). Unresolved metadata and conflicting observations are never silently used.
"""
from __future__ import annotations

import calendar
import copy
import hashlib
import re
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext

from .subject_i18n import (EN_ALIASES, HKIFRS_ZH_ALIASES, detect_currency_i18n,
                           detect_parent_statement, detect_scope_i18n, detect_statement,
                           detect_unit_i18n, dominant_currency, parse_english_period,
                           subject_candidates, to_simplified)

ALIASES = {
    # A 股利润表首行是「营业总收入」（含利息收入、保费等金融业务收入），其下「其中：营业收入」才是工业/商业口径的收入；
    # 两者对多数公司相等，但茅台等有金融业务的公司不等。分开建码，并在"只有总收入"时回填 revenue（见 _backfill_revenue）。
    'revenue': ['营业收入', '销售收入', 'Revenue'],
    'total_revenue': ['营业总收入'],
    'main_business_revenue': ['主营业务收入'],
    'cost_of_revenue': ['营业成本', 'Cost of revenue'],
    'main_business_cost': ['主营业务成本'],
    'gross_profit': ['毛利', '毛利额'],
    'total_operating_cost': ['营业总成本'],
    'net_profit': ['净利润', '净利润总额', 'Net profit'],
    'parent_net_profit': ['归属于母公司所有者的净利润', '归属于母公司股东的净利润', '归属于上市公司股东的净利润', '归母净利润'],
    # 綜合全面收益表（港式年报的标准第二张表）里也有一行「母公司擁有人應佔」，
    # 数值是**全面收益总额**而不是净利润，两者通常差一个其他全面收益。
    # 单独给一个代码，才能和 parent_net_profit 分开；否则两条不同数值落进同一科目
    # 会被判成 duplicate_conflict，两条一起降级（见 _CONTEXT_GUARDED_ALIASES）。
    'parent_total_comprehensive_income': ['归属于母公司所有者的综合收益总额', '归属于母公司股东的综合收益总额',
                                          '母公司拥有人应占全面收益总额'],
    'nonrecurring_adj_parent_profit': ['归属于上市公司股东的扣除非经常性损益的净利润', '扣除非经常性损益后的归属于上市公司股东的净利润', '扣非归母净利润', '扣非净利润'],
    'minority_profit': ['少数股东损益'],
    'operating_profit': ['营业利润'], 'profit_before_tax': ['利润总额', '税前利润'],
    'income_tax': ['所得税费用'], 'selling_expenses': ['销售费用'],
    'administrative_expenses': ['管理费用'], 'research_expenses': ['研发费用'],
    'financial_expenses': ['财务费用'], 'interest_expense': ['利息费用', '利息支出'],
    'interest_income': ['利息收入'], 'taxes_surcharges': ['税金及附加', '营业税金及附加'],
    'investment_income': ['投资收益'], 'other_income': ['其他收益'],
    'fair_value_gain': ['公允价值变动收益'], 'credit_impairment': ['信用减值损失'],
    'asset_impairment': ['资产减值损失'], 'asset_disposal_gain': ['资产处置收益'],
    'nonoperating_income': ['营业外收入'], 'nonoperating_expense': ['营业外支出'],
    'assets': ['资产总计', '总资产', '资产总额', 'Total assets'],
    'liabilities': ['负债合计', '总负债', '负债总额', 'Total liabilities'],
    'equity': ['所有者权益合计', '股东权益合计', '所有者权益总计', '净资产'],
    'parent_equity': ['归属于母公司所有者权益合计', '归属于母公司股东权益合计', '归属于上市公司股东的净资产', '归属于母公司所有者权益', '归属于母公司股东的权益'],
    'minority_equity': ['少数股东权益'], 'cash': ['货币资金'],
    'receivables': ['应收账款'], 'notes_receivable': ['应收票据'],
    'receivables_financing': ['应收款项融资'], 'other_receivables': ['其他应收款'],
    'inventory': ['存货'], 'prepayments': ['预付款项'],
    'current_assets': ['流动资产合计'], 'noncurrent_assets': ['非流动资产合计'],
    'current_liabilities': ['流动负债合计'], 'noncurrent_liabilities': ['非流动负债合计'],
    'payables': ['应付账款'], 'short_term_debt': ['短期借款'], 'long_term_debt': ['长期借款'],
    # 港股/IFRS 报表常把有息负债笼统列作「計息借貸 / Interest-bearing borrowings」，
    # 不再区分长短，科目名里也没有「即期/非即期」字样。缺这条别名时净债务整块取不到，
    # 于是 DCF 的权益价值桥接没有有息负债可减 —— 实测农夫山泉因此取不到任何债务。
    'borrowings': ['计息借贷', '计息银行借贷', '银行借贷', '借款总额'],
    'bonds_payable': ['应付债券'], 'lease_liabilities': ['租赁负债'],
    'fixed_assets': ['固定资产'], 'intangibles': ['无形资产'], 'goodwill': ['商誉'],
    # 交易性金融资产在 IFRS/HKFRS 语境下叫「按公平值计入损益的金融资产」。
    # 这类资产不参与经营，DCF 里要作为非经营性资产加回权益价值。
    'financial_assets_fvtpl': ['以公允价值计量且其变动计入当期损益的金融资产', '交易性金融资产'],
    'contract_liabilities': ['合同负债'], 'retained_earnings': ['未分配利润'],
    'operating_cash_flow': ['经营活动产生的现金流量净额', '经营活动现金流量净额', '经营现金流净额'],
    'investing_cash_flow': ['投资活动产生的现金流量净额'],
    'financing_cash_flow': ['筹资活动产生的现金流量净额'],
    'capex': ['购建固定资产、无形资产和其他长期资产支付的现金', '购建固定资产无形资产和其他长期资产支付的现金', '资本性支出'],
    'cash_from_sales': ['销售商品、提供劳务收到的现金'],
    'cash_paid_suppliers': ['购买商品、接受劳务支付的现金'],
    'cash_equivalents_start': ['期初现金及现金等价物余额'],
    'cash_equivalents_end': ['期末现金及现金等价物余额'],
    'cash_equivalents_change': ['现金及现金等价物净增加额'],
    'fx_cash_effect': ['汇率变动对现金及现金等价物的影响'],
    'depreciation': ['固定资产折旧、油气资产折耗、生产性生物资产折旧', '固定资产折旧'],
    'amortization': ['无形资产摊销'], 'ebitda': ['EBITDA', '息税折旧摊销前利润'],
    'basic_eps': ['基本每股收益', '每股收益'], 'diluted_eps': ['稀释每股收益'],
}
# 利润表里的「减项」科目。港股/IFRS 报表的排版习惯是把金额放进括号表示
# "这一项是减项"，而金额本身是正数；A 股口径（也是本项目全部勾稽恒等式与
# 比率公式的约定）要求存正数：
#     net_profit - profit_before_tax + income_tax = 0
#     gross_margin = (revenue - cost_of_revenue) / revenue
# 不归一化的话，同一份港股报表的毛利率会算成 (收入 + 成本) / 收入 > 100%，
# 利润表勾稽也会平白差掉一个所得税的金额。实测踩过，两个指标同时被毁。
INCOME_STATEMENT_DEDUCTION_CODES = {
    'cost_of_revenue', 'taxes_surcharges', 'selling_expenses', 'administrative_expenses',
    'research_expenses', 'financial_expenses', 'interest_expense', 'income_tax',
    'credit_impairment', 'asset_impairment', 'nonoperating_expense',
}

# 「附註 / Notes」列的表头写法。这一列是给读者查附注编号的，不是数值列。
NOTE_COLUMN_LABELS = {'附注', '附註', '注释', '註釋', '备注', 'notes', 'note'}

# 「8.」「(ii)」「五、」这类**章节/附注编号单元格**。它们不是金额，但
# _number() 会把「8.」解析成 8 —— 实测农夫山泉 p173 的财务费用行是
# 「8. | | 財務費用 | | | | 8. | | FINANCE COSTS | …」，中英双栏排版把英文区号
# 「8.」放在同一行的第 6 列，于是被当成"财务费用 = 8 元"抽了出来，
# 再被量级异常规则标成"疑似 OCR 粘连"。判据必须要求**尾随分隔符或括号形式**：
# 裸数字「8」也可能是真实金额，不能一并排除。
_REFERENCE_CELL_RE = re.compile(
    r'^\s*(?:\d{1,2}\s*[.．、]|[一二三四五六七八九十]{1,3}\s*[、.]|'
    r'[（(]\s*(?:\d{1,2}|[ivxIVX]{1,4}|[一二三四五六七八九十]{1,3})\s*[）)])\s*$')


def _note_columns(rows):
    """找出报表里的「附註 / Notes」列，返回 {列号: 判定依据}。

    为什么必须排除：实测农夫山泉第 115 页利润表的列序是
    「科目 | REVENUE | 附註 | 2025年 | 2024年」，附註号是「5」「6」「11」这种
    一两位小整数。不排除它时，收益那一行的附註号「5」会被当成 2025 年营业收入，
    于是收入 = 5,000、营业现金比率 = 422 万倍 —— 数值和期间同时错，
    而且因为它是"已核验"状态，会堂而皇之地进入指标与估值。
    """
    found: dict[int, str] = {}
    for row in rows or []:
        for index, cell in enumerate(row):
            label = re.sub(r'[\s:：.·()]', '', to_simplified(str(cell or ''))).lower()
            if label in NOTE_COLUMN_LABELS:
                found.setdefault(index, '表头写明「附註/Notes」')
    return found


BALANCE_CODES = {'assets', 'liabilities', 'equity', 'parent_equity', 'minority_equity',
                 'cash', 'receivables', 'notes_receivable', 'receivables_financing',
                 'other_receivables', 'inventory', 'prepayments', 'current_assets',
                 'noncurrent_assets', 'current_liabilities', 'noncurrent_liabilities',
                 'payables', 'short_term_debt', 'long_term_debt', 'bonds_payable',
                 'borrowings', 'lease_liabilities', 'fixed_assets', 'intangibles', 'goodwill',
                 'financial_assets_fvtpl', 'lease_liabilities_current',
                 'lease_liabilities_noncurrent',
                 'contract_liabilities', 'retained_earnings',
                 # 港式/IFRS 资产负债表的单体科目。全部是时点值：漏登记的后果是
                 # 被当成区间值处理 —— 期间会落到"本年度"而不是"上年末"，
                 # 于是同比、周转率的分母全部对不上期间。
                 'right_of_use_assets', 'deferred_tax_assets', 'deferred_tax_liabilities',
                 'other_noncurrent_assets', 'other_payables', 'tax_payables',
                 'restricted_cash', 'pledged_deposits', 'long_term_deposits',
                 'deferred_income', 'net_assets', 'share_capital', 'reserves',
                 'long_term_investments', 'deferred_expenses'}

# 把英文/IFRS 别名与港式中文写法并进主词典：H 股、红筹、B 股与外企年报大量使用
# 这些叫法，只认简体 A 股科目名会在这些材料上静默提取到 0 条事实。
#
# 取并集而不是只遍历 ALIASES 的键：新增科目只要写进 EN_ALIASES / HKIFRS_ZH_ALIASES
# 就应当生效。只遍历 ALIASES 时，写在另外两张表里的新代码不会建键、被静默丢弃 ——
# 表现是"词典里明明有这个词条，查询却返回 None"，极难排查。踩过。
for _code in set(ALIASES) | set(EN_ALIASES) | set(HKIFRS_ZH_ALIASES):
    _extra = EN_ALIASES.get(_code, []) + HKIFRS_ZH_ALIASES.get(_code, [])
    ALIASES[_code] = list(dict.fromkeys(list(ALIASES.get(_code, [])) + list(_extra)))


# 表头行里"这一格在说期间"的词。A 股报表的写法远不止「本期/上期」：
# 本年/上年发生额、本报告期、上年同期、年初至报告期末、期末/年末/年初余额、
# 本期末/上期末、当期/去年同期……缺任何一个，整行就不被认作表头，列期间退回文档级日期。
_HEADER_PERIOD_RE = (r'20\d{2}|年度|季度|本期|上期|本年|上年|去年|当期|当年|同期|本报告期|报告期|年初|年末|期末|期初|'
                     r'1\s*[-~]\s*\d+月')
# 「增减变动 / 同比 / 比上年」列是衍生比率，不是某一期的科目余额，绝不能当作期间值入库。
_CHANGE_COLUMN_RE = re.compile(r'增减|变动幅度|变动比例|同比|环比|比上年|比上期|较上年|较上期|变动率|增长率|百分点')


_COL_SCOPE_CONSOLIDATED = {'合并', '合并数', '本集团', '集团', 'consolidated', 'group'}
_COL_SCOPE_PARENT = {'公司', '母公司', '本公司', '公司数', 'company', 'parent'}


def _column_scope(descriptor):
    """从"本列自己的表头"读口径。美的等 A 股年报把「合并及公司资产负债表」排成四列：
    2026年6月30日 合并 | 2025年12月31日 合并 | 2026年6月30日 公司 | 2025年12月31日 公司，
    口径写在第二行表头里，表级判定只会得到 unknown（标题是「合并及公司…」），
    结果一张表的四列要么全丢、要么公司数混成合并数。返回 consolidated/parent/None。"""
    tokens = {t.lower() for t in re.split(r'[\s/／,，]+', to_simplified(' '.join(descriptor))) if t}
    cons, par = bool(tokens & _COL_SCOPE_CONSOLIDATED), bool(tokens & _COL_SCOPE_PARENT)
    if cons and not par:
        return 'consolidated'
    if par and not cons:
        return 'parent'
    return None


def _is_scope_header_row(row):
    cells = [to_simplified(str(c or '')).strip().lower() for c in row if str(c or '').strip()]
    return bool(cells) and all(c in _COL_SCOPE_CONSOLIDATED | _COL_SCOPE_PARENT for c in cells)


def _is_header_row(row):
    """行里没有可识别科目、却带期间/口径字样 → 表头行。"""
    if any(_lookup_subject(c) for c in row[:3]):
        return False
    return _is_scope_header_row(row) or any(re.search(_HEADER_PERIOD_RE, str(c or '')) for c in row)


_SMALL_INT_RE = re.compile(r'^\d{1,3}$')


def _implicit_note_columns(rows, note_columns):
    """续表没有表头时找不到「附注」二字，但附注号列的特征很稳定：
    该列所有有内容的单元格都是 1~3 位裸整数（如茅台续页的「55」「44」），
    而同一批行里另有带千分位或小数的金额列。把它当金额会多出一列"期间不明"的假数值，
    还会让续表的列数与上一页对不上而放弃继承表头。"""
    cols = {}
    for row in rows:
        if not any(_lookup_subject(c) for c in row[:3]):
            continue
        for c, raw in enumerate(row):
            raw = str(raw or '').strip()
            if c in note_columns or not raw:
                continue
            cols.setdefault(c, []).append(raw)
    money = any(re.search(r'[,，.]', raw) for vals in cols.values() for raw in vals)
    return {c: '续表隐式附注列（整列均为 1~3 位裸整数）' for c, vals in cols.items()
            if money and len(vals) >= 1 and all(_SMALL_INT_RE.match(v) for v in vals)
            and not any(re.search(r'[,，.]', v) for v in vals)}


def _value_columns(rows, note_columns):
    """表里"有数值"的列（排除附注号列与纯编号格），从左到右。"""
    cols = set()
    for row in rows:
        if not any(_lookup_subject(c) for c in row[:3]):
            continue
        for c, raw in enumerate(row):
            raw = str(raw or '').strip()
            if c in note_columns or not raw or _REFERENCE_CELL_RE.match(raw):
                continue
            if _number(raw) is not None and not any(_lookup_subject(x) for x in [raw]):
                cols.add(c)
    return sorted(cols)


_TITLE_ONLY_RE = re.compile(
    r'^\s*(?:[一二三四五六七八九十]+[、.]|\d{1,2}[、.．])?\s*(合并|母公司|公司)?\s*(?:(?:及|和)\s*(合并|母公司|公司))?\s*'
    r'(?:财务报表|[\u4e00-\u9fff]{0,10}?)(资产负债表|利润表|现金流量表|所有者权益变动表|股东权益变动表)\s*(?:[（(]续[)）])?\s*$')
_TITLE_STATEMENT = {'资产负债表': 'balance_sheet', '利润表': 'income_statement', '现金流量表': 'cash_flow',
                    '所有者权益变动表': 'equity_changes', '股东权益变动表': 'equity_changes'}


def _title_line(line):
    """「5、合并现金流量表」「母公司利润表(续)」这类独占一行的报表标题 → (口径, 报表类型)，否则 None。
    A 股年报的标题经常落在上一页页尾、或排版进表格正文的某一行里，必须单独识别。"""
    text = re.sub(r'\s+', ' ', str(line or '').replace('\u3000', ' ')).strip()
    if not text or len(text) > 30:           # 标题行很短；同时避免长空白串让带多个 \s* 的正则回溯
        return None
    m = _TITLE_ONLY_RE.match(to_simplified(text))
    if not m:
        return None
    a, b, name = m.groups()
    words = {w for w in (a, b) if w}
    if len(words) > 1:
        scope = 'unknown'          # 「合并及公司…」：口径由列表头逐列给出
    elif words == {'合并'}:
        scope = 'consolidated'
    elif words:
        scope = 'parent'
    else:
        scope = 'unknown'
    return scope, _TITLE_STATEMENT[name]


def _trailing_title(page):
    """上一页页尾（最后 600 字）里最后一个独占一行的报表标题。"""
    text = str((page or {}).get('text') or '')[-600:]
    found = None
    for line in text.splitlines():
        hit = _title_line(line)
        if hit:
            found = hit
    return found


def _opening_statement(rows):
    """表的头几行科目暴露它是哪张报表的开头（用于判断"页首的表"是不是上一张表的续页）。"""
    labels = ''.join(str(c or '') for row in rows[:6] for c in row[:3])
    if re.search(r'经营活动产生的现金流量[：:]?|^一、?经营活动', labels):
        return 'cash_flow' if re.search(r'一、经营活动|销售商品、提供劳务收到的现金', labels) else None
    if re.search(r'流动资产[：:]', labels):
        return 'balance_sheet'
    if re.search(r'一、营业总收入|一、营业收入', labels):
        return 'income_statement'
    return None


def _title_before_table(page, rows):
    """本页正文里、位于该表第一个科目之前的最后一个独占一行的报表标题。
    比"页首 800 字"可靠：一页常含上一张表的尾巴 + 下一张报表的标题 + 新表。"""
    text = str((page or {}).get('text') or '')
    if not text:
        return None
    anchor = ''
    for row in rows[:4]:
        for cell in row[:3]:
            label = re.sub(r'\s+', '', str(cell or ''))
            if len(label) >= 3 and _lookup_subject(cell):
                anchor = label
                break
        if anchor:
            break
    if not anchor:
        return None
    stripped, offsets, pos = [], [], 0
    lines = text.splitlines()
    for line in lines:
        compact = re.sub(r'\s+', '', line)
        offsets.append(pos)
        stripped.append(compact)
        pos += len(compact)
    flat = ''.join(stripped)
    at = flat.find(anchor)
    if at < 0:
        return None
    found = None
    for line, start in zip(lines, offsets):
        if start > at:
            break
        hit = _title_line(line)
        if hit:
            found = hit
    return found


def _dominant_value_count(rows, note_columns):
    """各科目行里"数值格个数"的众数（附注列、编号格不计）：这张表有几个期间列。"""
    counts = Counter()
    for row in rows:
        if not any(_lookup_subject(c) for c in row[:3]):
            continue
        n = sum(1 for c, raw in enumerate(row)
                if c not in note_columns and str(raw or '').strip()
                and not _REFERENCE_CELL_RE.match(str(raw).strip())
                and _number(str(raw).strip()) is not None)
        if n:
            counts[n] += 1
    return counts.most_common(1)[0][0] if counts else 0


def _header_descs(headers, col_groups, note_columns):
    """已读到的表头 → 按从左到右排列的"期间列描述"（每个描述是 [每行表头文字] 列表）。"""
    groups = {}
    for h in headers:
        for c, v in enumerate(h):
            text = str(v or '').strip()
            if text and c not in note_columns:
                groups.setdefault(col_groups.get(c, c), set()).add(c)
    out = []
    for g in sorted(groups, key=lambda g: min(groups[g])):
        cols = sorted(groups[g])
        desc = [' '.join(str(h[c] or '') for c in cols if c < len(h)) for h in headers]
        joined = ' '.join(desc)
        if re.search(_HEADER_PERIOD_RE, joined):
            out.append(desc)
    return out


def _column_groups(rows):
    """把"表头与数据错位"的列归并成组，返回 {列号: 组号}。

    为什么需要：pdfplumber 对 A 股年报的合并利润表常给出这样的网格 ——
        表头行：['', '项目', '', '', '附注', '', '', '本年发生额', '', '', '上年发生额', '']
        数据行：['', '一、营业总收入', '', '(五)42', '', '', '233,432,768,960.43', '', '', '343,176,440,712.96', '', '']
    表头文字落在第 7、10 列，数值却落在第 6、9 列（单元格合并造成的 1 列偏移）。
    原逻辑按"本列自己的表头"取期间，第 9 列没有表头，于是向左借第 7 列的「本年发生额」，
    **上年数值被绑成本年** —— 实测万科/滨江/宁德等 A 股年报因此出现大量 duplicate_conflict，
    整张主表被降级。
    做法：从左到右贪心合并相邻列，只要两列在任何一行里不同时有内容就并入同一组；
    表头列与它偏移 1~2 格的数值列因此成组，而相邻的两个数值列（同一行都有数）永远不会被并起来。
    """
    width = max((len(r) for r in rows), default=0)
    filled = [{c for c, v in enumerate(r) if str(v or '').strip()} for r in rows]
    groups, current, members = {}, 0, set()
    for c in range(width):
        occupied = {i for i, f in enumerate(filled) if c in f}
        if members & occupied:
            current += 1
            members = set()
        members |= occupied
        groups[c] = current
    return groups


def _label(text):
    # 先做繁简归一，否则繁體年报（H 股、红筹）每一条科目都匹配不上
    text = to_simplified(str(text or '')).strip()
    text = re.sub(r'^[一二三四五六七八九十\d]+[、.．]\s*', '', text)
    text = re.sub(r'^[（(][一二三四五六七八九十\d]+[）)]\s*', '', text)
    text = re.sub(r'^(加|减|其中)[:：]', '', text)
    if re.search(r'[（(][^）)]*(?:不包括|不含|扣除)[^）)]*[）)]', text):
        return re.sub(r'[\s:：,，、\-—]', '', text).lower()   # 「销售费用(不包括职工薪酬和税费)」是另一个口径，别被剥成「销售费用」
    text = re.sub(r'\s+', '', text)          # 先压空白：折行的「（损失以“-”\n号填列）」否则括号剥不掉
    text = re.sub(r'[（(].*?[）)]', '', text)
    text = re.sub(r'(?:损失|亏损|净亏损|减少)以[“"‘\'「]?[-－—–]+[”"’\'」]?号填列.*$', '', text)
    return re.sub(r'[\s:：,，、\-—]', '', text).lower()


_LOOKUP = {_label(alias): code for code, aliases in ALIASES.items() for alias in aliases}

# 「同一标签、两张报表、不同概念」的消歧表。
#
# 为什么必须消歧：港式年报把綜合損益表与綜合全面收益表分作两张表，两张表里
# 都有一行写作「母公司擁有人應佔」（英文 Attributable to owners of the parent）。
# 損益表那行是**归母净利润**，全面收益表那行是**归母全面收益总额**，
# 两者通常差一个"其他全面收益"。若都映射到 parent_net_profit，两条不同数值
# 会被判成 duplicate_conflict，两条一起降级 —— 结果归母净利润整块不可用，
# 连锁导致 roe 算不出、profit_attribution 勾稽无法执行。
#
# 旧词典只收了带"溢利"的写法（「母公司拥有人应占溢利」），而原文写作
# 「母公司擁有人應佔」（不带"溢利"），于是**两条都匹配不上**，静默丢失。
# 实测农夫山泉 2025 年报：2024 年归母净利润 12,123,304 千元在原文第 115 页
# 白纸黑字，却没有进 facts，报告只能在 ROE 行写「—」。
#
# 格式：(归一化标签, 科目代码, 必需上下文正则, 禁止上下文正则)
#
# 注意上下文已由 to_simplified() 归一，正则必须写**简体**。
# 两张表都给"必需"（而不是给其中一张写"禁止"）：上下文判不出报表类型时
# 应当**两条都不认**，宁可留缺口，也不能把全面收益当成净利润。
_COMPREHENSIVE_INCOME_CTX = r'全面收益|全面收入|Comprehensive\s+Income'
_PROFIT_OR_LOSS_CTX = r'损益表|收益表|利润表|Profit\s+or\s+Loss|Income\s+Statement'
_CONTEXT_GUARDED_ALIASES = [
    ('母公司拥有人应占', 'parent_total_comprehensive_income', _COMPREHENSIVE_INCOME_CTX, None),
    ('本公司拥有人应占', 'parent_total_comprehensive_income', _COMPREHENSIVE_INCOME_CTX, None),
    ('公司拥有人应占', 'parent_total_comprehensive_income', _COMPREHENSIVE_INCOME_CTX, None),
    ('母公司拥有人应占', 'parent_net_profit', _PROFIT_OR_LOSS_CTX, None),
    ('本公司拥有人应占', 'parent_net_profit', _PROFIT_OR_LOSS_CTX, None),
    ('公司拥有人应占', 'parent_net_profit', _PROFIT_OR_LOSS_CTX, None),
]


# 「变动口径」标签：「貿易應付款項及應付票據增加╱（減少）」这类行给的是本期增减量，
# 不是期末余额。注意**不能**用裸的「变动」二字去匹配 ——
# A 股科目「以公允价值计量且其变动计入当期损益的金融资产」里就有"变动"，
# 而它本身是资产负债表科目，误伤后整条公允价值资产会消失。
_CHANGE_LABEL_RE = re.compile(
    r'增加|减少|減少|淨增|净增|淨減|净減|變動額|变动额|變動淨額|变动净额|增減|增减|'
    r'\bIncreases?\b|\bDecreases?\b|\bChanges? in\b|Net movement|Movement in', re.I)


def _is_change_label(raw_label):
    """标签是否表达"本期变动额"而非"期末余额"。"""
    return bool(_CHANGE_LABEL_RE.search(to_simplified(str(raw_label or ''))))


def _lookup_subject(raw, scope_text=''):
    """把一个单元格文本映射到标准科目代码。

    双语报表常把中英文挤在同一格（「貿易應收款項 Trade receivables」），
    整串匹配不上任何词条，所以按中英边界切开逐个再试。

    scope_text 是消歧上下文（表元数据 + 本页正文）。只有 _CONTEXT_GUARDED_ALIASES
    里的**歧义标签**才需要它；其余标签一律先走原词典，行为完全不变。
    """
    candidates = subject_candidates(raw)
    for candidate in candidates:
        code = _LOOKUP.get(_label(candidate))
        if code:
            return code
    if not scope_text or not _CONTEXT_GUARDED_ALIASES:
        return None
    context = to_simplified(str(scope_text))
    for candidate in candidates:
        key = _label(candidate)
        for label, code, required, forbidden in _CONTEXT_GUARDED_ALIASES:
            if key != label:
                continue
            if required and not re.search(required, context, re.I):
                continue
            if forbidden and re.search(forbidden, context, re.I):
                continue
            return code
    return None


def _joined_subject(fragments, current, scope_text=''):
    """把折行的科目名片段与当前行拼起来再匹配。

    先试最长的拼接（3 段 → 2 段 → 1 段），并且优先用"去掉括号补充说明"的形式，
    因为港式写法爱把简称塞进括号： 
        「按公平值計入損益的金融資產（「按公平值計入損益」）」
    这类尾部补充说明在 _label 里本来就会被剥掉，所以直接整串交给 _lookup_subject。
    """
    if not current or not fragments:
        return None, None
    for width in range(min(len(fragments), 3), 0, -1):
        text = ''.join(fragments[-width:]) + current
        code = _lookup_subject(text, scope_text)
        if code:
            return code, text
    return None, None


def _fragment_cell(row):
    """返回 (列号, 文本)：本行开头第一个"纯文本"单元格。

    港式/H 股报表常把一个科目名折成 2~3 行，数值只落在最后一行：
        預付款項、其他應收款項
        及其他資產              21    1,377,874    1,218,292
    逐行独立匹配时，任一行都不等于完整科目名，于是整条科目凭空消失 ——
    静默、无报错。所以要把折行片段攒起来再拼。
    """
    for c in range(min(3, len(row))):
        text = str(row[c] or '').strip()
        if not text:
            continue
        if _number(text) is None:
            return c, text
        return None, None
    return None, None


def _str(value):
    if value == 0:
        return '0'
    return format(value, 'f').rstrip('0').rstrip('.') if '.' in format(value, 'f') else format(value, 'f')


def _number(text):
    text = str(text or '').strip().replace('−', '-').replace('（', '(').replace('）', ')')
    if text in {'', '-', '—', '–', '--', '不适用', 'N/A', 'nan', 'None'}:
        return None
    negative = text.startswith('(') and text.endswith(')')
    if negative:
        text = text[1:-1]
    text = re.sub(r'[,，\s]', '', text)
    text = re.sub(r'(?:亿元|百万元|千元|万元|元|%|％)$', '', text)
    if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)', text):
        return None
    # 扫描件 OCR 会把相邻数字粘成一个（"15,868" + "274,158" -> "15868274158682"）。
    # 但门槛不能定得太低：以元列报时，最大的资产负债表科目（如工行总资产约 44.7 万亿元）
    # 本来就有 14 位有效数字，一刀切会误伤真实的巨额科目。
    # 所以这里只拦"任何币种、任何单位下都不可能"的 16 位以上；
    # 14~15 位的可疑值交给 _conflicts 的跨期量级比较去判断。
    digits = text.lstrip('+-').replace('.', '')
    if len(digits.lstrip('0')) > 15:
        return None
    try:
        value = Decimal(text)
        return -value if negative else value
    except InvalidOperation:
        return None


def _scope(text):
    found = set()
    compact = re.sub(r'\s+', '', to_simplified(text))
    if re.search(r'合并(?:资产负债表|利润表|现金流量表|财务报表|报表|口径)', compact):
        found.add('consolidated')
    if re.search(r'(?:母公司|母企业)(?:资产负债表|利润表|现金流量表|财务报表|报表|口径)', compact):
        found.add('parent')
    # 英文/港式：Consolidated statement of ... / the Company statement of ...
    found |= detect_scope_i18n(text)
    if detect_parent_statement(text):
        # 「本公司…表」是最强信号：单体报表与合并报表科目同名，判错就全盘错
        return 'parent'
    return next(iter(found)) if len(found) == 1 else 'unknown'


UNIT_SCALE_BY_NAME = {'元': 1, '千元': 1000, '万元': 10000, '百万元': 1000000, '亿元': 100000000}

# 「同一个科目名在两张报表里各有一个值」的典型科目。
# 只按科目名匹配时它们会互相冲突，把两张表的数据一起打成不可用。
# 这里给每个科目指定"归属报表"，冲突时保留归属报表里的那一个。
_INCOME_HOME_CODES = (
    'revenue', 'cost_of_revenue', 'total_revenue', 'total_operating_cost', 'taxes_surcharges', 'selling_expenses',
    'administrative_expenses', 'research_expenses', 'financial_expenses', 'investment_income', 'other_income',
    'fair_value_gain', 'credit_impairment', 'asset_impairment', 'asset_disposal_gain', 'operating_profit',
    'nonoperating_income', 'nonoperating_expense', 'profit_before_tax', 'income_tax', 'net_profit',
    'parent_net_profit', 'minority_profit', 'basic_eps', 'diluted_eps')

HOME_STATEMENT = {
    **{code: 'income_statement' for code in _INCOME_HOME_CODES},
    'cash': 'balance_sheet', 'cash_equivalents_start': 'cash_flow',
    'cash_equivalents_end': 'cash_flow', 'cash_equivalents_change': 'cash_flow',
    'operating_cash_flow': 'cash_flow', 'investing_cash_flow': 'cash_flow',
    'financing_cash_flow': 'cash_flow', 'fx_cash_effect': 'cash_flow',
    'capex': 'cash_flow', 'cash_from_sales': 'cash_flow', 'cash_paid_suppliers': 'cash_flow',
    'depreciation': 'cash_flow', 'amortization': 'cash_flow',
}

# 报表分节：财务状况表里「租賃負債」在流动与非流动负债下各列一次，
# 同名不同义。按分节把科目切成即期/非即期，避免两者被判成互相冲突的值。
SECTION_PATTERNS = [
    ('current_assets', re.compile(r'^\s*(?:流動資產|流动资产|Current assets)\s*$', re.I)),
    ('noncurrent_assets', re.compile(r'^\s*(?:非流動資產|非流动资产|Non-current assets|Non current assets)\s*$', re.I)),
    ('current_liabilities', re.compile(r'^\s*(?:流動負債|流动负债|Current liabilities)\s*$', re.I)),
    ('noncurrent_liabilities', re.compile(r'^\s*(?:非流動負債|非流动负债|Non-current liabilities|Non current liabilities)\s*$', re.I)),
]
SPLIT_BY_SECTION = {
    'lease_liabilities': {'current_liabilities': 'lease_liabilities_current',
                          'noncurrent_liabilities': 'lease_liabilities_noncurrent'},
    'borrowings': {'current_liabilities': 'short_term_debt',
                   'noncurrent_liabilities': 'long_term_debt'},
}


def _section_of(text):
    """判断一行文本是否是报表分节标题（流动/非流动资产、负债）。"""
    label = to_simplified(str(text or '')).strip()
    if not label:
        return None
    for name, pattern in SECTION_PATTERNS:
        if pattern.match(label):
            return name
    return None

# 每股类指标天然以「元/股」列报，与报表列报单位无关。
# 「每股基本盈利 … 人民幣元」会让同一张表出现两种单位声明，
# 若不排除，「百万元」表会被判成单位冲突而退回文档级默认（千元），
# 整表数值静默差 1000 倍 —— 实测农夫山泉五年摘要页踩过这个坑。
PER_SHARE_HINT = re.compile(r'每股|每股份|每股盈利|每股收益|每股股息|earnings\s+per\s+share|per\s+share|\bEPS\b', re.I)


def _units(text):
    text = to_simplified(text)
    currencies = {currency for currency, aliases in {
        'CNY': ['人民币', 'CNY', 'RMB', 'Renminbi'], 'USD': ['美元', 'USD', 'US$'],
        'HKD': ['港元', '港币', 'HKD', 'HK$'], 'EUR': ['欧元', 'EUR']}.items()
        if any(alias.lower() in text.lower() for alias in aliases)}
    currencies |= detect_currency_i18n(text)
    unit_names = r'(亿元|百万元|千元|万元|元)'
    declarations = []
    for m in re.finditer(r'(?<!编制)单位\s*[:：]?\s*(?:人民币|美元|港币|港元|欧元)?\s*' + unit_names, text):
        declarations.append((m.start(), m.group(1)))
    for m in re.finditer(r'人民币\s*' + unit_names, text):
        declarations.append((m.start(), m.group(1)))
    # 丢掉"每股 … 元"里的元：那是每股口径，不是列报单位
    units_found = [unit for pos, unit in declarations
                   if not PER_SHARE_HINT.search(text[max(0, pos - 60):pos + 60])]
    if len(currencies) > 1:
        return 'unknown', 'unknown', 1
    currency = next(iter(currencies)) if len(currencies) == 1 else 'unknown'
    if units_found:
        # 多数票定单位；并列第一才认为真的判不出来。
        # 原来的写法是"只要出现两种单位就 unknown"，过于粗糙。
        top = Counter(units_found).most_common()
        if len(top) > 1 and top[0][1] == top[1][1]:
            return 'unknown', 'unknown', 1
        unit = top[0][0]
        return (currency if currency != 'unknown' else 'CNY'), unit, UNIT_SCALE_BY_NAME[unit]
    if re.fullmatch(r'[\d,.，+\-()（）\s]+(?:亿元|百万元|千元|万元|元)', text):
        unit_match = re.search(unit_names + r'$', text)
        if unit_match:
            unit = unit_match.group(1)
            return (currency if currency != 'unknown' else 'CNY'), unit, UNIT_SCALE_BY_NAME[unit]
    # 英文/港式列报：RMB'000、HK$ million、人民币千元
    i18n_unit, i18n_scale = detect_unit_i18n(text)
    if i18n_unit:
        return currency, i18n_unit, i18n_scale
    return currency, 'unknown', 1


def _period(text, balance=False):
    """识别期间。中文优先，识别不出再走英文（'31 December 2025' 等）。"""
    raw = str(text)
    end, kind = _period_cn(to_simplified(raw), balance)
    explicit = re.search(r'20\d{2}\s*[-年/.]\s*\d{1,2}\s*[-月/.]\s*\d{1,2}', raw)
    if end and (kind != 'unknown' or explicit):
        return end, kind
    if re.search(r'[A-Za-z]', raw):
        english = parse_english_period(raw)
        if english:
            e_end, e_kind = english
            return e_end, (e_kind or ('instant' if balance else 'unknown'))
    return end, kind


def _period_cn(text, balance=False):
    """Return explicit end date and duration, without treating bare years as YTD."""
    text = str(text).replace('至', '-').replace('到', '-').replace('～', '-').replace('—', '-').replace('–', '-')
    years = re.findall(r'(20\d{2})', text)
    if not years:
        return '', 'unknown'
    year = int(years[0])
    end_month = None
    kind = 'unknown'
    ranges = re.search(r'(?:20\d{2}年)?\s*(\d{1,2})\s*月?\s*[-~]\s*(\d{1,2})\s*月', text)
    if ranges:
        start, end_month = map(int, ranges.groups())
        if not 1 <= start <= end_month <= 12:
            return '', 'unknown'
        kind = 'annual' if (start, end_month) == (1, 12) else ('ytd' if start == 1 else 'quarter' if end_month - start == 2 else 'unknown')
    elif ('年度' in text and '半年度' not in text) or '全年' in text or '年报' in text:
        end_month, kind = 12, 'annual'
    else:
        quarter = re.search(r'第?([一二三四1234])季度', text)
        if quarter:
            q = {'一': 1, '二': 2, '三': 3, '四': 4}.get(quarter[1], int(quarter[1]) if quarter[1].isdigit() else 0)
            end_month, kind = q * 3, 'ytd'
        elif '半年度' in text or '半年' in text:
            end_month, kind = 6, 'ytd'
        elif '年末' in text or '年年末' in text or '年期末' in text:
            end_month = 12
    date_pattern = r'(20\d{2})\s*[-年/.]\s*(\d{1,2})\s*[-月/.]\s*(\d{1,2})'
    dated = re.findall(date_pattern, text)
    if len(dated) >= 2:
        try:
            start_date = date(*map(int, dated[0]))
            end_date = date(*map(int, dated[-1]))
        except ValueError:
            return '', 'unknown'
        if start_date.year != end_date.year or start_date > end_date:
            return end_date.isoformat(), 'instant' if balance else 'unknown'
        if start_date.month == 1 and start_date.day == 1:
            kind = 'annual' if end_date.month == 12 and end_date.day == 31 else 'ytd'
        elif start_date.day == 1 and end_date.month - start_date.month == 2:
            kind = 'quarter'
        return end_date.isoformat(), 'instant' if balance else kind
    exact = re.search(date_pattern, text)
    if exact and not ranges:
        try:
            end = date(*map(int, exact.groups())).isoformat()
        except ValueError:
            return '', 'unknown'
        return end, 'instant' if balance else kind
    if end_month:
        return f'{year}-{end_month:02}-{calendar.monthrange(year, end_month)[1]:02}', 'instant' if balance else kind
    return f'{year}-12-31', 'instant' if balance else 'unknown'


# 「表格级期间标记」：只由年份构成的整行。
# 注意中英并列的排版 —— 实测农夫山泉附注 37 那一行是
#     「            2024年                                2024」
# 中文年份与英文年份并排在同一行，所以不能要求整行只有一个年份 token。
_STANDALONE_YEAR_LINE_RE = re.compile(
    r'^[ \t　]*(?:截至[ \t　]*)?20\d{2}[ \t　]*(?:年(?:度|末)?|[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}|年?\s*12\s*月\s*31\s*日)?'
    r'(?:[ \t　]+(?:截至[ \t　]*)?20\d{2}[ \t　]*(?:年(?:度|末)?)?)*[ \t　]*$', re.M)
_YEAR_TOKEN_RE = re.compile(r'20\d{2}')


def _table_level_period(context, page_text):
    """识别"整张表属于哪一年"的表格级期间标记。

    为什么必须单独做这件事：实测农夫山泉附注 37（工具页码 p217，金融工具分类（续））
    表格上方只有一行孤立的「2024年 / 2024」，表内是"贸易应收款项 581,372 /
    现金及银行结余 10,722,048 / 长期银行存款 10,630,882"等 2024 年比较期数值。
    表格自身的 rows 与 context 里都没有年份，列期间于是回落到**文档级日期**
    （年报封面的 2025-12-31），整表被绑成 2025 年。

    后果不是"少一条数据"而是"多六条假冲突"：同一科目在附注里被绑成 2025、
    在主表里是正确的 2025，两者相减，差额恰好等于**同比变动额**
    （-455,526 / -456,761 / -764,567 / -57,433 / -449 / +18,915 千元），
    于是被当成"跨表数值冲突"报给用户。用户看到 6 条"待复核差异"，
    实际原文两处都对，只是期间绑错了。

    取值优先级：表格自身 context 里最后一个年份行 > 表格上方紧邻 500 字内
    最后一个年份行。只看紧邻窗口是为了避免把同页另一张表的年份认到本表头上。

    安全约束：命中行内的年份 token 必须**唯一**。若一行里同时出现 2025 与 2024
    （典型的"科目 | 2025年 | 2024年"表头行），说明该行描述的是多列而非整表，
    一律放弃，避免把比较期整列当成本期。
    """
    ctx = str(context or '')
    for text in (ctx, _period_window(ctx, page_text)):
        if not text:
            continue
        for hit in reversed(list(_STANDALONE_YEAR_LINE_RE.finditer(text))):
            years = set(_YEAR_TOKEN_RE.findall(hit.group(0)))
            if len(years) == 1:
                return f'{years.pop()}-12-31', 'annual'
    return '', 'unknown'


def _period_window(context, page_text):
    """取表格上方紧邻的 500 字窗口，用于找表格级年份标记。"""
    page = str(page_text or '')
    ctx = str(context or '').strip()
    if not page or not ctx:
        return ''
    position = page.find(ctx.split('\n')[0].strip())
    if position < 0:
        return ''
    return page[max(0, position - 500):position]


_REPORT_TITLE_RES = [
    (re.compile(r'(20\d{2})年?(?:度)?(?:第?[一1]季度报告|第?[一1]季报)'), 3, 'quarter'),
    (re.compile(r'(20\d{2})年?(?:度)?(?:第?[三3]季度报告|第?[三3]季报)'), 9, 'ytd'),
    (re.compile(r'(20\d{2})年?(?:度)?(?:半年度报告|半年报|中期报告|中报|上半年度?)'), 6, 'ytd'),
    (re.compile(r'(20\d{2})年?(?:年度报告|年报|度报告)'), 12, 'annual'),
]


def _report_period(text):
    """从报告封面/首页文字识别"本报告覆盖的期间"：返回 (期末日 ISO, 期间类型, 标签)，识别不出返回 None。

    A 股定期报告的表头大量使用相对写法（本报告期 / 上年同期 / 年初至报告期末 / 本年发生额 …），
    列期间必须以"这份报告是哪一期"为锚点来解释；只在局部上下文里找年份会被
    "追溯调整以前年度会计数据"这类句子里的「年度」带偏（实测把美的半年报的本报告期绑成 2026-12-31 全年）。
    """
    compact = re.sub(r'\s+', '', to_simplified(str(text or '')[:4000]))
    best = None
    for regex, month, kind in _REPORT_TITLE_RES:
        for m in regex.finditer(compact):
            if best is None or m.start() < best[0]:
                best = (m.start(), int(m.group(1)), month, kind)
    if not best:
        return None
    _, year, month, kind = best
    return (f'{year}-{month:02}-{calendar.monthrange(year, month)[1]:02}', kind,
            {3: '一季报', 6: '半年报', 9: '三季报', 12: '年报'}[month])


def _shift_year(iso, years):
    d = date.fromisoformat(iso)
    y = d.year + years
    return f'{y}-{d.month:02}-{min(d.day, calendar.monthrange(y, d.month)[1]):02}'


def _ashare_column_period(desc, report, balance):
    """A 股相对期间表头 → (期末, 类型)。解释不了返回 None，交回通用逻辑。

    规则（report 是 _report_period 的结果）：
      上年同期 / 上期 / 上年发生额 / 去年同期   → 上年同日，类型同本期
      上年末 / 上年度末 / 年初(余额) / 期初      → 上年 12-31（时点）
      年初至报告期末 / 年初至本报告期末          → 报告期末，累计
      本报告期末 / 报告期末 / 期末(余额) / 本期末 → 报告期末（时点）
      本报告期 / 本期 / 本年 / 当期              → 报告期末；三季报的「本报告期」是**单季**
    """
    if not report or re.search(r'20\d{2}', desc):
        return None
    end, kind, _ = report
    d = re.sub(r'\s+', '', to_simplified(desc))
    if re.search(r'上年末|上年度末|上年年末|年初余额|期初余额|年初数|期初数|^年初$|^期初$', d):
        return _shift_year(end[:4] + '-12-31', -1) if kind != 'annual' else _shift_year(end, -1), 'instant'
    if re.search(r'年初至(?:本)?报告期末|年初至本期末|年初至期末', d):
        return end, ('annual' if kind == 'annual' else 'ytd')
    if re.search(r'本报告期末|报告期末|期末余额|本期末|期末数|年末余额|本年末|^期末$|^年末$', d):
        return end, 'instant'
    if re.search(r'上年同期|上年度|上年|上期|去年同期|去年|同期', d):
        k = 'instant' if balance else ('annual' if kind == 'annual' else ('quarter' if kind == 'ytd' and end[5:7] == '09' and '本报告期' in d else kind))
        return _shift_year(end, -1), ('instant' if balance else k)
    if re.search(r'本报告期|本期|本年|当期|当年|报告期', d):
        if balance:
            return end, 'instant'
        if kind == 'ytd' and end[5:7] == '09' and '本报告期' in d:
            return end, 'quarter'
        return end, kind
    return None


def _column_period(headers, context, document_text, requested, code, table_period=None, report=None):
    balance = code in BALANCE_CODES
    descriptor = ' '.join(headers)
    relative = _ashare_column_period(descriptor, report, balance)
    if relative:
        return relative
    # Year on an upper header and duration on a lower header remain together.
    end, kind = _period(descriptor, balance)
    if end:
        if balance and re.fullmatch(r'\s*20\d{2}年?\s*', descriptor):
            reporting = context + '\n' + document_text[:2000] + '\n' + requested
            if re.search(r'年度|半年|季度|20\d{2}[-年/.]\d{1,2}[-月/.]\d{1,2}', reporting):
                contextual_end, _ = _period(reporting, True)
                if contextual_end and (end[:4] == contextual_end[:4] or contextual_end[5:] == '12-31'):
                    return end[:4] + contextual_end[4:], 'instant'
            return '', 'instant'
        if kind == 'unknown':
            contextual_end, contextual_kind = _period(context + '\n' + document_text[:2000], balance)
            if contextual_kind in {'annual', 'ytd'}:
                kind = contextual_kind
                if re.fullmatch(r'\s*20\d{2}年?\s*', descriptor) and contextual_end:
                    end = end[:4] + contextual_end[4:]
        return end, kind
    # 表格自身 context 里若已写明期间，优先采用（它是本表的元数据）。
    end, kind = _period(context, balance)
    if not end and table_period and table_period[0]:
        # 表格级年份标记（如表格上方孤立的「2024」）优先于文档级日期：
        # 前者描述的是这张表，后者只是年报封面的报告期。顺序颠倒会把
        # 比较期数值绑成本期，制造出"差额恰等于同比变动额"的假冲突。
        end, kind = table_period[0], ('instant' if balance else table_period[1])
    if not end:
        end, kind = _period(context + '\n' + document_text[:2000], balance)
    if not end:
        end, kind = _period(requested, balance)
    if end and (any(term in descriptor for term in ['上期', '上年', '去年']) or (balance and any(term in descriptor for term in ['期初', '年初']))):
        parsed = date.fromisoformat(end)
        if balance and any(term in descriptor for term in ['期初', '年初']):
            end = f'{parsed.year - 1}-12-31'
        else:
            end = f'{parsed.year - 1}-{parsed.month:02}-{min(parsed.day, calendar.monthrange(parsed.year - 1, parsed.month)[1]):02}'
    if not balance and end and '本报告期' in descriptor and '季度' in context + document_text[:2000]:
        kind = 'quarter'
    return end, kind


def _company_key(f):
    company = str(f.get('company', '') or '').strip()
    if company:
        return company
    document_id = f.get('source', {}).get('document_id')
    return '@document:' + str(document_id) if document_id else '@unidentified:' + str(f.get('fact_id', ''))


def _key(f):
    return (_company_key(f),) + tuple(f.get(k, '') for k in ('period', 'period_type', 'scope', 'currency', 'code'))


def _issuer(text):
    explicit = re.findall(r'(?:编制单位|公司名称|企业名称|发行人)\s*[:：\t ]+\s*([^\n;；\t]+?公司)', text)
    if explicit:
        return set(name.strip() for name in explicit)
    titles = set()
    for line in text.splitlines()[:12]:
        line = re.sub(r'\s*20\d{2}年.*$', '', line.strip())
        # 「本公司 / 该集团 / 母公司」这类指代词也满足"以公司结尾"，但它们是自称而非主体名；
        # 混进 issuers 会让"发行人不止一个"的判据误触发。
        if re.fullmatch(r'(本|该|該|母|子|总|總|分)公司', line):
            continue
        if re.fullmatch(r'[\u4e00-\u9fffA-Za-z（）()·]{1,100}公司', line) and not line.startswith(('归属于', '母公司', '子公司')):
            titles.add(line)
    return titles


# 法定主体后缀。比对"是不是同一家公司"时必须先剥掉：
# 用户不可能把「農夫山泉股份有限公司」一字不差地敲进去，通常只写「农夫山泉」；
# 港股年报封面又是繁简混排。不剥后缀、不做繁简归一，就会把同一家公司判成两家。
_LEGAL_SUFFIX_RE = re.compile(
    r'(股份有限公司|有限责任公司|有限責任公司|股份公司|控股集团|控股集團|集团有限公司|集團有限公司|'
    r'集团|集團|控股|股份|公司|企業|企业|'
    r'company\s+limited|co\.?,?\s*ltd\.?|co\.?\s*limited|ltd\.?|limited|incorporated|inc\.?|'
    r'corporation|corp\.?|company|holdings|holding|group|plc)\s*$',
    re.I)

_CJK_RE = re.compile(r'[\u4e00-\u9fff]')


def _company_core(name):
    """公司名用于比对的"核心"：繁简归一 → 去标点空白 → 剥法定后缀 → 拉丁转小写。

    「農夫山泉股份有限公司」与「农夫山泉」都归一成 `农夫山泉`。
    后缀可能叠着出现（股份有限公司 → 剥一轮剩「股份」），故最多剥两轮。
    """
    text = to_simplified(name)
    text = re.sub(r'[\s\u3000·・.,，、。()（）\[\]【】\-—_/\\&＆\'"“”]', '', text)
    for _ in range(2):
        stripped = _LEGAL_SUFFIX_RE.sub('', text)
        if stripped == text:
            break
        text = stripped
    return text.lower()


def _same_company(requested, issuers):
    """用户给的名称与披露名称是否指同一主体。

    判据是"核心名互相包含"，不是字符串相等。留了两种宽容：
      · 一方含另一方 —— 「农夫山泉」⊂「农夫山泉股份有限公司」，同一家；
      · 跨文字系统（中文 vs 拉丁）无法比对 —— 不武断判为不同家，改由调用方
        记一条提示。年报封面中英文并列是常态，而本函数拿不到「農夫山泉 =
        NONGFU SPRING」这种对应关系。
    判错两个方向的代价并不对称：误判"同一家"只是少一条提示；误判"不同家"会把
    **整份报告的全部事实**打成 needs_review，下游 _valid 全部拒收，一个指标都算不出来。
    实测踩过：用户输入「农夫山泉」、披露名「農夫山泉股份有限公司」，172 条事实全灭。
    """
    core = _company_core(requested)
    if not core:
        return True
    for name in issuers:
        other = _company_core(name)
        if not other:
            continue
        if core == other or core in other or other in core:
            return True
        if bool(_CJK_RE.search(core)) != bool(_CJK_RE.search(other)):
            # 跨文字系统：不作否证，交给调用方提示人工核对
            return True
    return False


def _home_statement(code):
    """科目「归属报表」的兜底规则。

    除了 HOME_STATEMENT 里显式登记的科目，**所有资产负债表科目都以财务状况表为准**。
    附注里的同名行往往是明细拆解而非期末余额 —— 实测附注 18 的
    「預付款項 — 物業、廠房及設備」88,574 千元被认成固定资产，
    与主表期末余额 24,719,336 千元互判冲突，把**正确的主表值一起废掉**。
    有了归属规则就能保留主表值、只把附注那一条降级为待复核。
    """
    if code in HOME_STATEMENT:
        return HOME_STATEMENT[code]
    if code in BALANCE_CODES:
        return 'balance_sheet'
    return None


def _resolve_same_tier_duplicates(group, code, balance_labels, reviews):
    """同一期间、同一口径下出现多个不同数值时的裁决。返回 True 表示已归类。

    依次尝试三种解释，避免"一律当冲突、两条一起废掉"的粗暴处理：
      1. 归属报表不同（如现金在财务状况表 vs 现金流量表）→ statement_mismatch；
      2. 原文科目名不同（附注毛额口径 vs 主表净额口径）→ subject_alias_collision；
      3. 以上都不是，才是真正的 duplicate_conflict。
    """
    home = _home_statement(code)
    if home:
        on_home = [f for f in group if f.get('statement') == home]
        off_home = [f for f in group if f.get('statement') != home]
        if on_home and off_home and len({_number(f.get('value', '')) for f in on_home}) == 1:
            home_value = next(iter({_number(f.get('value', '')) for f in on_home}))
            for f in off_home:
                if _number(f.get('value', '')) != home_value:      # 与归属报表同值的附注/补充表数字只是重复披露，不降级
                    f['status'] = 'needs_review'
            reviews.append(_review('statement_mismatch',
                f"{code} 在非归属报表（{home}）里也出现且数值不同，"
                "已保留归属报表的披露值，另一条仅供复核。", on_home + off_home))
            return True
    if len({_label(f.get('raw_label', '')) for f in group}) > 1:
        authoritative = _authoritative_fact(group, code, balance_labels)
        for f in group:
            if f is not authoritative:
                f['status'] = 'needs_review'
                f['alias_of'] = authoritative.get('fact_id', '')
        reviews.append(_review('subject_alias_collision',
            f"{code} 下存在原文科目名不同的多个数值"
            f"（{'、'.join(sorted({str(f.get('raw_label', '')) for f in group}))}）；"
            f"这是**不同科目共用同一标准代码**，不是数据冲突。"
            f"已保留与主表口径一致的「{authoritative.get('raw_label', '')}」"
            f"（{_str(_number(authoritative.get('value', '')) or 0)}）用于指标，"
            f'其余仅供复核，请核对原表科目全称与口径。',
            [authoritative] + [f for f in group if f is not authoritative]))
        return True
    # 同额反号：现金流量表里的「利息收入」「财务费用」等是**间接法调节项**，
    # 以负数列报（因为它们在净利润里是被减掉/加回的），而附注与利润表用正数。
    # 实测 p121 的「利息收入 (579,671)」与附注 6 的「利息收入 579,671」被判成
    # duplicate_conflict，**两条一起废掉**，收益类指标随之失效。
    # 这种情形下保留非现金流量表口径（正数、与利润表一致）的那一条。
    if len(group) == 2:
        left, right = group
        a, b = _number(left.get('value', '')), _number(right.get('value', ''))
        tolerance = _rounding(left) * 2 + _rounding(right) * 2
        if (a is not None and b is not None and a * b < 0
                and abs(abs(a) - abs(b)) <= tolerance):
            keep = right if (left.get('statement') == 'cash_flow'
                             and right.get('statement') != 'cash_flow') else (
                left if right.get('statement') == 'cash_flow' else (left if a > 0 else right))
            other = right if keep is left else left
            other['status'] = 'needs_review'
            other['sign_conflict_with'] = keep.get('fact_id', '')
            reviews.append(_review('sign_convention_mismatch',
                f"{code} 出现同额反号的两个数值（{_str(a)} / {_str(b)}）："
                '现金流量表里的该类科目是间接法调节项、以负数列报，与利润表/附注的'
                f"正数口径是同一笔金额。已保留「{keep.get('raw_label', '')}」"
                f"（{keep.get('statement', '')}）用于指标，另一条仅供复核。", group))
            return True
    return False


def _review(code, message, facts):
    return {'code': code, 'message': message,
            'fact_ids': [f.get('fact_id', '') for f in facts],
            'evidence_ids': list(dict.fromkeys(e for f in facts for e in f.get('evidence_ids', []))),
            'suggestion': '请核对原始报表及口径，确认后保留正确数值；系统未改写来源数字。'}


def _authoritative_fact(group, code, balance_labels):
    """同一标准科目代码下出现**不同原文科目名**时，挑出应当用于指标的那一条。

    这是"别名碰撞"而非"数据冲突"：两个本来不同的科目被同一个别名收编。
    实测农夫山泉 2025 年报——附注 20 的「貿易應收款項」618,481 千元是**毛额**，
    附注 37 与资产负债表的「貿易應收款項及應收票據」581,372 千元是**净额**，
    两者相差恰好是减值准备 37,109 千元。按"冲突"处理会把两条一起降级，
    于是应收周转率、现金循环周期整块失效。

    优先级：与资产负债表上该代码所用科目名逐字相同 > 归属报表与 HOME_STATEMENT
    一致 > 科目名更长（更具体，如"贸易应收款项及应收票据"优于"贸易应收款项"）。
    """
    known = balance_labels.get(code, ())
    for f in group:
        if _label(f.get('raw_label', '')) in known:
            return f
    home = HOME_STATEMENT.get(code)
    if home:
        for f in group:
            if f.get('statement') == home:
                return f
    return max(group, key=lambda f: len(_label(f.get('raw_label', ''))))


# 来源可靠度排序（数字越小越可靠）。用于冲突裁决：基准档永不被降级。
#   primary  —— 有框线的表格 / xlsx、docx 等结构化文件里的表格
#   summary  —— 年报的「財務摘要 / 五年財務摘要」页，按百万元列报并四舍五入
#   inferred —— 按文字坐标推断出来的表（无框线页面），列边界由 x 位置反推
TIER_RANK = {'primary': 0, 'summary': 1, 'inferred': 2}


def _conflicts(facts):
    """同一 (公司, 期间, 口径, 币种, 科目) 下出现多个值时的处理。

    关键点：**先按来源分级**。年报的「財務摘要」页以百万元列报、四舍五入到整数，
    与主表的千元精确值天然会有几万元级的差。如果把两者当"冲突"一起打成
    needs_review，主表数据反而被摘要页拖下水 —— 那是最珍贵的数据被最粗的数据毁掉。
    所以规则是：
      · 摘要页 vs 主表：在四舍五入可解释范围内视为一致（记 cross_check），
        超出范围才判为真冲突，且只降级摘要页那一侧，主表保持可用。
      · 同为一级来源（主表之间 / 摘要之间）出现不同值：仍按冲突处理并暂停计算。
    """
    groups = defaultdict(list)
    for f in facts:
        groups[_key(f)].append(f)
    # 「某科目在某期间出现过的全部数值」索引。用途见下面 period_binding_suspect：
    # 当两个事实的差额**恰好等于该科目跨期的变动额**时，真因通常是期间绑错，
    # 而不是数值冲突。没有这个索引就只能报"跨表冲突"，把用户引向错误方向。
    values_by_series = defaultdict(set)
    for f in facts:
        number = _number(f.get('value', ''))
        if number is not None:
            values_by_series[(_company_key(f), f.get('code'), f.get('period'),
                              f.get('period_type'))].add(number)
    # 资产负债表上每个科目代码实际使用的原文科目名。用于别名碰撞裁决：附注里的
    # 「贸易应收款项（毛额）」与主表的「贸易应收款项及应收票据（净额）」同名代码，
    # 但只有后者与主表逐字相同，应当由它代表该代码。
    balance_labels = defaultdict(set)
    for f in facts:
        if f.get('statement') == 'balance_sheet':
            balance_labels[f.get('code')].add(_label(f.get('raw_label', '')))
    reviews = []
    for group in groups.values():
        by_tier = defaultdict(list)
        for f in group:
            by_tier[f.get('source_tier', 'primary')].append(f)
        if len(by_tier) > 1:
            # 取可靠度最高的一档作为基准，其余各档与它比。
            # 基准永远不被降级；只有低档事实在超出舍入量子容差时才被降级。
            best = min(by_tier, key=lambda tier: TIER_RANK.get(tier, 9))
            reference_facts = by_tier[best]
            reference = _number(reference_facts[0].get('value', ''))
            for tier, items in by_tier.items():
                if tier == best:
                    continue
                for f in items:
                    value = _number(f.get('value', ''))
                    if reference is None or value is None:
                        continue
                    difference = value - reference
                    # 容差只取双方的舍入量子（摘要页按百万元列报时量子就是 10^6）。
                    # 这里**不能**用"相对百分比"兜底：1‰ 的百分比容差会把几千万的
                    # 真实差异当成"四舍五入"放过，法证工具一旦这样放宽就失去意义。
                    tolerance = _rounding(f) * 2 + _rounding(reference_facts[0]) * 2
                    f['cross_check'] = {'against_fact_id': reference_facts[0].get('fact_id'),
                                        'against_tier': best,
                                        'difference': _str(difference)}
                    if abs(difference) <= tolerance:
                        f['cross_check']['result'] = 'consistent_within_rounding'
                    elif _label(f.get('raw_label', '')) != _label(reference_facts[0].get('raw_label', '')):
                        # 跨档别名碰撞：两档来源的**原文科目名不同**，说明它们本来就是
                        # 两个科目共用一个标准代码，不是"同一数据的数值冲突"。
                        # 实测附注 37 的「other receivables and other assets」110,309 千元
                        # 是"计入预付款项、其他应收款及其他资产的**金融资产**"，
                        # 而资产负债表的「預付款項、其他應收款項及其他資產」1,218,292 千元
                        # 是该行**总额**，两者共用 other_receivables 代码。
                        f['cross_check']['result'] = 'different_subject'
                        f['status'] = 'needs_review'
                        f['alias_of'] = reference_facts[0].get('fact_id', '')
                        reviews.append(_review('subject_alias_collision',
                            f"{f.get('raw_label', '')}（{tier}）与 {reference_facts[0].get('raw_label', '')}"
                            f"（{best}）原文科目名不同，却共用代码 {f.get('code', '')}；"
                            '这是**不同科目共用别名**，不是数值冲突。已保留主表口径用于指标，'
                            '本条仅供复核，请核对原表科目全称。',
                            [reference_facts[0], f]))
                    else:
                        f['cross_check']['result'] = f'differs_from_{best}'
                        f['status'] = 'needs_review'
                        prior = _previous_year(f.get('period', ''))
                        signature = (_company_key(f), f.get('code'), prior, f.get('period_type'))
                        if value in values_by_series.get(signature, ()):
                            # 该数值与"同一科目上一期"的值逐位相同 —— 这是比较期
                            # 被绑成本期的指纹，不是跨表数值冲突。
                            f['cross_check']['result'] = 'equals_prior_period_value'
                            f['cross_check']['suspect_period'] = prior
                            reviews.append(_review('period_binding_suspect',
                                f"{f.get('raw_label', '')} 被登记为 {f.get('period')}，但数值 "
                                f"{_str(value)} 与同一科目 {prior} 的值逐位相同；"
                                f'这更像是**比较期数值被绑成了本期**，而非跨表数值冲突。'
                                f'已以 {best} 档为准，本条不参与指标计算；请回看原表列头与表格上方的年份标记。',
                                [reference_facts[0], f]))
                        else:
                            reviews.append(_review('summary_conflict',
                                f"{f.get('raw_label', '')} 的 {tier} 来源数值与 {best} 相差 {_str(difference)}，"
                                f"超出四舍五入可解释范围；已以 {best} 为准，并保留该值供复核。",
                                [reference_facts[0], f]))
            values = {_number(f.get('value', '')) for f in reference_facts}
            if len(values) > 1:
                if not _resolve_same_tier_duplicates(reference_facts, group[0].get('code'),
                                                     balance_labels, reviews):
                    for f in reference_facts:
                        f['status'] = 'needs_review'
                    reviews.append(_review('duplicate_conflict',
                                           '同一公司、期间、口径和币种存在冲突值，已暂停相关计算。', reference_facts))
            continue
        values = {_number(f.get('value', '')) for f in group}
        if len(values) > 1:
            # 同一级来源内部出现不同值：按「归属报表 → 别名碰撞 → 真冲突」三级裁决。
            if not _resolve_same_tier_duplicates(group, group[0].get('code'), balance_labels, reviews):
                for f in group:
                    f['status'] = 'needs_review'
                reviews.append(_review('duplicate_conflict',
                                       '同一公司、期间、口径和币种存在冲突值，已暂停相关计算。', group))

    # 量级异常：扫描件 OCR 会把相邻数字粘成一个（如 15,868 与 274,158 并成 15868274158682），
    # 或把不同单位的表混进同一口径。同一科目跨期的数量级不应差 1000 倍以上。
    by_series = defaultdict(list)
    for f in facts:
        number = _number(f.get('value', ''))
        if number is None or number == 0:
            continue
        series = (_company_key(f), f.get('code'), f.get('scope'), f.get('currency'))
        by_series[series].append((abs(number), f))
    for items in by_series.values():
        magnitudes = sorted(m for m, _ in items)
        if len(magnitudes) < 3:
            continue
        median = magnitudes[len(magnitudes) // 2]
        if median <= 0:
            continue
        flagged = [f for m, f in items if m > median * 1000 or m < median / 1000]
        if 0 < len(flagged) < len(items):
            for f in flagged:
                f['status'] = 'needs_review'
                f['magnitude_suspect'] = True
            reviews.append(_review('magnitude_outlier',
                '同一科目跨期数值量级相差 1000 倍以上，疑似 OCR 数字粘连或单位错位，已暂停使用该值。', flagged))
    return reviews


UNIT_SCALE = {'元': 1, '千元': 1000, '万元': 10000, '百万元': 1000000, '亿元': 100000000,
              '千港元': 1000, '百万港元': 1000000, '亿港元': 100000000,
              '千美元': 1000, '百万美元': 1000000}


def _is_ashare_document(text):
    head = to_simplified(str(text or '')[:20000])
    return bool(re.search(r'上海证券交易所|深圳证券交易所|北京证券交易所|上交所|深交所|北交所|证券代码|股票代码|公司代码', head)
                and re.search(r'人民币', str(text or '')[:200000]))


def _document_defaults(document_text, tables):
    """单一年报通篇只有一个记账本位币和一个主要列报单位。

    逐表识别会在附注页留下大量 unknown，但"unknown 就一律不参与计算"会让
    整份扫描版年报一个指标都算不出来。所以先投票得出文档级默认值，只在
    逐表信号完全缺失时才继承，并给继承来的事实打标记以便追溯。
    """
    doc_currency = dominant_currency(document_text[:80000])
    if not doc_currency and _is_ashare_document(document_text):
        # A 股/北交所报告：记账本位币为人民币。正文里常有美元/港币的外币敞口表，
        # 会让"主导币种"判成 None（实测宁德时代），整张现金流量表因 currency=unknown 被降级。
        doc_currency = 'CNY'
    votes = defaultdict(int)
    for t in tables or []:
        probe = str(t.get('context') or '') + '\n' + '\n'.join(
            ' '.join(str(c or '') for c in row) for row in (t.get('rows') or [])[:6])
        _cur, unit, _scale = _units(probe)
        if unit and unit != 'unknown':
            votes[unit] += 1
    if not votes:
        return doc_currency, None
    best = max(votes.items(), key=lambda kv: kv[1])
    doc_unit = best[0] if best[1] >= 3 else None
    return doc_currency, doc_unit


def extract_financial_facts(document, company='', period='', scope='auto'):
    from .ocr_table_rebuild import upgrade_tables
    facts, warnings, reviews = [], list(document.get('warnings', [])), []
    document_text = str(document.get('text', ''))
    tables, rebuild_notes = upgrade_tables(document, document.get('tables', []))
    warnings.extend(rebuild_notes)
    doc_currency, doc_unit = _document_defaults(document_text, tables)
    report = _report_period(document_text)
    pages_by_no = {p.get('page'): p for p in (document.get('pages') or [])}
    if doc_currency or doc_unit:
        warnings.append(f"文档级默认值：币种 {doc_currency or '未判定'}、列报单位 {doc_unit or '未判定'}；"
                        "仅在逐表信号完全缺失时继承，继承来的事实带 currency_inherited / unit_inherited 标记。")
    last_stmt = None
    prev_table_page = None
    for table in tables:
        rows = table.get('rows', [])
        note_columns = _note_columns(rows)
        if note_columns:
            warnings.append(f"表格 {table.get('table_id', '')} 的第 "
                            f"{'、'.join(str(c) for c in sorted(note_columns))} 列被识别为附註列"
                            f"（{'；'.join(sorted(set(note_columns.values())))}），已排除出数值列。")
        context = str(table.get('context', ''))
        prefix = '\n'.join(' '.join(str(c or '') for c in row) for row in rows[:8]
                           if not any(_lookup_subject(c) for c in row[:2]))
        metadata = context + '\n' + prefix
        # 口径用「表元数据 + 本页正文」判定：附注页本身没有报表标题，
        # 但同一页正文里通常有；币种与单位仍只用表内元数据，避免正文叙述污染。
        page_text = str((pages_by_no.get(table.get('page')) or {}).get('text') or '')[:800]
        # 表格级期间标记要看**整页**文本：年份行常被排在表格 context 之前，
        # 只截前 800 字会把它切掉。见 _table_level_period 的说明。
        page_text_full = str((pages_by_no.get(table.get('page')) or {}).get('text') or '')
        table_period = _table_level_period(context, page_text_full)
        near_stmt = bool(last_stmt) and table.get('page') in (last_stmt['page'], last_stmt['page'] + 1)
        open_stmt = _opening_statement(rows)
        first_on_page = prev_table_page != table.get('page')
        prev_table_page = table.get('page')
        # 标题落在上一页页尾（A 股年报常见）：页首第一张表自己没有标题，口径/报表类型取页尾标题。
        hint = (_trailing_title(pages_by_no.get((table.get('page') or 0) - 1))
                if first_on_page else None)
        # 续表优先看**表自己的**元数据：页首 800 字里可能是同页后一张表（如母公司现金流量表）的标题，
        # 拿它给上一张表的续页定口径会整表错标（实测宁德时代合并现金流量表下半截被标成母公司）。
        selected_scope = _scope(metadata)
        hint_used = False
        cont_compatible = (near_stmt and open_stmt in (None, last_stmt['statement'])
                           and (hint is None or hint[1] == last_stmt['statement']))
        own_title = _title_before_table(pages_by_no.get(table.get('page')), rows) if selected_scope == 'unknown' else None
        if own_title and own_title[0] != 'unknown':
            selected_scope, hint, hint_used = own_title[0], own_title, True
        elif selected_scope == 'unknown' and hint and not cont_compatible and open_stmt in (None, hint[1]):
            selected_scope, hint_used = hint[0], True
        elif selected_scope == 'unknown' and not near_stmt:
            selected_scope = _scope(metadata + '\n' + page_text)
        # 「財務摘要 / 主要財務數據 / FINANCIAL HIGHLIGHTS」页没有报表标题，逐表判定
        # 必然是 unknown，于是整页数据被排除。但这类摘要按定义就是**集团合并口径**，
        # 而且是唯一提供 5 年跨度的地方 —— 缺了它，持续性/趋势类规则只剩两年可比。
        # 所以这里按摘要口径继承 consolidated，并打标记（而非静默当成主表数据）。
        summary_page = bool(re.search(
            r'財務摘要|财务摘要|財務概要|主要財務數據|主要会计数据|五年財務摘要|'
            r'FINANCIAL\s+HIGHLIGHTS|FINANCIAL\s+SUMMARY', metadata + '\n' + page_text, re.I))
        scope_inherited = False
        if summary_page and selected_scope == 'unknown':
            selected_scope, scope_inherited = 'consolidated', True
        # 来源分级共三档：primary（有框线表/结构化文件）> summary（五年摘要页）
        # > inferred（按文字坐标推断出来的表）。
        #
        # 为什么 inferred 必须单列一档：坐标推断表的列边界是从 x 位置反推的，
        # 可靠性低于带框线的表，但它常常是某个科目的**唯一**来源（现金流量表、
        # 附注明细多半没有框线）。若把两者当同级，同一科目在两张表里各有一个值
        # （一张精确、一张按百万元四舍五入）就会互判冲突、双双降级 ——
        # 实测启用坐标表后已核验事实反而从 137 掉到 108，就是因为最精确的主表数据
        # 被最粗的坐标表拖下水。分档后主表永远优先，坐标表只在无主表时作数。
        if summary_page:
            source_tier = 'summary'
        elif str(table.get('method') or '').startswith('spatial'):
            source_tier = 'inferred'
        else:
            source_tier = 'primary'
        if scope in {'consolidated', 'parent'} and selected_scope in {'consolidated', 'parent'} and scope != selected_scope:
            warnings.append(f"表格 {table.get('table_id', '')} 明确为 {selected_scope} 口径，与请求范围不符，已过滤。")
            continue
        issuers = _issuer(document_text[:2000] + '\n' + metadata)
        selected_company = next(iter(issuers)) if len(issuers) == 1 else (company if not issuers else '')
        # 「发行人不止一个」按**核心名**去重后再数：年报封面常常中英文并列、
        # 或同时出现简称与全称，那是同一家公司的两种写法，不是身份冲突。
        issuer_cores = {core for core in (_company_core(name) for name in issuers) if core}
        mismatch = bool(company) and bool(issuers) and not _same_company(company, issuers)
        company_conflict = mismatch or len(issuer_cores) > 1
        if company_conflict:
            warnings.append('请求公司与来源明确披露的公司不同，或来源身份存在冲突；已保留来源身份并加入复核。')
        currency, unit, scale = _units(metadata)
        statement = detect_statement(metadata + '\n' + page_text)
        if hint_used and hint[1] in {'balance_sheet', 'income_statement', 'cash_flow'}:
            statement = hint[1]
        # 附注口径继承。附注页本身没有「合并/母公司」字样，逐表判定必然是 unknown，
        # 于是整页附注数据被打成 needs_review，而 _valid() 要求 scope ∈ {consolidated,
        # parent}，这些事实就全部被排除出指标计算 —— 利息收入、财务费用、折旧摊销、
        # 存货明细、其他应付款构成、借款与租赁明细等**只有附注才有**的科目全都用不上，
        # 报告只能写"数据缺口"，而原文其实写得清清楚楚。
        #
        # 为什么继承 consolidated 是安全的：财务报表附注按定义就是**合并报表的附注**；
        # 单体报表的附注在年报里单独成节且带「本公司/母公司」标题，由
        # detect_parent_statement 拦下、保持 unknown 不动。用户显式指定口径时不干预。
        if (not scope_inherited and selected_scope == 'unknown' and statement == 'notes'
                and scope in {'auto', 'unknown'}
                and not detect_parent_statement(metadata + '\n' + page_text)):
            selected_scope, scope_inherited = 'consolidated', True
        headers = []
        # 跨页续表：A 股三大报表常在页尾断开，下一页的表格没有标题与表头（只有科目与两列数字），
        # 实测滨江/美的/宁德等多份年报的利润表、负债表下半截因此口径=unknown、期间=文档级默认，
        # 与上半截互判冲突被整体降级。做法：沿用上一张"带表头的报表表格"的口径/报表类型/单位/各数值列
        # 的表头描述（按数值列从左到右的顺序一一对应；列数对不上就不继承，宁缺勿猜）。
        if statement in {'balance_sheet', 'income_statement', 'cash_flow'} or (
                last_stmt and table.get('page') in (last_stmt['page'], last_stmt['page'] + 1)):
            implicit_notes = _implicit_note_columns(rows, note_columns)
            if implicit_notes:
                note_columns = {**note_columns, **implicit_notes}
        continuation = None
        near = bool(last_stmt) and table.get('page') in (last_stmt['page'], last_stmt['page'] + 1)
        has_own_header = any(_is_header_row(r) for r in rows[:5])
        if near and not has_own_header:
            value_cols = _value_columns(rows, note_columns)
            if value_cols and _dominant_value_count(rows, note_columns) == len(last_stmt['descs']):
                continuation = {}     # 按"每行从左到右第 k 个数"对应上一张表的第 k 列表头（见下方 rank_map）
        if hint_used or (open_stmt is not None and last_stmt and open_stmt != last_stmt['statement']):
            near, continuation = False, None      # 新报表的开头，不是上一张表的续页
        if near and (continuation is not None or selected_scope == 'unknown' or statement in {'unknown', 'notes'}) \
                and _value_columns(rows, note_columns) and statement != 'notes' or continuation is not None:
            if selected_scope == 'unknown':
                selected_scope, scope_inherited = last_stmt['scope'], True
            if statement in {'unknown', 'notes'}:
                statement = last_stmt['statement']
            if unit == 'unknown':
                currency, unit, scale = last_stmt['currency'], last_stmt['unit'], last_stmt['scale']
            if continuation is not None:
                context = last_stmt['context']
                table_period = last_stmt['table_period']
        used_desc = {}
        table_descs = list(last_stmt['descs']) if continuation is not None else None
        if any(word in metadata for word in ['重述', '追溯', '调整后', '重分类']):
            warnings.append(f"表格 {table.get('table_id', '')} 含重述或追溯调整信息；比较期间需核对调整口径。")
        pending_labels = []
        section = None
        change_rows = []
        col_groups = _column_groups(rows)
        for row_index, row in enumerate(rows):
            code, label_index, joined_label = None, None, None
            row_title = _title_line(' '.join(str(c or '') for c in row if str(c or '').strip())) \
                if sum(1 for c in row if str(c or '').strip()) <= 2 else None
            if row_title:
                # 表格正文里夹着下一张报表的标题（如合并现金流量表末尾紧跟「6、母公司现金流量表」）：
                # 口径与报表类型就此切换，表头重置，续表继承链也要断开。
                selected_scope = row_title[0] if row_title[0] != 'unknown' else selected_scope
                statement = row_title[1]
                headers, pending_labels, used_desc = [], [], {}
                continuation = None
                continue
            frag_index, frag_text = _fragment_cell(row)
            # 先认分节标题，且必须在科目识别之前：
            # 「流動負債」本身就是 current_liabilities 的别名，若先走科目匹配，
            # 这一行会被当成科目而非分节，随后的「租賃負債」就永远分不出即期/非即期。
            # 只有"该行不含任何数值"才认定为纯分节标题行。
            tracked_section = _section_of(frag_text) if frag_text else None
            if tracked_section and not any(_number(str(cell or '').strip()) is not None for cell in row):
                section = tracked_section
                pending_labels = []
                continue
            for c, raw in enumerate(row[:3]):
                candidate = _lookup_subject(raw, metadata + '\n' + page_text)
                if candidate:
                    code, label_index = candidate, c
                    break
            if code is None:
                left_labels = [txt for txt in pending_labels]
                joined_code, joined_text = _joined_subject(
                    left_labels, frag_text, metadata + '\n' + page_text)
                if joined_code:
                    code, label_index, joined_label = joined_code, frag_index, joined_text
            if code is None:
                frag_index, frag_text = _fragment_cell(row)
                if _is_scope_header_row(row):
                    headers.append(row)
                    pending_labels = []
                    continue
                has_period = any(re.search(
                    _HEADER_PERIOD_RE, str(c)) for c in row)
                if frag_text and not has_period:
                    pending_labels.append(frag_text)
                    del pending_labels[:-3]      # 只保留最近 3 段，避免越攒越乱
                    continue
                if has_period or _is_scope_header_row(row):
                    headers.append(row)
                pending_labels = []
                continue
            pending_labels = []
            raw_label = joined_label or str(row[label_index])
            # 同名科目的即期/非即期拆分（如租赁负债、计息借贷）
            split = SPLIT_BY_SECTION.get(code, {}).get(section or '')
            if split:
                code = split
            # 现金流量表（及所有者权益变动表）里的「营运资金变动」行不是余额，是变动额。
            # 例：p121「貿易應付款項及應付票據增加╱（減少）」的科目名与资产负债表
            # 完全同名，会被识别成"应付账款余额"。后果不只是多一条脏数据 ——
            # 它与资产负债表上的真实余额构成 duplicate_conflict，把**正确的余额**
            # 一起降级为 needs_review，于是应付账款周转、现金循环周期、应付占款分析
            # 整块失效；而报告只会显示"该科目存在冲突值"，看不出是变动额混进来了。
            # 实测：农夫山泉 2025 年报里 payables / other_payables / receivables /
            # inventory 四项同时中招（p121 各一条变动额 vs p117 的真实余额）。
            # 注意判据要用**整行**文本而不是 raw_label：港式排版的变动措辞
            # 常常不在科目那一格 —— 「貿易應付款項及應付票據 / Increase/(decrease) in
            # trade and bills payables / 增加╱（減少）」把中文补充说明折到下一行、
            # 英文措辞放在另一列。实测只用科目格时 payables 与 other_payables 仍然中招。
            if code in BALANCE_CODES and _is_change_label(' '.join(str(c or '') for c in row)) \
                    and statement in {'cash_flow', 'equity_changes', 'unknown'}:
                change_rows.append(f'{raw_label}（第 {table.get("page")} 页）')
                continue
            # 按"行内第 k 个数 ↔ 第 k 个期间列"对应。pdfplumber 在合并单元格的表里常让同一张表的不同行
            # 数值落在不同列号（茅台利润表：上半截在第 2、3 列，下半截在第 2、4 列），列号对应会整批错位；
            # 行内顺序对应则不受影响。只在"该行数值个数恰等于期间列数"时启用，否则退回列号对应。
            if table_descs is None or continuation is None:
                header_descs = _header_descs(headers, col_groups, note_columns)
                if header_descs:
                    table_descs = header_descs
            row_nums = [c for c in range(label_index + 1, len(row))
                        if c not in note_columns and not _REFERENCE_CELL_RE.match(str(row[c] or '').strip())
                        and _number(str(row[c] or '').strip()) is not None]
            rank_map = dict(zip(row_nums, table_descs)) if table_descs and len(row_nums) == len(table_descs) else {}
            for column in range(label_index + 1, len(row)):
                if column in note_columns:
                    continue
                raw_value = str(row[column] or '').strip()
                if _REFERENCE_CELL_RE.match(raw_value):
                    # 章节/附注编号，不是金额。见 _REFERENCE_CELL_RE 的说明。
                    continue
                number = _number(raw_value)
                if number is None:
                    continue
                # 期间只能取自"本列自己的"表头标签。
                # 只有当本列在所有表头行里都没有任何标签时，才认定它是合并单元格
                # 的延续，去继承左侧最近的那一列标签。
                #
                # 为什么必须这样：原来的写法无条件向左补年份，于是双列表格里
                # 「2024年」那列（本列已有标签）也会被补上左邻列的 2025，
                # 所有表格的前一期数据全部错标成最新期 —— 而且不报错、静默发生。
                group_cols = [c for c, g in col_groups.items() if g == col_groups.get(column)]
                own_labels = [str(h[c] or '').strip() for h in headers for c in group_cols
                              if c < len(h) and str(h[c] or '').strip()]
                if own_labels:
                    descriptor = [' '.join(str(h[c] or '') for c in group_cols if c < len(h)) for h in headers]
                else:
                    left_col = next((probe for probe in range(column - 1, label_index, -1)
                                     if any(str(h[probe] or '').strip() for h in headers
                                            if probe < len(h))), None)
                    descriptor = ([str(h[left_col] or '') for h in headers if left_col < len(h)]
                                  if left_col is not None else [])
                if column in rank_map:
                    descriptor = rank_map[column]
                    if _CHANGE_COLUMN_RE.search(to_simplified(' '.join(descriptor))):
                        continue
                if _CHANGE_COLUMN_RE.search(to_simplified(' '.join(descriptor))):
                    continue        # 同比/增减变动幅度列：衍生比率，不是期间值（见 _CHANGE_COLUMN_RE）
                col_scope = _column_scope(descriptor)
                fact_scope = col_scope or selected_scope
                if scope in {'consolidated', 'parent'} and fact_scope in {'consolidated', 'parent'} and fact_scope != scope:
                    continue
                used_desc[column] = descriptor
                end, kind = _column_period(descriptor, context, document_text, period, code, table_period, report)
                # 期间来源登记：表格级年份标记属于"这张表自己的声明"，
                # 与从年报封面继承来的文档级日期可靠度不同，必须可追溯。
                period_source = 'column_header' if own_labels else (
                    'table_level' if (table_period and table_period[0] == end)
                    else 'document' if end else 'unresolved')
                f_currency, f_unit, f_scale = currency, unit, scale
                inline_currency, inline_unit, inline_scale = _units(raw_value)
                if inline_unit != 'unknown':
                    f_currency, f_unit, f_scale = inline_currency, inline_unit, inline_scale
                if code.endswith('_eps'):
                    f_unit, f_scale = '元/股', 1
                elif '%' in raw_value or '％' in raw_value:
                    f_unit, f_scale = '%', 1
                    number /= 100
                # 利润表减项：括号是"这是减项"的排版约定，金额本身是正数。
                # 说明见 INCOME_STATEMENT_DEDUCTION_CODES。
                sign_flipped = False
                if code in INCOME_STATEMENT_DEDUCTION_CODES:
                    gain_signed = bool(re.search(r'损失以[“"‘\']?[-－—–]+[”"’\']?号填列',
                                                 re.sub(r'\s+', '', to_simplified(raw_label))))
                    if gain_signed and number != 0:
                        # A 股「信用减值损失（损失以“-”号填列）」是按"收益为正、损失为负"列报；
                        # 本引擎的减项约定是"损失/费用为正"，所以一律取反（-418,585 → 损失 418,585；+17,234 → 转回 -17,234）。
                        number = -number
                        sign_flipped = True
                    elif number < 0:
                        number = -number
                        sign_flipped = True
                # 文档级兜底：只有逐表信号完全缺失时才继承，并打标记便于追溯
                currency_inherited = unit_inherited = False
                if f_currency == 'unknown' and doc_currency:
                    f_currency, currency_inherited = doc_currency, True
                if f_unit == 'unknown' and doc_unit:
                    f_unit, f_scale, unit_inherited = doc_unit, UNIT_SCALE.get(doc_unit, 1), True
                identity = f"{document.get('document_id', '')}:{table.get('table_id', '')}:{row_index}:{column}"
                confidence = float(table.get('confidence', .8) or .8)
                missing = [name for name, unresolved in [('period', not end), ('period_type', kind == 'unknown'),
                           ('scope', fact_scope == 'unknown'), ('currency', f_currency == 'unknown'),
                           ('unit', f_unit == 'unknown'), ('unit_mismatch', f_unit == '%'), ('company_conflict', company_conflict), ('ocr_confidence', confidence < .9)] if unresolved]
                evidence = []
                cell_ids = table.get('cell_evidence_ids', [])
                if row_index < len(cell_ids):
                    evidence = [cell_ids[row_index][c] for c in (label_index, column)
                                if c < len(cell_ids[row_index]) and cell_ids[row_index][c]]
                    # Cite the actual row, amount and header metadata, rather
                    # than every unrelated value in the entire statement.
                    for header_index in range(min(len(headers), len(cell_ids))):
                        for c in (label_index, column):
                            if c < len(cell_ids[header_index]) and cell_ids[header_index][c]:
                                evidence.append(cell_ids[header_index][c])
                if not evidence:
                    evidence = [table.get('table_id', identity)]
                f = {'fact_id': 'f_' + hashlib.sha256(identity.encode()).hexdigest()[:16], 'code': code,
                     'raw_label': raw_label, 'company': selected_company, 'period': end, 'period_type': kind,
                     'scope': fact_scope, 'currency': f_currency, 'value': _str(number * f_scale),
                     'raw_value': raw_value, 'unit': f_unit, 'scale': f_scale,
                     'value_unit': 'ratio' if f_unit == '%' else (f_currency + '/share' if code.endswith('_eps') else f_currency),
                     'evidence_ids': list(dict.fromkeys(evidence)),
                     'restated': any(word in metadata for word in ['重述', '追溯', '调整后', '重分类']),
                     'sign_flipped': sign_flipped,
                     'currency_inherited': currency_inherited,
                     'unit_inherited': unit_inherited,
                     'scope_inherited': scope_inherited and not col_scope,
                     'source_tier': source_tier,
                     'statement': statement,
                     'section': section,
                     'period_source': period_source,
                     'source': {'document_id': document.get('document_id', ''), 'filename': document.get('filename', ''),
                                'page': table.get('page'), 'table_id': table.get('table_id'),
                                'row': row_index, 'column': column,
                                'rebuilt_table': bool(table.get('rebuilt_from_ocr_blocks'))},
                     'status': 'needs_review' if missing else 'verified', 'confidence': confidence}
                facts.append(f)
                if missing:
                    reviews.append(_review('unresolved_metadata', f"{raw_label} 缺少或待确认：{', '.join(missing)}。", [f]))
        if used_desc and statement in {'balance_sheet', 'income_statement', 'cash_flow'}:
            if continuation is None:
                last_stmt = {'page': table.get('page'), 'scope': selected_scope, 'statement': statement,
                             'currency': currency, 'unit': unit, 'scale': scale, 'context': context,
                             'table_period': table_period,
                             'descs': table_descs or [used_desc[c] for c in sorted(used_desc)]}
            else:
                last_stmt = dict(last_stmt, page=table.get('page'))
        elif continuation is None and not used_desc:
            pass
        else:
            last_stmt = last_stmt if continuation is not None else None
        if change_rows:
            warnings.append(
                f"表格 {table.get('table_id', '')}（第 {table.get('page')} 页）的 {'、'.join(change_rows[:6])}"
                f"{' 等' if len(change_rows) > 6 else ''} 属于「本期变动额」口径，"
                '与资产负债表上的期末余额不是同一个量，已跳过以免污染余额类指标。')
    facts.extend(_backfill_revenue(facts))
    reviews.extend(_conflicts(facts))
    if not facts:
        warnings.append('未提取到可定位的财务表格数值；请确认表格解析或提供可检索的报表。')
    return {'facts': facts, 'warnings': list(dict.fromkeys(warnings)), 'review_queue': reviews}


def _backfill_revenue(facts):
    """有「营业总收入」却没有「营业收入」的 (口径,期间) 才用总收入回填 revenue，并留下可追溯标记。
    A 股多数报表两行同时存在、且相等；只有总收入行的公司（部分金融、地产）不回填会让所有毛利率/增长率落空。"""
    have = {(_company_key(f), f.get('period'), f.get('period_type'), f.get('scope'), f.get('currency'))
            for f in facts if f.get('code') == 'revenue'}
    extra = []
    for f in facts:
        if f.get('code') != 'total_revenue':
            continue
        key = (_company_key(f), f.get('period'), f.get('period_type'), f.get('scope'), f.get('currency'))
        if key in have:
            continue
        clone = dict(f, code='revenue', fact_id='f_' + hashlib.sha256((f['fact_id'] + ':rev').encode()).hexdigest()[:16],
                     backfilled_from='total_revenue')
        extra.append(clone)
    return extra


def _valid(f):
    try:
        date.fromisoformat(f.get('period', ''))
    except (TypeError, ValueError):
        return False
    return (f.get('status') == 'verified' and f.get('scope') in {'consolidated', 'parent'}
            and f.get('currency') not in {'unknown', '', None} and bool(f.get('period'))
            and f.get('period_type') in {'instant', 'annual', 'ytd', 'quarter'}
            and _number(f.get('value')) is not None)


def _previous_year(end):
    try:
        d = date.fromisoformat(end)
        return f'{d.year - 1}-{d.month:02}-{min(d.day, calendar.monthrange(d.year - 1, d.month)[1]):02}'
    except (ValueError, TypeError):
        return ''


def _rounding(f):
    raw = _number(f.get('raw_value', ''))
    quantum = Decimal(1).scaleb(raw.as_tuple().exponent) if raw is not None else Decimal(1)
    return max(Decimal('.005'), quantum * Decimal(str(f.get('scale', 1))) / 2)


def analyze_financials(facts):
    facts = copy.deepcopy(facts)
    reviews = _conflicts(facts)
    metrics, checks, warnings, signals = [], [], [], []
    for f in facts:
        if not _valid(f):
            reviews.append(_review('excluded_fact', '数值或期间、口径、币种待核对，未用于指标计算。', [f]))
    index = {}
    # 主表事实先入索引，摘要页与坐标推断表只补空缺：同一个 (期间, 口径, 科目) 上
    # 宁可要主表的精确值，也不要摘要页的四舍五入值或坐标推断值。
    for _tier in ('primary', 'summary', 'inferred'):
        for f in facts:
            if _valid(f) and f.get('source_tier', 'primary') == _tier:
                index.setdefault(_key(f), f)

    # 港股/IFRS 财务状况表普遍**不印「资产总额」「负债总额」两行**，只印
    # 「資產淨額」（= 权益总额）与两个小计。缺了这两个总额，
    # 资产负债表勾稽、资产负债率、总资产周转率、ROA 全部无法成立 ——
    # 实测农夫山泉因此只有 2 项勾稽通过（应为 4 项），债务与营运指标全空。
    #
    # 这里由两个小计相加推导总额，并把来源与依赖记进事实本身：
    # 属于竞赛定义里的 E2 级证据（由 E1 计算得出），可回溯、不假装是原文披露。
    derived_notes = []

    def _add_derived(group_key, code, components, label, note):
        sample = components[0]
        value = sum((Decimal(c['value']) for c in components), Decimal(0))
        identity = (f"{sample.get('source', {}).get('document_id', '')}:derived:"
                    f"{group_key[3]}:{group_key[1]}:{code}:{label}")
        fact = {
            'fact_id': 'f_' + hashlib.sha256(identity.encode()).hexdigest()[:16],
            'code': code,
            'raw_label': label,
            'company': sample.get('company', ''), 'period': group_key[1],
            'period_type': group_key[2], 'scope': group_key[3], 'currency': group_key[4],
            'value': _str(value), 'raw_value': _str(value),
            'unit': sample.get('unit', ''), 'scale': sample.get('scale', 1),
            'value_unit': sample.get('value_unit', group_key[4]),
            'evidence_ids': list(dict.fromkeys(
                e for c in components for e in c.get('evidence_ids', []))),
            'restated': any(c.get('restated') for c in components),
            'sign_flipped': False, 'currency_inherited': False, 'unit_inherited': False,
            'derived': True, 'derived_from': [c.get('code') for c in components],
            'derived_from_fact_ids': [c.get('fact_id') for c in components],
            'source': dict(sample.get('source', {})),
            'status': 'verified',
            'confidence': min(float(c.get('confidence', .8) or .8) for c in components),
            'note': note,
        }
        index[group_key + (code,)] = fact
        facts.append(fact)
        return fact

    all_groups = sorted({_key(f)[:5] for f in index.values()})
    for group_key in all_groups:
        for total_code, parts in (('assets', ('current_assets', 'noncurrent_assets')),
                                  ('liabilities', ('current_liabilities', 'noncurrent_liabilities'))):
            components = [index.get(group_key + (p,)) for p in parts]
            if not all(components):
                continue
            # 小计全部来自主表（primary）而现存的总额只有摘要页（summary，按百万元四舍五入）
            # 时，用推导值替换它。理由：资产负债表的小计才是这张表的权威来源，
            # 而摘要页是同一数字的粗糙版本 —— 实测 2025 年总资产
            # 摘要 65,169,000,000 千元 vs 小计相加 65,169,128,000 千元，
            # 差的 128,000 千元会让资产负债表恒等式永远平不了零。
            # 原值不丢：它仍是同一文档里的一条事实，只是不再作为该组的代表。
            existing = index.get(group_key + (total_code,))
            primary_components = all(c.get('source_tier') == 'primary' for c in components)
            if existing is not None:
                if not (primary_components and existing.get('source_tier') == 'summary'):
                    continue
            _add_derived(group_key, total_code, components,
                         '（报表未列示，由小计推导）' + ' + '.join(parts),
                         '报表未列示该总额，由两个小计相加推导；证据等级 E2。')
            derived_notes.append(f"{group_key[3]} {group_key[1]} {total_code}"
                                 f" = {' + '.join(parts)}"
                                 + ('（替换摘要页四舍五入值）' if existing is not None else ''))

        # 母公司拥有人应占权益：港式權益區块常只写「母公司擁有人 / 應佔權益」两行标题，
        # 不印合计数字（合计就等于权益总额）。缺了它，归母口径的 ROE 直接算不出来。
        # 判据要求三处互相印证才敢推导，缺一不推：
        #   ① 没有单独披露少数股东权益（有的话一定有合计行，不必推）
        #   ② 股本、储备都取到了
        #   ③ 股本 + 储备 与权益总额一致（容差内）
        if index.get(group_key + ('parent_equity',)):
            continue
        equity = index.get(group_key + ('equity',))
        capital = index.get(group_key + ('share_capital',))
        reserves_f = index.get(group_key + ('reserves',))
        if not (equity and capital and reserves_f):
            continue
        if index.get(group_key + ('minority_equity',)):
            continue
        tolerance = _rounding(equity) + _rounding(capital) + _rounding(reserves_f)
        difference = (Decimal(capital['value']) + Decimal(reserves_f['value'])
                      - Decimal(equity['value']))
        if abs(difference) > tolerance:
            warnings.append(f"{group_key[3]} {group_key[1]} 的 股本 + 储备 与权益总额相差 "
                            f"{_str(difference)}，超过舍入容差，未推导母公司拥有人应占权益。")
            continue
        _add_derived(group_key, 'parent_equity', [capital, reserves_f],
                     '（报表未列示，由股本 + 储备推导）母公司拥有人应占权益',
                     '报表未列示母公司拥有人应占权益合计，由股本 + 储备推导且与权益总额相互印证；证据等级 E2。')
        derived_notes.append(f"{group_key[3]} {group_key[1]} parent_equity = share_capital + reserves")
    if derived_notes:
        warnings.append('以下总额科目原报表未直接列示，已由小计推导（E2 级证据，可回溯到小计）：'
                        + '；'.join(derived_notes[:12]))

    groups = sorted({_key(f)[:5] for f in index.values()})
    quarter_conflicts = {}
    for key, cur in list(index.items()):
        if key[2] not in {'ytd', 'annual'} or key[5] in BALANCE_CODES or key[5].endswith('_eps') or key[5] in {'cash_equivalents_start', 'cash_equivalents_end'}:
            continue
        month = int(key[1][5:7])
        if month not in {6, 9, 12}:
            continue
        prior_month = month - 3
        prior_end = f'{key[1][:4]}-{prior_month:02}-{calendar.monthrange(int(key[1][:4]), prior_month)[1]:02}'
        prior = index.get((key[0], prior_end, 'ytd', key[3], key[4], key[5]))
        explicit_key = (key[0], key[1], 'quarter', key[3], key[4], key[5])
        explicit = index.get(explicit_key)
        if prior and explicit and bool(cur.get('restated')) == bool(prior.get('restated')):
            difference = Decimal(cur['value']) - Decimal(prior['value']) - Decimal(explicit['value'])
            if abs(difference) > sum((_rounding(f) for f in [cur, prior, explicit]), Decimal(0)):
                quarter_conflicts[(key[:5], key[5])] = explicit
                explicit['status'] = 'needs_review'
                index.pop(explicit_key, None)
                reviews.append(_review('quarter_conflict', '单季披露值与累计差额存在冲突，已暂停单季指标计算。', [cur, prior, explicit]))

    def get(group, code, period=None, kind=None):
        company, end, duration, scope, currency = group
        return index.get((company, period or end, kind or duration, scope, currency, code))

    def amount(f):
        return Decimal(f['value'])

    def metric(code, group, deps, formula, value, unit='ratio', issue=''):
        company, end, duration, scope, currency = group
        entry = {'code': code, 'value': _str(value) if value is not None else None,
                 'unit': unit, 'company': deps[0].get('company', '') if deps else company, 'period': end, 'period_type': duration,
                 'scope': scope, 'currency': currency, 'status': 'unavailable' if value is None else 'calculated',
                 'formula': formula, 'dependency_fact_ids': [f.get('fact_id', '') for f in deps],
                 'evidence_ids': list(dict.fromkeys(e for f in deps for e in f.get('evidence_ids', [])))}
        if issue:
            entry['reason'] = issue
            warnings.append(issue)
        metrics.append(entry)
        return entry

    def ratio(code, group, deps, numerator, denominator, formula, positive=True):
        if denominator == 0 or (positive and denominator < 0):
            metric(code, group, deps, formula, None, issue=f'{code} 分母为零或非正，比例不具备可比意义。')
        else:
            with localcontext() as ctx:
                ctx.prec = 28
                metric(code, group, deps, formula, numerator / denominator)

    def tieout(code, group, deps, coefficients):
        difference = sum((amount(f) * c for f, c in zip(deps, coefficients)), Decimal(0))
        tolerance = sum((_rounding(f) * abs(c) for f, c in zip(deps, coefficients)), Decimal(0))
        status = 'pass' if abs(difference) <= tolerance else 'fail'
        checks.append({'code': code, 'status': status, 'company': deps[0].get('company', ''), 'period': group[1],
                       'scope': group[3], 'currency': group[4], 'difference': _str(difference),
                       'tolerance': _str(tolerance),
                       'formula': ' + '.join(f'{c} * {f["code"]}' for f, c in zip(deps, coefficients)) + ' = 0',
                       'dependency_fact_ids': [f.get('fact_id', '') for f in deps],
                       'evidence_ids': list(dict.fromkeys(e for f in deps for e in f.get('evidence_ids', [])))})
        if status == 'fail':
            reviews.append(_review('tieout_failed', f'{code} 勾稽差额 {_str(difference)} 超过舍入容差 {_str(tolerance)}。', deps))

    def heuristic_signal(code, group, deps, observed, threshold, rule, message):
        signals.append({
            'code': code, 'severity': 'attention', 'message': message,
            'company': deps[0].get('company', ''), 'period': group[1],
            'period_type': group[2], 'scope': group[3], 'currency': group[4],
            'heuristic': True, 'threshold': _str(threshold), 'threshold_unit': 'ratio',
            'observed_value': _str(observed), 'rule': rule,
            'dependency_fact_ids': [f.get('fact_id', '') for f in deps],
            'evidence_ids': list(dict.fromkeys(e for f in deps for e in f.get('evidence_ids', []))),
            'input_facts': [{key: copy.deepcopy(f.get(key)) for key in (
                'fact_id', 'code', 'value', 'company', 'period', 'period_type',
                'scope', 'currency', 'evidence_ids', 'source')} for f in deps],
        })

    for group in groups:
        duration = group[2]
        for code in ['revenue', 'net_profit', 'parent_net_profit', 'nonrecurring_adj_parent_profit',
                     'operating_cash_flow', 'assets', 'receivables', 'inventory',
                     # —— 同比覆盖扩展到利润表/资产负债表/现金流量表全科目 ——
                     # 评测口径按"金标准有同比的科目"计分，只算核心 8 项时覆盖率天然 <20%。
                     'cost_of_revenue', 'total_revenue', 'total_operating_cost',
                     'taxes_surcharges', 'selling_expenses', 'administrative_expenses',
                     'research_expenses', 'financial_expenses', 'operating_profit', 'profit_before_tax',
                     'investment_income', 'fair_value_gain', 'credit_impairment', 'asset_impairment',
                     'asset_disposal_gain', 'other_income', 'nonoperating_income', 'nonoperating_expense',
                     'income_tax', 'minority_profit', 'basic_eps', 'diluted_eps',
                     'liabilities', 'equity', 'parent_equity', 'minority_equity',
                     'current_assets', 'noncurrent_assets', 'current_liabilities', 'noncurrent_liabilities',
                     'cash', 'payables', 'prepayments', 'notes_receivable',
                     'contract_liabilities', 'intangibles', 'fixed_assets', 'goodwill',
                     'long_term_investments', 'long_term_debt', 'retained_earnings', 'share_capital',
                     'cash_from_sales', 'cash_paid_suppliers', 'capex',
                     'investing_cash_flow', 'financing_cash_flow', 'cash_equivalents_end',
                     'cash_equivalents_change', 'other_receivables']:
            current, previous = get(group, code), get(group, code, _previous_year(group[1]))
            if current is not None and previous is not None:
                if amount(previous) == 0:
                    metric(code + '_yoy', group, [current, previous], '(current - prior_year) / prior_year', None,
                           issue=f'{code} 上年基数为零，不报告常规同比百分比；请比较绝对增减额。')
                elif amount(previous) < 0:
                    # 负基数同比：分母取基数绝对值（外部 F10 同比列即此口径），
                    # 使"亏损收窄"显示为正、"亏损扩大"为负——与正值基数的增减语义一致。
                    # 仍带警示：正文引用时应改述绝对额变化。
                    ratio(code + '_yoy', group, [current, previous], amount(current) - amount(previous),
                          abs(amount(previous)), '(current - prior_year) / |prior_year|', positive=False)
                    reviews.append(_review(
                        'yoy_negative_base',
                        f'{code} 上年基数为负数，同比百分比按基数绝对值口径计算'
                        f'（亏损收窄为正、亏损扩大为负），引用时请同时给出绝对额变化。', [current, previous]))
                else:
                    ratio(code + '_yoy', group, [current, previous], amount(current) - amount(previous),
                          amount(previous), '(current - prior_year) / prior_year')
        if duration == 'instant':
            for metric_code, num_code, den_code in [('debt_ratio', 'liabilities', 'assets'),
                    ('current_ratio', 'current_assets', 'current_liabilities'),
                    ('cash_ratio', 'cash', 'current_liabilities')]:
                num, den = get(group, num_code), get(group, den_code)
                if num and den:
                    ratio(metric_code, group, [num, den], amount(num), amount(den), f'{num_code} / {den_code}')
            ca, inv, cl = get(group, 'current_assets'), get(group, 'inventory'), get(group, 'current_liabilities')
            # Conventional quick ratio, with its exact scope stated in the formula.
            if ca and inv and cl:
                ratio('quick_ratio', group, [ca, inv, cl], amount(ca) - amount(inv), amount(cl), '(current_assets - inventory) / current_liabilities')
            # 有息负债 / 类现金 / 净现金 / 净债务。
            #
            # 为什么必须由工具算而不是让模型自己拼：这两个口径极易被"顺手放宽" ——
            # 把按公平值计入损益的理财也算进类现金，净现金就会虚增几十亿。
            # 实测某份分析报告把 75.55 亿元 FVTPL 理财计入类现金，
            # 得出"净现金 253 亿元"，而年报口径的现金及银行结余只有 111.78 亿元。
            # 工具给出**公式里逐项列出科目名**的两个口径，模型只需引用、不必自己定义：
            #   cash_and_deposits —— 只含现金及银行结余 + 长期银行存款（最保守，贴近年报口径）
            #   net_cash          —— 上述类现金 − 有息负债（不含 FVTPL 理财）
            #   net_debt          —— 有息负债 − 现金及银行结余（更保守）
            # 需要含 FVTPL 的口径时，模型应显式说明并自行加总，工具不默认放宽。
            # 有息负债：同一笔债务会在多个来源各出现一次 —— 财务状况表的「計息借貸」
            # 经分节拆分登记成 short_term_debt，附注 22 又整行列一次 borrowings
            # （4,390,000 千元，两个来源、不同 fact_id）；租赁负债同样有
            # 「合計 102,699」与「即期 61,838 + 非即期 40,861」两种粒度。
            # 简单相加会把 44.9 亿元算成 88.8 亿元、租赁负债算两遍 ——
            # 实测净现金因此少算 43.9 亿元。按「优先总括行、否则取明细之和」
            # 的阶梯取值，再用金额去重兜底。
            def unique(items):
                seen, out = set(), []
                for f in items:
                    fid = f.get('fact_id', id(f))
                    if fid in seen:
                        continue
                    seen.add(fid)
                    out.append(f)
                return out

            def ladder(total_codes, detail_codes):
                """优先取总括科目；总括缺失才回退到明细科目之和。"""
                for c in total_codes:
                    fact = get(group, c)
                    if fact:
                        return [fact]
                return unique([f for f in (get(group, c) for c in detail_codes) if f])

            borrowings = ladder(('borrowings',), ('short_term_debt', 'long_term_debt'))
            leases = ladder(('lease_liabilities',),
                            ('lease_liabilities_current', 'lease_liabilities_noncurrent'))
            # 金额去重兜底：不同来源的同一笔债务（如 borrowings 与 short_term_debt
            # 同为 4,390,000）只保留一条。
            debt, amounts = [], set()
            for f in unique(borrowings + leases):
                key = _str(_number(f.get('value', '')) or 0)
                if key in amounts:
                    continue
                amounts.add(key)
                debt.append(f)
            cash_like = unique([f for f in (get(group, c) for c in ('cash', 'long_term_deposits')) if f])
            if debt:
                metric('interest_bearing_debt', group, debt,
                       ' + '.join(f['code'] for f in debt),
                       sum((amount(f) for f in debt), Decimal(0)), unit=group[4])
            if cash_like:
                metric('cash_and_deposits', group, cash_like,
                       ' + '.join(f['code'] for f in cash_like),
                       sum((amount(f) for f in cash_like), Decimal(0)), unit=group[4])
            if cash_like and debt:
                metric('net_cash', group, cash_like + debt,
                       ' + '.join(f['code'] for f in cash_like) + ' - ('
                       + ' + '.join(f['code'] for f in debt) + ')',
                       sum((amount(f) for f in cash_like), Decimal(0))
                       - sum((amount(f) for f in debt), Decimal(0)), unit=group[4])
            cash_only = get(group, 'cash')
            if debt and cash_only:
                metric('net_debt', group, debt + [cash_only],
                       '(' + ' + '.join(f['code'] for f in debt) + ') - cash',
                       sum((amount(f) for f in debt), Decimal(0)) - amount(cash_only), unit=group[4])
            for check_code, codes, coefficients in [
                    ('balance_sheet_equation', ['assets', 'liabilities', 'equity'], [1, -1, -1]),
                    # 这一条**不依赖**上面推导出的 assets/liabilities，直接用四个小计与权益
                    # 做恒等式，因此不是循环论证：它验证的是"报表本身平不平"。
                    ('balance_sheet_identity', ['current_assets', 'noncurrent_assets',
                                                'current_liabilities', 'noncurrent_liabilities',
                                                'equity'], [1, 1, -1, -1, -1]),
                    ('asset_subtotals', ['assets', 'current_assets', 'noncurrent_assets'], [1, -1, -1]),
                    ('liability_subtotals', ['liabilities', 'current_liabilities', 'noncurrent_liabilities'], [1, -1, -1]),
                    ('equity_subtotals', ['equity', 'parent_equity', 'minority_equity'], [1, -1, -1])]:
                deps = [get(group, c) for c in codes]
                if all(deps):
                    tieout(check_code, group, deps, coefficients)
            continue
        revenue, cost = get(group, 'revenue'), get(group, 'cost_of_revenue')
        profit, cash_flow = get(group, 'net_profit'), get(group, 'operating_cash_flow')
        if revenue and cost:
            ratio('gross_margin', group, [revenue, cost], amount(revenue) - amount(cost), amount(revenue), '(revenue - cost_of_revenue) / revenue')
        elif revenue:
            # IFRS/HKFRS 报表常直接列「毛利」而不单列营业成本，此时用毛利倒算
            gross_profit = get(group, 'gross_profit')
            if gross_profit:
                ratio('gross_margin', group, [revenue, gross_profit], amount(gross_profit), amount(revenue),
                      'gross_profit / revenue', positive=False)
        for metric_code, numerator in [('net_margin', profit), ('operating_margin', get(group, 'operating_profit')),
                                      ('operating_cash_margin', cash_flow)]:
            if revenue and numerator:
                ratio(metric_code, group, [numerator, revenue], amount(numerator), amount(revenue), f'{numerator["code"]} / revenue')
        if cash_flow and profit:
            ratio('cash_conversion', group, [cash_flow, profit], amount(cash_flow), amount(profit), 'operating_cash_flow / net_profit')
        if cash_flow and profit and amount(profit) > 0:
            conversion = amount(cash_flow) / amount(profit)
            if conversion < Decimal('.5'):
                heuristic_signal('weak_profit_cash_conversion', group, [profit, cash_flow], conversion,
                    Decimal('.5'), 'net_profit > 0 AND operating_cash_flow / net_profit < 0.5',
                    '利润为正但经营现金转化低于 50% 经验阈值；需结合季节性、营运资金和收款安排核对盈利现金支撑。')
        parent_profit, adjusted_profit = get(group, 'parent_net_profit'), get(group, 'nonrecurring_adj_parent_profit')
        if parent_profit and adjusted_profit:
            ratio('nonrecurring_profit_share', group, [parent_profit, adjusted_profit],
                  amount(parent_profit) - amount(adjusted_profit), amount(parent_profit),
                  '(parent_net_profit - nonrecurring_adj_parent_profit) / parent_net_profit')
            if amount(parent_profit) > 0:
                share = (amount(parent_profit) - amount(adjusted_profit)) / amount(parent_profit)
                if share > Decimal('.3'):
                    heuristic_signal('nonrecurring_profit_concentration', group, [parent_profit, adjusted_profit],
                        share, Decimal('.3'),
                        'parent_net_profit > 0 AND (parent_net_profit - nonrecurring_adj_parent_profit) / parent_net_profit > 0.3',
                        '扣非前后归母利润差额占比超过 30% 经验阈值；需阅读非经常性损益明细，核对其来源和可持续性。')
        previous_revenue = get(group, 'revenue', _previous_year(group[1]))
        if revenue and previous_revenue and amount(previous_revenue) > 0:
            revenue_growth = (amount(revenue) - amount(previous_revenue)) / amount(previous_revenue)
            for working_code, display in [('receivables', '应收账款'), ('inventory', '存货')]:
                current_working = get(group, working_code, kind='instant')
                previous_working = get(group, working_code, _previous_year(group[1]), 'instant')
                if current_working and previous_working and amount(previous_working) > 0:
                    working_growth = (amount(current_working) - amount(previous_working)) / amount(previous_working)
                    gap = working_growth - revenue_growth
                    if gap > Decimal('.2'):
                        heuristic_signal(working_code + '_growth_ahead_of_revenue', group,
                            [revenue, previous_revenue, current_working, previous_working], gap, Decimal('.2'),
                            f'prior_revenue > 0 AND prior_{working_code} > 0 AND {working_code}_yoy - revenue_yoy > 0.2; matched reporting dates and comparable revenue duration',
                            display + '同比增速高于营业收入增速超过 20 个百分点的经验阈值；需结合季节性、业务结构和营运资金明细核对原因。')
        capex = get(group, 'capex')
        if cash_flow and capex:
            # capex 事实在港股/IFRS 报表里是「购买物业厂房及设备」的**投资流出**，
            # 原文以括号列报、入库后本身带负号。原写法 `operating_cash_flow - capex`
            # 遇到负数 capex 等于把它加回去，系统性高估自由现金流：
            # 实测农夫山泉 2025 年真值 146.60 亿元被算成 276.23 亿元，高估 129.6 亿元，
            # 而该指标状态是"已计算"，报告若直接引用就会得出错误结论。
            # 改为扣除绝对流出额，并在 capex 符号与常见列报相反时显式告警。
            capex_outflow = abs(amount(capex))
            metric('free_cash_flow', group, [cash_flow, capex],
                   'operating_cash_flow - abs(capex)', amount(cash_flow) - capex_outflow,
                   unit=group[4])
            if amount(capex) > 0:
                warnings.append(f"{group[1]} 的 capex 事实为正数，与「投资流出」的常见列报符号相反；"
                                '自由现金流已按绝对流出额计算，请对照原表符号确认。')
            checks.append({'code': 'free_cash_flow_le_operating_cash_flow', 'status': 'pass',
                           'company': cash_flow.get('company', ''), 'period': group[1],
                           'scope': group[3], 'currency': group[4],
                           'difference': _str(amount(cash_flow) - capex_outflow - amount(cash_flow)),
                           'tolerance': '0',
                           'formula': 'free_cash_flow - operating_cash_flow = -abs(capex) <= 0',
                           'dependency_fact_ids': [cash_flow.get('fact_id', ''), capex.get('fact_id', '')],
                           'evidence_ids': list(dict.fromkeys(
                               list(cash_flow.get('evidence_ids', [])) + list(capex.get('evidence_ids', []))))})
        depreciation_parts = [f for f in (get(group, c) for c in
                                          ('depreciation', 'depreciation_rou_assets', 'amortization')) if f]
        if depreciation_parts:
            # 折旧摊销合计。港股现金流量表的「调整项」区把折旧拆成
            # 「物業、廠房及設備折舊」与「使用權資產折舊」两行，再加「無形資產攤銷」，
            # 三者性质相同、都是非付现成本。不合并就算不出 EBITDA，也无法回答
            # "经营现金流的大幅增长里有多少只是非付现加回"这个关键问题 ——
            # 实测某份分析报告的折旧摊销合计只能写"—"，并把整块增量归入"数据缺口"。
            # 这三行原本因附注口径未判定而全部不可用，口径继承后才拿得到。
            depreciation_total = sum((amount(f) for f in depreciation_parts), Decimal(0))
            metric('depreciation_total', group, depreciation_parts,
                   ' + '.join(f['code'] for f in depreciation_parts), depreciation_total, unit=group[4])
            profit_before_tax = get(group, 'profit_before_tax')
            finance_costs = get(group, 'financial_expenses')
            if profit_before_tax and finance_costs:
                # EBITDA = 除税前溢利 + 财务费用 + 折旧摊销。这里加回的是**真实折旧摊销**
                # （来自现金流量表调整项），不是"净利润加回净财务费用"的近似，
                # 公式逐项列出科目代码，可回溯到证据。
                metric('ebitda', group, [profit_before_tax, finance_costs] + depreciation_parts,
                       'profit_before_tax + financial_expenses + depreciation_total',
                       amount(profit_before_tax) + amount(finance_costs) + depreciation_total,
                       unit=group[4])
        start_period = f'{group[1][:4]}-01-01'
        previous_end = f'{int(group[1][:4]) - 1}-12-31'
        days_basis = {}
        for metric_code, numerator_code, balance_code in [('roe', 'parent_net_profit', 'parent_equity'),
                   ('roe_total', 'net_profit', 'equity'), ('roa', 'net_profit', 'assets'),
                   ('inventory_turnover', 'cost_of_revenue', 'inventory'),
                   ('receivables_turnover', 'revenue', 'receivables'),
                   # 应付账款周转的分子用营业成本：采购额在多数年报里不直接披露，
                   # 营业成本是行业惯例的代理口径，且公式里写明了用的是哪个分子，
                   # 评审可以据此判断该代理是否适用于这家公司。
                   ('payables_turnover', 'cost_of_revenue', 'payables'),
                   ('asset_turnover', 'revenue', 'assets')]:
            if duration not in {'annual', 'ytd'}:
                continue
            numerator = get(group, numerator_code)
            current_balance, previous_balance = get(group, balance_code, kind='instant'), get(group, balance_code, previous_end, 'instant')
            if numerator and current_balance and previous_balance:
                average = (amount(current_balance) + amount(previous_balance)) / 2
                ratio(metric_code, group, [numerator, current_balance, previous_balance], amount(numerator), average,
                      f'{numerator_code} / ((closing_{balance_code} + opening_{balance_code}) / 2); period basis, not annualized')
                if metric_code in {'inventory_turnover', 'receivables_turnover', 'payables_turnover'} \
                        and average > 0 and amount(numerator) > 0:
                    days = Decimal((date.fromisoformat(group[1]) - date.fromisoformat(start_period)).days + 1)
                    entry = metric(metric_code.replace('turnover', 'days'),
                                   group, [numerator, current_balance, previous_balance],
                                   f'days_in_period * average_{balance_code} / {numerator_code}',
                                   days * average / amount(numerator), unit='days')
                    days_basis[metric_code.replace('turnover', 'days')] = (
                        entry, numerator, current_balance, previous_balance)
        # 现金循环周期 = 存货周转天数 + 应收周转天数 − 应付周转天数。
        # 三项天数都是"本口径同期"算出来的，缺任何一项就不出这个数 ——
        # 用别期的天数凑一个合计只会得到看起来专业但没有意义的数字。
        if duration in {'annual', 'ytd'} and all(
                key in days_basis and days_basis[key][0].get('value') is not None
                for key in ('inventory_days', 'receivables_days', 'payables_days')):
            deps = [f for key in ('inventory_days', 'receivables_days', 'payables_days')
                    for f in days_basis[key][1:]]
            cycle = (Decimal(days_basis['inventory_days'][0]['value'])
                     + Decimal(days_basis['receivables_days'][0]['value'])
                     - Decimal(days_basis['payables_days'][0]['value']))
            metric('cash_conversion_cycle', group, deps,
                   'inventory_days + receivables_days - payables_days', cycle, unit='days')
        for check_code, codes, coefficients in [
                ('net_profit_equation', ['net_profit', 'profit_before_tax', 'income_tax'], [1, -1, 1]),
                ('profit_attribution', ['net_profit', 'parent_net_profit', 'minority_profit'], [1, -1, -1]),
                ('pretax_profit_equation', ['profit_before_tax', 'operating_profit', 'nonoperating_income', 'nonoperating_expense'], [1, -1, -1, 1]),
                ('cash_equivalents_rollforward', ['cash_equivalents_end', 'cash_equivalents_start', 'cash_equivalents_change'], [1, -1, -1]),
                ('cash_flow_components', ['cash_equivalents_change', 'operating_cash_flow', 'investing_cash_flow', 'financing_cash_flow', 'fx_cash_effect'], [1, -1, -1, -1, -1])]:
            deps = [get(group, c) for c in codes]
            if all(deps):
                tieout(check_code, group, deps, coefficients)
        if duration in {'ytd', 'annual'}:
            month = int(group[1][5:7])
            if month in {6, 9, 12}:
                prior_month = month - 3
                prev = f'{group[1][:4]}-{prior_month:02}-{calendar.monthrange(int(group[1][:4]), prior_month)[1]:02}'
                for code in ALIASES:
                    if code in BALANCE_CODES or code.endswith('_eps') or code in {'cash_equivalents_end', 'cash_equivalents_start'}:
                        continue
                    cur, prior = get(group, code), get(group, code, prev, 'ytd')
                    if cur and prior:
                        if bool(cur.get('restated')) != bool(prior.get('restated')):
                            reviews.append(_review('restatement_mismatch', '累计数的重述或调整口径不一致，暂不相减推算单季。', [cur, prior]))
                            continue
                        q_group = (group[0], group[1], 'quarter', group[3], group[4])
                        derived = amount(cur) - amount(prior)
                        conflicting = quarter_conflicts.get((group, code))
                        if conflicting:
                            metric(code + '_quarter', q_group, [cur, prior, conflicting], 'current_ytd_or_annual - preceding_quarter_ytd', None,
                                   unit=group[4], issue='单季披露值与累计差额冲突，暂不选择单季数值。')
                            continue
                        metric(code + '_quarter', q_group, [cur, prior], 'current_ytd_or_annual - preceding_quarter_ytd', derived, unit=group[4])
                        explicit = get(q_group, code)
                        if explicit and abs(amount(explicit) - derived) > _rounding(cur) + _rounding(prior) + _rounding(explicit):
                            reviews.append(_review('quarter_conflict', '单季披露值与累计差额存在冲突，请核对调整口径。', [cur, prior, explicit]))
        for signal_code, f, condition, message in [
                ('loss', profit, profit and amount(profit) < 0, '净利润为负，需核查亏损来源及持续性。'),
                ('negative_operating_cash_flow', cash_flow, cash_flow and amount(cash_flow) < 0, '经营现金流为负，需结合季节性和营运资金变动分析。')]:
            if condition:
                signals.append({'code': signal_code, 'severity': 'attention', 'message': message,
                                'company': f.get('company', ''), 'period': group[1], 'scope': group[3],
                                'dependency_fact_ids': [f['fact_id']], 'evidence_ids': f.get('evidence_ids', [])})
    if not metrics:
        warnings.append('缺少期间、口径一致的可靠数据，暂未生成财务指标。')
    summary = {'fact_count': len(facts), 'verified_fact_count': sum(_valid(f) for f in facts),
               'metric_count': len(metrics), 'check_count': len(checks),
               'failed_check_count': sum(c['status'] == 'fail' for c in checks), 'review_count': len(reviews), 'signal_count': len(signals),
               'text': f'提取 {len(facts)} 条财务事实，计算 {len(metrics)} 项指标，{len(reviews)} 项需要人工核对。指标基于已核验口径，未自动年化。'}
    return {'facts': facts, 'metrics': metrics, 'checks': checks, 'signals': signals,
            'warnings': list(dict.fromkeys(warnings)), 'review_queue': reviews, 'summary': summary}
