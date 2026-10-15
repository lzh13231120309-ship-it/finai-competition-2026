"""附注索引与关键披露定位。

解决的问题
----------
分析报告把"工具没抽到"写成"数据缺口"，读者会理解为"年报未披露"。这两件事
性质完全不同：前者是取数范围问题，后者才是披露缺失。实测某份年报分析报告
列了 9 项"数据缺口"，其中 7 项在年报附注里写得清清楚楚：

    · 分产品 / 分区域收入与毛利率      → 附注 4  分部资料
    · 存货明细（原材料 / 在製品 / 製成品）→ 附注 19
    · 应收账款减值准备 41,522 千元      → 附注 20
    · 计息借贷利率 0.30%–2.80%          → 附注 27
    · 其他应付款项及应计费用 119.62 亿元 → 附注 25
    · 利息收入 5.80 亿元                → 附注 6
    · 股息 0.99 元/股、派息率 70%        → 附注 12

本模块扫描全文，建立「附注号 → 标题 → 页码」索引，并把常见分析需求定位到
具体附注与页码。报告于是可以写"见附注 19（第 197 页）"，而不是"数据缺口"。

边界
----
页码是**工具内部页码**（与表格 ID 中的 p{页码} 一致），不是印刷页脚数字；
两者在多数年报里相同，但有封面/插页时会差 1–2 页。引用时应说明是工具页码，
或回原文核对。索引是启发式定位，不是"该附注一定含此数据"的断言。
"""
from __future__ import annotations

import re

CJK = re.compile(r'[\u4e00-\u9fff]')

# 附注标题：「18. 其他非流動資產   18. OTHER NON-CURRENT ASSETS」
# 中英并列是港式年报的固定排版，两个标题在同一行。
_NOTE_HEADING_RE = re.compile(
    r'^[ \t\u3000]*(\d{1,2})[ \t\u3000]*[.．、][ \t\u3000]*(\S[^\n]{0,90}?)[ \t\u3000]*$', re.M)
_CAPS_TITLE_RE = re.compile(r"[A-Z0-9][A-Z0-9\s,&()\-/\.'’]{2,}")

# 「財務報表附註 / NOTES TO THE FINANCIAL STATEMENTS」是附注章节的起点。
# 必须要求**整行只由该标题构成**（允许带"（續）/(continued)"）：
# 目录页与企业管治报告里也会出现这几个字，但它们后面跟着页码或正文，
# 松散匹配会把章节起点判成第 2 页（目录），于是企业管治报告里的编号段落
# （「1. 本公司依法運作情況。」「5. 本公司關連交易情況。」）被当成附注 1–6
# 混进索引，还让后续 36 条真附注无法与它们区分。
_NOTES_SECTION_RE = re.compile(
    r'^[ \t\u3000]*(?:財務報表附註|财务报表附注|NOTES\s+TO\s+THE\s+FINANCIAL\s+STATEMENTS)'
    r'[ \t\u3000]*(?:[（(]\s*(?:續|续|continued)\s*[）)])?[ \t\u3000]*$', re.M | re.I)

# 常见分析需求 → 定位关键词（中英并列，繁体在查询前会先转简体）。
NEED_KEYWORDS: dict[str, list[str]] = {
    '分部/分产品/分区域收入': ['分部', '分類收入', '分类收入', '收入分解', 'segment', 'disaggregation', 'revenue by'],
    '存货明细与跌价准备': ['存货', 'inventor'],
    '应收账款账龄与预期信用损失': ['贸易应收款项', '预期信贷损失', '贸易应收', 'trade receivable', 'expected credit loss'],
    '借款利率、期限与担保': ['计息借贷', '计息银行借贷', '银行借贷', '借款', 'borrowing', 'interest rate'],
    '其他应付款项构成': ['其他应付款项', 'other payable'],
    '利息收入与财务费用明细': ['利息收入', '财务费用', '财务成本', 'interest income', 'finance cost'],
    '股息与派息率': ['股息', 'dividend'],
    '审计意见类型': ['独立核数师报告', '核数师报告', '审计报告', 'independent auditor'],
    '关联方交易': ['关联方', '关连交易', 'related party'],
    '外币敞口与汇率风险': ['外币', '汇率风险', 'foreign currency', 'exchange risk'],
    '承诺与或有负债': ['承诺', '或然负债', '或有负债', 'commitment', 'contingent'],
    '资产质押与受限资金': ['资产质押', '受限资金', 'restricted cash', 'pledge of assets'],
    '政府补助': ['政府补助', '政府补贴', 'government grant'],
    '收入确认政策': ['收入确认', 'revenue recognition'],
    '金融工具分类与公平值层级': ['金融工具', '公平值', 'financial instrument', 'fair value'],
    '每股盈利与股本变动': ['每股盈利', '每股基本', '股本', 'earnings per share'],
}


