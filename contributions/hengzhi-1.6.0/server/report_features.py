"""研报质量 v2：可确定性检测的子指标（0–5 分 + 命中证据 + 规则 ID）。

设计原则
- 每个子指标只做"可观察"的事：命中哪些原文片段、在第几行、哪条规则；不靠关键词计数就直接给满分，
  需要"关键词 + 数字/时点/传导"等共现才给高分（反堆砌）。
- 评分函数是纯函数：同一文本同一规则版本 → 同一结果。无随机、无时间、无网络。
- 分值区间和阈值集中在 ANCHORS，便于答辩时说明"为什么是这个分"，也便于校准。
- 一个指标不适用（如无图表时的"图表来源"）返回 score=None，由上层从加权均值中剔除，而不是给 0 或满分。
"""
from __future__ import annotations

import re
from collections import OrderedDict

from .report_text import Doc

RULES_VERSION = 'report-quality-v2.0'

# ---------------------------------------------------------------- 词表（规则 ID 在注释里）
SECTION_DEFS = OrderedDict([
    ('summary', ('投资要点/核心观点', 1.0, r'投资要点|核心观点|主要观点|投资逻辑|推荐逻辑|投资摘要|报告摘要|摘要|事件[:：]|点评[:：]|投资评级|投资建议', 2)),
    ('company_industry', ('公司与行业', 1.0, r'公司(简介|概况|介绍|业务|基本情况|发展历程|主营)|行业(分析|概况|格局|现状|空间|景气|背景|供需)|主营业务|业务(拆分|结构|分析|布局)|产业链|竞争格局|市场(规模|空间|格局)', 3)),
    ('financial', ('财务分析', 1.0, r'财务(分析|状况|数据|概览|表现|摘要)|业绩(回顾|分析|复盘|点评|表现)|盈利能力|收入(分析|拆分|结构)|毛利率|净利率|现金流|资产负债', 4)),
    ('forecast', ('盈利预测与假设', 1.5, r'盈利预测|业绩预测|财务预测|预测(与|及)假设|核心假设|关键假设|基本假设|假设(与|及)预测|盈利预测与|预测表|财务预测', 2)),
    ('valuation', ('估值方法与可比', 1.5, r'估值|目标价|可比公司|DCF|相对估值|绝对估值|市盈率|PE|PB|EV/EBITDA|合理(价值|价格|价位)', 3)),
    ('catalyst', ('催化剂', 0.7, r'催化剂|催化因素|股价催化|事件驱动|重要事件|未来(催化|关注)|短期催化|关注(要点|点)|跟踪(要点|指标)', 2)),
    ('risk', ('风险提示', 1.5, r'风险提示|风险因素|主要风险|潜在风险|风险(分析|及应对)|不确定性', 1)),
    ('disclaimer', ('免责声明', 0.8, r'免责声明|重要声明|法律声明|分析师(声明|承诺)|评级说明|投资评级(说明|定义)|本报告(仅供|不构成|所载)|仅供.{0,6}参考|不构成.{0,6}(投资)?(建议|要约)', 1)),
])

UNIT = r'(?:亿元|万亿元|百万元|万元|千元|亿美元|万美元|美元|元/股|元/吨|元/千瓦时|元|倍|万吨|万辆|万台|亿股|万股|亿件|万件|个百分点|百分点|bp|BP|%|pct|GWh|GW|MW|万千瓦|亿千瓦时|亿立方米|美元/桶|美元/吨|吨|公斤|万片|亿只|亿美元)'
NUM = r'[-+]?\d[\d,]*(?:\.\d+)?'
NUM_UNIT_RE = re.compile(rf'(?<![\w.])({NUM})\s*({UNIT})')
NUM_ANY_RE = re.compile(rf'(?<![\w.\-])({NUM})')
YEAR_RE = re.compile(r'(?:19|20)\d{2}\s*(?:年|[AEae]\b|[/／]\d{2}年?)|[HQ][1-4]|[1-4]Q|[一二三四]季度|第[一二三四]季|[上下]半年|\d{4}\s*[/-]\s*\d{1,2}|\d{1,2}月份?|2\d[AEae]\b|20\d{2}[AaEe]')
PERIOD_RE = re.compile(r'(?:19|20)\d{2}\s*年?|[HQ][1-4]|[1-4]Q|[一二三四]季度|第[一二三四]季|[上下]半年|同比|环比|全年|年内|年底|期末|期初|\d{2}E|\d{2}A|未来|近[一二三四五六七八九十\d]+[年月个]')
METRIC_RE = re.compile(r'营业收入|营收|收入|净利润|归母|扣非|毛利率|净利率|毛利|利润|EPS|每股|ROE|ROIC|现金流|资本开支|产能|产量|销量|出货|市占率|份额|价格|均价|成本|费用|费用率|负债率|估值|市值|目标价|PE|PB|市盈率|市净率|EBITDA|订单|库存|开工率|增速|增长率|同比|环比')
BASIS_RE = re.compile(r'归母|扣非|合并|母公司|同比|环比|调整后|含税|不含税|口径|按.{1,8}(计算|测算|口径)|摊薄')
SRC_RE = re.compile(r'(?:资料|数据|图表|信息|报告)?来源[:：]|数据来源|资料来源|Source[s]?\s*[:：]|据[^，。；]{1,18}(?:披露|公告|统计|数据显示|显示|报道|年报|季报|财报|招股)|(?:公司|上市公司)?(?:年报|半年报|季报|公告|招股说明书|招股书)|Wind|万得|Bloomberg|彭博|Choice|iFinD|同花顺|聚源|国家统计局|海关总署|工信部|央行|中国人民银行|SMM|Mysteel|百川|卓创|IDC|Gartner|CPCA|中汽协|中电联|统计局|交易所|证监会|公开资料|公司官网|调研纪要|专家访谈')
SRC_ANNOT_RE = re.compile(r'(?:资料|数据|图表|信息)?来源\s*[:：]|Source[s]?\s*[:：]', re.I)

