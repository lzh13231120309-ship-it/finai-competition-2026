"""研报草稿解析：PDF / Word / Markdown / 纯文本 -> 带位置的块（段落、表格行、标题）。

为什么需要它：赛题给的是"外部研报 + 原始财报"，研报格式不受控。核查器要能回答
"错误在第几页、哪一段、哪个表格行"，所以解析层必须保留位置，并把 PDF 里一格一行的
表格还原成"一行一条"，把被硬换行打断的段落还原成完整段落。

对外接口（其它模块请只依赖这几个）：
    parse_report(path_or_text, fmt=None) -> Document
    Document.blocks      : list[Block]   按阅读顺序
    Document.sentences() : list[Sentence]  段落按句切分，每句带 block_id / page / 偏移
    Document.full_text() : str
    Block 字段: id, kind('para'|'row'|'heading'|'caption'), page, text, cells(list[str])
纯标准库 + PyMuPDF(PDF) + python-docx(docx，缺失时退回 zip 解析)。
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

SENT_END = '。！？；!?;'
_NUMLIKE = re.compile(r'^[\s\-–—+−(（]*\d[\d,，.]*\s*(?:%|％|pct|倍|x|X|元|亿|万|百万|亿元|百万元|万元)?[)）]*$|^[-–—−]$|^N/?A$|^n\.?a\.?$', re.I)
_BOILER = re.compile(r'请务必阅读正文之后的免责声明|证券研究报告\s*\|?\s*$|^\d{1,2}$|^敬请参阅|免责声明及其项下所有内容')
_CAPTION = re.compile(r'^(?:图|表|图表|Figure|Table)\s*\d+\s*[:：.、]')
_HEADING = re.compile(r'^(?:[一二三四五六七八九十]+、|\d{1,2}[、.．]\s*[一-鿿]|[（(][一二三四五六七八九十\d]+[）)]|#{1,6}\s)')


@dataclass
class Block:
    id: int
    kind: str          # para | row | heading | caption
    page: int | None
    text: str
    cells: list = field(default_factory=list)
    line_no: int = 0   # 同页内序号，便于定位

    def to_dict(self):
        return {'id': self.id, 'kind': self.kind, 'page': self.page, 'text': self.text,
                'cells': self.cells, 'line_no': self.line_no}


@dataclass
class Sentence:
    block_id: int
    page: int | None
    kind: str
    text: str
    start: int         # 在 block.text 内的偏移
    end: int


class Document:
    def __init__(self, blocks, source='', fmt='text', page_count=0):
        self.blocks = blocks
        self.source = source
        self.fmt = fmt
        self.page_count = page_count

    def full_text(self):
        return '\n'.join(b.text for b in self.blocks)

    def sentences(self):
        out = []
        for b in self.blocks:
            if b.kind == 'row':
                out.append(Sentence(b.id, b.page, b.kind, b.text, 0, len(b.text)))
                continue
            for s, e in split_sentences(b.text):
                out.append(Sentence(b.id, b.page, b.kind, b.text[s:e], s, e))
        return out

    def block(self, bid):
        return self.blocks[bid]


def split_sentences(text):
    """按 。！？； 切句，保留偏移；小数点不会被切（只切中文句末标点）。"""
    spans, start = [], 0
    for i, ch in enumerate(text):
        if ch in '。！？；!?;' and not (ch in '!?;' and i + 1 < len(text) and text[i + 1].isalnum()):
            seg = text[start:i + 1]
            if seg.strip():
                spans.append((start, i + 1))
            start = i + 1
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


def _norm_ws(s):
    return re.sub(r'[ \t\u00a0\u3000]+', ' ', s).strip()


def is_numeric_cell(s):
    return bool(_NUMLIKE.match(s.strip()))


def _cjk_join(a, b):
    """相邻两行拼接时，中文之间不加空格。"""
    return bool(re.match(r'[\u4e00-\u9fff（(“"《，。、；：]', b[:1])) or bool(re.search(r'[\u4e00-\u9fff）)”"》，。、；：]$', a))


# ----------------------------------------------------------------- PDF

def _pdf_rows(page):
    """按内容流顺序取行；同一块内同一水平线上的短行并成一行（表格一行一条）；
    相邻块之间同一水平线上的短行也并成一行（如表头、侧栏"收盘价 | 39.26 元"）。"""
    d = page.get_text('dict')
    units = []
    for blk in d.get('blocks', []):
        if blk.get('type') != 0:
            continue
        lines = []
        for ln in blk.get('lines', []):
            txt = ''.join(sp['text'] for sp in ln['spans'])
            if not txt.strip():
                continue
            size = max((sp['size'] for sp in ln['spans']), default=10)
            x0, y0, x1, y1 = ln['bbox']
            lines.append({'t': txt.strip(), 'x0': x0, 'x1': x1, 'yc': (y0 + y1) / 2, 'h': y1 - y0, 'size': size})
        # 块内按 y 聚类
        groups = []
        for ln in lines:
            for g in groups:
                if abs(g[0]['yc'] - ln['yc']) <= max(2.0, 0.3 * ln['h']) and len(ln['t']) <= 40 and len(g[0]['t']) <= 40:
                    g.append(ln)
                    break
            else:
                groups.append([ln])
        for g in groups:
            g.sort(key=lambda z: z['x0'])
            units.append({'t': ' | '.join(z['t'] for z in g), 'cells': [z['t'] for z in g], 'x0': g[0]['x0'], 'x1': max(z['x1'] for z in g),
                          'yc': g[0]['yc'], 'h': g[0]['h'], 'size': g[0]['size'], 'solo_long': len(g) == 1 and len(g[0]['t']) > 40})
    rows = []
    for u in units:
        if rows:
            p = rows[-1]
            same_y = abs(p['yc'] - u['yc']) <= max(2.5, 0.35 * p['h'])
            if same_y and not p['solo_long'] and not u['solo_long'] and u['x0'] >= p['x1'] - 1:
                p['cells'] += u['cells']
                p['t'] = p['t'] + ' | ' + u['t']
                p['x1'] = u['x1']
                continue
        rows.append(u)
    # 并排两张表（如资产负债表|利润表）：数字单元格之后又出现文字标签，则在此处拆行
    out = []
    for r in rows:
        cells = r['cells']
        if len(cells) >= 4:
            cut = [0]
            nnum = 0
            for i, c in enumerate(cells):
                if is_numeric_cell(c):
                    nnum += 1
                elif nnum >= 2 and i > 0 and not _is_unit_like(c):
                    cut.append(i)
                    nnum = 0
            if len(cut) > 1:
                cut.append(len(cells))
                for a_, b_ in zip(cut, cut[1:]):
                    sub = dict(r)
                    sub['cells'] = cells[a_:b_]
                    sub['t'] = ' | '.join(sub['cells'])
                    out.append(sub)
                continue
        out.append(r)
    return out


def _is_unit_like(c):
    return bool(re.match(r'^[\d.,%\s]+$', c)) or bool(re.match(r'^(?:\d{4}[A-Ea-e]?|[1-4]Q\d{2}|\d{2}Q[1-4])$', c))


def parse_pdf(path):
    import pymupdf
    doc = pymupdf.open(str(path))
    blocks, bid = [], 0
    for pno, page in enumerate(doc, start=1):
        rows = _pdf_rows(page)
        if not rows:
            continue
        from collections import Counter
        cnt = Counter(round(r['x1'] / 3) * 3 for r in rows if len(r['t']) >= 25 and len(r['cells']) == 1)
        right = cnt.most_common(1)[0][0] if cnt else 0
        state = {'para': None, 'ln': 0}

        def flush():
            nonlocal bid
            p = state['para']
            if p is not None:
                state['ln'] += 1
                blocks.append(Block(bid, p['kind'], pno, p['text'], [], state['ln']))
                bid += 1
                state['para'] = None

        szc = Counter()
        for r_ in rows:
            szc[round(r_['size'])] += len(r_['t'])
        body_size = szc.most_common(1)[0][0] if szc else 10

        def kind_of(t, size):
            return 'heading' if ((len(t) < 40 and t[-1:] not in SENT_END and _HEADING.match(t)) or (size >= 1.25 * body_size and len(t) < 50)) else 'para'

        for r in rows:
            t = _norm_ws(r['t'])
            if _BOILER.search(t) and len(t) < 40:
                continue
            ncell = len(r['cells'])
            if ncell >= 2 or is_numeric_cell(t):
                flush()
                state['ln'] += 1
                cells = [_norm_ws(c) for c in r['cells']]
                blocks.append(Block(bid, 'row', pno, ' | '.join(cells), cells, state['ln']))
                bid += 1
                continue
            if _CAPTION.match(t):
                flush()
                state['ln'] += 1
                blocks.append(Block(bid, 'caption', pno, t, [], state['ln']))
                bid += 1
                continue
            p = state['para']
            if p is None:
                state['para'] = {'kind': kind_of(t, r['size']), 'text': t, 'x1': r['x1'], 'x0': r['x0']}
                continue
            if (p['x1'] >= right - 30 or (len(p['text']) >= 25 and p['text'][-1:] not in '。！？；:：')) and abs(p['x0'] - r['x0']) <= 14 and p['kind'] == 'para' and not _HEADING.match(t) and r['size'] < 1.25 * body_size:
                p['text'] += t if _cjk_join(p['text'], t) else ' ' + t
                p['x1'] = r['x1']
                continue
            flush()
            state['para'] = {'kind': kind_of(t, r['size']), 'text': t, 'x1': r['x1'], 'x0': r['x0']}
        flush()
    return Document(blocks, str(path), 'pdf', len(doc))


# ----------------------------------------------------------------- Markdown / 文本

_PAGE_MARK = re.compile(r'^\s*(?:<!--\s*page\s*(\d+)\s*-->|\[\[?\s*第?\s*(\d+)\s*页\s*\]?\]|-{3,}\s*page\s*(\d+)\s*-{3,}|<page\s+(\d+)>)\s*$', re.I)


def parse_markdown(text, source='', fmt='markdown'):
    blocks, st = [], {'bid': 0, 'page': None, 'ln': 0, 'buf': []}

    def add(kind, t, cells=None):
        st['ln'] += 1
        blocks.append(Block(st['bid'], kind, st['page'], t, cells or [], st['ln']))
        st['bid'] += 1

    def flush():
        buf = st['buf']
        if buf:
            t = buf[0]
            for nxt in buf[1:]:
                t += nxt if _cjk_join(t, nxt) else ' ' + nxt
            t = _norm_ws(t)
            kind = 'caption' if _CAPTION.match(t) else ('heading' if _HEADING.match(t) and len(t) < 50 and t[-1:] not in SENT_END else 'para')
            add(kind, re.sub(r'^#{1,6}\s*', '', t))
            st['buf'] = []

    for raw in text.splitlines():
        line = raw.rstrip()
        m = _PAGE_MARK.match(line)
        if m:
            flush()
            st['page'] = int(next(g for g in m.groups() if g))
            st['ln'] = 0
            continue
        if not line.strip():
            flush()
            continue
        if re.match(r'^\s*\|?[\s:\-|]+\|?\s*$', line) and '-' in line:
            continue
        if line.lstrip().startswith('|') or line.count('|') >= 2:
            flush()
            cells = [_norm_ws(c) for c in line.strip().strip('|').split('|')]
            add('row', ' | '.join(cells), cells)
            continue
        if line.lstrip().startswith('#'):
            flush()
            add('heading', re.sub(r'^#{1,6}\s*', '', line.strip()))
            continue
        st['buf'].append(line.strip())
    flush()
    return Document(blocks, source, fmt, max([(b.page or 0) for b in blocks] or [0]))


# ----------------------------------------------------------------- docx

def parse_docx(path):
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except Exception:  # pragma: no cover
        return _parse_docx_zip(path)
    d = docx.Document(str(path))
    blocks, bid, ln_no = [], 0, 0
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit('}', 1)[-1]
        if tag == 'p':
            p = Paragraph(child, d)
            t = _norm_ws(p.text)
            if not t:
                continue
            style = (p.style.name or '').lower() if p.style is not None else ''
            kind = 'heading' if style.startswith('heading') or style.startswith('title') else ('caption' if _CAPTION.match(t) else 'para')
            ln_no += 1
            blocks.append(Block(bid, kind, None, t, [], ln_no))
            bid += 1
        elif tag == 'tbl':
            for row in Table(child, d).rows:
                cells, seen = [], set()
                for c in row.cells:
                    if id(c._tc) in seen:
                        continue
                    seen.add(id(c._tc))
                    cells.append(_norm_ws(c.text))
                if any(cells):
                    ln_no += 1
                    blocks.append(Block(bid, 'row', None, ' | '.join(cells), cells, ln_no))
                    bid += 1
    return Document(blocks, str(path), 'docx', 0)


def _parse_docx_zip(path):  # pragma: no cover
    with zipfile.ZipFile(path) as zf:
        xml = zf.read('word/document.xml').decode('utf-8', 'replace')
    xml = re.sub(r'</w:p>', '\n', xml)
    xml = re.sub(r'</w:tc>', ' | ', xml)
    xml = re.sub(r'<[^>]+>', '', xml)
    return parse_markdown(xml, str(path), 'docx')


# ----------------------------------------------------------------- 入口

def parse_report(src, fmt=None):
    """src 可以是路径（pdf/docx/md/txt）或直接是文本。"""
    if isinstance(src, Path) or (isinstance(src, str) and len(src) < 500 and '\n' not in src and Path(src).expanduser().is_file()):
        p = Path(src).expanduser()
        suf = (fmt or p.suffix.lstrip('.')).lower()
        if suf == 'pdf':
            return parse_pdf(p)
        if suf == 'docx':
            return parse_docx(p)
        return parse_markdown(p.read_text(encoding='utf-8', errors='replace'), str(p), 'markdown' if suf == 'md' else 'text')
    return parse_markdown(str(src), '', 'text')