def _simplify(text: str) -> str:
    try:
        from .subject_i18n import to_simplified
        return to_simplified(str(text or ''))
    except Exception:                                     # pragma: no cover
        return str(text or '')


def _looks_like_title(title: str) -> bool:
    """过滤掉不是附注标题的编号行（正文条目、比例、条款引用等）。"""
    if not title or '%' in title:
        return False
    if title.startswith(('（', '(')):
        return False
    # 中文长句（含句号、逗号结尾且很长）是正文条目，不是标题
    if CJK.search(title) and len(title) > 26 and title.endswith(('。', '，', '、')):
        return False
    if CJK.search(title):
        return True
    return bool(_CAPS_TITLE_RE.fullmatch(title))


def build_notes_index(pages, text: str = '') -> dict:
    """建立附注索引与需求定位表。

    参数
    ----
    pages : [{'page': int, 'text': str}, ...]  解析器产出的逐页文本
    text  : 全文（可选）。仅在 pages 缺失时用于兜底判断附注章节是否存在。

    返回
    ----
    {'notes': [{'note': 19, 'page': 197, 'title': '存貨  INVENTORIES'}],
     'notes_section_start': 160,
     'disclosure_map': {'存货明细与跌价准备': [{'note': 19, 'page': 197, 'title': ...}]},
     'unlocated': ['政府补助'],
     'note': '...'}
    """
    pages = list(pages or [])
    ordered = sorted(((p.get('page'), str(p.get('text') or '')) for p in pages
                      if isinstance(p, dict) and p.get('page') is not None),
                     key=lambda item: item[0])
    notes_section_start = None
    for number, page_text in ordered:
        if _NOTES_SECTION_RE.search(page_text):
            notes_section_start = number
            break
    if notes_section_start is None and text and _NOTES_SECTION_RE.search(str(text)):
        # 页码信息不可用但全文命中：不做章节过滤，避免整块索引丢失。
        notes_section_start = 0

    notes, seen = [], {}
    for number, page_text in ordered:
        if notes_section_start is not None and number < notes_section_start:
            continue
        for match in _NOTE_HEADING_RE.finditer(page_text):
            note_number = int(match.group(1))
            title = match.group(2).strip()
            if not 1 <= note_number <= 80 or not _looks_like_title(title):
                continue
            # 同一附注跨页时保留首次出现的位置（附注的正文起点）。
            if note_number in seen:
                continue
            seen[note_number] = True
            notes.append({'note': note_number, 'page': number,
                          'title': re.sub(r'\s{2,}', '  ', title)[:80]})
    notes.sort(key=lambda item: (item['note'], item['page']))

    # 需求定位：先在附注标题里找，再退到该页正文里找。
    by_page = dict(ordered)
    disclosure_map, unlocated = {}, []
    for need, keywords in NEED_KEYWORDS.items():
        hits = []
        for entry in notes:
            blob = _simplify(entry['title'])
            if any(keyword.lower() in blob.lower() for keyword in keywords):
                hits.append(entry)
        if not hits:
            for number, page_text in ordered:
                if notes_section_start is not None and number < notes_section_start:
                    continue
                blob = _simplify(page_text)
                if any(keyword.lower() in blob.lower() for keyword in keywords):
                    hits.append({'note': None, 'page': number, 'title': ''})
                    if len(hits) >= 3:
                        break
        if hits:
            disclosure_map[need] = hits[:3]
        else:
            unlocated.append(need)

    return {
        'notes': notes,
        'notes_section_start': notes_section_start,
        'disclosure_map': disclosure_map,
        'unlocated': unlocated,
        'note': ('页码为工具内部页码（与表格 ID 的 p{页码} 一致），与印刷页脚可能相差 1–2 页；'
                 '索引是启发式定位，用于把"工具未抽取"与"文档未披露"区分开，'
                 '引用前请回原文核对。'),
    }


def disclosure_hint(result: dict) -> list[dict]:
    """把索引压成模型可直接引用的一行行提示（控制上下文体积）。"""
    lines = []
    for need, hits in (result.get('disclosure_map') or {}).items():
        where = '；'.join(
            f"附注{h['note']}（第{h['page']}页）" if h.get('note') else f"第{h['page']}页"
            for h in hits)
        lines.append({'需求': need, '原文位置': where})
    return lines
