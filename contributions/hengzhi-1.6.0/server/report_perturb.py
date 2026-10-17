"""保义扰动：同一份研报在"格式层面"的各种变化，用于稳健性实验（评分不应因此改变）。

每个扰动函数都是纯函数 text -> text，且不改变任何实质内容（不增删数字、观点、章节）。
PDF/docx/md 三种来源的典型差异被拆成 8 种独立扰动，实验时既可单独施加也可组合。
"""
from __future__ import annotations

import random
import re
import unicodedata

from .report_text import S2T

HEADER = '{org}证券研究报告 | 公司深度研究 | 请务必阅读正文之后的免责条款和声明'


def p_header_footer(text: str, every: int = 38) -> str:
    """每隔若干行插入页眉、页码、页脚（PDF 逐页抽取的典型噪声）。"""
    lines = text.split('\n')
    out, page = [], 1
    for i, ln in enumerate(lines, 1):
        out.append(ln)
        if i % every == 0:
            out += [f'- {page} -', HEADER.format(org='某某'), f'敬请参阅最后一页特别声明', f'第 {page} 页 / 共 {max(2, len(lines)//every)} 页']
            page += 1
    return '\n'.join(out)


def p_whitespace(text: str, seed: int = 7) -> str:
    """多余空格、制表符、行尾空白、连续空行；汉字间插入 PDF 常见的单空格。"""
    rnd = random.Random(seed)
    out = []
    for ln in text.split('\n'):
        if rnd.random() < 0.25:
            ln = re.sub(r'(?<=[一-鿿])(?=[一-鿿])', lambda m: ' ' if rnd.random() < 0.04 else '', ln)
        if rnd.random() < 0.2:
            ln = '\t' + ln
        out.append(ln + ('   ' if rnd.random() < 0.3 else ''))
        if rnd.random() < 0.1:
            out.append('')
    return '\n'.join(out)


def p_hardwrap(text: str, width: int = 34) -> str:
    """把长行按固定宽度硬折行（PDF/邮件式折行），短行（标题、表格格）不动。"""
    out = []
    for ln in text.split('\n'):
        if len(ln) > width + 6:
            out += [ln[i:i + width] for i in range(0, len(ln), width)]
        else:
            out.append(ln)
    return '\n'.join(out)


def p_fullwidth(text: str) -> str:
    """ASCII 数字/字母/常用标点 → 全角（Word 中文输入法常见）。"""
    res = []
    for ch in text:
        if ch == ' ':
            res.append(ch)
        elif '!' <= ch <= '~' and ch not in '\n':
            res.append(chr(ord(ch) + 0xFEE0))
        else:
            res.append(ch)
    return ''.join(res)


def p_traditional(text: str) -> str:
    return ''.join(S2T.get(c, c) for c in text)


def p_markdown(text: str) -> str:
    """Markdown/docx 风格：标题加 #，列表加 -，粗体，表格数字格加竖线。"""
    out = []
    for ln in text.split('\n'):
        t = ln.strip()
        if t and len(t) <= 16 and not re.search(r'[。，；,;]', t) and re.search(r'要点|观点|分析|预测|估值|风险|提示|声明|建议|催化|逻辑', t) and not re.match(r'^[\d.,%\-]+$', t):
            out.append('## ' + t)
        elif re.match(r'^[-+]?[\d,]+(?:\.\d+)?%?$', t):
            out.append('| ' + t + ' |')
        elif len(t) > 30 and re.search(r'[。；]$', t) and hash(t) % 5 == 0:
            out.append('- **' + t[:6] + '**' + t[6:])
        else:
            out.append(ln)
    return '\n\n'.join(out) if False else '\n'.join(out)


def p_crlf_zero_width(text: str) -> str:
    """CRLF、BOM、零宽字符、不间断空格、换页符。"""
    t = text.replace(' ', ' ', 1) if False else text
    t = re.sub(r'(?<=[一-鿿])(?=[一-鿿])', lambda m: '​' if (m.start() % 97 == 0) else '', t)
    t = t.replace('，', '，‌') if False else t
    return '﻿' + t.replace('\n', '\r\n').replace('\r\n\r\n\r\n', '\r\n\f\r\n')


def p_number_spacing(text: str) -> str:
    """数字与单位之间加/去空格（"12.3亿元" ↔ "12.3 亿元"）。"""
    return re.sub(r'(\d)(亿元|万元|百万元|%|倍|元)', r'\1 \2', text)


PERTURBATIONS = {
    'header_footer': p_header_footer,
    'whitespace': p_whitespace,
    'hardwrap': p_hardwrap,
    'fullwidth': p_fullwidth,
    'traditional': p_traditional,
    'markdown': p_markdown,
    'crlf_zerowidth': p_crlf_zero_width,
    'number_spacing': p_number_spacing,
}


def combo(text: str, names: list) -> str:
    for n in names:
        text = PERTURBATIONS[n](text)
    return text