OPINION_RE = re.compile(r'看好|维持|上调|下调|认为|判断|预计|有望|推荐|建议(?:关注|买入|增持)|受益|首次覆盖|给予|重点关注|我们(?:预计|认为|看好|判断)|评级')
CAUSAL_RE = re.compile(r'因为|由于|因此|所以|导致|带动|驱动|受益于|得益于|主要系|主要源于|源于|拉动|推动|使得|从而|进而|叠加|得益|系因|原因(?:是|在于)|影响所致|致使')
COUNTER_STRONG_RE = re.compile(r'(?:若|如果|假如|倘若|一旦|假设)[^。；]{2,40}?(?:则|将|会|就|导致|使|下降|下调|低于|不及|减少|承压|失效|下滑|恶化|压缩)|(?:低于|不及|弱于)预期(?:的)?情形|下行(?:情景|风险情形)|悲观(?:情景|假设)|压力测试|证伪|反证|观点(?:失效|被证伪)|反向(?:情形|验证)|敏感性分析|敏感度|边际变化的反例|不成立')
COUNTER_WEAK_RE = re.compile(r'但|然而|不过|需要警惕|需警惕|需关注|风险在于|隐忧|制约|不排除|可能(?:导致|使|低于|不及)')
FACT_MARK_RE = re.compile(r'(?:公司|据|根据)[^，。；]{0,16}(?:披露|公告|公布|发布|显示|统计)|实现|录得|同比(?:增长|下降|增|减|提升|下滑)|报告期内|\d{4}\s*年(?:全年|上半年|前三季度|一季度|[1-4]Q|H[12])?(?:公司)?实现|数据显示|\d{2}A|截至')
INFER_MARK_RE = re.compile(r'预计|预测|测算|推算|估算|假设|假定|预期|我们(?:预计|估计|测算|假设)|\d{2}E|\d{4}\s*E|按[^，。；]{1,16}(?:假设|测算|估算)')
VIEW_MARK_RE = re.compile(r'我们认为|看好|判断|建议|维持[^，。；]{0,6}评级|观点|核心逻辑|推荐|首次覆盖|给予[^，。；]{0,6}评级|认为')

METHOD_DEFS = OrderedDict([
    ('DCF', r'DCF|现金流折现|自由现金流折现|FCFF|FCFE|折现模型|贴现模型'),
    ('PE', r'市盈率|(?<![A-Za-z])PE(?![A-Za-z])|P/E|(?<![A-Za-z])PEG(?![A-Za-z])'),
    ('PB', r'市净率|(?<![A-Za-z])PB(?![A-Za-z])|P/B'),
    ('PS', r'市销率|(?<![A-Za-z])PS(?![A-Za-z])|P/S'),
    ('EV', r'EV/EBITDA|EV/Sales|EV/EBIT|企业价值倍数'),
    ('SOTP', r'分部估值|SOTP|分类估值|分业务估值|分部加总'),
    ('COMPS', r'可比公司|可比估值|相对估值|同业(?:估值|平均)|行业平均|可比企业'),
    ('DIV', r'股息率|股利折现|DDM|分红(?:率|收益)'),
    ('NAV', r'RNAV|NAV|重置成本|净资产重估|资产重估'),
])
MULTIPLE_APPLY_RE = re.compile(r'对应[^。；]{0,12}?(?:20\d{2}\s*年?|\d{2}E)?[^。；]{0,8}?\d+(?:\.\d+)?\s*倍|给予[^。；]{0,16}?\d+(?:\.\d+)?\s*倍|\d+(?:\.\d+)?\s*倍\s*(?:PE|PB|PS|EV|市盈率|市净率)|(?:PE|PB|市盈率|市净率)[^。；]{0,10}?\d+(?:\.\d+)?\s*倍|(?:PE|PB)\s*(?:为|约|至)?\s*\d+(?:\.\d+)?\s*[xX倍]')
ASSUMP_STRONG_RE = re.compile(r'WACC|折现率|贴现率|永续(?:增长|增速)|终值|无风险利率|风险溢价|β|Beta|资本成本|股权成本|债务成本')
ASSUMP_GEN_RE = re.compile(r'(?:核心|关键|主要|基本|重要)?假设|假定|按[^，。；]{1,20}(?:计算|测算)|前提')
MULT_SOURCE_RE = re.compile(r'可比公司(?:平均|均值|中位)?|历史(?:中枢|均值|区间|分位)|行业(?:平均|均值|中位)|近[一二三四五六七八九十\d]+年(?:平均|均值|中枢|分位)|分位数|同业|对标')
SCENARIO_RE = re.compile(r'情景|乐观|悲观|中性|基准|敏感性|敏感度|压力|牛市|熊市|上行|下行')
RANGE_RE = re.compile(r'(?:合理|目标)(?:价值|价格|价位|估值|市值)?(?:区间|范围)|估值区间|\d+(?:\.\d+)?\s*[-~～—–至到]\s*\d+(?:\.\d+)?\s*(?:元|倍)')
TARGET_RE = re.compile(r'目标价|目标股价|合理(?:价值|价格|价位)|目标市值|目标价格')
RATING_RE = re.compile(r'买入|增持|强烈推荐|推荐|持有|中性|减持|卖出|跑赢(?:行业|大市)|优于大市|强于大市|领先大市|看好|同步大市|回避|谨慎推荐')
HORIZON_RE = re.compile(r'6\s*个月|12\s*个月|六个月|十二个月|未来\s*\d+\s*个月|半年|1\s*年内|一年内|12M|6M')

RISK_ITEM_RE = re.compile(r'[^，。；、,;:：\s（）()]{2,16}风险')
RISK_TRANSMIT_RE = re.compile(r'导致|影响|使得|压低|下滑|下降|减少|恶化|承压|拖累|侵蚀|压缩|冲击|波动|延迟|超预期|不及预期|低于预期|提升|上升|可能|将')
RISK_GENERIC_RE = re.compile(r'^(?:宏观经济|宏观|政策|市场|行业|竞争|经营|公司|其他|系统性|不可抗力|突发事件)?(?:不确定性)?(?:风险)?$')
RISK_NUM_RE = re.compile(rf'{NUM}\s*(?:%|个百分点|倍|元|亿元|万元|bp|pct)')
FORECAST_LINK_RE = re.compile(r'盈利预测|业绩|EPS|净利润|目标价|估值|下调|评级|营收|收入|利润')
UNCERTAIN_RE = re.compile(r'不确定|可能|或将|或许|存在(?:偏差|不确定|风险)|有待|不及预期|低于预期|假设|预测|仅供参考|不排除|难以|波动')

