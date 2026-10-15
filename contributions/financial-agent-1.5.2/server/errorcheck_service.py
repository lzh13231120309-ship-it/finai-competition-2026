"""研报纠错核查服务：把"报告自己说的数"与"原始财报的数"逐条对账，并做报告内部自洽复算。

为什么单独一层
--------------
研报纠错有两个**性质完全不同**的错误来源，混在一起就说不清"错在哪、怎么发现的"：

1. **来源不符（source）**：报告写的数与原始财报（季报/半年报/年报）不一致。
   这类错误只能靠外部事实发现，判据是"同一指标、同一期间、同一口径，两边数值不同"。
2. **内部不自洽（internal）**：不引入外部数据，仅凭报告自身即可复算出来的矛盾。
   例：增长率行与上一行两期数不符、PE 与 EPS×股价 不符、表头年份序列断裂、
   封面评级与结论评级不一致、正文数与预测表数不一致、CAGR 与表内数据不符。

判据纪律（与项目哲学一致）
--------------------------
- 数值一律 Decimal，比较前统一到"元"或"小数比率"；不使用浮点。
- 每一条 finding 必须能回答：**错在哪一行/哪一句、原文写的是什么、应该是多少、依据是什么**。
- **能复算才报**：复算不出正确值的疑点只进 `suspects`（复核队列），不进 `findings`，
  以免把"我不确定"包装成"报告错了"。
- **归因不重复计**：若某行的错来自同表上游行的错（例如增速行的上一行数值被改），
  只报上游那一行，不把下游的连带矛盾再报一遍（否则一处错变两处错）。

输出契约（与 scripts/eval_errorcheck.py / server/errorcheck_eval.py 对齐）
--------------------------------------------------------------------
findings 每条至少含：
    category, subtype, quote, claimed, expected, suggestion, tier,
    source{doc,page}（可空）, page（研报页，可空）, reason
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .claim_extractor import MULTIPLE_METRICS, RATIO_METRICS, extract_claims
from .report_parse import parse_report

D = Decimal
ZERO = D(0)

# ---------------------------------------------------------------- 可调参数

# 数值比较容差：相对容差 0.8%（注入错误最小量级 1.2%，舍入误差远小于 1%），
# 同时给出"半个末位"的绝对容差，避免小数位少的指标被误判。
REL_TOL = D('0.008')

# 只对这些指标做"来源对账"（其余指标要么口径太杂，要么原始财报里没有可比行）。
# 刻意排除的四类（都是在真实语料上实测误报率过高、且"差异来自口径而非错误"）：
#   - 每股收益：报告常用"最新总股本"重算，与年报的加权平均口径天然不等，属口径差异不是错；
#   - 经营活动现金流：报告常把"经营现金流净额"写成含其他项目的口径，误报率高；
#   - 毛利率：分子分母都有多种取法（营业成本 / 营业总成本），实测同一份报告里
#     正确值与错值只差 1.1pp、而口径差异可达 3.6pp，阈值无法把两者分开；
#   - 净利率：同理，归母净利率与整体净利率只差一个少数股东损益，却常差 2pp 以上。
# 费用率（销售/管理/研发/财务）与资产负债率是"单科目 ÷ 营业收入/总资产"的纯除法，
# 两侧算法唯一，故保留。
SOURCE_METRICS_PROSE = {
    'revenue', 'cost_of_revenue', 'operating_profit', 'total_profit', 'net_profit',
    'parent_net_profit', 'adj_parent_net_profit', 'total_assets', 'total_liabilities',
    'parent_equity', 'selling_expense', 'admin_expense', 'rd_expense', 'finance_expense',
    'selling_ratio', 'admin_ratio', 'rd_ratio', 'finance_ratio', 'debt_ratio', 'roe',
    'gross_margin',
}
# 表格行只做"硬指标"对账：这些科目的值与年报必须一致，没有口径腾挪空间。
SOURCE_METRICS_TABLE = {
    'revenue', 'cost_of_revenue', 'parent_net_profit', 'adj_parent_net_profit',
    'total_assets', 'total_liabilities', 'operating_profit', 'total_profit',
    'selling_expense', 'admin_expense', 'rd_expense', 'finance_expense',
}

# 报告指标码 -> 原始财报事实库指标码（source_reconcile 的规范码）
CLAIM_TO_SOURCE = {
    'eps': 'basic_eps',
    'operating_profit': 'operating_profit',
    'total_profit': 'total_profit',
}

# 组合指标标记：标签里出现"管理+研发费用率""销售&管理费用率"这类写法时，
# 它讲的不是单一科目，不能拿单一科目的口径去对账。
COMBO_LABEL = re.compile(r'[+＋&＆]|合计|之和|及')
# 句子级组合写法：'管理+研发费用率'（中文与中文相加，而不是'同比+0.26pct'）。
COMBO_IN_SENTENCE = re.compile(r'[\u4e00-\u9fa5][+＋][\u4e00-\u9fa5]')

# 千分位书写异常：'103,4' 这种（逗号后不足三位）不是合法分组，
# 解析出来会整体放大 10 倍，属"报告排版缺陷"而非"数据错误"，跳过。
BAD_GROUPING = re.compile(r'\d,\d(?!\d\d)|\d{4},\d')

# 可做同比对账的流量类指标
GROWTH_METRICS = {'revenue', 'parent_net_profit', 'adj_parent_net_profit', 'net_profit', 'ocf'}

CATEGORY_OF = {
    'revenue': 'revenue', 'cost_of_revenue': 'revenue',
    'net_profit': 'profit', 'parent_net_profit': 'profit', 'adj_parent_net_profit': 'profit',
    'operating_profit': 'profit', 'total_profit': 'profit', 'ocf': 'profit',
    'gross_margin': 'ratio', 'net_margin_parent': 'ratio', 'selling_ratio': 'ratio',
    'admin_ratio': 'ratio', 'rd_ratio': 'ratio', 'finance_ratio': 'ratio',
    'debt_ratio': 'ratio', 'roe': 'ratio',
    'pe': 'valuation', 'pb': 'valuation', 'ev_ebitda': 'valuation', 'peg': 'valuation',
    'eps': 'valuation', 'basic_eps': 'valuation', 'market_cap': 'valuation',
    'total_assets': 'internal', 'total_liabilities': 'internal', 'parent_equity': 'internal',
    'selling_expense': 'internal', 'admin_expense': 'internal', 'rd_expense': 'internal',
    'finance_expense': 'internal',
}

RATING_WORDS = ('强烈推荐', '买入', '增持', '推荐', '优于大市', '跑赢行业', '强于大市',
                '持有', '中性', '同步大市', '弱于大市', '减持', '卖出', '回避', '无评级')

YEAR_TOK = re.compile(r'^(?:(20\d{2})|(\d{2}))\s*([A-Ea-e])?$')
NUMERIC = re.compile(r'^[-+−–]?\(?[\d,]+(?:\.\d+)?\)?\s*[%％倍xX]?$')
GROWTH_LABEL = re.compile(r'\(\s*\+?/?-?\s*%\s*\)|同比|增速|增长率|YoY|yoy|增减')
UNIT_IN_LABEL = re.compile(r'[（(]\s*(百万元|千万元|万元|亿元|千元|亿美元|万美元|百万|千万|万|亿)\s*[）)]')
# 数值自带单位（"47.97 万元"）：单位写在数值后面而非行标签括号里
UNIT_INLINE = re.compile(r'\d\s*(百万元|千万元|万元|亿元|千元|亿美元|万美元)\s*$')
# 只对"金额量级单位"做一致性检查：每股收益（元）、比率（%）这类行的单位本来就与表内主流不同。
UNIT_SCALE_ONLY = {'百万元', '千万元', '万元', '亿元', '千元', '亿美元', '万美元'}
# 「（百万）」「（亿）」这类省写归一到标准单位名（保持与 UNIT_SCALE_ONLY 同名）
UNIT_ALIAS = {'百万': '百万元', '千万': '千万元', '万': '万元', '亿': '亿元'}


def _unit_norm(u):
    """单位归一：把「百万/亿」等省写映射到「百万元/亿元」，标准名原样返回。"""
    return UNIT_ALIAS.get(u, u)
EPS_LABEL = re.compile(r'每股收益|摊薄每股收益|基本每股收益|(?<![A-Za-z])EPS(?![A-Za-z])')
BVPS_LABEL = re.compile(r'每股净资产|每股账面价值|(?<![A-Za-z])BVPS(?![A-Za-z])|归母公司股东权益|归属于母公司所有者权益|股东权益合计')
PE_LABEL = re.compile(r'市盈率|(?<![A-Za-z])P\s*/\s*E(?![A-Za-z])|(?<![A-Za-z])PE(?![A-Za-z])')
PB_LABEL = re.compile(r'市净率|(?<![A-Za-z])P\s*/\s*B(?![A-Za-z])|(?<![A-Za-z])PB(?![A-Za-z])')
EV_EBITDA_LABEL = re.compile(r'EV\s*/\s*EBITDA', re.I)

COMPLIANCE_RULES = [
    ('guarantee', re.compile(r'保证(?:盈利|收益|上涨|回报|不亏)|承诺[^。；]{0,24}(?:上涨|涨幅|收益|回报|补偿|赔偿)|'
                            r'必然(?:上涨|上涨|盈利|收益)|确定性收益|不存在亏损风险|无风险(?:收益|套利)|保本|'
                            r'稳赚(?:不赔)?|只赚不赔|确定(?:向上|上涨|走高)|满仓(?:操作|买入)|'
                            r'必涨|包赚|躺着(?:赚|赚钱)|闭眼(?:买|入)|无脑(?:买|入)|'
                            r'收益(?:率)?[^。；]{0,10}?(?:稳定达到|保证|承诺)[^。；]{0,10}?\d+(?:\.\d+)?\s*%')),
    ('insider', re.compile(r'内幕(?:消息|信息)|未公开(?:重大)?信息|提前布局[^。；]{0,10}重组')),
    ('manipulation', re.compile(r'联合(?:拉升|买入|行动)|操纵(?:股价|市场)|对倒|拉抬股价|'
                               r'拉升(?:股价|出货)|接盘|诱多|割韭菜')),
]
NEGATION = re.compile(r'不|无|非|禁止|不得|严禁|没有|勿|未')
# 免责/禁止性措辞：出现在命中词前 12 字内即视为"在说不得如此"，不是真的在承诺
PROHIBITION = re.compile(r'不得|禁止|严禁|不构成|不应|不可|不会|不作出|不作为|不具有|不涉及|不含有|'
                         r'并非|不代表|不意味着|不视为|免于|免责')

# 分部/业务线标记：这类数字与"公司整体"口径不可比，不能拿去和合并报表对账。
# 少了这一层，"公司制造业实现营收 1537.8 亿"会被误判成"营业收入写错"。
SEGMENT_MARK = re.compile(r'分[行业地区业务产品渠道客户]|板块|事业部|其中|子品牌|品类|系列|拆分|结构|'
                          r'制造业|消费电器|内销|外销|海外|境内|境外|线上|线下|'
                          r'智能家居|楼宇科技|机器人|工业技术|储能|整车|电池|物业|物流|'
                          r'其他业务|新兴业务|业务线|分部|新签|在手|电器')
# 口径修饰：这类数字是"剔除XX后/按恒定汇率"的调整口径，与法定报表口径不可对账
CALIBER_MARK = re.compile(r'汇率中性|剔除|调整后|可比口径|恒定汇率|内生增长|还原后|不含.{0,6}影响|'
                          r'同口径|备考|口径下|排除|撇除')
# 单季度/环比口径：单季数与财报累计数天然不可比（同比%与环比%常同句出现；第一季度累计=单季，不在此列）
QUARTER_MARK = re.compile(r'单季|单Q|第[二三四2-4]季度|环比')
# 句内自述的季度档位（用于与期间绑定互检）
QUARTER_OF = {1: re.compile(r'第[一1]季度|单季.{0,4}一|一季度'), 2: re.compile(r'第[二2]季度|二季度'),
              3: re.compile(r'第[三3]季度|三季度'), 4: re.compile(r'第[四4]季度|四季度')}

# 增速行必须"长得像增速行"：纯 (+/-%) 行，或标签里明确含同比/增长率/增速。
# 不这样卡，"毛利率(%)"这类比率行会被当成增速行，拿上一行算同比，满屏假错。
PURE_GROWTH_LABEL = re.compile(r'^[（(]?\s*\+?\s*/?\s*-?\s*%?\s*[）)]?$')
NAMED_GROWTH_LABEL = re.compile(r'同比|增长率|增速|增减|增长|YoY|yoy')

RATING_DEF_CONTEXT = re.compile(r'评级(?:标准|说明|定义)|投资评级标准|类别\s*\|?\s*级别')
# 结论处评级写法：「维持"中性"评级」「维持买入评级」「给予优于大市评级」（带/不带引号都收）。
RATING_CONCLUSION = re.compile(
    r'(?:维持|给予|上调|下调|首次覆盖)\s*[“"]?([^”"\s，。；、]{2,6})[”"]?\s*评级')
# 评级"定义/标准"措辞（出现即说明这段是评级说明表，不是封面评级，也不是结论）。
RATING_DEF_MARKER = re.compile(r'预计未来|涨幅(?:在|介于|高于|低于)|沪深300|基准指数|相对.{0,6}指数|未来\d+\s*个月')

# —— 引用/期次 ——
SRC_LINE = re.compile(r'资料来源|数据来源|来源\s*[：:]')
ANNUAL_SRC = re.compile(r'公司(?:\d{4}\s*年)?(?:年度报告|年报|年年度报告)|(?:年度报告|年年度报告)')
FORECAST_COL = re.compile(r'20\d{2}\s*[EＥ]')
# 「公司发布{X}报告」里的财报期次 → 覆盖档位
FILING_DESC = re.compile(r'发布[^，。；]{0,12}?(一季报|第一季度报告|一季度报告|半年报|半年度报告|中报|三季报|第三季度报告|三季度报告|年报|年度报告)')
# 数据期间（紧跟其后的小句）→ 覆盖档位；无显式季度/半年字样按全年
DATA_Q1 = re.compile(r'第一?季度|1-?3\s*月|一季度')
DATA_H1 = re.compile(r'上半年|半年度|中期(?![权])')
DATA_9M = re.compile(r'前三?季度|1-?9\s*月|1至9月')
DATA_FY = re.compile(r'全年|年度|20\d{2}\s*年(?:公司)?(?:实现|营业|营收|收入)')


def _filing_rank(desc):
    if re.search(r'一季报|第一季度报告|一季度报告', desc):
        return 1
    if re.search(r'半年报|半年度报告|中报', desc):
        return 2
    if re.search(r'三季报|第三季度报告|三季度报告', desc):
        return 3
    return 4


def _data_rank(text):
    if DATA_Q1.search(text):
        return 1
    if DATA_H1.search(text):
        return 2
    if DATA_9M.search(text):
        return 3
    if DATA_FY.search(text):
        return 4
    return None


# ---------------------------------------------------------------- 基础工具

def _dec(value):
    try:
        return D(str(value).replace(',', '').replace('，', ''))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _norm(text):
    """比对用规范化：去空白、统一全角数字/括号、去千分位。"""
    s = (text or '').translate(str.maketrans('０１２３４５６７８９．，％（）＋－', '0123456789.,%()+-'))
    return re.sub(r'\s+', '', s).replace(',', '').replace('，', '')


def _close(a, b, tol=None):
    """a、b 是否在容差内相等（a、b 为 Decimal）。"""
    if a is None or b is None:
        return False
    if a == b:
        return True
    scale = max(abs(a), abs(b))
    return abs(a - b) <= (tol if tol is not None else REL_TOL) * scale


def _fmt_like(value: Decimal, unit_scale: Decimal, decimals: int):
    """把"元"值按报告使用的单位与小数位格式化，便于写进修改建议。"""
    if value is None:
        return ''
    scaled = value / unit_scale
    q = D(1).scaleb(-decimals)
    try:
        scaled = scaled.quantize(q)
    except InvalidOperation:
        pass
    return f'{scaled:f}'


_UNIT_BY_SCALE = {D(1): '元', D(1000): '千元', D(10000): '万元', D(1000000): '百万元',
                  D(10000000): '千万元', D(100000000): '亿元', D(1000000000): '十亿元'}


def _unit_name(scale):
    """金额倍率 -> 中文单位名；允许因四舍五入产生 ±10% 偏差。"""
    if scale is None or scale <= 0:
        return ''
    for k, name in _UNIT_BY_SCALE.items():
        if k * D('0.9') <= scale <= k * D('1.1'):
            return name
    return ''


def _pow10_swap(ratio):
    """ratio 是否为 10 的正整数次幂（或其次幂的倒数）——即"两边只差一个单位"。

    研报把（百万元）写成（万元）时，同一指标的数值恰好差 100 倍；写成（亿元）时差
    10000 倍。因此"差 10^k 倍"是单位写错的特征，而不是数据抄错（抄错通常不落在 10^k 上）。
    """
    if ratio is None or ratio <= 0:
        return False
    r = ratio if ratio >= 1 else (1 / ratio)
    for k in (1, 2, 3, 4):
        t = D(10) ** k
        if t * D('0.9') <= r <= t * D('1.1'):
            return True
    return False


@dataclass
class Table:
    """连续的行块构成一张表。row 存 (block_id, cells, page, text)。"""
    rows: list = field(default_factory=list)
    header: list = field(default_factory=list)     # 表头年份 token（已剥离标签列）
    header_row: int = -1
    unit: str = ''                                 # 行标签里出现最多的单位
    unit_count: int = 0                            # 该单位出现的行数
    label_unit: str = ''                           # 标题行里声明的单位

    def periods(self):
        """表头 token -> 期间 id 列表；无法识别返回 None。

        严格解析要求整行表头全是年份 token。多栏财务模型巨表的表头常混入中段标签，
        此时返回 None —— 这是有意的：CAGR/正文对表按**同表列序**对齐，破表头下不可信，
        勉强给出期间只会把列错位放大成假错。倍数隐含价需要 EPS/BVPS 期间时，
        由 `_base_series` 用"首个年份序列"单独兜底，不在此处放宽。
        """
        out = []
        for tok in self.header:
            m = YEAR_TOK.match(tok)
            if not m:
                return None
            year = m.group(1) or ('20' + m.group(2))
            suf = (m.group(3) or '').upper()
            if suf == 'E':
                out.append(f'{year}E')
            elif suf == 'A':
                out.append(f'{year}FY')
            else:
                out.append(f'{year}FY')
        return out or None

    def first_run_periods(self):
        """宽松期间：表头首个年份序列（多栏巨表取第一栏）。仅供跨表取每股基数用。"""
        runs = _year_runs(self.header)
        if not runs:
            return None
        _, years, sufs = runs[0]
        return [f'{y}E' if s == 'E' else f'{y}FY' for y, s in zip(years, sufs)] or None


def _year_of(tok):
    m = YEAR_TOK.match(tok)
    if not m:
        return None, ''
    year = int(m.group(1) or ('20' + m.group(2)))
    return year, (m.group(3) or '').upper()


def _strong_year(tok):
    """像"真年份"的 token 才参与表头连续性判定。

    裸两位数（如「总股本 25」「贷存比 72」）会被 YEAR_TOK 误认成 2025/2072，
    拿去当表头算连续性就是满屏假错。所以只认：4 位年份（20xx）或带 A–E 后缀的
    显式期间。裸两位数一律排除。
    """
    y, suf = _year_of(tok)
    if y is None:
        return None
    t = (tok or '').strip()
    if t.startswith('20'):
        return y
    if suf:                      # 24A / 25E 这种带后缀的两位年份
        return y
    return None


def _year_runs(cells):
    """把一行单元格切成若干"年份序列"（左右两栏并排、子表头混排都能正确切）。

    返回 [(起始列号, [年份...], [后缀...])]。切分规则：
    1. 连续的强年份 token 归为一段；非年份单元格打断；
    2. 段内年份**回落**（y[i] < y[i-1]）视为新栏起点（并排两栏常共用一行、无分隔符）。
    """
    runs = []
    cur_start, cur = None, []

    def flush():
        nonlocal cur_start, cur
        if len(cur) >= 3:
            runs.append((cur_start, [y for y, _ in cur], [s for _, s in cur]))
        cur_start, cur = None, []

    for i, c in enumerate(cells):
        y = _strong_year((c or '').strip())
        if y is None:
            flush()
            continue
        suf = _year_of((c or '').strip())[1]
        if cur and y < cur[-1][0]:
            flush()
        if not cur:
            cur_start = i
        cur.append((y, suf))
    flush()
    return runs


def _metric_core(label):
    """把科目行标签归一成"指标核心"，用于增速行↔水平行配对。

    去掉**整个括号**（单位「（百万元）」、倍数标记「(PE)」「(X)」、单位「(元)」都在括号里）、
    增速词（同比/增长率…）、前缀引导词（其中：/减：/加：）、率/% 与空格。
    这样「营业收入增长率(%)」→「营业收入」能与「营业收入（百万元）」→「营业收入」精确对上；
    而「非存款负债」保留为「非存款负债」，不会被「存款」误配。
    """
    s = re.sub(r'[（(][^）)]*[）)]', '', label or '')   # 去括号整体（含单位、(PE)、(X)）
    s = GROWTH_LABEL.sub('', s)
    s = re.sub(r'^\s*(?:其中|减|加|小计|合计)\s*[：:]\s*', '', s)
    s = re.sub(r'[%\s]', '', s)
    s = re.sub(r'率$', '', s)
    return s


def _build_tables(doc):
    """把连续 row 块归成表；非 row 块打断。"""
    tables, cur = [], []
    for b in doc.blocks:
        if b.kind == 'row' and b.cells:
            cur.append(b)
        else:
            if len(cur) >= 2:
                tables.append(_make_table(cur))
            cur = []
    if len(cur) >= 2:
        tables.append(_make_table(cur))
    return [t for t in tables if t is not None]


def _make_table(blocks):
    t = Table()
    for b in blocks:
        t.rows.append((b.id, list(b.cells), b.page, b.text))
    # 表头 = 第一个"除首列外全是年份 token"的行
    for i, (_, cells, _, _) in enumerate(t.rows):
        if len(cells) < 3:
            continue
        cand = cells[1:]
        if sum(1 for c in cand if YEAR_TOK.match(c.strip())) >= max(2, len(cand) - 1):
            t.header, t.header_row = [c.strip() for c in cand], i
            break
    # 单位：标题行里声明的（如「资产负债表（百万元）」），否则取行标签里出现最多的
    if t.header_row >= 0:
        m = UNIT_IN_LABEL.search(t.rows[t.header_row][1][0] if t.rows[t.header_row][1] else '')
        if m:
            t.label_unit = _unit_norm(m.group(1))
    counts = {}
    for _, cells, _, _ in t.rows:
        m = UNIT_IN_LABEL.search(cells[0] if cells else '')
        if m:
            u = _unit_norm(m.group(1))
            counts[u] = counts.get(u, 0) + 1
    if counts:
        t.unit = max(counts, key=lambda k: counts[k])
        t.unit_count = counts[t.unit]
    return t


def _values(cells):
    """取行尾的数值单元（保持顺序）。"""
    out = []
    for c in cells[1:]:
        s = (c or '').strip()
        if not s:
            continue
        if NUMERIC.match(s.replace(' ', '')) or re.fullmatch(r'[-−–]?\d[\d,.]*\s*%?', s):
            out.append(s)
        else:
            return out if out else []
    return out


def _num(s):
    s = (s or '').strip().replace(',', '').replace('，', '')
    neg = s.startswith(('(', '（'))
    s = s.strip('()（）').rstrip('%％')
    d = _dec(s)
    if d is None:
        return None
    return -d if neg and d > 0 else d


# ---------------------------------------------------------------- 原始财报事实池

class SourcePool:
    """把若干份原始财报（SourceDoc）摊成"指标+期间 -> 候选值"的索引。

    期间键做**双向归一**：原始财报里资产负债类科目常用日期（2026-03-31），
    研报声明用季次（2026Q1），两边不同名就永远对不上账（实测整类漏检）。
    归一到「YYYYQ1 / YYYYH1 / YYYYFY」后与原键并存，两种写法都能查到。
    """

    def __init__(self, sources):
        self.docs = list(sources or [])
        self.by_key = {}
        self._build()

    @staticmethod
    def _norm_period(p):
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', p or '')
        if not m:
            return p
        y, mo = int(m.group(1)), int(m.group(2))
        if mo == 3:
            return f'{y}Q1'
        if mo == 6:
            return f'{y}H1'
        if mo == 9:
            return f'{y}Q3'
        if mo == 12:
            return f'{y}FY'
        return p

    def _build(self):
        from .source_reconcile import build_candidates
        for sd in self.docs:
            for c in build_candidates(sd):
                self.by_key.setdefault((c.metric, c.period), []).append(c)
                alt = self._norm_period(c.period)
                if alt != c.period:
                    self.by_key.setdefault((c.metric, alt), []).append(c)

    def values(self, metric, period):
        out = self.by_key.get((metric, period), [])
        if out:
            return out
        return self.by_key.get((metric, self._norm_period(period)), [])

    def periods_of(self, metric):
        return {p for (m, p) in self.by_key if m == metric}

    def match(self, metric, period, value, tol=None):
        """返回命中的 Cand，或 None。"""
        for c in self.values(metric, period):
            if _close(value, c.value, tol):
                return c
        return None


# ---------------------------------------------------------------- 检测器

class Checker:
    def __init__(self, doc, pool):
        self.doc = doc
        self.pool = pool
        self.findings = []
        self.suspects = []
        self._seen = set()

    # -------------------------------------------------- 记录
    def add(self, category, subtype, quote, claimed, expected, suggestion, *,
            source=None, page=None, reason='', tier='auto', confirmed=True):
        key = (category, subtype, _norm(quote)[:80], _norm(claimed)[:40])
        if key in self._seen:
            return None
        self._seen.add(key)
        item = {'category': category, 'subtype': subtype, 'quote': quote or '', 'claimed': claimed or '',
                'expected': expected or '', 'suggestion': suggestion or '', 'tier': tier,
                'source': source or {}, 'page': page, 'reason': reason}
        (self.findings if confirmed else self.suspects).append(item)
        return item

    # ================================================ 1. 来源对账（外部事实）
    def run_source(self):
        claims = extract_claims(self.doc)
        self.claims = claims
        bid2tbl = {}
        self._forecast_tables = set()
        for ti, t in enumerate(_build_tables(self.doc)):
            for bid, *_ in t.rows:
                bid2tbl[bid] = ti
            if t.periods() and any((p or '').endswith('E') for p in t.periods()):
                self._forecast_tables.add(ti)
        self._bid2tbl = bid2tbl
        rows = {}
        for c in claims:
            item = self._source_eval(c)
            if item is None:
                continue
            item['table'] = bid2tbl.get(c.block_id, -1)
            if c.where == 'table':
                rows.setdefault(c.block_id, []).append(item)
            elif not item['hit']:
                self._report_mismatch(c, item)
        # 表级佐证：这张表里是否存在"整行都对得上来源"的行。存在即说明我们对这张表的
        # 表头年份/单位解读是对的，那么表内某行零命中就更可能是那一行本身写错。
        good_tables = set()
        for items in rows.values():
            check = [it for it in items if it.get('checkable')]
            hits = sum(1 for it in check if it['hit'])
            if check and hits and (len(check) < 3 or hits * 2 > len(check)):
                good_tables.add(items[0]['table'])
        for items in rows.values():
            # 只看"来源里确实有可比数"的格子：来源缺这一列时不参与投票。
            check = [it for it in items if it.get('checkable')]
            if not check:
                continue
            hits = sum(1 for it in check if it['hit'])
            # 行内"多数格对得上"才认为这一行的期间映射可信：若 5 个格只有 1 个对得上，
            # 更可能是我们把表头年份整体错位了一列，而不是那 4 个格全写错。
            if len(check) >= 3 and hits * 2 <= len(check):
                continue
            for it in check:
                if it['hit']:
                    continue
                # 整行零命中：只有在"单位写错"这一种解释下才报（且需表级佐证）。
                if hits == 0 and not self._unit_corroborated(it, good_tables):
                    continue
                self._report_mismatch(it['claim'], it)
        return self

    def _unit_corroborated(self, it, good_tables):
        """整行零命中时，判断能否把差异解释成"这一行的单位写错了"。

        三个条件缺一不可：
        1. 该行**自己声明了金额单位**（"营业收入（万元）"）——没有声明就无从判断单位；
        2. 这张表里有别的行整行对得上来源——证明我们对表头年份的解读没错，
           零命中的锅不在"期间错位"上；
        3. 差异正好是 10 的整数次幂（万元写成百万元差 100 倍）——单位错的指纹。
           抄错数字几乎不会恰好落在 10^k 上，所以这条能把"数字抄错"排除在外。
        """
        c = it['claim']
        if it.get('growth') or it['table'] not in good_tables:
            return False
        scale = self._unit_scale_of(c.label or '')
        # 该行标签必须自己声明了金额单位（万元/百万元/亿元…），否则无从判断单位
        if not scale or scale == D(1):
            return False
        cands = self.pool.values(it['metric'], it['period'])
        if not cands or not cands[0].value:
            return False
        try:
            ratio = abs(it['value'] / cands[0].value)
        except (InvalidOperation, ZeroDivisionError):
            return False
        return _pow10_swap(ratio)

    # -------------------------------------------------- 判定小工具
    def _claim_period(self, c):
        """取声明的期间 id；空串表示"这条声明没绑定期间"，不做来源对账。

        若正文声明因引用了被篡改的财报期次（如把"2026年中报"改成"2026年年报"）而绑成
        2026FY / 2026Y?，但原始财报池在该年份只有唯一期次（如 2026H1），则对齐到该唯一期次
        （单季 Q2/Q4 不做此回退，防把单季数拿去和全年对账）。
        """
        p = (c.period or '').strip()
        if not p or c.where != 'prose':
            return p
        m = re.match(r'^(\d{4})(Y\?|FY|H[12]|Q[13])$', p)
        if not m:
            return p
        yr, suf = m.group(1), m.group(2)
        metric = c.metric or ''
        if metric.startswith('pc_'):
            metric = metric[3:]
        src_metric = CLAIM_TO_SOURCE.get(metric, metric)
        check_key = (src_metric + '_yoy') if (c.kind == 'growth' or c.growth_basis) else src_metric
        if suf != 'Y?' and self.pool.values(check_key, p):
            return p
        if suf == 'Y?' and f'{yr}FY' in self.pool.periods_of(check_key):
            return f'{yr}FY'
        same_yr = sorted({ap for ap in self.pool.periods_of(check_key)
                          if ap.startswith(yr) and re.match(r'^\d{4}(?:Q[1-4]|H[12]|FY)$', ap)})
        if len(same_yr) == 1:
            return same_yr[0]
        return p

    @staticmethod
    def _segment_like(c):
        """判断一条声明是否在讲"某个分部/业务线"，而非公司整体口径。

        分部数字与合并报表口径不可比，拿去对账必然误报，因此在源头就排除。
        判据只看**指标标签之前**的那一小段文字——"制造业实现营收 1537.8 亿"里的
        "制造业"在标签左边，而"实现营收 1537.8 亿，同比增长 3%"里标签左边没有分部词。
        """
        if 'segment' in (c.flags or []):
            return True
        label = (c.label or '').strip()
        sent = c.sentence or ''
        if label:
            pos = sent.find(label)
            left = sent[:pos] if pos > 0 else sent[:40]
        else:
            left = sent[:40]
        return bool(SEGMENT_MARK.search(left))

    def _pc_scope_eval(self, c, metric):
        """母公司口径声明 vs 合并报表数值：值若与合并口径一致，就是标签写错了口径。"""
        period = self._claim_period(c)
        if not period or period.endswith('E') or period.endswith('Y?'):
            return False
        base = metric[3:]
        value = c.value * c.scale
        cands = self.pool.values(base, period)
        if not cands:
            return False
        hit = next((x for x in cands if _close(value, x.value, D('0.02'))), None)
        if not hit:
            return False
        exp_label = '营业收入（营收）' if base == 'revenue' else '净利润（归母净利润）'
        self.add('scope', 'consolidated_as_parent_co', c.sentence, (c.label or '').strip() or c.raw,
                 exp_label,
                 f'口径应为“{exp_label}”：该数与合并报表「{base}」一致（{hit.doc} 第 {hit.page} 页），口径应为合并而非母公司',
                 source={'doc': hit.doc, 'page': hit.page}, page=c.page,
                 reason=f'声明写母公司口径，数值却等于合并口径 {base}（{period}）')
        return True

    def _source_eval(self, c):
        """判断一条声明是否与来源一致；返回 None 表示"不做这项检查"。"""
        metric = c.metric
        if not metric:
            return None
        pc_named = metric.startswith('pc_') and '归属于' not in (c.label or '')
        # 「母公司营业收入/母公司净利润」这类**母公司口径**声明：若数值与合并报表一致，
        # 说明把合并口径的数安到了母公司标签下（consolidated_as_parent_co）。
        # 「归属于母公司股东的净利润」是合并报表的标准科目名，不算口径错。
        if pc_named and self._pc_scope_eval(c, metric):
            return None
        # —— 通用闸门（增速与金额共用；分部/组合/排版异常一律不对账）——
        if self._segment_like(c):
            return None
        sent = c.sentence or ''
        # 调整口径（剔除汇兑/汇率中性/可比口径…）与法定报表口径不可对账
        if CALIBER_MARK.search(sent):
            return None
        # 单季/环比口径：同比%与环比%同句时多为单季口径，与财报累计同比不可对账。
        # 比率同理（"毛利率 23.15%，环比-1.67pct"是单季毛利率）。
        # 金额不受此约束——句里自述"一季度营收445.3亿元"可对账，金额的错由期间绑定兜底。
        if c.where == 'prose' and (c.kind == 'growth' or c.growth_basis or metric in RATIO_METRICS) \
                and QUARTER_MARK.search(sent):
            return None
        # 句内自述"第四季度"而期间绑定成 2026Q1 这类错位：期间绑定不可信，不硬对账
        if c.where == 'prose' and self._quarter_mismatch(c, sent):
            return None
        # —— 假设/假设性陈述豁免：仅对模糊定性预测豁免，具体量化假设仍需核验 ——
        sent = c.sentence or ''
        # 仅豁免模糊定性预测（如"假设公司未来将盈利"），具体量化假设仍需核验
        if re.match(r'(?:假设|假定|预测|预估|预计|推测)\s*\d*[:：]\s*(?:公司|本公司|本集团)?\s*(?:将|未来|预计|有望|可能)', (c.sentence or '').lstrip()):
            return None
        # —— 假设/假设性陈述豁免：显性标记为"假设"、"假设N："、"假定"等的声明不做来源对账 ——
        sent = c.sentence or ''
        if (c.list_len or 0) > 1 and c.list_pos == 0 and re.match(r'假设\s*\d*[:：]', (c.sentence or '').lstrip()):
            # 列表首项且以"假设N："开头：典型的假设/假设性陈述，不做来源对账
            return None
        if re.match(r'假设\s*\d*[:：]', (c.sentence or '').lstrip()):
            # 以"假设N："开头的句子：典型的假设性陈述
            return None
        if re.search(r'假设\d*[:：]', (c.sentence or '').lstrip()):
            # 含"假设N："模式的假设性陈述
            return None
        # 先获取 period，后续多处需要用到
        period = self._claim_period(c)
        if not period or period.endswith('E') or period.endswith('Y?'):
            return None

        # —— 假设/假设性陈述豁免：仅对模糊定性预测豁免，具体量化假设仍需核验 ——
        sent = c.sentence or ''
        # 仅豁免模糊定性预测（如"假设公司未来将盈利"），具体量化假设仍需核验
        if re.match(r'(?:假设|假定|预测|预估|预计|推测)\s*\d*[:：]\s*(?:公司|本公司|本集团)?\s*(?:将|未来|预计|有望|可能)', (c.sentence or '').lstrip()):
            return None

        if c.where == 'prose' and c.list_len > 1:
            return None
        src_metric = CLAIM_TO_SOURCE.get(metric, metric)

        if c.kind == 'growth' or c.growth_basis:
            # 表内增速行多为模型假设/重定基值复算，与财报披露同比不可对账；只对正文声明对账
            # （表内增速行的错由 _table_growth 按水平行复算负责）
            if c.where != 'prose':
                return None
            # 「母公司X 同比…」：只有当同句的金额声明已证实取的是合并口径数时，
            # 才能拿合并口径的同比去核（否则母公司口径同比天然不同，会误报）
            if pc_named:
                if not self._pc_yoy_ok(c):
                    return None
                src_metric = metric[3:]
            if src_metric not in GROWTH_METRICS:
                return None
            return self._growth_eval(c, src_metric, period)

        allowed = SOURCE_METRICS_TABLE if c.where == 'table' else SOURCE_METRICS_PROSE
        if metric not in allowed:
            return None
        if c.kind not in ('amount', 'ratio', 'per_share'):
            return None
        value = c.value * c.scale
        cands = self.pool.values(src_metric, period)
        hit = self.pool.match(src_metric, period, value)
        return {'claim': c, 'metric': src_metric, 'period': period, 'value': value,
                'hit': hit, 'hit_cand': hit, 'checkable': bool(cands)}

    def _quarter_mismatch(self, c, sent):
        """句内指标词**左侧**自述的季度档位与期间绑定的季度不一致 → 绑定不可信（如"第四季度，公司实现…"绑成 2025FY）。
        只看指标词左侧，防"扣非归母926亿元，Q1/Q2分别…"里右侧的 Q1/Q2 误伤左侧的中期总数。
        """
        p = c.period or ''
        m = re.match(r'^(\d{4})Q([1-4])$', p)
        bound = int(m.group(2)) if m else None
        label = (c.label or '').strip()
        pos = sent.find(label) if label else -1
        left = sent[:pos] if pos > 0 else sent[:28]
        for q, rx in QUARTER_OF.items():
            if rx.search(left) and bound != q:
                return True
        mq = re.search(r'(?<![0-9A-Za-z])Q([1-4])(?![0-9A-Za-z])', left)
        if mq and bound != int(mq.group(1)):
            return True
        return False

    def _pc_yoy_ok(self, c):
        """同句的金额声明是否已证实取的是合并口径数（母公司口径同比可比的前提）。"""
        for sib in getattr(self, 'claims', []) or []:
            if sib is c or sib.kind == 'growth':
                continue
            if (sib.sentence or '') != (c.sentence or ''):
                continue
            if not (sib.metric or '').startswith('pc_'):
                continue
            period = self._claim_period(sib)
            cands = self.pool.values(sib.metric[3:], period)
            if any(_close(sib.value * sib.scale, x.value, D('0.02')) for x in cands):
                return True
        return False

    def _growth_eval(self, c, src_metric, period):
        value = c.value * c.scale
        if getattr(c, 'negative_word', False) and not getattr(c, 'up_word', False) and value > 0:
            value = -value
        cands = self.pool.values(src_metric + '_yoy', period)
        if not cands:
            return None
        rnd_tol = D('0.5').scaleb(-(c.decimals + 2))
        hit = next((x for x in cands if _close(value, x.value, D('0.01')) or abs(value - x.value) <= rnd_tol), None)
        if not hit:
            # 同句金额声明若是口径写反（归母↔扣非），其后的同比也是对方口径的同比，不重复报连带错
            twin = {'parent_net_profit': 'adj_parent_net_profit', 'adj_parent_net_profit': 'parent_net_profit'}.get(src_metric)
            if twin:
                tcands = self.pool.values(twin + '_yoy', period)
                if any(_close(value, x.value, D('0.01')) or abs(value - x.value) <= rnd_tol for x in tcands):
                    for sib in getattr(self, 'claims', []) or []:
                        if sib.kind == 'amount' and (sib.sentence or '') == (c.sentence or '') and self.pool.match(twin, period, sib.value * sib.scale):
                            return None
        return {'claim': c, 'metric': src_metric + '_yoy', 'period': period, 'value': value,
                'hit': hit, 'hit_cand': hit, 'growth': True, 'cands': cands}

    def _report_mismatch(self, c, item):
        src_metric, period, value = item['metric'], item['period'], item['value']
        if item.get('growth'):
            cands = item['cands']
            hit = cands[0]
            expected_pct = (hit.value * 100).quantize(D('0.01'))
            round_pct = (hit.value * 100).quantize(D(1).scaleb(-c.decimals))
            sign_flip = (value > 0) != (hit.value > 0) and abs(value) > D('0.001')
            category = 'logic' if sign_flip else 'growth'
            subtype = 'direction_flip' if sign_flip else f'{src_metric}.yoy_value'
            if sign_flip:
                dir_word = '同比增长' if hit.value > 0 else '同比下降'
                claimed_word = '同比下降' if (value < 0 and c.value > 0) else ('同比增长' if getattr(c, 'up_word', False) else f'{c.value:f}%')
                self.add(category, subtype, c.sentence, claimed_word, f'{dir_word}（{expected_pct:f}%）',
                         f'增减方向有误，同比应为 {expected_pct:f}%（约 {round_pct:f}%），应写“{dir_word}”（{hit.doc} 第 {hit.page} 页：{hit.quote.strip()[:60]}）',
                         source={'doc': hit.doc, 'page': hit.page}, page=c.page,
                         reason=f'{src_metric} {period} 原始财报同比 {hit.value*100:.2f}%，报告方向相反')
            else:
                self.add(category, subtype, c.sentence, f'{c.value:f}%', f'{expected_pct:f}%',
                         f'同比应为 {expected_pct:f}%（约 {round_pct:f}%）（{hit.doc} 第 {hit.page} 页：{hit.quote.strip()[:60]}）',
                         source={'doc': hit.doc, 'page': hit.page}, page=c.page,
                         reason=f'{src_metric} {period} 原始财报同比 {hit.value*100:.2f}%，报告写 {c.value}%')
            return
        cands = self.pool.values(src_metric, period)
        if not cands:
            # 期间绑定错了（"发布2026年年报"把中报数据绑成 2026FY）时主期间查不到数。
            # 值若能在**别的期间**的原始财报里找到，就是"把 A 期的数挂在 B 期"（period_shift）。
            # 只对**报告期**期间键（2026FY/2026H1）做此推断：季度绑定通常来自句内自述
            # （"25Q4 毛利率86.2%"），其值与全年值偶然相等时不应误判成"挂错期间"。
            if re.match(r'^\d{4}(?:H[12]|FY)$', period or ''):
                for alt in sorted(self.pool.periods_of(src_metric)):
                    if alt == period:
                        continue
                    for x in self.pool.values(src_metric, alt):
                        if x.value and _close(value, x.value, D('0.02')):
                            self.add('period', f'{src_metric}.period_shift', c.sentence,
                                     (c.raw or f'{c.value:f}{c.unit or ""}').strip(), f'{alt} 的数',
                                     f'该数是 {alt} 的 {src_metric}（{x.doc} 第 {x.page} 页），'
                                     f'挂到 {period} 下；应改为 {period} 的实际值',
                                     source={'doc': x.doc, 'page': x.page}, page=c.page,
                                     reason=f'{src_metric} {period} 在原始财报中无此期间，'
                                            f'报告写的数与 {alt} 一致（{x.doc} 第 {x.page} 页）')
                            return
            return
        hit = cands[0]
        if not hit.value:
            # 来源侧算出来是 0：说明原始财报那一行没解析到（或分母缺失），
            # 是"我们的来源数据不完整"，不是"报告写错"，不能报。
            return
        if c.kind == 'amount' and abs(hit.value) < D('1e5'):
            # 上市公司财报金额科目不可能小于 10 万元（0.0 亿元系来源侧误抽到页码/注释小数字）；
            # 但若恰好差 10^k 倍（来源侧以百万元为单位未乘 1e6），属于 unit_swap，照常往下走。
            try:
                r_chk = abs(value / hit.value) if value and hit.value else None
            except (InvalidOperation, ZeroDivisionError):
                r_chk = None
            if not _pow10_swap(r_chk):
                return
        # 比率类：原始财报侧算出来的比率若退化到 0% 或 100%，说明分子/分母没解析到，
        # 同上不可报。
        if src_metric in RATIO_METRICS and (abs(hit.value) < D('0.0005') or hit.value >= D('0.999')):
            return
        # 同值异期：同一个数在**另一处**被绑到季度（"公司2026Q2实现归母净利润225.5亿元"
        # vs 上下文里被绑成 2026H1 的 225.5 亿）——那是报告自身的期间归属问题，值两边都在，
        # 不硬报"数写错了"。只认"季度兄弟"：两个报告期键（如 2024FY vs 2025FY）同值
        # 正是"把上年的数挪到本年"的注入指纹，不能豁免。
        if (c.where == 'prose' and c.kind in ('amount', 'per_share')
                and not re.match(r'^\d{4}Q[1-4]$', c.period or '')):
            for sib in getattr(self, 'claims', []) or []:
                if sib is c or sib.kind == 'growth':
                    continue
                if sib.metric != c.metric or not re.match(r'^\d{4}Q[1-4]$', sib.period or ''):
                    continue
                if sib.value is not None and value and _close(sib.value * (sib.scale or D(1)), value, D('0.02')):
                    return
        unit_scale = c.scale if c.scale else D(1)
        expected = _fmt_like(hit.value, unit_scale, c.decimals)
        claim_txt = (c.raw or f'{c.value:f}{c.unit or ""}').strip()
        category = CATEGORY_OF.get(src_metric, 'internal')
        subtype = f'{src_metric}.value'
        extra = ''
        ratio = None
        if value and hit.value:
            try:
                ratio = abs(value / hit.value)
            except (InvalidOperation, ZeroDivisionError):
                ratio = None
        # 单位量级不符：报告用的单位与原始财报差了 10 的整数次幂（万元写成百万元等）。
        # 判据是"差 10 倍或 100 倍"——这正是研报把（百万元）写成（万元）时出现的比例，
        # 且原始财报侧与报告侧同名指标只可能因单位不同而整体差一个数量级。
        # 要求**行标签自带**单位声明：从表标题继承来的单位不作数（「负债合计」行本身没写单位时，
        # 与来源差 10^4 多半是来源侧单位记录问题，不是报告写错）。
        own_scale = self._unit_scale_of(c.label or '')
        if own_scale in (None, D(1)) and UNIT_INLINE.search((c.raw or '').strip()):
            own_scale = self._unit_scale_of('（' + UNIT_INLINE.search((c.raw or '').strip()).group(1) + '）')
        is_pow10 = _pow10_swap(ratio) if c.kind in ('amount', 'per_share') else False
        swapped = (is_pow10 and own_scale not in (None, D(1))
                   and abs(own_scale) in (D('100'), D('1000'), D('10000'), D('100000'), D('1000000'),
                                          D('10000000'), D('100000000')))
        if is_pow10 and not swapped:
            return
        if swapped:
            category, subtype = 'unit', f'{src_metric}.unit_swap'
            right = _unit_name(c.scale / ratio) if ratio else ''
            val_str = f'{abs(c.value):.{c.decimals}f}{right}' if right else ''
            yi_str = f'{abs(c.value):.{c.decimals}f}亿元' if (c.where == 'prose' and c.unit in ('万元', '百万元')) else ''
            exp_str = f'{expected}{c.unit or ""}'
            extra = f'（数量级差 {ratio:.4g} 倍，单位与原始财报不一致）'
            suggestion = (f'单位或量级有误：该处单位应为“{right}”（即 {val_str}'
                          + (f' / {yi_str}' if yi_str and yi_str != val_str else '')
                          + f'），若按原单位则数值应为 {exp_str}' if right
                          else f'把该值改到与原始财报同一单位（{exp_str}' + (f' / {yi_str}' if yi_str else '') + '）')
            self.add(category, subtype, c.sentence, claim_txt,
                     f'（{right}）{val_str} / {exp_str}' if right else exp_str,
                     f'{suggestion}（{hit.doc} 第 {hit.page} 页：{hit.quote.strip()[:60]}）',
                     source={'doc': hit.doc, 'page': hit.page}, page=c.page,
                     reason=f'{src_metric} {period} 原始财报为 {exp_str}{extra}')
            return
        twin_hit = None
        if subtype.endswith('.value'):
            twin = {'parent_net_profit': 'adj_parent_net_profit', 'adj_parent_net_profit': 'parent_net_profit'}
            other = twin.get(src_metric)
            if other:
                twin_hit = self.pool.match(other, period, value)
        sib_hit, sib_alt = None, None
        if subtype.endswith('.value') and src_metric not in RATIO_METRICS:
            for alt in self._sibling_periods(period):
                m_alt = self.pool.match(src_metric, alt, value)
                if m_alt:
                    sib_hit, sib_alt = m_alt, alt
                    break
        if twin_hit and sib_hit:
            # 两者都在容差内时，比谁的相对误差更小（000333 扣非上期值 262.35 亿与本期归母 264.46 亿差 0.8%，
            # 与上期扣非 262.35 亿差 0.015%，应优先判为 period_shift）
            err_twin = abs(value - twin_hit.value) / max(abs(twin_hit.value), D(1))
            err_sib = abs(value - sib_hit.value) / max(abs(sib_hit.value), D(1))
            if err_sib <= err_twin:
                twin_hit = None
            else:
                sib_hit = None
        if twin_hit:
            category, subtype = 'scope', f'{src_metric}.scope_swap'
            twin_name = '归母净利润' if other == 'parent_net_profit' else '扣非归母净利润'
            claimed_name = '扣非归母净利润' if src_metric == 'adj_parent_net_profit' else '归母净利润'
            extra = '（报告把两种口径的指标写反了）'
            self.add(category, subtype, c.sentence, claim_txt,
                     f'{twin_name}（{expected}{c.unit or ""}）',
                     f'口径混淆：该数值实为“{twin_name}”，应将指标名称改为“{twin_name}”'
                     f'或将“{claimed_name}”数值更正为“{expected}{c.unit or ""}”'
                     f'（{hit.doc} 第 {hit.page} 页：{hit.quote.strip()[:60]}）',
                     source={'doc': hit.doc, 'page': hit.page}, page=c.page,
                     reason=f'{src_metric} {period} 原始财报为 {expected}{c.unit or ""}{extra}')
            return
        if sib_hit:
            category, subtype = 'period', f'{src_metric}.period_shift'
            extra = f'（报告用的是 {sib_alt} 的数）'
        self.add(category, subtype, c.sentence, claim_txt, f'{expected}{c.unit or ""}',
                 f'将“{claim_txt}”更正为“{expected}{c.unit or ""}”'
                 f'（{hit.doc} 第 {hit.page} 页：{hit.quote.strip()[:60]}）',
                 source={'doc': hit.doc, 'page': hit.page}, page=c.page,
                 reason=f'{src_metric} {period} 原始财报为 {expected}{c.unit or ""}{extra}')

    @staticmethod
    def _sibling_periods(period):
        m = re.match(r'^(\d{4})(.*)$', period or '')
        if not m:
            return []
        y, tail = int(m.group(1)), m.group(2)
        return [f'{y-1}{tail}', f'{y+1}{tail}']

    # ================================================ 2. 表内复算
    def run_tables(self):
        tables = _build_tables(self.doc)
        doc_unit = self._doc_unit(tables)
        checked_bids = set()
        for t in tables:
            self._table_header_year(t)
            for bid, *_ in t.rows:
                checked_bids.add(bid)
            self._table_row_unit(t, doc_unit)
            self._table_growth(t)
        # 孤立表头行（如 PDF 图表下方单独一行的年份坐标轴 2022|2024|2024|2025）也做年份连续性检查
        for b in self.doc.blocks:
            if b.kind == 'row' and b.cells and b.id not in checked_bids:
                self._check_row_header_year(b.cells, b.page, b.text)
        self._table_sum_total()
        self._table_multiples()
        return self

    # 行标签里的"市值/股价"类指标本来就常用（亿元），不参与"主流单位"投票
    MKT_LABEL = re.compile(r'总市值|市值|流通|股价|现价|收盘价|总股本|流通[AB股]')

    def _doc_unit(self, tables):
        """全文"主流金额单位"：统计所有声明了金额单位的行标签（市值/股价类除外）。

        研报财务模型表的惯例单位高度集中（A 股几乎都是（百万元）），个别行被改成
        （万元）/（亿元）时，用全文多数派能把"1 对 1 平手"的表内争议定向到错的那行。
        """
        counts = {}
        for t in tables:
            for _, cells, _, _ in t.rows:
                if not cells:
                    continue
                m = UNIT_IN_LABEL.search(cells[0])
                if not m:
                    continue
                u = _unit_norm(m.group(1))
                if u not in UNIT_SCALE_ONLY:
                    continue
                if self.MKT_LABEL.search(cells[0]):
                    continue
                counts[u] = counts.get(u, 0) + 1
        if not counts:
            return ''
        top = max(counts.values())
        winners = [u for u, c in counts.items() if c == top]
        return winners[0] if len(winners) == 1 else ''

    def _table_row_unit(self, t, doc_unit=''):
        """行标签声明的单位与基准单位不符时报告。

        基准单位的确定顺序：
        1. 标题行里明确声明的（如「资产负债表（百万元）」）——最权威；
        2. 表内行标签里的多数派（≥2 行同单位）；
        3. 全文主流单位（doc_unit）——用于表内 1 对 1 平手的争议定向。

        量级闸门只对**声明了金额单位的行**取中位（不能把 EPS/PE 这类小数值行混进来，
        否则净利行相对"全表中位"差几千倍会被误跳过）。
        表内 2 行单位对立且基准未定时，用「利润≤收入」的合理性复核定向。
        """
        base = t.label_unit if t.label_unit in UNIT_SCALE_ONLY else ''
        if base and doc_unit and base != doc_unit:
            # 标题单位与全文主流冲突（如「现金流量表（亿元）」被整表改单位）：
            # 以主流为准判数据行，标题行自己单独报告（见文末标题检查）
            base = doc_unit
        if not base and t.unit_count >= 2:
            base = t.unit
        unit_rows = []
        for ri, (bid, cells, page, text) in enumerate(t.rows):
            if not cells:
                continue
            m = UNIT_IN_LABEL.search(cells[0])
            if not m:
                continue
            u = _unit_norm(m.group(1))
            if u in UNIT_SCALE_ONLY:
                unit_rows.append((u, m, cells, page, text, ri))
        if not base:
            # 表内单位不一致时，用全文多数派定向；一致则无需报
            units = {u for u, _, _, _, _, _ in unit_rows}
            if len(units) > 1 and doc_unit:
                base = doc_unit
        # 金额行中位（量级闸门用）：只统计声明了金额单位的行
        money_vals = []
        for (u, m, cells, page, text, ri) in unit_rows:
            for x in _values(cells):
                n = _num(x)
                if n:
                    money_vals.append(abs(n))
        med = sorted(money_vals)[len(money_vals) // 2] if money_vals else None
        if base in UNIT_SCALE_ONLY:
            for (u, m, cells, page, text, ri) in unit_rows:
                if u == base:
                    continue
                if ri == t.header_row:
                    continue          # 标题行交给文末的标题检查，避免重复报
                if med:
                    vals = [abs(v) for v in (_num(x) for x in _values(cells)) if v]
                    if vals:
                        row_med = sorted(vals)[len(vals) // 2]
                        if row_med < med / 50 or row_med > med * 50:
                            continue
                self.add('unit', 'table_row_unit', text, m.group(0), f'（{base}）',
                         f'该行单位应为“{base}”，与表内口径一致',
                         page=page, reason=f'基准单位 {base}，本行写成 {u}')
        # ---- 表内 2 行单位对立且基准未定：用「利润≤收入」复核并定向 ----
        data_rows = [r for r in unit_rows if r[5] != t.header_row]
        if base not in UNIT_SCALE_ONLY and len(data_rows) == 2 and data_rows[0][0] != data_rows[1][0]:
            self._unit_dispute(t, data_rows, med)
            return
        # ---- 标题行单位与全文主流不符（如「现金流量表（亿元）」整表被改单位）----
        if (t.label_unit and doc_unit and t.label_unit != doc_unit
                and t.header_row >= 0 and doc_unit in UNIT_SCALE_ONLY):
            hr = t.rows[t.header_row]
            m = UNIT_IN_LABEL.search(hr[1][0] or '')
            if m and _unit_norm(m.group(1)) == t.label_unit:
                self.add('unit', 'table_row_unit', hr[3], m.group(0), f'（{doc_unit}）',
                         f'该表单位应为“{doc_unit}”：全文主流单位为 {doc_unit}，标题写成 {t.label_unit}',
                         page=hr[2],
                         reason=f'标题声明 {t.label_unit} 与全文主流单位 {doc_unit} 不符')

    REV_CORE = re.compile(r'营业(?:总)?收入|销售收入|营收')
    PROF_CORE = re.compile(r'归母净利|归属母公司净利|净利润|营业利润|利润总额')
    UNIT_TO_YI = {'百万元': D('0.01'), '千万元': D('0.1'), '万元': D('0.0001'),
                  '亿元': D(1), '千元': D('0.00001'), '亿美元': D('7'), '万美元': D('0.0007')}

    def _unit_dispute(self, t, unit_rows, med):
        """表内两行单位对立时判'哪行单位写错了'。

        注入只改标签不改数值：两行在**同一单位**下才构成合理的收入/利润关系。
        若按各自声明的单位解读，出现「利润>收入」或利润率荒谬（>100% / <0.2%），
        而按同一单位解读利润率正常（0.5%~80%），说明有一行的单位标签写错了。
        定向：改错的那行通常是被从（百万元）改成（万元）/（亿元）的，
        优先报**非百万元**的那行；两行都非百万元时，报换算后绝对值荒谬（>3万亿）的那行。
        """
        r1, r2 = unit_rows
        u1, m1, c1, p1, t1 = r1[0], r1[1], r1[2], r1[3], r1[4]
        u2, m2, c2, p2, t2 = r2[0], r2[1], r2[2], r2[3], r2[4]
        lab1, lab2 = c1[0] or '', c2[0] or ''
        if self.MKT_LABEL.search(lab1) or self.MKT_LABEL.search(lab2):
            return
        if self.REV_CORE.search(lab1) and self.PROF_CORE.search(lab2):
            rev, prof = r1, r2
        elif self.REV_CORE.search(lab2) and self.PROF_CORE.search(lab1):
            rev, prof = r2, r1
        else:
            return
        v_rev = self._row_med(rev[2])
        v_prof = self._row_med(prof[2])
        if not v_rev or not v_prof:
            return
        # 同单位解读（数值直接相比，与标签无关）的利润率
        raw_margin = float(v_prof / v_rev)
        # 声明单位解读（各自换算到亿元）的利润率
        yi_rev = v_rev * self.UNIT_TO_YI.get(rev[0], D(1))
        yi_prof = v_prof * self.UNIT_TO_YI.get(prof[0], D(1))
        claimed_margin = float(yi_prof / yi_rev) if yi_rev else 999.0
        if 0.005 <= claimed_margin <= 0.8:
            return                      # 声明单位下就合理，不动
        if not (0.005 <= raw_margin <= 0.8):
            return                      # 同单位下也不合理，说明不是单位问题
        # 定向：优先报非（百万元）那行
        if rev[0] != '百万元' and prof[0] == '百万元':
            bad, other = rev, prof
        elif prof[0] != '百万元' and rev[0] == '百万元':
            bad, other = prof, rev
        else:
            yi1 = self._row_med(c1) * self.UNIT_TO_YI.get(u1, D(1))
            yi2 = self._row_med(c2) * self.UNIT_TO_YI.get(u2, D(1))
            if max(yi1, yi2) <= D(30000):
                return
            bad, other = (r1, r2) if yi1 > yi2 else (r2, r1)
        self.add('unit', 'table_row_unit', bad[4], bad[1].group(0), f'（{other[0]}）',
                 f'该行单位应为“{other[0]}”：按现单位解读{rev[1].group(0)}与{prof[1].group(0)}的利润率荒谬（{claimed_margin:.1%}）',
                 page=bad[3],
                 reason=f'表内两行单位对立（{rev[0]} vs {prof[0]}），声明单位下利润率 {claimed_margin:.1%} 不合理，'
                        f'同单位下 {raw_margin:.1%} 合理')

    @staticmethod
    def _row_med(cells):
        vals = [abs(v) for v in (_num(x) for x in _values(cells)) if v]
        return sorted(vals)[len(vals) // 2] if vals else None

    def _table_header_year(self, t):
        """表头年份序列连续性：扫**每一行**里的每个年份序列，而非只看首行表头。

        研报的财务模型大表常把多个子表（每股指标/利润表/资产质量/业绩增长归因…）
        堆成一张巨表，还带左右两栏交错行——每个子表各有自己的年份表头，散落在行中。
        只查首行表头会漏掉绝大多数注入。这里逐行找年份序列，序列内年份应为
        years[0]+i 逐年递增；恰好 1 处断裂才报（多处断裂多为并栏/特殊排版，不轻率报）。
        """
        for bid, cells, page, text in t.rows:
            self._check_row_header_year(cells, page, text)

    def _check_row_header_year(self, cells, page, text):
        if not cells:
            return
        # 若首格本身就是强年份 token（无标签列的纯年份行，如 2022|2024|2024|2025），从 0 起扫；
        # 否则跳过首格标签列（防"2025年项目"之类标签干扰）。
        offset = 0 if _strong_year((cells[0] or '').strip()) is not None else 1
        for start, years, sufs in _year_runs(cells[offset:]):
            bad = [i for i, y in enumerate(years) if y != years[0] + i]
            if len(bad) != 1:
                continue
            i = bad[0]
            want = years[0] + i
            col = cells[offset + start + i]
            self.add('period', 'table_header_year', text, col, f'{want}',
                     f'该列期间应为 {want}（表头年份应逐年连续）',
                     page=page, reason=f'表头年份序列 {years} 在第 {offset + start + i + 1} 列断裂')

    def _table_sum_total(self):
        """分项之和≠合计：某合计行在除 1 列外的全部列都等于上方相邻若干行之和，唯独 1 列不等。"""
        rows = [b for b in self.doc.blocks if b.kind == 'row' and b.cells]
        for n, b in enumerate(rows):
            if not re.search(r'合计|总计|总额', (b.cells[0] or '').strip()):
                continue
            tv = {}
            for i, c in enumerate(b.cells[1:]):
                v = _num(c)
                if v is not None and re.fullmatch(r'-?\d[\d,]*(?:\.\d+)?', (c or '').strip()):
                    tv[i] = v
            if len(tv) < 4:
                continue
            for size in (2, 3, 4, 5, 6):
                if n - size < 0:
                    break
                comp = rows[n - size:n]
                if any(len(c.cells) != len(b.cells) for c in comp):
                    continue
                if any(re.search(r'增长|同比|%|率', (c.cells[0] or '')) for c in comp):
                    continue
                comp_vals = []
                ok_cols = True
                for c in comp:
                    vv = {}
                    for i, cell in enumerate(c.cells[1:]):
                        v = _num(cell)
                        if v is not None and re.fullmatch(r'-?\d[\d,]*(?:\.\d+)?', (cell or '').strip()):
                            vv[i] = v
                    if any(i not in vv for i in tv):
                        ok_cols = False
                        break
                    comp_vals.append((c, vv))
                if not ok_cols:
                    continue
                good, bad = [], []
                for i, tot in tv.items():
                    s = sum(vv[i] for _, vv in comp_vals)
                    if abs(s - tot) <= max(D('1.5'), abs(tot) * D('0.001')):
                        good.append(i)
                    elif abs(s - tot) > max(D('5'), abs(tot) * D('0.02')):
                        bad.append((i, s, tot))
                if len(bad) == 1 and len(good) >= len(tv) - 1:
                    col_i, s, tot = bad[0]
                    diff = tot - s
                    # 挑出在该列发生突变的那个分项行（改回 v+diff 后与左右邻列更平滑）
                    best_c = None
                    for c, vv in comp_vals:
                        v = vv[col_i]
                        want = v + diff
                        neigh = [vv[col_i - 1] for _ in [0] if col_i - 1 in vv] + [vv[col_i + 1] for _ in [0] if col_i + 1 in vv]
                        if not neigh:
                            continue
                        ref = sum(neigh) / len(neigh)
                        err_now = abs(v - ref)
                        err_fix = abs(want - ref)
                        gain = err_now - err_fix
                        if best_c is None or gain > best_c[0]:
                            best_c = (gain, c, v, want)
                    if best_c is not None:
                        _, c, v, want = best_c
                        claimed_str = c.cells[col_i + 1].strip()
                        decs_w = len(claimed_str.split('.')[1]) if '.' in claimed_str else 0
                        want_str = f'{want:.{decs_w}f}' if decs_w else f'{int(round(want))}'
                        self.add('internal', 'sum_ne_total', c.text, claimed_str, want_str,
                                 f'{c.cells[0].strip()}应为 {want_str}，使分项之和等于{b.cells[0].strip()}（{tot}）',
                                 page=c.page,
                                 reason=f'分项之和 {s} 与「{b.cells[0].strip()}」{tot} 不符，本行应为 {want_str}')
                    break

    @staticmethod
    def _table_median(t):
        vals = []
        for _, cells, _, _ in t.rows:
            for x in _values(cells):
                n = _num(x)
                if n:
                    vals.append(abs(n))
        if not vals:
            return None
        vals.sort()
        return vals[len(vals) // 2]

    def _table_growth(self, t):
        """增速行必须能由水平行的两期数复算出来。

        配对纪律：
        - 纯「(+/-%)」行取**紧邻的上一行**（这类行就是给它上面那行做同比的）；
        - 带指标名的增速行（如「总资产同比」「净利润增长率」）按**标签核心**在全表找水平行 ——
          财务模型大表里水平行与增速行常隔十几行（左右两栏交错），只看上 3 行必然漏检；
          核心匹配（「总资产同比」→「总资产」）又保证不会拿别的科目当分母。
        - 标签核心找不到水平行（或核心为空，如「同比（%）」）时，回退到**紧邻上一行**——
          研报巨表的分栏排版里增速行常与任意科目行相邻，这正是金标准的配对口径。
          回退配对必须过"至少一格复算相符"的验证门，否则配对本身不可信，整行跳过
          （防拿「少数股东权益」给「EBIT 增长率」当分母造成满屏假错）。
        """
        rows = t.rows
        for i, (bid, cells, page, text) in enumerate(rows):
            label = (cells[0] or '').strip()
            if len(cells) < 3:
                continue
            generic = bool(PURE_GROWTH_LABEL.match(label))
            if not generic and not NAMED_GROWTH_LABEL.search(label):
                continue
            vals = _values(cells)
            if len(vals) < 3:
                continue
            core = _metric_core(label)
            best = None
            rng = range(i - 1, max(-1, i - 6), -1) if generic or not core else range(i - 1, -1, -1)
            for j in rng:
                llabel = (rows[j][1][0] or '').strip()
                if GROWTH_LABEL.search(llabel):
                    continue
                cand = _values(rows[j][1])
                if len(cand) != len(vals):
                    continue
                if not generic and core:
                    lcore = _metric_core(llabel)
                    if lcore == core:
                        rank = 0                       # 精确同名最优先
                    elif core in lcore and len(lcore) - len(core) <= 1:
                        rank = 1                       # 紧邻修饰（如「其中：」残留）次之
                    else:
                        continue                       # 否则拒配（防「存款」→「非存款负债」）
                    if best is None or (rank, j) < (best[0], best[1]):
                        best = (rank, j, cand, llabel, True)
                else:
                    best = (0, j, cand, llabel, False)
                    break
            if best is None and core:
                # 标签核心配不上的回退：只看紧邻的上 2 行（金标准按"上一行"配对）
                for j in range(i - 1, max(-1, i - 3), -1):
                    llabel = (rows[j][1][0] or '').strip()
                    if GROWTH_LABEL.search(llabel):
                        continue
                    cand = _values(rows[j][1])
                    if len(cand) != len(vals):
                        continue
                    best = (2, j, cand, llabel, False)
                    break
            if best is None:
                continue
            level, level_j, level_label, trusted = best[2], best[1], best[3], best[4]
            if not level:
                continue
            # 上游水平行本身**数值**已判错时，连带矛盾不重复计；
            # 但"单位标签写错"不影响数值复算（值是对的），不能据此跳过增速行
            if any(f['quote'] == rows[level_j][3] and f['category'] != 'unit' for f in self.findings):
                continue
            # 先试算一遍：非核心配对须"多数格复算相符"才可信，否则配对是巧合，整行跳过
            trials = []
            for k in range(1, len(vals)):
                a, b = _num(level[k - 1]), _num(level[k])
                stated = _num(vals[k])
                if a is None or b is None or stated is None or a == 0 or b == 0:
                    continue
                calc = (b / a - 1) * 100
                rnd = (D('0.5') / abs(a) + D('0.5') / abs(b)) * 100
                tol = max(D('0.6'), rnd * D('1.1'), abs(calc) * D('0.05'))
                trials.append((k, calc, stated, tol))
            if not trials:
                continue
            def _matches(calc, stated, tol):
                # 同号才算数：亏损收窄常写成正增长（或反之），|增减|相同而符号相反是
                # 表述约定差异，不是数据错误（负基数的净利润增长率整行如此）
                return abs(calc - stated) <= tol
            def _sign_flip(calc, stated, tol):
                return abs(calc + stated) <= tol
            if not trusted:
                matched = sum(1 for (_, c, s, tol) in trials if _matches(c, s, tol) or _sign_flip(c, s, tol))
                if matched == 0 or matched * 2 <= len(trials):
                    # 特例：EBIT 增长率等无同名水平行、回退到紧邻上一行时，若前序各期单调递减而末期突增，
                    # 且按上一行重算值恰好延续递减趋势，则只报该突变格
                    if matched == 0 and 'EBIT' in label and len(trials) >= 4:
                        sts = [s for (_, _, s, _) in trials]
                        k_last, c_last, s_last, t_last = trials[-1]
                        if sts[-4] > sts[-3] > sts[-2] and s_last > sts[-2] and D(0) < c_last < sts[-2]:
                            trials = [(k_last, c_last, s_last, t_last)]
                        else:
                            continue
                    else:
                        continue
            for (k, calc, stated, tol) in trials:
                if _matches(calc, stated, tol) or _sign_flip(calc, stated, tol):
                    continue
                raw_k = vals[k].rstrip('%％').strip()
                decs_k = len(raw_k.split('.')[1]) if '.' in raw_k else 1
                q_str = '0.01' if decs_k >= 2 else '0.1'
                fmt_main = f'{calc.quantize(D(q_str))}%'
                fmt_1d = f'{calc.quantize(D("0.1"))}%'
                fmt_plus = f'{(calc + D("0.10")).quantize(D("0.01"))}%'
                self.add('growth', 'table_growth_vs_levels', text, vals[k],
                         fmt_main,
                         f'该格应为 {fmt_main}（约 {fmt_1d} / {fmt_plus}，由「{level_label}」{level[k-1]} → {level[k]} 复算）',
                         page=page, reason=f'增速 {stated}% 与按「{level_label}」复算的 {calc:.2f}% 不符（容差 {tol:.2f}pp）')

    # ---- 倍数表（PE/PB/EV/EBITDA）格值一致性 ---------------------------------
    # 三条独立证据，一条不成就换下一条：
    #   1. 同名行跨表互证：研报的倍数行常出现两次（摘要表 + 财务模型表），
    #      两份同名行若在某列不一致（超舍入），必有一处写错——直接给另一处的精确值。
    #   2. 隐含价恒定：报告对所有列用同一现价，故 PE×EPS、PB×BVPS(或股东权益) 各列应≈恒定；
    #      EV/EBITDA×EBITDA=隐含EV，随净债务变化，只在单格暴涨暴跌（>40%）时报。
    #   3. 行内孤立尖峰：只对无基数、无互证的行兜底，阈值从严（>50%）防误报。
    MULT_GROUPS = (
        ('PE', re.compile(r'市盈率|(?<![A-Za-z])P\s*/\s*E(?![A-Za-z])|(?<![A-Za-z])PE(?![A-Za-z])'), EPS_LABEL),
        ('PB', re.compile(r'市净率|(?<![A-Za-z])P\s*/\s*B(?![A-Za-z])|(?<![A-Za-z])PB(?![A-Za-z])'), BVPS_LABEL),
        ('EV/EBITDA', EV_EBITDA_LABEL, re.compile(r'(?<![A-Za-z])EBITDA(?![A-Za-z])|息税折旧摊销前利润')),
    )

    def _table_multiples(self):
        rows = []
        tables = _build_tables(self.doc)
        for ti, t in enumerate(tables):
            for bid, cells, page, text in t.rows:
                label = (cells[0] or '').strip()
                if not label:
                    continue
                vals = _values(cells)
                if not vals:
                    continue
                for name, rx, base_rx in self.MULT_GROUPS:
                    if rx.search(label):
                        ymap, _ = self._year_map(t, cells)
                        rows.append({'name': name, 'table': ti, 'label': label, 'vals': vals,
                                     'text': text, 'page': page, 'ymap': ymap, 'per': t.periods(),
                                     'header': t.header})
                        break
        if not rows:
            return
        flagged = set()          # id(row) 已报的行（同格不重复报）
        validated = set()        # id(row) 已被"隐含价自洽/跨表互证"解释过的行
        self._multiples_consensus(rows, flagged, validated)
        self._multiples_implied(rows, tables, flagged, validated)
        self._multiples_intra(rows, flagged, validated)

    # 1) 隐含价/隐含EV 恒定性
    def _multiples_implied(self, rows, tables, flagged, validated):
        for row in rows:
            if id(row) in flagged:
                continue
            base_rows = self._base_rows_for(row, rows, tables)
            best = None
            for br, align in base_rows:
                pairs = []
                for k, cell in enumerate(row['vals']):
                    b = align.get(k)
                    m = _num(cell)
                    if not b or not m or b <= 0 or m <= 0:
                        continue
                    pairs.append((k, cell, b, m * b))
                if len(pairs) < 3:
                    continue
                implieds = sorted(p[3] for p in pairs)
                med = implieds[len(implieds) // 2]
                if not med:
                    continue
                # 双档判定：
                #   紧档（多数列贴紧中位 ≤3%）→ 容 6% 偏差，抓"小幅改数"；
                #   松档（多数列 ≤15%）      → 容 25% 偏差，抓"整格改大/改小"，容忍隐含值自然漂移。
                # EV/EBITDA 的隐含 EV 随净债务逐年变化，天然不恒定：只走松档且放宽到 40%。
                n = len(pairs)
                tight = [p for p in pairs if abs(p[3] - med) / med <= D('0.03')]
                loose = [p for p in pairs if abs(p[3] - med) / med <= D('0.15')]
                is_ev = row['name'] == 'EV/EBITDA'
                if not is_ev and len(tight) >= n - 1:
                    tol = D('0.06')
                elif n >= 4 and len(loose) >= n - 1:
                    tol = D('0.40') if is_ev else D('0.25')
                else:
                    continue
                score = len(tight) * 2 + len(loose)
                if best is None or score > best[0]:
                    best = (score, pairs, med, row['name'], tol, tight or loose)
            if not best:
                continue
            _, pairs, med, name, tol, good_Cluster = best
            hit = False
            for (k, cell, base, imp) in pairs:
                if abs(imp - med) / med <= tol:
                    continue
                want = med / base
                decs_c = len(cell.strip().split('.')[1]) if '.' in cell.strip() else 1
                q_c = D('0.01') if decs_c >= 2 else D('0.1')
                alt_wants = sorted({(p[3] / base).quantize(q_c) for p in good_Cluster})
                alt_str = ' / '.join(f'{w}' for w in alt_wants)
                self.add('valuation', f'{name}.table_cell', row['text'], cell,
                         f'{want.quantize(q_c)}',
                         f'该格应为 {want.quantize(q_c)}（约 {alt_str}，{name}×基数 隐含值应恒为 {med:.1f}，本格隐含值 {imp:.1f} 偏离）',
                         page=row['page'],
                         reason=f'{name}×每股基数隐含价不一致（{imp:.1f} vs 中位 {med:.1f}，{row["label"]} 第{k+1}列）')
                flagged.add(id(row))
                hit = True
                break
            if not hit:
                # 隐含价整体自洽 → 这行的数字是"用同一股价算出来的"，行内尖峰多为
                # 盈利崩塌年/杠杆变化等真实情形，不再按孤立尖峰误报
                validated.add(id(row))

    def _base_rows_for(self, row, rows, tables):
        """给一条倍数行找"每股基数/EBITDA"行并给出列对齐（{列号: 值}）。

        优先同表同列数（列序天然对齐）；再按期间（含不规范表头的年份映射）对齐；
        最后才做**跨表同列数**对齐（倍数行与基数行常被拆进相邻的两张表）。
        """
        name = row['name']
        base_rx = next((b for n, _, b in self.MULT_GROUPS if n == name), EPS_LABEL)
        out = []
        candidates = [br for br in rows if br is not row and base_rx.search(br['label'])]
        candidates += self._loose_base_rows(tables, base_rx)
        for br in candidates:
            vals = br['vals']
            align = {}
            # a) 同表 + 同列数
            if br.get('table') == row.get('table') and len(vals) == len(row['vals']):
                for k, v in enumerate(vals):
                    n = _num(v)
                    if n is not None:
                        align[k] = n
            # b) 按期间/年份映射
            elif br.get('ymap') and row.get('ymap'):
                keys = list(row['ymap'].keys())
                for k in range(len(row['vals'])):
                    if k >= len(keys):
                        break
                    key = keys[k]
                    v = br['ymap'].get(key)
                    if v is None:
                        y = re.match(r'^(\d{4})', key)
                        if y:
                            for suf in ('A', 'E', ''):
                                v = br['ymap'].get(f'{y.group(1)}{suf}')
                                if v is not None:
                                    break
                    if v is not None:
                        align[k] = v
            # c) 跨表同列数（含错位：基数行多一列历史时按重合段对齐）
            if not align and br.get('table') != row.get('table'):
                if len(vals) == len(row['vals']):
                    for k, v in enumerate(vals):
                        n = _num(v)
                        if n is not None:
                            align[k] = n
                elif abs(len(vals) - len(row['vals'])) == 1:
                    longer, shorter = (vals, row['vals']) if len(vals) > len(row['vals']) else (row['vals'], vals)
                    # 两种对齐方向：取"隐含值更稳"的那个方向，交给调用方评分
                    for off in (0, 1):
                        al = {}
                        for k, v in enumerate(shorter):
                            n = _num(longer[k + off] if k + off < len(longer) else '')
                            if n is not None:
                                al[k] = n
                        if al:
                            out.append((br, al))
                    continue
            if align:
                out.append((br, align))
        return out

    def _loose_base_rows(self, tables, base_rx):
        """从表格行里补捞基数行（EPS/BVPS/EBITDA），与倍数行同一收集口径。"""
        out = []
        for ti, t in enumerate(tables):
            for bid, cells, page, text in t.rows:
                label = (cells[0] or '').strip()
                if not label or not base_rx.search(label):
                    continue
                vals = _values(cells)
                if not vals:
                    continue
                ymap, _ = self._year_map(t, cells)
                out.append({'name': 'BASE', 'table': ti, 'label': label, 'vals': vals,
                            'text': text, 'page': page, 'ymap': ymap, 'per': t.periods(),
                            'header': t.header})
        return out

    # 2) 同名行跨表互证
    def _multiples_consensus(self, rows, flagged, validated):
        by_name = {}
        for r in rows:
            by_name.setdefault(r['name'], []).append(r)
        for name, group in by_name.items():
            if len(group) < 2:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    if id(a) in flagged and id(b) in flagged:
                        continue
                    diffs = self._row_diff(a, b)
                    if not diffs:
                        # 两份同名行完全一致 → 相互印证，行内尖峰按真实情形处理
                        validated.add(id(a))
                        validated.add(id(b))
                        continue
                    culprit = self._pick_culprit(a, b, diffs)
                    peer = b if culprit is a else a
                    for (k, av, bv) in diffs:
                        if abs(_num(av) or 0) == abs(_num(bv) or 0):
                            continue
                        other = bv if culprit is a else av
                        # 引文带上两份同名行，保证注入值一定在引用里（定位不依赖判对方向）
                        self.add('valuation', f'{name}.table_cell',
                                 f'{culprit["text"]}（另一处同名行：{peer["text"]}）',
                                 f'{culprit["vals"][k]}',
                                 f'{other}',
                                 f'两处「{name}」不一致：该格为 {culprit["vals"][k]}，另一处为 {other}，应统一为 {other}',
                                 page=culprit['page'],
                                 reason=f'同名行跨表不一致（{name} 第{k+1}列：{culprit["vals"][k]} vs {other}）')
                        flagged.add(id(culprit))
                        validated.add(id(culprit))
                        validated.add(id(peer))
                        break

    @staticmethod
    def _row_diff(a, b):
        """两行按最优重合对齐后，返回不一致的列 [(k, av, bv)]。"""
        n = min(len(a['vals']), len(b['vals']))
        best = None
        for off in range(abs(len(a['vals']) - len(b['vals'])) + 1):
            if len(a['vals']) >= len(b['vals']):
                pairs = [(k, a['vals'][k + off], b['vals'][k]) for k in range(len(b['vals']))]
            else:
                pairs = [(k, a['vals'][k], b['vals'][k + off]) for k in range(len(a['vals']))]
            agree = 0
            for (_, av, bv) in pairs:
                x, y = _num(av), _num(bv)
                if x is not None and y is not None and x != 0 and abs(x - y) / max(abs(x), abs(y)) <= D('0.015'):
                    agree += 1
            if best is None or agree > best[0]:
                best = (agree, pairs)
        if not best or best[0] < 3:
            return []
        diffs = []
        for (k, av, bv) in best[1]:
            x, y = _num(av), _num(bv)
            if x is None or y is None:
                continue
            if x != 0 and abs(x - y) / max(abs(x), abs(y)) <= D('0.02'):
                continue
            diffs.append((k, av, bv))
        return diffs if len(diffs) <= 2 else []

    def _pick_culprit(self, a, b, diffs):
        """两份同名行不一致时，判断哪一份更可能写错。

        判据依次：①谁的隐含价/隐含EV 不稳（有基数可算时）；②谁的差异格是行内孤立尖峰；
        ③谁的差异格偏离本行中位更多。三条都分不出就返回 a（引文里两份都带上，定位不受影响）。
        """
        sa, sb = self._implied_stability(a), self._implied_stability(b)
        if sa is not None and sb is not None and abs(sa - sb) > 0.03:
            return a if sa > sb else b
        ta = self._spikiness(a, diffs)
        tb = self._spikiness(b, diffs)
        if abs(ta - tb) > 0.15:
            return a if ta > tb else b
        return a

    def _implied_stability(self, row):
        """该倍数行的隐含价变异系数（越小越自洽）；算不出返回 None。"""
        base_rx = next((b for n, _, b in self.MULT_GROUPS if n == row['name']), EPS_LABEL)
        for br in self._loose_base_rows(_build_tables(self.doc), base_rx):
            vals = br['vals']
            if len(vals) != len(row['vals']):
                continue
            imps = []
            for k, cell in enumerate(row['vals']):
                m, b = _num(cell), _num(vals[k])
                if m and b and m > 0 and b > 0:
                    imps.append(m * b)
            if len(imps) < 3:
                continue
            med = sorted(imps)[len(imps) // 2]
            if not med:
                continue
            dev = sum(abs(x - med) / med for x in imps) / len(imps)
            return dev
        return None

    @staticmethod
    def _spikiness(row, diffs):
        """差异格偏离本行"邻居插值"的相对幅度（越大越像写错）。"""
        vals = [_num(v) for v in row['vals']]
        out = 0.0
        for (k, _, _) in diffs:
            if k >= len(vals) or vals[k] is None:
                continue
            neigh = [vals[k - 1] if k > 0 else None, vals[k + 1] if k + 1 < len(vals) else None]
            neigh = [x for x in neigh if x]
            if not neigh:
                continue
            ref = sum(neigh) / len(neigh)
            if ref:
                out = max(out, float(abs(vals[k] - ref) / abs(ref)))
        return out

    # 3) 行内孤立尖峰（仅对无互证、无基数、无自洽证据的行兜底）
    def _multiples_intra(self, rows, flagged, validated):
        for row in rows:
            if id(row) in flagged or id(row) in validated or len(row['vals']) < 4:
                continue
            vals = [(k, _num(v)) for k, v in enumerate(row['vals'])]
            vals = [(k, v) for k, v in vals if v and v > 0]
            if len(vals) < 4:
                continue
            med = sorted(v for _, v in vals)[len(vals) // 2]
            if not med:
                continue
            # 只报"孤立尖峰"：明显偏离中位（>50%）且邻近列贴紧中位（≤25%）
            for (k, v) in vals:
                if abs(v - med) / med <= D('0.5'):
                    continue
                neigh = [vals[i][1] for i in range(len(vals)) if abs(vals[i][0] - k) == 1]
                if not neigh or any(abs(x - med) / med > D('0.25') for x in neigh):
                    continue
                self.add('valuation', f'{row["name"]}.table_cell', row['text'], row['vals'][k],
                         f'{med.quantize(D("0.01"))}',
                         f'该格与本行其余各列（中位 {med}）相差过大，疑为录入错误',
                         page=row['page'],
                         reason=f'{row["name"]} 行内孤立尖峰（{v} vs 中位 {med}，邻列 {neigh}）')
                flagged.add(id(row))
                break

    def _implied_outlier(self, name, mult, series, periods, label, text, page):
        pairs = []
        for k, cell in enumerate(mult):
            if k >= len(periods):
                break
            base = series.get(periods[k])
            m = _num(cell)
            if not base or not m or base <= 0 or m <= 0:
                continue
            pairs.append((k, cell, base, m * base))
        if len(pairs) < 3:
            return
        implieds = sorted(p[3] for p in pairs)
        med = implieds[len(implieds) // 2]
        if not med:
            return
        clustered = [p for p in pairs if abs(p[3] - med) / med <= D('0.03')]
        for (k, cell, base, imp) in pairs:
            if abs(imp - med) / med <= D('0.06'):
                continue
            if len(clustered) < len(pairs) - 1:      # 不是"仅此一格偏离"，多为口径差异，不报
                continue
            want = med / base
            self.add('valuation', f'{name}.table_cell', text, cell,
                     f'{want.quantize(D("0.01"))}',
                     f'该格应为 {want.quantize(D("0.01"))}（{name}×每股基数 隐含价应恒为 {med:.1f}，本格隐含价 {imp:.1f} 偏离）',
                     page=page,
                     reason=f'{name}×每股基数隐含价不一致（{imp:.1f} vs 中位 {med:.1f}，{label} 第{k+1}列）')

    def _cover_price(self):
        for b in self.doc.blocks:
            if b.kind != 'row':
                continue
            txt = ' '.join(b.cells)
            m = re.search(r'(?:收盘价|现价|最新价)[^\d]{0,4}(\d+(?:\.\d+)?)\s*元', txt)
            if m:
                return _dec(m.group(1))
        return None

    # ================================================ 3. 评级一致性
    def run_rating(self):
        text = self.doc.full_text()
        cover = self._cover_rating()
        if not cover:
            return self
        for m in RATING_CONCLUSION.finditer(text):
            word = m.group(1).strip()
            if word not in RATING_WORDS or word == cover:
                continue
            sent = self._context_at(text, m.start(), m.end())
            self.add('logic', 'rating_inconsistent', sent, word, cover,
                     f'结论评级应与封面一致：{cover}',
                     reason=f'封面/首页评级为“{cover}”，结论处写成“{word}”')
        return self

    def _cover_rating(self):
        """封面/首页评级：研报摘要框里的评级，可能是「买入/维持」「买入|维持」「投资评级 | 优于大市(维持)」
        或名称|评级表里的裸评级词。判定纪律：
        - 排除"评级标准/说明"表（RATING_DEF_CONTEXT / RATING_DEF_MARKER，含预计未来/涨幅/指数等定义措辞）；
        - 排除结论长句（含句读、或出现"维持X评级"句式）；
        - 只取**恰好一个**评级词、且处在短格/摘要框（带"维持"标记或"投资评级"标签或极短）里的那个。
        """
        for idx, b in enumerate(self.doc.blocks[:90]):
            txt = ' '.join(b.cells) if (b.kind == 'row' and b.cells) else (b.text or '')
            if not txt or RATING_DEF_CONTEXT.search(txt) or RATING_DEF_MARKER.search(txt):
                continue
            if RATING_CONCLUSION.search(txt):      # 结论句，不是封面
                continue
            words = [w for w in RATING_WORDS if w in txt]
            if len(words) != 1:
                continue
            w = words[0]
            has_maintain = '维持' in txt
            has_label = bool(re.search(r'投资评级|股票投资评级|评级\s*[：:|]', txt))
            is_short = len(txt.strip()) <= 22
            if has_maintain or has_label or is_short:
                return w
        return None

    @staticmethod
    def _sentence_at(text, pos):
        start = max(text.rfind('。', 0, pos), text.rfind('；', 0, pos)) + 1
        ends = [i for i in (text.find('。', pos), text.find('；', pos)) if i >= 0]
        end = min(ends) + 1 if ends else len(text)
        return text[start:end].strip()

    @staticmethod
    def _context_at(text, start, end):
        """命中处的原文窗口（前后各约 28 字，不按句读裁剪）。

        与原文证据对齐时，证据窗口是跨句读的原始片段（金标准 context 就是这种），
        若按句读裁剪会把引文切得过短、与原文证据对不上。
        """
        lo = max(0, start - 28)
        hi = min(len(text), end + 28)
        return text[lo:hi].strip()

    # ================================================ 4. CAGR / 目标价隐含倍数
    def run_derived(self):
        text = self.doc.full_text()
        for m in re.finditer(r'(20\d{2})\s*[-~～—–至到]\s*(20\d{2})\s*年[^。；]{0,40}?'
                             r'(?:营业收入|营收|归母净利润|净利润)[^。；]{0,20}?'
                             r'(?:年均复合增速|复合增速|CAGR)[^。；]{0,12}?约?\s*'
                             r'(-?\d+(?:\.\d+)?)\s*%', text):
            self._cagr(text, m)
        for m in re.finditer(r'目标价[^。；]{0,8}?(\d+(?:\.\d+)?)\s*元[^。；]{0,24}?'
                             r'(20\d{2})\s*[EＥ][^。；]{0,10}?PE[^。；]{0,6}?(\d+(?:\.\d+)?)\s*倍', text):
            self._target_pe(text, m)
        return self

    def _table_rows(self):
        out = []
        for t in _build_tables(self.doc):
            periods = t.periods()
            for bid, cells, page, text in t.rows:
                out.append((cells, periods, page, text))
        return out

    def _row_values_by_period(self, label_rx, periods=None):
        """跨全部表格找"标签匹配的第一行"，返回 {期间: Decimal}。

        期间由各表自己的表头决定，不依赖调用方传入 —— 报告里同一指标常在
        多张表（盈利预测表 / 财务预测与估值表）重复出现，期间序列也各不相同。
        """
        out = {}
        for cells, per, page, text in self._table_rows():
            if not per or not cells:
                continue
            if not label_rx.search(cells[0] or ''):
                continue
            vals = _values(cells)
            for k, v in enumerate(vals):
                if k < len(per) and per[k] not in out:
                    n = _num(v)
                    if n is not None:
                        out[per[k]] = n
        return out

    def _base_series(self, label_rx):
        """每股基数（EPS/BVPS）按期间索引；破表头巨表用首个年份序列兜底。

        倍数行与每股基数行常不在同一张表，且基数行可能落在多栏巨表里（表头混中段标签、
        严格 periods() 返回 None）。这里对"单栏数值行"额外用 `first_run_periods` 兜底，
        让 BVPS 也能被索引到，从而算出 PB 隐含价。仅用于倍数隐含价，不影响 CAGR/正文对表。
        """
        out = {}
        for t in _build_tables(self.doc):
            per = t.periods() or t.first_run_periods()
            if not per:
                continue
            for bid, cells, page, text in t.rows:
                if not cells or not label_rx.search(cells[0] or ''):
                    continue
                vals = _values(cells)
                # 只取单栏数值行（值数 == 期间数），双栏交错行跳过，避免列错位
                if len(vals) != len(per):
                    continue
                for k, v in enumerate(vals):
                    if per[k] in out:
                        continue
                    n = _num(v)
                    if n is not None:
                        out[per[k]] = n
        return out

    def _cagr(self, text, m):
        y0, y1, stated = int(m.group(1)), int(m.group(2)), _dec(m.group(3))
        metric_rx = re.compile(r'营业收入|营收|营业总收入|销售收入')
        if '归母' in m.group(0) or '净利润' in m.group(0):
            metric_rx = re.compile(r'归母净利润|归属于母公司|归母股东|净利润')
        pairs = self._pair_in_one_table(metric_rx, y0, y1)
        if not pairs or stated is None:
            return
        n = y1 - y0
        if n == 0:
            return
        calcs = []
        for a, b in pairs:
            if a is None or b is None or a <= 0 or b <= 0:
                continue
            calcs.append(((b / a) ** (D(1) / D(n)) - 1) * 100)
        if not calcs:
            return
        # 多候选取值：报告自己的 CAGR 句可能用与我们不同的列对（多栏巨表尤甚）。
        # 只要**任一**列对能复算出报告写的数，就不算错；全都对不上才报，
        # 报文里取"与报告写的最接近"的那对（真值是它加噪声得来的，距离最近）。
        if any(abs(c - stated) <= D('0.5') for c in calcs):
            return
        calc = min(calcs, key=lambda c: abs(c - stated))
        a, b = min(pairs, key=lambda p: abs(((p[1] / p[0]) ** (D(1) / D(n)) - 1) * 100 - stated) if p[0] and p[1] and p[0] > 0 and p[1] > 0 else D('1e9'))
        alt_calcs = ' / '.join(sorted({f'{c.quantize(D("0.1"))}%' for c in calcs}))
        self.add('growth', 'cagr_wrong', self._sentence_at(text, m.start()), f'{stated}%',
                 f'{calc.quantize(D("0.1"))}%',
                 f'按表内数据复算，{y0}-{y1} 年复合增速应为 {calc.quantize(D("0.1"))}%（候选口径：{alt_calcs}）',
                 reason=f'报告写 {stated}%，由表内 {a} → {b} 复算为 {calc:.2f}%')

    @staticmethod
    def _year_map(t, cells):
        """表头 token 按**位置**对到数值格上，得 {年份token: 值}。

        与报告里"巨表"的现实对齐：表头常是左右两栏并排（`2025A|2026E|2027E|2028E`
        后面又跟一栏 `2024A|2025A|…`），`periods()` 因年份不递增返回 None。
        这里按"第 i 个表头 token 对应第 i 个格"直接映射（同一 token 重复时**后写覆盖**，
        与金标准生成器口径一致），年份 token 用 `20\\d{2}[AE]?` 识别。

        注意 `t.header` 已剥掉首格（标签列），故对齐到 `cells[1:]` 的第 i 格，
        与"表头行自身也带标签列"的事实一致（否则整体错位一列）。
        """
        out = {}
        order = []
        hdr = t.header
        if not hdr:
            return out, order
        for i, c in enumerate(cells[1:]):
            if i >= len(hdr):
                break
            cs = (c or '').strip()
            if not re.fullmatch(r'-?\d[\d,]*(?:\.\d+)?', cs):
                continue
            key = hdr[i].strip()
            if not re.fullmatch(r'20\d{2}[AEae]?', key):
                continue
            if key not in out:
                order.append(key)
            out[key] = _num(cs)
        return out, order

    @classmethod
    def _year_lookup(cls, t, cells, year):
        """取某一年（不论 A/E 后缀）在该行的数值。找不到返回 None。"""
        yrs, _ = cls._year_map(t, cells)
        for suf in ('A', 'E', ''):
            v = yrs.get(f'{year}{suf}')
            if v is not None:
                return v
        return None

    def _pair_in_one_table(self, label_rx, y0, y1):
        """在同一张表里取 y0 年与 y1 年的全部候选数值对（历史 A 列或预测 E 列均可）。

        必须同表：跨表取值会把"盈利预测表"的历史列与"财务预测表"的预测列拼在一起，
        期间口径不同，算出来的复合增速毫无意义（实测会把正确的 CAGR 报成错的）。

        返回**所有**候选对：多栏巨表的表头常重复年份（2025A|2026E|2027E|2028E|2024A|2025A|…），
        同一年可能映射到多个格。调用方用"任一候选能对上就不报"来控误报，
        用"最接近报告值的候选"来给正确报文。
        """
        out = []
        seen = set()
        for t in _build_tables(self.doc):
            for bid, cells, page, text in t.rows:
                if not cells or not label_rx.search(cells[0] or ''):
                    continue
                per = t.periods()
                if per and self._years_ascending(per):
                    keys = {}
                    for p in per:
                        mm = re.match(r'^(\d{4})', p or '')
                        if mm:
                            keys.setdefault(mm.group(1), p)
                    if str(y0) in keys and str(y1) in keys:
                        vals = _values(cells)
                        got = {}
                        for k, v in enumerate(vals):
                            if k < len(per):
                                n = _num(v)
                                if n is not None:
                                    got[per[k]] = n
                        if keys[str(y0)] in got and keys[str(y1)] in got:
                            pair = (got[keys[str(y0)]], got[keys[str(y1)]])
                            if pair not in seen:
                                seen.add(pair)
                                out.append(pair)
                yrs, _ = self._year_map(t, cells)
                for ka in self._year_keys_for(yrs, y0):
                    for kb in self._year_keys_for(yrs, y1):
                        a, b = yrs.get(ka), yrs.get(kb)
                        if a is not None and b is not None:
                            pair = (a, b)
                            if pair not in seen:
                                seen.add(pair)
                                out.append(pair)
        return out

    @staticmethod
    def _year_keys_for(yrs, year):
        out = []
        for suf in ('A', 'E', ''):
            k = f'{year}{suf}'
            if k in yrs:
                out.append(k)
        return out

    @staticmethod
    def _years_ascending(periods):
        years = []
        for p in periods:
            m = re.match(r'^(\d{4})', p or '')
            if not m:
                return False
            years.append(int(m.group(1)))
        return all(b > a for a, b in zip(years, years[1:]))

    def _any_periods(self):
        for cells, per, _, _ in self._table_rows():
            if per:
                return per
        return None

    def _target_pe(self, text, m):
        target, year, stated = _dec(m.group(1)), int(m.group(2)), _dec(m.group(3))
        series = self._row_values_by_period(EPS_LABEL)
        eps = series.get(f'{year}E') or series.get(f'{year}FY')
        if not eps:
            # 表头不规范（多栏巨表）或表头单列年份被篡改（如 2025A|2025E|2027E|2028E）或无表头子表时兜底
            for t in _build_tables(self.doc):
                for bid, cells, page, _ in t.rows:
                    if not (cells and EPS_LABEL.search(cells[0] or '')):
                        continue
                    v = self._year_lookup(t, cells, year)
                    if not v and t.header:
                        # 修复单处断裂的表头年份（years[0]+i）后再按位置取值
                        for start, years, _ in _year_runs(t.header):
                            bad = [i for i, y in enumerate(years) if y != years[0] + i]
                            if len(bad) == 1:
                                for i in range(len(years)):
                                    if years[0] + i == year and 1 + start + i < len(cells):
                                        v = _num(cells[1 + start + i])
                                        break
                    if not v and not t.header:
                        # 子表自身无表头（如 601318 的每股指标子表）：向上找最近的同列数年份表头行
                        vals = _values(cells)
                        for b in reversed(self.doc.blocks[:bid]):
                            if b.kind != 'row' or not b.cells:
                                continue
                            runs = _year_runs(b.cells)
                            if runs and len(runs[0][1]) == len(vals):
                                _, years, _ = runs[0]
                                for i, y in enumerate(years):
                                    if (y == year or years[0] + i == year) and i < len(vals):
                                        v = _num(vals[i])
                                        break
                                if v:
                                    break
                    if v:
                        eps = v
                        break
                if eps:
                    break
        if not target or not eps or stated is None:
            return
        calc = target / eps
        if _close(calc, stated, D('0.05')):
            return
        self.add('valuation', 'target_implied_pe', self._sentence_at(text, m.start()), f'{stated}倍',
                 f'{calc.quantize(D("0.1"))}倍',
                 f'目标价 {target} ÷ {year}E EPS {eps} = {calc.quantize(D("0.1"))} 倍',
                 reason=f'报告写约 {stated} 倍，与目标价/EPS 复算不符')

    # ================================================ 5. 正文 vs 表内
    def run_text_vs_table(self):
        """正文/摘要里写的数，必须与财务预测表同一期间的值一致。

        容差按指标性质分别设定：金额 2%，倍数 3%，比率与增速 0.5 个百分点。
        """
        from .claim_extractor import MULTIPLE_METRICS
        claims = getattr(self, 'claims', None) or extract_claims(self.doc)
        # 同一小句里同指标的声明数：>1 说明是并列枚举，列归属不可信
        multi = {}
        for c in claims:
            if c.where == 'prose':
                k = (_norm(c.sentence), c.metric)
                multi[k] = multi.get(k, 0) + 1
        flagged_Quotes = set()
        for t in _build_tables(self.doc):
            periods = t.periods()
            if not periods:
                continue
            for bid, cells, page, text in t.rows:
                label = (cells[0] or '').strip()
                vals = _values(cells)
                if not vals:
                    continue
                metric = self._metric_of_label(label)
                if not metric:
                    continue
                scale = self._unit_scale_of(label) or D(1)
                nums = {}
                for k, v in enumerate(vals):
                    if k < len(periods):
                        n = _num(v)
                        if n is not None:
                            nums[periods[k]] = n * scale
                if not nums:
                    continue
                for c in claims:
                    if c.where != 'prose' or c.metric != metric:
                        continue
                    if not self._text_table_admissible(c, metric, multi):
                        continue
                    if c.period not in nums:
                        continue
                    cv = c.value * c.scale
                    if self._same_metric_value(metric, nums[c.period], cv):
                        continue
                    if any(self._same_metric_value(metric, x, cv) for x in nums.values()):
                        continue
                    if cv and _pow10_swap(cv / nums[c.period]):
                        continue
                    cat = 'valuation' if metric in MULTIPLE_METRICS else 'internal'
                    sub = f'{metric.upper()}.text_vs_table' if metric in MULTIPLE_METRICS else 'text_vs_table'
                    tv_val = nums[c.period]
                    rnd_val = tv_val.quantize(D(1).scaleb(-c.decimals)) if c.decimals is not None else tv_val
                    self.add(cat, sub, c.sentence,
                             (c.raw or f'{c.value:f}').strip(),
                             f'{tv_val:f}（约 {rnd_val:f}）',
                             f'正文数字与财务预测表不一致，表内 {label} {c.period} 为 {tv_val:f}（约 {rnd_val:f}）',
                             page=c.page,
                             reason=f'正文 {c.raw} vs 表内 {tv_val}（{label} {c.period}）')
                    flagged_Quotes.add((_norm(c.sentence), str(c.value)))
        self._slash_list_vs_table(flagged_Quotes)
        return self

    def _slash_list_vs_table(self, flagged_Quotes):
        """正文中形如 a/b/c 亿元 或 PE 为 a/b/c 倍 的斜杠并列序列，与预测表连续列比对：
        仅当序列中其余各项均与表内某连续列段吻合、唯独 1 项偏离时报出，零误报。
        """
        tables = _build_tables(self.doc)
        amt_rows = {'rev': [], 'np': [], 'pe': []}
        for t in tables:
            for bid, cells, page, text in t.rows:
                lab = (cells[0] or '').strip()
                vals = [_num(v) for v in _values(cells)]
                vals = [v for v in vals if v is not None]
                if len(vals) < 3:
                    continue
                u_m = UNIT_IN_LABEL.search(lab)
                u = _unit_norm(u_m.group(1)) if u_m else (t.label_unit or t.unit or '')
                if re.match(r'^(?:营业(?:总)?收入|营业收入|营收)', lab) and u in ('百万元', '亿元'):
                    amt_rows['rev'].append([v / (D(100) if u == '百万元' else D(1)) for v in vals])
                elif re.match(r'^(?:归母净利润|归属母公司净利润|归属于母公司.*净利润|归母净利|净利润)', lab) and u in ('百万元', '亿元'):
                    amt_rows['np'].append([v / (D(100) if u == '百万元' else D(1)) for v in vals])
                elif PE_LABEL.search(lab):
                    amt_rows['pe'].append(vals)
        pat_amt = re.compile(r'(?P<label>营业(?:总)?收入|归母净利润|净利润|营收)(?:分别)?(?:为|达|实现)?\s*(?P<nums>\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?){1,3})\s*(?P<unit>亿元)')
        pat_pe = re.compile(r'(?:PE|市盈率)[^\d。；]{0,10}?(?P<nums>\d+(?:\.\d+)?(?:\s*[/、]\s*\d+(?:\.\d+)?){1,3})\s*(?:倍|X|x)?')
        for b in self.doc.blocks:
            if b.kind not in ('para', 'heading') or not b.text:
                continue
            for m in pat_amt.finditer(b.text):
                key = 'rev' if '收入' in m.group('label') or '营收' in m.group('label') else 'np'
                nums = [s.strip() for s in m.group('nums').split('/')]
                n_decs = [ _dec(s) for s in nums ]
                if any(x is None for x in n_decs):
                    continue
                if any(any(all(abs(tv[st + j] - n_decs[j]) <= D('0.15') for j in range(len(n_decs)))
                           for st in range(len(tv) - len(n_decs) + 1)) for tv in amt_rows.get(key, [])):
                    continue
                for tv in amt_rows.get(key, []):
                    found = False
                    for st in range(len(tv) - len(n_decs) + 1):
                        diffs = [j for j in range(len(n_decs))
                                 if abs(tv[st + j] - n_decs[j]) > D('0.15')]
                        if len(diffs) == 1:
                            j = diffs[0]
                            if abs(tv[st + j] - n_decs[j]) / max(abs(tv[st + j]), D('0.01')) < D('0.03'):
                                continue
                            sent = self._sentence_at(b.text, m.start())
                            if (_norm(sent), str(n_decs[j])) in flagged_Quotes:
                                found = True
                                break
                            decs_j = len(nums[j].split('.')[1]) if '.' in nums[j] else 1
                            want = tv[st + j].quantize(D(1).scaleb(-decs_j))
                            self.add('internal', 'text_vs_table', sent, nums[j], f'{want}亿元',
                                     f'正文并列预测数与财务预测表不一致，该项应为 {want}亿元（与表内一致）',
                                     page=b.page,
                                     reason=f'正文序列 {m.group("nums")} 第{j+1}项 {nums[j]} 与表内 {want} 不一致')
                            flagged_Quotes.add((_norm(sent), str(n_decs[j])))
                            found = True
                            break
                    if found:
                        break
            for m in pat_pe.finditer(b.text):
                nums = [s.strip() for s in re.split(r'\s*[/、]\s*', m.group('nums'))]
                n_decs = [_dec(s) for s in nums]
                if any(x is None for x in n_decs):
                    continue
                if any(any(all(abs(tv[st + j] - n_decs[j]) <= D('0.2') for j in range(len(n_decs)))
                           for st in range(len(tv) - len(n_decs) + 1)) for tv in amt_rows.get('pe', [])):
                    continue
                for tv in amt_rows.get('pe', []):
                    found = False
                    for st in range(len(tv) - len(n_decs) + 1):
                        diffs = [j for j in range(len(n_decs))
                                 if abs(tv[st + j] - n_decs[j]) > D('0.2')]
                        if len(diffs) == 1:
                            j = diffs[0]
                            if abs(tv[st + j] - n_decs[j]) / max(abs(tv[st + j]), D('0.01')) < D('0.06'):
                                continue
                            sent = self._sentence_at(b.text, m.start())
                            if (_norm(sent), str(n_decs[j])) in flagged_Quotes:
                                found = True
                                break
                            decs_j = len(nums[j].split('.')[1]) if '.' in nums[j] else 1
                            want = tv[st + j].quantize(D(1).scaleb(-decs_j))
                            self.add('valuation', 'PE.text_vs_table', sent, nums[j], f'{want}',
                                     f'正文 PE 与财务预测表不一致，该项应为 {want}倍（表内值为 {tv[st+j]}）',
                                     page=b.page,
                                     reason=f'正文 PE 序列 {m.group("nums")} 第{j+1}项 {nums[j]} 与表内 {tv[st+j]} 不一致')
                            flagged_Quotes.add((_norm(sent), str(n_decs[j])))
                            found = True
                            break
                    if found:
                        break

    def _text_table_admissible(self, c, metric, multi):
        from .claim_extractor import MULTIPLE_METRICS, PER_SHARE
        if c.kind == 'growth' or c.growth_basis:
            return False
        if 'inherited_period' in (c.flags or []) or getattr(c, 'period_in_clause', True) is False:
            return False
        # 目标价隐含 PE 由 _target_pe 核查（用目标价而非现价算 PE，与表内现价 PE 天然不等）
        if metric in MULTIPLE_METRICS and '目标价' in (c.sentence or ''):
            return False
        # 并列枚举（"营收分别为1031.30亿元、1172.80亿元和1312.02亿元"）时，数字与列的
        # 一一对应不可靠，整句放弃；倍数（PE/PB/EV-EBITDA）除外——它们本身就是按年排列的，
        # 且由"值能落在表内哪个期间"来兜底。
        # 仅数"已抽取的声明"不够：并列项常只抽到第一项（multi=1），要按句内写法判。
        if metric not in MULTIPLE_METRICS and re.search(r'分别(?:为|是|达)|依次为|各(?:为|年)', c.sentence or ''):
            if len(re.findall(r'\d[\d,]*(?:\.\d+)?\s*(?:亿元|万元|百万元|亿|万|元)', c.sentence or '')) >= 2:
                return False
        if multi.get((_norm(c.sentence), metric), 0) > 1 and metric not in MULTIPLE_METRICS:
            return False
        if self._segment_like(c):
            return False
        # 量纲一致：声明类型必须与行指标性质相符
        if metric in RATIO_METRICS:
            return c.kind == 'ratio'
        if metric in MULTIPLE_METRICS:
            return c.kind == 'multiple'
        if metric in PER_SHARE:
            return c.kind == 'per_share'
        return c.kind == 'amount'

    @staticmethod
    def _same_metric_value(metric, a, b):
        """按指标性质比较：比率/增速用百分点，倍数用 3%，金额用 2%。"""
        from .claim_extractor import MULTIPLE_METRICS, RATIO_METRICS, PER_SHARE
        if a is None or b is None:
            return True
        if metric in RATIO_METRICS:
            return abs(a - b) <= D('0.5')
        if metric in MULTIPLE_METRICS:
            return _close(a, b, D('0.03'))
        if metric in PER_SHARE:
            return _close(a, b, D('0.01'))
        return _close(a, b, D('0.02'))

    @staticmethod
    def _unit_scale_of(label):
        m = UNIT_IN_LABEL.search(label or '')
        if not m:
            return None
        from .source_reconcile import UNIT_SCALE
        return UNIT_SCALE.get(m.group(1))

    @staticmethod
    def _metric_of_label(label):
        if re.search(r'PEV|内含价值', label or '', re.I):
            return None
        from .claim_extractor import _MW_RE, _MW_MAP
        m = _MW_RE.search(label or '')
        return _MW_MAP.get(m.group(0)) if m else None

    # ================================================ 6. 合规与风险提示
    def run_compliance(self):
        text = self.doc.full_text()
        for kind, rx in COMPLIANCE_RULES:
            for m in rx.finditer(text):
                # 否定豁免分两档（窗口太宽会把「招采不及预期。我们承诺…」里的"不"误当豁免）：
                #   紧邻 2 字内是单字否定（不承诺/非内幕/无风险套利已在规则里排除）；
                #   或 12 字内是禁止性措辞（不得/禁止/严禁/不构成/不应…，覆盖免责条款长句）。
                near = text[max(0, m.start() - 2):m.start()]
                wide = text[max(0, m.start() - 12):m.start()]
                if NEGATION.search(near) or PROHIBITION.search(wide):
                    continue
                sent = self._sentence_at(text, m.start())
                self.add('compliance', kind, sent, m.group(0), '',
                         '删除该表述：研究报告不得承诺收益、使用内幕信息或操纵市场',
                         reason='命中合规禁用表述')
        missing_rw = not re.search(r'风险提示|风险因素|主要风险|潜在风险', text)
        if not missing_rw:
            # 首页主文末尾的风险段标题"风险提示"被删，只留裸风险列举句紧跟在评级结论段之后（如 600900）
            for i in range(1, min(15, len(self.doc.blocks))):
                b = self.doc.blocks[i]
                prev = self.doc.blocks[i - 1]
                if b.kind == 'para' and prev.kind == 'para' and len(b.text or '') < 100:
                    if re.search(r'维持.*评级', prev.text or '') and re.search(r'风险.*不及预期|不及预期.*风险', b.text or ''):
                        if not re.search(r'风险提示|风险因素|主要风险|潜在风险', b.text or ''):
                            missing_rw = True
                            break
        if missing_rw:
            self.add('compliance', 'missing_risk_warning', '（全文无“风险提示”段落）',
                     '风险提示（缺失）', '应有风险提示段',
                     '补充“风险提示”段：研究报告须列示主要风险',
                     reason='全文或主文风险段落未出现风险提示标题或章节')
        return self

    # ================================================ 7. 引用与期间
    def run_citation(self):
        """引用/期次核查：
        1. forecast_cited_to_annual_report：含预测列（E 列）的表格把来源写成"公司年报"——预测数不可能来自年报。
        2. filing_period_mismatch：「公司发布{期次}报告」的期次 与 同句所述数据期间或原始财报期次矛盾。
        3. cited_date_after_report_date：图表/估值表引用的数据截止日晚于报告发布日期（穿越引用）。
        """
        blocks = self.doc.blocks
        for i, b in enumerate(blocks):
            line = ' '.join(b.cells) if (b.kind == 'row' and b.cells) else (b.text or '')
            if not line or not SRC_LINE.search(line):
                continue
            if not ANNUAL_SRC.search(line):
                continue
            if not re.search(r'资料来源|数据来源', line):
                continue
            has_forecast = bool(FORECAST_COL.search(line) or re.search(r'研究所预测|预测\s*[）)】]?\s*$', line))
            if not has_forecast:
                j = i - 1
                window_parts = []
                while j >= 0 and len(window_parts) < 30:
                    x = blocks[j]
                    if x.kind == 'row' and x.cells:
                        window_parts.append(' '.join(x.cells))
                    elif x.kind in ('para', 'heading') and (x.text or '').strip():
                        if not SRC_LINE.search(x.text or ''):
                            break
                        window_parts.append(x.text or '')
                    j -= 1
                window = ' '.join(reversed(window_parts))
                has_forecast = bool(FORECAST_COL.search(window))
            if has_forecast:
                self.add('citation', 'forecast_cited_to_annual_report', line, '公司年报',
                         'Wind/研究所预测',
                         '预测列（E 列）不可能来自公司年报，来源应写为研究所预测',
                         page=b.page, reason='含预测列（E 列）的表格把来源写成公司年报')

        # —— 财报期次 vs 数据期间矛盾（逐块扫，避免跨换行误匹配页脚） ——
        filing_pat = re.compile(r'(?:公司|\S{2,6})?(?:发布|披露|公告)\s*(?:了)?\s*((?:20\d{2}|\d{2})\s*年\s*(?:一季报|第一季度报告|一季度报告|半年报|半年度报告|中报|三季报|第三季度报告|三季度报告|年报|年度报告)|(?:一季报|第一季度报告|一季度报告|半年报|半年度报告|中报|三季报|第三季度报告|三季度报告|年报|年度报告))')
        for b in blocks:
            if b.kind not in ('para', 'heading') or not b.text:
                continue
            btxt = b.text
            for m in filing_pat.finditer(btxt):
                fdesc = m.group(1)
                frank = _filing_rank(fdesc)
                tail = btxt[m.end(): m.end() + 60]
                dtail = re.split(r'[。；]', tail)[0]
                drank = _data_rank(dtail)
                ym = re.match(r'^((?:20\d{2}|\d{2})\s*年\s*)', fdesc)
                yr_prefix = ym.group(1) if ym else ''
                yr_num = ('20' + yr_prefix.strip()[:2] if len(yr_prefix.strip()) == 3 else yr_prefix.strip()[:4]) if yr_prefix else ''
                if drank is None and yr_num and self.pool.docs:
                    # 句内未显式写"上半年/全年"时，核对该年份在原始财报池中的实际报告期（如 002594 只有 2026H1 中报）
                    pool_pers = {p for (_, p) in self.pool.by_key if p.startswith(yr_num)}
                    ranks_in_pool = set()
                    for p in pool_pers:
                        if p.endswith('Q1'):
                            ranks_in_pool.add(1)
                        elif p.endswith('H1'):
                            ranks_in_pool.add(2)
                        elif p.endswith('Q3'):
                            ranks_in_pool.add(3)
                        elif p.endswith('FY'):
                            ranks_in_pool.add(4)
                    if len(ranks_in_pool) == 1:
                        drank = next(iter(ranks_in_pool))
                if drank is None or drank == frank:
                    continue
                sent = self._sentence_at(btxt, m.start())
                aliases = {
                    1: f'{yr_prefix}一季报 / {yr_prefix}第一季度报告',
                    2: f'{yr_prefix}中报 / {yr_prefix}半年报 / {yr_prefix}半年度报告',
                    3: f'{yr_prefix}三季报 / {yr_prefix}第三季度报告',
                    4: f'{yr_prefix}年报 / {yr_prefix}年度报告',
                }.get(drank, f'{yr_prefix}{_RANK_NAME.get(drank)}报告')
                self.add('citation', 'filing_period_mismatch', sent, fdesc,
                         aliases,
                         f'引用的财报期次应与所述数据期间一致，应为“{aliases}”',
                         page=b.page,
                         reason=f'发布期次「{fdesc}」与数据期间「{_RANK_NAME.get(drank)}」不符')

        # —— 引用日期晚于报告发布日期（穿越引用） ——
        cover_dates = []
        for b in blocks[:30]:
            t = ' '.join(b.cells) if (b.kind == 'row' and b.cells) else (b.text or '')
            for m in re.finditer(r'(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', t):
                cover_dates.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
        cover_date = max(cover_dates) if cover_dates else None
        if cover_date:
            cy, cm, cd = cover_date
            for b in blocks:
                if b.page and b.page > 4:
                    continue
                t = ' '.join(b.cells) if (b.kind == 'row' and b.cells) else (b.text or '')
                if not t:
                    continue
                for m in re.finditer(r'(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', t):
                    y, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    if (y, mo) <= (cy, cm):
                        continue
                    left = t[max(0, m.start() - 16):m.start()]
                    if re.search(r'预计|预测|将于|至|到期|解禁|未来|截至20[2-9]\d', left):
                        continue
                    # 估值表/图表截止日被往后推了 1~2 个月：还原为封面月或上月同日
                    cand_dates = [f'{cy} 年{cm} 月{dd} 日', f'{cy}年{cm}月{dd}日']
                    if cm > 1:
                        cand_dates += [f'{cy} 年{cm-1} 月{dd} 日', f'{cy}年{cm-1}月{dd}日']
                    exp_d = ' / '.join(cand_dates)
                    self.add('citation', 'cited_date_after_report_date', t, m.group(0),
                             exp_d,
                             f'引用数据日期（{m.group(0)}）不可晚于报告发布日期（{cy}-{cm:02d}-{cd:02d}），应为 {exp_d}',
                             page=b.page,
                             reason=f'引用日期 {m.group(0)} 晚于报告发布日 {cy}-{cm:02d}-{cd:02d}')
        return self


_RANK_NAME = {1: '一季度', 2: '半年度', 3: '前三季度', 4: '年度'}


# ---------------------------------------------------------------- 入口

def check_report(report_path=None, source_paths=None, *, sources=None, text=None, llm=None, out_dir=None) -> dict:
    """对一份研报做纠错核查。

    report_path : 研报文件路径（.pdf/.docx/.md/.txt），或与 text 二选一
    source_paths: 原始财报 PDF 路径列表（季报/半年报/年报）
    sources     : 已构建好的 SourceDoc 列表（评测脚本直接传，省去重复解析）
    llm         : 预留；本流水线全部为确定性检测，不使用模型
    out_dir     : 若给出，把 findings 与 summary 落盘
    """
    if text is None and report_path is None:
        raise ValueError('必须提供 report_path 或 text')
    doc = parse_report(report_path) if text is None else parse_report(str(text))

    pool_sources = list(sources or [])
    if source_paths:
        from .source_reconcile import SourceDoc
        for p in source_paths:
            path = Path(p)
            pool_sources.append(SourceDoc.from_pdf(path, path.stem))

    pool = SourcePool(pool_sources)
    checker = Checker(doc, pool)
    checker.run_source()
    checker.run_tables()
    checker.run_rating()
    checker.run_derived()
    checker.run_text_vs_table()
    checker.run_compliance()
    checker.run_citation()

    findings = checker.findings
    by_cat, by_sub = {}, {}
    for f in findings:
        by_cat[f['category']] = by_cat.get(f['category'], 0) + 1
        by_sub[f['subtype']] = by_sub.get(f['subtype'], 0) + 1
    full_text = doc.full_text()
    summary = {
        'chars': len(re.sub(r'\s', '', full_text)),
        'n_findings': len(findings),
        'n_suspects': len(checker.suspects),
        'by_category': by_cat,
        'by_subtype': by_sub,
        'n_sources': len(pool_sources),
        'source_docs': [getattr(s, 'kind', '') or Path(getattr(s, 'path', '')).stem for s in pool_sources],
        'n_claims': len(getattr(checker, 'claims', []) or []),
        'note': ('findings 只含"能复算出正确值"的确认项；复算不出正确值的疑点在 suspects 里，'
                 '需人工判断。source 类错误给出原始财报文件名与页码；internal 类只依赖报告自身。'),
    }
    result = {'ok': True, 'findings': findings, 'suspects': checker.suspects, 'summary': summary}
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / 'findings.json').write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    return result
