"""研报文本规范化与分段（质量评估 v2 的第一层，纯确定性、仅用标准库）。

目的：让 PDF 抽取 / docx / Markdown 三种来源、含页眉页脚噪声、全半角、简繁混排的同一份报告，
规范化后得到（几乎）相同的"行 → 段落 → 句子"结构，评分才不会因为格式漂移。

每个句子都带 line（在输入原文中的行号，1 起）与 offset，供"为什么"钻取和复核队列定位。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# 繁 -> 简：使用项目内置的 OpenCC TSCharacters 单字全表（Apache-2.0，见 server/trad_to_simp_data.py）
from .trad_to_simp_data import TRAD_TO_SIMP_FULL as _T2S
S2T: dict = {}
for _t, _s in _T2S.items():
    if _t != _s:
        S2T.setdefault(_s, _t)

_NOISE_ZW = dict.fromkeys(map(ord, '​‌‍﻿­⁠'), None)
_PAGE_NO = re.compile(r'^[\-—–\s]*(第\s*)?\d{1,3}\s*(页)?\s*(/\s*\d{1,3}|共\s*\d{1,3}\s*页)?[\-—–\s]*$')
_PAGE_NO2 = re.compile(r'^(page|p\.?)\s*\d+(\s*(of|/)\s*\d+)?$', re.I)
_TOC = re.compile(r'[.·…。]{5,}\s*\d{0,3}\s*$|^\s*(目\s*录|图\s*目\s*录|表\s*目\s*录)\s*$')
_PERSON = re.compile(r'执业证号|执业资格|电话[:：]?\s*\d|邮箱[:：]|E-?mail|传真|联系人|联系电话|@[\w.-]+\.\w+', re.I)
_BOILER = re.compile(r'请务必阅读(正文之后|正文后|本报告正文后|最后一页)?.{0,6}(免责|重要|声明)|敬请参阅.{0,10}(声明|免责)|请阅读.{0,6}(免责|重要声明)|详见.{0,6}(免责|声明)')
_TERMINAL = re.compile(r'[。！？；!?;:：]$')
_CELL = re.compile(r'^[-+−–]?\(?[\d.,]+\)?\s*[%％xX倍元]?$|^[-–—−]$|^n/?a$|^NA$|^N/A$', re.I)
_CAPTION = re.compile(r'^(图|表|图表|附图|附表|Figure|Table|Chart|Fig\.)\s*\d+\s*[:：.、\s]', re.I)
_BULLET = re.compile(r'^(\d+(\.\d+){0,3}[、.\s]\s*\S|[一二三四五六七八九十]+[、.]\s*\S|[（(][一二三四五六七八九十\d]+[）)]\s*\S|第[一二三四五六七八九十\d]+[章节部分])')
_START = re.compile(r'^(?:[◼●■◆▲•▪·\-\*]|\d+[、.)）]\s*\S|[一二三四五六七八九十]+[、.]|[（(][一二三四五六七八九十\d]+[）)])')
_BULLET_ONLY = re.compile(r'^[•●■◆▲\-·\*]\s*')
_HEADKW = re.compile(r'投资要点|核心观点|摘要|投资(逻辑|评级|建议)|推荐逻辑|事件|点评|公司|行业|业务|财务|业绩|盈利|预测|假设|估值|可比|催化|风险|声明|免责|结论|展望|附表|目标价|概况|分析|评级|来源|跟踪|关注')
_SENT_END = re.compile(r'(?<=[。！？；])|(?<=[!?;])(?=\s)')


def to_simplified(text: str) -> str:
    return ''.join(_T2S.get(c, c) for c in text)


def to_traditional(text: str) -> str:
    return ''.join(S2T.get(c, c) for c in text)


@dataclass
class Line:
    no: int          # 原文行号（1 起）
    text: str
    kind: str        # prose / heading / cell / caption / source / table_label
    offset: int = 0


@dataclass
class Sentence:
    text: str
    line: int
    para: int
    offset: int = 0


@dataclass
class Doc:
    raw_len: int
    lines: list = field(default_factory=list)       # 清洗后的 Line
    sentences: list = field(default_factory=list)   # Sentence（仅正文）
    headings: list = field(default_factory=list)    # Line
    cells: list = field(default_factory=list)       # Line（表格数字格）
    captions: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    paragraphs: list = field(default_factory=list)  # (para_no, line_no, text)
    quality: dict = field(default_factory=dict)
    prose: str = ''
    canon: str = ''


def _digits_to_hash(s: str) -> str:
    return re.sub(r'\d+', '#', s)


def normalize_line(s: str) -> str:
    s = unicodedata.normalize('NFKC', s).translate(_NOISE_ZW)
    s = re.sub(r'^\s{0,3}#{1,6}\s+', '', s)
    s = re.sub(r'\*\*|__', '', s)
    s = re.sub(r'^\s*[|｜]\s*|\s*[|｜]\s*$', '', s)
    s = to_simplified(s)
    s = s.replace('　', ' ').replace('\xa0', ' ')
    # 相邻汉字间的空白（PDF 常见的字间距、docx 软换行）一律删除
    s = re.sub(r'(?<=[一-鿿，。；：、！？（）《》“”%])\s+(?=[一-鿿，。；：、！？（）《》“”])', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    # NFKC 把全角标点变成半角，统一回中文标点，使“，”与“,”两种写法等价
    s = re.sub(r'(?<=[一-鿿]),', '，', s)
    s = re.sub(r',(?=[一-鿿])', '，', s)
    s = re.sub(r'(?<=[一-鿿]);', '；', s)
    s = re.sub(r'(?<=[一-鿿]):(?!\d)', '：', s)
    s = re.sub(r'(?<=[一-鿿])\?', '？', s)
    s = re.sub(r'(?<=[一-鿿])!', '！', s)
    return s


def _is_heading(t: str) -> bool:
    if len(t) > 32 or _TERMINAL.search(t) or _CELL.match(t):
        return False
    if _CAPTION.match(t):
        return False
    if _BULLET.match(t) and _HEADKW.search(t):
        return True
    if re.match(r'^(投资要点|核心观点|风险提示|投资建议|估值分析|盈利预测|免责声明|重要声明|分析师声明|主要风险|催化剂|估值与投资建议)', t):
        return True
    # 短行且以章节关键词结尾
    return len(t) <= 16 and bool(re.search(r'(要点|观点|逻辑|摘要|分析|预测|假设|估值|风险|提示|声明|建议|概况|展望|催化剂|回顾|附表|点评)$', t))


def preprocess(text: str) -> Doc:
    raw = text if isinstance(text, str) else ''
    doc = Doc(raw_len=len(raw))
    raw_lines = raw.replace('\r\n', '\n').replace('\r', '\n').replace('\f', '\n').split('\n')
    # docx/markdown 表格行 "a | b | c" 展开为逐格行（行号沿用原行号）
    expanded = []
    for rl in raw_lines:
        if rl.count('|') >= 2 and len(rl) < 400:
            expanded.append([c for c in re.split(r'\s*[|｜]\s*', rl) if c.strip()])
        else:
            expanded.append(None)
    cleaned = []
    offs = 0
    for i, rl in enumerate(raw_lines, 1):
        if expanded[i - 1] is not None:
            for cell in expanded[i - 1]:
                cleaned.append((i, normalize_line(cell), offs))
        else:
            cleaned.append((i, normalize_line(rl), offs))
        offs += len(rl) + 1
    cleaned = [(i, t, o) for i, t, o in cleaned if t]
    # 重复页眉页脚（≥3 次、≥10 字、含汉字、不含"来源"类标注）
    freq = {}
    for _, t, _o in cleaned:
        if len(t) >= 10 and re.search(r'[一-鿿]', t) and not re.search(r'来源|资料|数据', t):
            freq[_digits_to_hash(t)] = freq.get(_digits_to_hash(t), 0) + 1
    kept = []
    dropped = 0
    for i, t, o in cleaned:
        if (_PAGE_NO.match(t) and len(t) <= 12) or _PAGE_NO2.match(t) or _TOC.search(t) or _PERSON.search(t) \
                or _BOILER.search(t) or freq.get(_digits_to_hash(t), 0) >= 3 and len(t) >= 10 and not re.search(r'来源|资料|数据', t) \
                and re.search(r'[一-鿿]', t) and len(t) <= 60:
            dropped += 1
            continue
        kept.append((i, t, o))
    # 分类
    lines = []
    for i, t, o in kept:
        t2 = _BULLET_ONLY.sub('', t) if _BULLET_ONLY.match(t) and len(t) > 3 else t
        if _CELL.match(t2):
            kind = 'cell'
        elif _CAPTION.match(t2):
            kind = 'caption'
        elif re.match(r'^(资料来源|数据来源|图表来源|来源|Source|Sources)\s*[:：]', t2, re.I):
            kind = 'source'
        elif _is_heading(t2):
            kind = 'heading'
        elif len(t2) <= 10 and not re.search(r'[。，；！？]', t2) and not re.search(r'[一-鿿]{7,}', t2):
            kind = 'table_label'
        else:
            kind = 'prose'
        lines.append(Line(i, t2, kind, o))
    doc.lines = lines
    doc.headings = [l for l in lines if l.kind == 'heading']
    doc.cells = [l for l in lines if l.kind == 'cell']
    doc.captions = [l for l in lines if l.kind == 'caption']
    doc.sources = [l for l in lines if l.kind == 'source']
    # 段落：连续 prose 行合并（上一行不以句末标点结束且足够长则续接）
    paras = []
    cur = None
    for l in lines:
        if l.kind == 'prose':
            if cur and not _TERMINAL.search(cur['text']) and (len(cur['last']) >= 14 or cur['n'] >= 2) and not _START.match(l.text):
                cur['text'] += l.text
                cur['last'] = l.text
                cur['n'] += 1
            else:
                if cur:
                    paras.append(cur)
                cur = {'line': l.no, 'text': l.text, 'last': l.text, 'off': l.offset, 'n': 1}
        else:
            if cur:
                paras.append(cur)
                cur = None
            if l.kind in ('heading', 'caption', 'source'):
                paras.append({'line': l.no, 'text': l.text, 'last': l.text, 'off': l.offset, 'k': l.kind})
    if cur:
        paras.append(cur)
    sents = []
    for pn, p in enumerate(paras):
        if p.get('k'):
            continue
        for s in re.split(r'(?<=[。！？；])', p['text']):
            s = s.strip()
            if len(s) >= 4:
                sents.append(Sentence(s, p['line'], pn, p['off']))
    doc.sentences = sents
    doc.paragraphs = [(pn, p['line'], p['text']) for pn, p in enumerate(paras)]
    doc.prose = '\n'.join(s.text for s in sents)
    doc.canon = '\n'.join(l.text for l in lines)
    # 抽取质量信号
    total = max(1, len(kept))
    chars = sum(len(l.text) for l in lines) or 1
    cid = len(re.findall(r'\(cid:\d+\)|�|�', raw))
    cjk = len(re.findall(r'[一-鿿]', doc.canon))
    doc.quality = {
        'raw_chars': len(raw), 'clean_chars': chars, 'cjk_chars': cjk,
        'noise_lines_dropped': dropped, 'garbled_marks': cid,
        'cell_line_ratio': round(len(doc.cells) / total, 3),
        'avg_line_len': round(chars / total, 1),
        'cjk_ratio': round(cjk / chars, 3),
        'n_sentences': len(sents),
    }
    return doc