TRACK_RE = re.compile(r'跟踪|监测|持续关注|重点关注|关注(?:点|要点|指标)?|观察|密切关注|后续关注|需关注')
TRACK_METRIC_RE = re.compile(r'指标|价格|开工率|订单|库存|产能|出货|销量|利用率|毛利率|费用率|回款|现金流|招标|中标|运价|油价|煤价|价差|排产|数据|进展|批量|良率|渗透率|份额|增速|同比|环比')
TIME_RE = re.compile(r'(?:20\d{2}\s*年|\d{2}年)(?:[1-4]Q|Q[1-4]|H[12]|[上下]半年|[一二三四]季度|底|初|内)?|[1-4]Q|Q[1-4]|H[12]|[上下]半年|年底|年内|年中|季度|\d{1,2}\s*月|未来\s*\d*\s*[个]?(?:月|年|季度)|短期|中期|近期|明年|下一(?:季|年)|\d+\s*个月内')
CATALYST_RE = re.compile(r'催化|驱动|事件|落地|投产|发布|获批|招标|放量|交付|量产|复产|上市|审议|实施|公告')
OPEN_Q_RE = re.compile(r'待核验|待确认|有待(?:验证|确认|观察|核实|落地)|需(?:进一步|持续)?(?:验证|确认|观察|核实|跟踪)|尚不明确|无法(?:确认|验证|核实)|存疑|尚需|仍需|尚未(?:披露|明确)|信息披露(?:有限|不足)')
TRIGGER_RE = re.compile(r'(?:若|如果|一旦|当|假如)[^。；]{2,45}?(?:则|将|就|可|会)[^。；]{0,30}?(?:上调|下调|调整|降级|升级|评级|买入|增持|减持|止损|止盈|重新评估|修正|超预期|低于预期)|(?:上调|下调|调整)评级的?(?:条件|信号|触发)|触发条件|止损|止盈')

ABSOLUTE_RE = re.compile(r'绝对(?:安全|不会|没有|无风险)|一定(?:会|能|大涨|上涨)|必将(?:上涨|大涨|翻倍|暴涨)|毫无疑问|百分之百|100%(?:确定|成功|盈利)|确定性(?:极|很|非常)高|板上钉钉|稳赚|(?:必|一定|肯定|必定|将会|稳)\S{0,3}(?:翻倍|暴涨|大涨|涨停)|强烈买入|无脑(?:买|入|冲)|闭眼(?:买|入)|零风险|没有风险|不会(?:亏|下跌|跌)|只涨不跌|必涨|必赚|包赚|(?<!确)保本(?![报公])')
RUMOR_RE = re.compile(r'传闻|据传|听说|市场(?:盛传|传言|传闻)|小道消息|据说|未经证实|网传|有消息称|疑似|据知情人士')
# 一票否决：收益保证/确定性荐股（沿用旧模块的短语，再补"强烈买入且无风险""翻倍"类组合）
GUARANTEE_RE = re.compile(r'保证(?:收益|盈利|赚钱|回报)|收益保证|保本保收益|稳赚(?:不赔)?|包赚|必涨|必赚|无风险(?:收益|投资|套利)|确定性(?:收益|回报)|必(?:将|定)(?:上涨|涨停|大涨|翻倍)|肯定(?:会)?(?:上涨|涨停)|一定会(?:上涨|涨|翻倍)|(?:强烈买入|立即买入|满仓)[^。；]{0,12}(?:无风险|零风险|没有风险|稳赚|必涨)|(?:无风险|零风险|没有风险)[^。；]{0,12}(?:强烈买入|必涨|翻倍)|股价(?:将|必将|一定)[^。；]{0,6}翻倍|(?:一定|必定|必然)(?:会)?(?:翻倍|大涨|暴涨)')
NEGATE_RE = re.compile(r'(?:不|未|不能|不会|不得|禁止|拒绝|无法|没有|并非|不是|不作|不做|不提供|不承诺|不应|切勿|严禁|避免)(?:作出|提供|承诺|宣称|声称|给予|存在|任何|所谓|作|保证|构成)?\s*[“"\']?\s*$')
DISCLAIMER_CTX_RE = re.compile(r'投资者|风险自担|不构成|据此|据以|依据本报告|仅供|不承担')
DECL_CTX_RE = re.compile(r'本人保证|作者保证|署名人员|分析师(?:声明|承诺)|不构成|不作为|承诺均为无效|不对.{0,10}保证|不保证|不承诺|任何.{0,4}保证|独立、客观|合规渠道|研究方法专业审慎')
CAUSAL_INVERT_RE = re.compile(r'(?:股价|市值|估值)(?:上涨|提升|上升|走高)(?:导致|带动|推动|使得|拉动)[^。；]{0,10}(?:业绩|净利润|营收|收入|利润)(?:增长|提升|上升|改善)')


def _snip(text: str, m: re.Match | None = None, width: int = 70) -> str:
    if m is None:
        return text[:width]
    s = max(0, m.start() - 12)
    e = min(len(text), m.end() + width - 12)
    return ('…' if s else '') + text[s:e] + ('…' if e < len(text) else '')


def _hit(rule: str, text: str, line: int, m: re.Match | None = None, **extra) -> dict:
    d = {'rule': rule, 'text': _snip(text, m), 'line': line}
    d.update(extra)
    return d


def _lin(x: float, lo: float, hi: float, smin: float = 0.0, smax: float = 5.0) -> float:
    """在 [lo, hi] 间线性映射到 [smin, smax]，两端截断。"""
    if hi == lo:
        return smax
    return smin + (smax - smin) * max(0.0, min(1.0, (x - lo) / (hi - lo)))


def _sub(sid: str, dim: str, name: str, score, why: str, hits=None, counter=None, metrics=None, applicable=True) -> dict:
    return {'id': sid, 'dim': dim, 'name': name,
            'score': None if score is None else round(max(0.0, min(5.0, float(score))), 2),
            'why': why, 'hits': (hits or [])[:12], 'counter': (counter or [])[:6],
            'metrics': metrics or {}, 'applicable': applicable and score is not None}


# ================================================================ 各子指标
def sec_hits(doc: Doc) -> dict:
    """每个章节 → {'level': 1.0 标题命中 / 0.6 仅正文多次提及 / 0, 'hits': [...]}。"""
    res = {}
    for key, (name, w, pat, min_mentions) in SECTION_DEFS.items():
        rx = re.compile(pat)
        hits = []
        level = 0.0
        for h in doc.headings:
            m = rx.search(h.text)
            if m:
                hits.append(_hit(f'S.{key}.heading', h.text, h.no, m))
        for p in doc.paragraphs:
            pass
        if hits:
            level = 1.0
        else:
            # 行首"风险提示：…""催化剂：…"这类内联小标题
            for s in doc.sentences:
                m = re.match(rf'^(?:\d+[.、]?\s*)?(?:{pat})\s*[:：]', s.text)
                if m:
                    hits.append(_hit(f'S.{key}.inline', s.text, s.line, m))
                    level = 0.9
                    break
        if level == 0.0:
            ms = [(s, rx.search(s.text)) for s in doc.sentences]
            ms = [(s, m) for s, m in ms if m]
            if len(ms) >= min_mentions + (2 if key in ('valuation', 'summary') else 1):
                level = 0.6
                hits = [_hit(f'S.{key}.mention', s.text, s.line, m) for s, m in ms[:3]]
        res[key] = {'name': name, 'weight': w, 'level': level, 'hits': hits}
    return res


def f_structure(doc: Doc) -> dict:
    res = sec_hits(doc)
    tot = sum(v['weight'] for v in res.values())
    got = sum(v['weight'] * v['level'] for v in res.values())
    hits, counter = [], []
    for k, v in res.items():
        if v['level'] > 0:
            hits.append({'rule': f'S.{k}', 'text': f"{v['name']}（{'标题' if v['level'] >= 0.9 else '仅正文提及'}）",
                         'line': v['hits'][0]['line'] if v['hits'] else 0, 'evidence': v['hits'][:1]})
        else:
            counter.append({'rule': f'S.{k}', 'text': f"缺少：{v['name']}"})
    sc = 5 * got / tot
    return _sub('struct.completeness', 'logic', '结构完整性', sc,
                f'8 类标准章节加权覆盖 {got:.1f}/{tot:.1f}', hits, counter,
                {'sections': {k: v['level'] for k, v in res.items()}})


def _numbers_in(s: str):
    return NUM_ANY_RE.findall(s)


def _strip_ids(s: str) -> str:
    s = re.sub(r'(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{1,2}\s*月\s*\d{1,2}\s*日|(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}', ' ', s)
    s = re.sub(r'[A-Za-z]?\d{6,}|\d{3,4}-\d{7,8}|S\d{10,}', ' ', s)
    s = re.sub(r'[（(]\d{6}(?:\.\w{2})?[）)]|\d{6}\.(?:SH|SZ|BJ|HK)', ' ', s)
    s = re.sub(r'(?:19|20)\d{2}\s*年|\d{2}\s*年|第\s*\d+\s*[页章节]|图\s*\d+|表\s*\d+|\d+\s*[）)、]|\d+\.\d+\.\d+', ' ', s)
    s = re.sub(r'[HQ][1-4]|[1-4]Q|\d{2}[EA]\b|\b20\d{2}[EA]\b', ' ', s)
    return s


def f_data(doc: Doc):
    prose_chars = max(1, sum(len(s.text) for s in doc.sentences))
    n_num = 0
    n_unit = 0
    n_qual = 0
    unqual = []
    qual_hits = []
    for s in doc.sentences:
        t = _strip_ids(s.text)
        nums = [m for m in NUM_ANY_RE.finditer(t)]
        if not nums:
            continue
        units = list(NUM_UNIT_RE.finditer(t))
        has_period = bool(PERIOD_RE.search(s.text))
        has_metric = bool(METRIC_RE.search(s.text))
        for m in nums:
            n_num += 1
        k_unit = len(units)
        n_unit += min(k_unit, len(nums))
        if k_unit and has_period and has_metric:
            n_qual += min(k_unit, len(nums))
            if len(qual_hits) < 3:
                qual_hits.append(_hit('D.qualified', s.text, s.line, units[0]))
        elif len(unqual) < 3 and len(nums) >= 1:
            unqual.append(_hit('D.unqualified', s.text, s.line, nums[0], missing=[x for x, ok in (('单位', k_unit > 0), ('期间', has_period), ('指标/口径', has_metric)) if not ok]))
    n_cells = len(doc.cells)
    density = (n_num + 0.25 * n_cells) / (prose_chars / 1000.0)
    # 数字密度：每千字 ≥ 14 个数字视为充分（卖方深度报告的典型水平）
    s_density = _lin(density, 2, 14)
    ratio_q = n_qual / n_num if n_num else 0.0
    s_qual = _lin(ratio_q, 0.15, 0.65)
    out = [
        _sub('data.density', 'evidence', '数字密度', s_density,
             f'正文 {n_num} 个数字 + 表格数字格 {n_cells} 个；约 {density:.1f} 个/千字',
             qual_hits[:2], [], {'numbers': n_num, 'table_cells': n_cells, 'per_1k': round(density, 2)}),
        _sub('data.qualified', 'fact', '带单位期间口径的数字占比', s_qual if n_num >= 3 else (0.0 if prose_chars > 500 else None),
             f'{n_qual}/{n_num} 个数字同时带单位、期间与指标名（占比 {ratio_q:.0%}）',
             qual_hits, unqual, {'qualified': n_qual, 'numbers': n_num, 'ratio': round(ratio_q, 3)}),
    ]
    # 来源标注
    src_cnt = len(doc.sources)
    inline = []
    for s in doc.sentences:
        m = SRC_RE.search(s.text)
        if m:
            inline.append(_hit('D.source', s.text, s.line, m))
    n_src = src_cnt + len(inline)
    per_1k = n_src / (prose_chars / 1000.0)
    s_src = _lin(per_1k, 0.0, 1.6)
    hits = [_hit('D.source.annot', l.text, l.no) for l in doc.sources[:4]] + inline[:4]
    out.append(_sub('data.source_rate', 'evidence', '来源标注率', s_src,
                    f'来源标注行 {src_cnt} 处 + 正文出处表述 {len(inline)} 处；约 {per_1k:.2f} 处/千字',
                    hits, [] if n_src else [{'rule': 'D.source', 'text': '全文未发现任何来源标注'}],
                    {'source_lines': src_cnt, 'inline_sources': len(inline), 'per_1k': round(per_1k, 3)}))
    # 图表来源
    n_cap = len(doc.captions)
    if n_cap:
        # 来源行紧随图表标题；用 source 行数与 caption 数比，也计 caption 行内的来源
        cap_in = sum(1 for c in doc.captions if SRC_ANNOT_RE.search(c.text))
        rate = min(1.0, (src_cnt + cap_in) / n_cap)
        out.append(_sub('data.figure_source', 'evidence', '图表来源', _lin(rate, 0.0, 0.85),
                        f'{n_cap} 个图表标题，约 {src_cnt + cap_in} 处来源标注，覆盖率 {rate:.0%}',
                        [_hit('D.fig', c.text, c.no) for c in doc.captions[:3]], [],
                        {'captions': n_cap, 'source_annotations': src_cnt + cap_in, 'rate': round(rate, 3)}))
    else:
        out.append(_sub('data.figure_source', 'evidence', '图表来源', None, '无图表标题，不适用', applicable=False))
    return out


def _window(doc: Doc, i: int) -> str:
    return ' '.join(doc.sentences[j].text for j in range(max(0, i - 1), min(len(doc.sentences), i + 2)))


def f_argument(doc: Doc):
    S = doc.sentences
    op_total = op_ev = 0
    op_hits, op_miss = [], []
    for i, s in enumerate(S):
        m = OPINION_RE.search(s.text)
        if not m or len(s.text) < 12:
            continue
        op_total += 1
        w = _window(doc, i)
        if NUM_UNIT_RE.search(_strip_ids(w)) or re.search(r'\d+(?:\.\d+)?\s*[%倍]', w):
            op_ev += 1
            if len(op_hits) < 3:
                op_hits.append(_hit('A.opinion_ev', s.text, s.line, m))
        elif len(op_miss) < 3:
            op_miss.append(_hit('A.opinion_noev', s.text, s.line, m))
    r = op_ev / op_total if op_total else 0.0
    out = [_sub('arg.claim_evidence', 'evidence', '观点—证据对应', _lin(r, 0.2, 0.8) if op_total >= 3 else (1.0 if op_total else 0.0),
                f'{op_ev}/{op_total} 条观点句在前后一句内有量化证据（{r:.0%}）', op_hits, op_miss,
                {'opinions': op_total, 'with_evidence': op_ev})]
    caus, caus_q = [], 0
    for i, s in enumerate(S):
        m = CAUSAL_RE.search(s.text)
        if not m:
            continue
        q = bool(NUM_UNIT_RE.search(_strip_ids(s.text)) or re.search(r'\d+(?:\.\d+)?\s*[%倍]', s.text))
        caus.append((s, m, q))
        caus_q += q
    n = len(caus)
    s_c = 0.5 * _lin(n, 1, 8) + 0.5 * _lin(caus_q / n if n else 0, 0.15, 0.6)
    out.append(_sub('arg.causal', 'logic', '因果论证（连接词+量化）', s_c if n else 0.0,
                    f'因果句 {n} 句，其中带量化支撑 {caus_q} 句',
                    [_hit('A.causal', s.text, s.line, m, quantified=q) for s, m, q in caus if q][:3],
                    [_hit('A.causal_unq', s.text, s.line, m) for s, m, q in caus if not q][:3],
                    {'causal': n, 'quantified': caus_q}))
    strong, weak = [], []
    for s in S:
        m = COUNTER_STRONG_RE.search(s.text)
        if m:
            strong.append(_hit('A.counter', s.text, s.line, m))
        elif COUNTER_WEAK_RE.search(s.text):
            weak.append(s)
    sc = min(5.0, 1.6 * len(strong) + 0.15 * min(len(weak), 8))
    out.append(_sub('arg.counter', 'logic', '反向验证/反证条件', sc,
                    f'条件式反证/情景表述 {len(strong)} 处，转折/保留表述 {len(weak)} 处',
                    strong[:3], [] if strong else [{'rule': 'A.counter', 'text': '未发现“若…则…下调/失效”类可证伪条件'}],
                    {'strong': len(strong), 'weak': len(weak)}))
    fm = [s for s in S if FACT_MARK_RE.search(s.text)]
    im = [s for s in S if INFER_MARK_RE.search(s.text)]
    vm = [s for s in S if VIEW_MARK_RE.search(s.text)]
    cats = sum(len(x) >= 2 for x in (fm, im, vm))
    bal = min(len(fm), len(im), len(vm))
    sc = {0: 0.0, 1: 1.5, 2: 3.0, 3: 4.0}[cats] + (1.0 if bal >= 6 else 0.0)
    out.append(_sub('arg.epistemic', 'logic', '事实/推论/观点区分用语', sc,
                    f'事实用语 {len(fm)} 句 / 推论用语 {len(im)} 句 / 观点用语 {len(vm)} 句',
                    [_hit('A.fact', fm[0].text, fm[0].line)] if fm else [] + ([_hit('A.infer', im[0].text, im[0].line)] if im else []),
                    [], {'fact': len(fm), 'inference': len(im), 'view': len(vm)}))
    return out


def f_valuation(doc: Doc):
    S = doc.sentences
    methods = OrderedDict()
    hits_m = []
    for name, pat in METHOD_DEFS.items():
        rx = re.compile(pat)
        for s in S:
            m = rx.search(s.text)
            if m:
                methods[name] = methods.get(name, 0) + 1
                if len(hits_m) < 4 and name not in [h.get('method') for h in hits_m]:
                    hits_m.append(_hit('V.method', s.text, s.line, m, method=name))
    applied = [(s, MULTIPLE_APPLY_RE.search(s.text)) for s in S]
    applied = [(s, m) for s, m in applied if m]
    target = [(s, TARGET_RE.search(s.text)) for s in S]
    target = [(s, m) for s, m in target if m]
    n_m = len(methods)
    sc = 0.0
    if n_m:
        sc = {1: 2.5, 2: 3.5}.get(n_m, 4.0)
        if applied:
            sc += 1.0
    elif target:
        sc = 1.0
    out = [_sub('val.method', 'logic', '估值方法', min(5.0, sc),
                f'识别估值方法 {list(methods) or "无"}；给出“对应 X 倍/PE”的应用表述 {len(applied)} 处',
                hits_m + [_hit('V.apply', s.text, s.line, m) for s, m in applied[:2]],
                [] if n_m else [{'rule': 'V.method', 'text': '未发现任何估值方法（DCF/PE/PB/EV/可比等）'}],
                {'methods': dict(methods), 'applied': len(applied)})]
    strong = [(s, ASSUMP_STRONG_RE.search(s.text)) for s in S]
    strong = [(s, m) for s, m in strong if m]
    gen = [(s, ASSUMP_GEN_RE.search(s.text)) for s in S if re.search(r'\d', s.text)]
    gen = [(s, m) for s, m in gen if m]
    msrc = [(s, MULT_SOURCE_RE.search(s.text)) for s in S if re.search(r'倍|PE|PB|估值|市盈|市净', s.text)]
    msrc = [(s, m) for s, m in msrc if m]
    comps_table = any(re.search(r'可比公司', c.text) for c in doc.captions + doc.headings)
    sc = 1.5 * min(2, len(strong)) + 1.0 * min(2, len(gen)) + 1.2 * min(2, len(msrc)) + (0.8 if comps_table else 0)
    hs = [_hit('V.assump_strong', s.text, s.line, m) for s, m in strong[:2]] + [_hit('V.assump', s.text, s.line, m) for s, m in gen[:2]] + \
         [_hit('V.mult_source', s.text, s.line, m) for s, m in msrc[:2]]
    out.append(_sub('val.assumptions', 'logic', '关键假设与倍数来源', min(5.0, sc),
                    f'WACC/永续增长等 {len(strong)} 句；带数字的假设句 {len(gen)} 句；倍数来源（可比/历史分位）{len(msrc)} 句；可比公司表 {"有" if comps_table else "无"}',
                    hs, [] if hs else [{'rule': 'V.assump', 'text': '未交代估值/预测的关键假设与倍数来源'}],
                    {'strong': len(strong), 'general': len(gen), 'mult_source': len(msrc), 'comps_table': comps_table}))
    sce = [(s, SCENARIO_RE.search(s.text)) for s in S]
    sce = [(s, m) for s, m in sce if m]
    sce_num = [(s, m) for s, m in sce if re.search(r'\d', s.text)]
    heading_sens = any(re.search(r'敏感性|情景', c.text) for c in doc.captions + doc.headings)
    sc = 0.0
    if sce:
        sc = 1.5 + min(2.0, 0.7 * len(sce_num)) + (1.5 if heading_sens else 0)
    out.append(_sub('val.scenario', 'logic', '情景与敏感性', min(5.0, sc),
                    f'情景/敏感性用语 {len(sce)} 句，其中带数字 {len(sce_num)} 句；敏感性标题/图表 {"有" if heading_sens else "无"}',
                    [_hit('V.scenario', s.text, s.line, m) for s, m in (sce_num or sce)[:3]],
                    [] if sce else [{'rule': 'V.scenario', 'text': '未发现情景分析或敏感性分析'}],
                    {'scenario': len(sce), 'with_numbers': len(sce_num), 'heading': heading_sens}))
    rng = [(s, RANGE_RE.search(s.text)) for s in S]
    rng = [(s, m) for s, m in rng if m and re.search(r'估值|目标|合理|价|倍|区间', s.text)]
    if rng:
        sc, why = 5.0, '给出估值/目标价区间而非单点'
    elif target and applied:
        sc, why = 3.2, '单点目标价，但交代了对应倍数'
    elif target:
        sc, why = 1.5, '仅有单点目标价，未交代推导'
    elif n_m:
        sc, why = 2.0, '有估值方法但无目标价/区间'
    else:
        sc, why = 0.0, '无估值结论'
    out.append(_sub('val.range', 'logic', '区间而非单点', sc, why,
                    [_hit('V.range', s.text, s.line, m) for s, m in rng[:2]] or [_hit('V.target', s.text, s.line, m) for s, m in target[:2]],
                    [], {'range': len(rng), 'target': len(target)}))
    return out


def _risk_section(doc: Doc):
    """返回风险提示段正文（标题到下一标题；或行内“风险提示：…”句）。"""
    rx = re.compile(SECTION_DEFS['risk'][2])
    paras = doc.paragraphs
    texts, line = [], 0
    for idx, (pn, ln, t) in enumerate(paras):
        is_head = any(h.no == ln and h.text == t for h in doc.headings)
        if (is_head and rx.search(t) and len(t) <= 20) or re.match(r'^(?:\d+[.、]?\s*)?(?:风险提示|风险因素|主要风险|潜在风险)\s*[:：]', t):
            if re.match(r'^(?:\d+[.、]?\s*)?(?:风险提示|风险因素|主要风险|潜在风险)\s*[:：]', t):
                texts.append(t)
                line = line or ln
                # 行内小标题后若紧接几行续写（PDF 换行），已在 paragraph 合并中处理
                continue
            line = line or ln
            acc = 0
            for pn2, ln2, t2 in paras[idx + 1: idx + 40]:
                if any(h.no == ln2 and h.text == t2 for h in doc.headings):
                    break
                acc += len(t2)
                if acc > 1800:
                    break
                if re.match(r'^(?:免责|重要声明|分析师声明|附表)', t2):
                    break
                texts.append(t2)
            break
    return '。'.join(texts)[:3000], line


def f_risk(doc: Doc):
    rs, line = _risk_section(doc)
    items = []
    for m in RISK_ITEM_RE.finditer(rs):
        w = re.sub(r'^(?:\d+[.、）)]|[一二三四五六七八九十][、.])', '', m.group()).strip()
        w = re.sub(r'^(?:风险提示|主要风险|潜在风险|包括|如|等)[:：]?', '', w)
        if len(w) >= 3 and w not in items:
            items.append(w)
    numbered = re.findall(r'(?:^|[。；\s])(?:\d+|[一二三四五六七八九十])[、.）)]\s*\S', rs)
    n_items = max(len(items), len(numbered))
    chars_per = len(rs) / max(1, n_items) if rs else 0
    out = []
    if not rs:
        out.append(_sub('risk.count', 'risk', '风险项数量', 0.0, '未找到“风险提示”章节或段落', [], [{'rule': 'R.none', 'text': '缺少风险提示'}]))
        out.append(_sub('risk.specific', 'risk', '风险具体性', 0.0, '无风险提示', [], []))
        out.append(_sub('risk.quantified', 'risk', '风险量化', 0.0, '无风险提示', [], []))
    else:
        sc = {0: 0.5, 1: 1.2, 2: 2.5, 3: 3.5, 4: 4.3}.get(n_items, 5.0)
        out.append(_sub('risk.count', 'risk', '风险项数量', sc, f'风险提示段 {len(rs)} 字，识别 {n_items} 个风险项',
                        [_hit('R.items', rs[:120], line)] + [{'rule': 'R.item', 'text': x, 'line': line} for x in items[:5]], [],
                        {'items': items[:12], 'n_items': n_items, 'chars': len(rs)}))
        trans = RISK_TRANSMIT_RE.findall(rs)
        generic = [x for x in items if RISK_GENERIC_RE.match(x.replace('风险', '')) or len(x) <= 6]
        sc = _lin(chars_per, 6, 40) * 0.75 + (1.25 if len(trans) >= 2 else 0.6 if trans else 0)
        sc -= 0.6 * min(3, len(generic))
        out.append(_sub('risk.specific', 'risk', '风险具体性', max(0.0, sc),
                        f'平均每项 {chars_per:.0f} 字；传导词 {len(trans)} 个；泛化风险项 {len(generic)} 个',
                        [_hit('R.specific', rs[:140], line)], [{'rule': 'R.generic', 'text': g} for g in generic[:4]],
                        {'chars_per_item': round(chars_per, 1), 'transmit_terms': len(trans), 'generic': len(generic)}))
        nums = RISK_NUM_RE.findall(rs)
        sens_q = [s for s in doc.sentences if re.search(r'风险|不及预期|下行', s.text) and RISK_NUM_RE.search(s.text)]
        k = len(nums) + len(sens_q)
        sc = 0.0 if k == 0 else (2.5 if k == 1 else (3.8 if k == 2 else 5.0))
        out.append(_sub('risk.quantified', 'risk', '风险量化', sc, f'风险段内量化表述 {len(nums)} 处；含风险字样的带数字句 {len(sens_q)} 句',
                        [_hit('R.quant', s.text, s.line) for s in sens_q[:3]], [], {'numbers_in_risk': len(nums), 'quant_sentences': len(sens_q)}))
    link = [s for s in doc.sentences if re.search(r'风险|若|如果|一旦|不及预期|低于预期', s.text) and FORECAST_LINK_RE.search(s.text)
            and re.search(r'下调|下滑|下降|低于|不及|影响|导致|调整|减少|压缩', s.text)]
    n = len(link)
    out.append(_sub('risk.link', 'risk', '风险与盈利预测联动', 0.0 if n == 0 else (3.0 if n == 1 else 4.2 if n == 2 else 5.0),
                    f'风险/条件与盈利预测、评级、目标价联动的句子 {n} 句', [_hit('R.link', s.text, s.line) for s in link[:3]],
                    [] if n else [{'rule': 'R.link', 'text': '风险未与盈利预测/目标价联动'}], {'sentences': n}))
    un = [s for s in doc.sentences if UNCERTAIN_RE.search(s.text)]
    per_1k = len(un) / max(1, len(doc.prose) / 1000)
    out.append(_sub('risk.uncertainty', 'risk', '不确定性措辞', _lin(per_1k, 0.5, 5.0), f'不确定性/假设性措辞 {len(un)} 句，约 {per_1k:.1f} 句/千字',
                    [_hit('R.unc', s.text, s.line, UNCERTAIN_RE.search(s.text)) for s in un[:2]], [], {'sentences': len(un), 'per_1k': round(per_1k, 2)}))
    return out


def f_action(doc: Doc):
    S = doc.sentences
    trk = [s for s in S if TRACK_RE.search(s.text) and TRACK_METRIC_RE.search(s.text) and len(s.text) >= 10]
    n = len(trk)
    out = [_sub('act.tracking', 'action', '跟踪指标', {0: 0.0, 1: 2.0, 2: 3.2, 3: 4.0}.get(n, 5.0), f'跟踪/监测类且指向具体指标的句子 {n} 句',
                [_hit('X.track', s.text, s.line, TRACK_RE.search(s.text)) for s in trk[:3]], [] if n else [{'rule': 'X.track', 'text': '无具体跟踪指标'}], {'n': n})]
    cat = [s for s in S if CATALYST_RE.search(s.text) and TIME_RE.search(_strip_ids(s.text).replace('Q', 'Q')) or (CATALYST_RE.search(s.text) and re.search(r'20\d{2}\s*年(?:[1-4]Q|Q[1-4]|H[12]|[上下]半年|底|[一二三四]季度)|[1-4]Q|Q[1-4]|[上下]半年|年底|年内|\d{1,2}月', s.text))]
    cat = list({id(s): s for s in cat}.values())
    n = len(cat)
    out.append(_sub('act.catalyst_timing', 'action', '催化剂与时点', {0: 0.0, 1: 2.0, 2: 3.0}.get(n, 4.0 if n < 5 else 5.0), f'催化/事件类且带时点的句子 {n} 句',
                    [_hit('X.catalyst', s.text, s.line, CATALYST_RE.search(s.text)) for s in cat[:3]], [] if n else [{'rule': 'X.catalyst', 'text': '无带时点的催化剂'}], {'n': n}))
    oq = [s for s in S if OPEN_Q_RE.search(s.text)]
    n = len(oq)
    out.append(_sub('act.open_questions', 'action', '待核验/待确认问题', 0.0 if n == 0 else (3.0 if n == 1 else 4.2 if n == 2 else 5.0), f'明示待验证/不确定事项 {n} 句',
                    [_hit('X.open', s.text, s.line, OPEN_Q_RE.search(s.text)) for s in oq[:3]], [] if n else [{'rule': 'X.open', 'text': '未提示待核验事项'}], {'n': n}))
    tr = [(s, TRIGGER_RE.search(s.text)) for s in S]
    tr = [(s, m) for s, m in tr if m]
    n = len(tr)
    out.append(_sub('act.trigger', 'action', '触发条件', {0: 0.0, 1: 3.0}.get(n, 5.0), f'评级/预测调整的触发条件 {n} 处',
                    [_hit('X.trigger', s.text, s.line, m) for s, m in tr[:3]], [] if n else [{'rule': 'X.trigger', 'text': '无评级调整/止损等触发条件'}], {'n': n}))
    head = doc.canon[:3500]
    rating = RATING_RE.search(head)
    target = TARGET_RE.search(doc.canon[:6000]) or TARGET_RE.search(doc.prose)
    horizon = HORIZON_RE.search(doc.canon[:6000]) or HORIZON_RE.search(doc.prose)
    sc = 2.0 * bool(rating) + 2.0 * bool(target) + 1.0 * bool(horizon)
    out.append(_sub('act.decision', 'action', '投资结论是否明确', sc,
                    f'评级{"有" if rating else "无"} / 目标价{"有" if target else "无"} / 期限{"有" if horizon else "无"}',
                    [{'rule': 'X.rating', 'text': rating.group(), 'line': 0}] if rating else [],
                    [{'rule': 'X.decision', 'text': x} for x, ok in (('无评级', rating), ('无目标价/合理价值', target), ('无投资期限', horizon)) if not ok],
                    {'rating': bool(rating), 'target': bool(target), 'horizon': bool(horizon)}))
    return out


def guarantee_hits(doc: Doc):
    """收益保证/确定性荐股：否定作用域按子句判定，免责声明里的“不保证…”不误伤。"""
    hits = []
    for s in doc.sentences:
        if DECL_CTX_RE.search(s.text):
            continue
        for clause in re.split(r'[，,；;]|但(?:是)?|然而|不过', s.text):
            for m in GUARANTEE_RE.finditer(clause):
                prefix = clause[:m.start()]
                if NEGATE_RE.search(prefix):
                    continue
                if re.search(r'不(?:得|能|可|应|会|宜).{0,6}$', prefix) or re.match(r'.{0,2}(?:的)?(?:说法|承诺|表述)(?:并非|不成立|不可信)', clause[m.end():]):
                    continue
                hits.append(_hit('C.guarantee', s.text, s.line, m, matched=m.group()))
    return hits


def f_compliance(doc: Doc):
    S = doc.sentences
    ab = []
    for s in S:
        if DECL_CTX_RE.search(s.text):
            continue
        for m in ABSOLUTE_RE.finditer(s.text):
            pre = s.text[max(0, m.start() - 6):m.start()]
            if NEGATE_RE.search(pre) or re.search(r'不(?:得|能|可|会)?(?:承诺|保证|作)', s.text[:m.end()]):
                continue
            ab.append(_hit('C.absolute', s.text, s.line, m, matched=m.group()))
            break
    gh = guarantee_hits(doc)
    n = len(ab)
    out = [_sub('comp.absolute', 'risk', '绝对化/保证性措辞', max(0.0, 5.0 - 2.5 * n - 2.5 * len(gh)),
                f'绝对化措辞 {n} 处，收益保证类 {len(gh)} 处', ab[:4] + gh[:2],
                [], {'absolute': n, 'guarantee': len(gh)})]
    rm = []
    for s in S:
        m = RUMOR_RE.search(s.text)
        if m and not SRC_RE.search(s.text):
            rm.append(_hit('C.rumor', s.text, s.line, m))
    out.append(_sub('comp.rumor', 'risk', '无来源传闻', max(0.0, 5.0 - 2.5 * len(rm)), f'无来源传闻/小道消息 {len(rm)} 处', rm[:3], [], {'n': len(rm)}))
    d = SECTION_DEFS['disclaimer']
    rx = re.compile(d[2])
    dh = [_hit('C.disclaimer', s.text, s.line, rx.search(s.text)) for s in S if rx.search(s.text) and DISCLAIMER_CTX_RE.search(s.text)]
    dh += [_hit('C.disclaimer.h', h.text, h.no) for h in doc.headings if rx.search(h.text)]
    # 页眉里的“请务必阅读…声明”已被当作噪声丢弃，所以必须有声明本身才算
    out.append(_sub('comp.disclaimer', 'risk', '免责声明/披露', 5.0 if len(dh) >= 2 else (3.5 if dh else 0.0), f'免责声明/披露表述 {len(dh)} 处',
                    dh[:2], [] if dh else [{'rule': 'C.disclaimer', 'text': '未发现免责声明/分析师声明'}], {'n': len(dh)}))
    return out


def all_features(doc: Doc, consistency: dict | None = None) -> list:
    subs = [f_structure(doc)]
    subs += f_data(doc)
    subs += f_argument(doc)
    subs += f_valuation(doc)
    subs += f_risk(doc)
    subs += f_action(doc)
    subs += f_compliance(doc)
    subs.append(consistency_sub(consistency))
    subs.append(consistency_sub(consistency, 'formula'))
    return subs


def consistency_sub(c: dict | None, which: str = 'arith') -> dict:
    """内部一致性（来自 errorcheck 适配层 report_consistency_adapter）。"""
    if which == 'formula':
        if not c or not c.get('formula_checked'):
            return _sub('consist.formula', 'fact', '显式公式复算', None, '报告内无可复算的显式算式', applicable=False)
        bad = c.get('formula_failed', 0)
        return _sub('consist.formula', 'fact', '显式公式复算', max(0.0, 5.0 - 2.5 * bad),
                    f"显式算式 {c['formula_checked']} 条，复算不符 {bad} 条", c.get('formula_hits', [])[:3], c.get('formula_bad', [])[:3])
    if not c or not c.get('checks'):
        return _sub('consist.arith', 'fact', '内部一致性（表内/表文复算）', None, '未能找到可交叉复算的数字组（如增长率行、PE×EPS、摘要与预测表）', applicable=False)
    n, bad = c['checks'], len(c.get('failures', []))
    return _sub('consist.arith', 'fact', '内部一致性（表内/表文复算）', max(0.0, 5.0 - 2.5 * bad - 0.0),
                f'可交叉复算 {n} 组，不符 {bad} 组', c.get('passes', [])[:3], c.get('failures', [])[:4], {'checks': n, 'failures': bad})
