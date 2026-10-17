"""公告文档的版面结构化：PDF/文本 -> 「正文段落 + 还原后的表格」。

为什么单独成一层：A 股公告里最难的不是"读懂"，而是 PDF 的文字层是**按单元格断行**的——
一个 `28,598,383` 会被拆成 `28,59\\n8,383`，一张 11 列的质押表在纯文本里变成 100 多行碎片。
抽取引擎要的是"行 = 一笔事件、列 = 字段"的二维表，所以先做版面还原：

1. 用 PyMuPDF 的表格检测拿到每页的表格与单元格（保留页码与 bbox，用于证据定位）。
2. 单元格内换行去掉（数字/日期/汉字直接拼接），None（合并单元格）记为空。
3. 识别多级表头：表头行 = 表体首个含数值行之前的所有行；上层分组标题向右铺开，
   列名 = 「分组/子列」。
4. 跨页续表：下一页顶端没有表头、列数一致的表格并入上一张；表头重复的也并入。
5. 表格所在区域从正文里剔除，避免表格碎片污染正文正则。

文本/Markdown 输入走同一数据结构（管道表格），保证 T1–T8 这类文本题与真实 PDF 同一条路径。
所有位置信息：page(1 起) + 表内行列号，可直接作为证据。
"""
from __future__ import annotations

import re
from pathlib import Path

_NUMERIC_CELL = re.compile(
    r'^[（(]?[+\-−－]?[¥￥]?\d[\d,，]*(?:[.．]\d+)?\s*(?:%|％|股|元|万元|万股|亿元|亿股)?[）)]?$')
_DATE_CELL = re.compile(r'^(?:19|20)\d{2}\s*[年\-/.]\s*\d{1,2}\s*[月\-/.]\s*\d{1,2}\s*日?$')
_NULL_CELL = {'—', '–', '-', '－', '―', '/', '不适用', 'N/A', 'NA'}


def clean_cell(value) -> str:
    """单元格归一：去换行、去 CJK/数字之间的空白；拉丁字符之间保留一个空格。"""
    if value is None:
        return ''
    s = str(value).replace('\u3000', ' ').replace('\xa0', ' ')
    s = re.sub(r'[\r\n]+', '\n', s).strip()
    parts = s.split('\n')
    out = parts[0]
    for part in parts[1:]:
        if out and part and re.match(r'[A-Za-z]', out[-1]) and re.match(r'[A-Za-z]', part[0]):
            out += ' ' + part
        else:
            out += part
    out = re.sub(r'(?<=[\u4e00-\u9fff（）()%％\d,.，：:/\-])\s+(?=[\u4e00-\u9fff（）()%％\d,.，：:/\-])', '', out)
    return out.strip()


def is_data_cell(cell: str) -> bool:
    c = cell.strip()
    if not c:
        return False
    return bool(_NUMERIC_CELL.match(c) or _DATE_CELL.match(c))


def is_null_cell(cell: str) -> bool:
    return cell.strip() in _NULL_CELL


def _is_data_row(cells: list[str]) -> bool:
    nonempty = [c for c in cells if c.strip()]
    if not nonempty:
        return False
    numeric = sum(1 for c in nonempty if is_data_cell(c))
    return numeric >= 1 and numeric >= max(1, len(nonempty) // 4)


def _norm_header(cells: list[str]) -> str:
    return '|'.join(re.sub(r'\s+', '', c) for c in cells)


class AnnTable:
    """还原后的表格。rows 为表体行（不含表头），labels 为每列的多级列名。"""

    def __init__(self, table_id: str, page: int, header_rows: list[list[str]],
                 rows: list[list[str]], bbox=None):
        self.table_id = table_id
        self.page = page
        self.pages = [page]
        self.bbox = bbox
        self.header_rows = header_rows
        self.rows = rows
        self.row_pages = [page] * len(rows)
        self.labels = self._labels(header_rows)
        self.all_rows = list(header_rows) + list(rows)     # 原始行序（键值表/转置表识别用）
        self.continued = False
        self.inherited_header = False

    @staticmethod
    def _labels(header_rows: list[list[str]]) -> list[str]:
        if not header_rows:
            return []
        n = max(len(r) for r in header_rows)
        grid = [list(r) + [''] * (n - len(r)) for r in header_rows]
        # 上层分组标题向右铺开：仅当其下一行对应位置有内容（说明它是分组而非纵向合并）
        for r in range(len(grid) - 1):
            last = ''
            for c in range(n):
                if grid[r][c]:
                    last = grid[r][c]
                elif last and grid[r + 1][c]:
                    grid[r][c] = last
                else:
                    last = ''
        labels = []
        for c in range(n):
            parts = []
            for r in range(len(grid)):
                v = re.sub(r'\s+', '', grid[r][c])
                if v and v not in parts:
                    parts.append(v)
            labels.append('/'.join(parts))
        return labels

    @property
    def ncols(self) -> int:
        return max([len(self.labels)] + [len(r) for r in self.rows])

    def to_dict(self) -> dict:
        return {'table_id': self.table_id, 'page': self.page, 'pages': self.pages,
                'labels': self.labels, 'rows': self.rows, 'row_pages': self.row_pages,
                'continued': self.continued}


class AnnDoc:
    def __init__(self, filename: str = '', source_kind: str = 'text'):
        self.filename = filename
        self.source_kind = source_kind        # pdf_text | text | ocr | pdf_scanned
        self.pages: list[dict] = []           # {'page', 'text'(正文，已剔除表格), 'raw'}
        self.tables: list[AnnTable] = []
        self.warnings: list[str] = []
        self.ocr_info: dict | None = None

    @property
    def text(self) -> str:
        return '\n'.join(p['text'] for p in self.pages)

    def page_at(self, pos: int) -> int | None:
        cursor = 0
        for p in self.pages:
            seg = len(p['text'])
            if cursor <= pos <= cursor + seg:
                return p['page']
            cursor += seg + 1
        return None

    def render(self) -> str:
        out = []
        for p in self.pages:
            out.append(f'--- 第 {p["page"]} 页 ---')
            out.append(p['text'])
        for t in self.tables:
            out.append(f'=== 表 {t.table_id}（第{"/".join(map(str, t.pages))}页，续表={t.continued}）===')
            out.append('列: ' + ' | '.join(t.labels))
            for r in t.rows:
                out.append('| ' + ' | '.join(r) + ' |')
        return '\n'.join(out)


def _split_header(raw_rows: list[list[str]]) -> tuple[list[list[str]], list[list[str]]]:
    first_data = None
    for i, row in enumerate(raw_rows):
        if _is_data_row(row):
            first_data = i
            break
    if first_data is None:
        # 无数值行：全文字表（首行当表头），或“只有多级表头、数据在下一页”的表
        n = 1
        while n < len(raw_rows) and not raw_rows[n][0].strip() and \
                any(raw_rows[n][c].strip() and not raw_rows[n - 1][c].strip() for c in range(len(raw_rows[n]))):
            n += 1
        return raw_rows[:n], raw_rows[n:]
    return raw_rows[:first_data], raw_rows[first_data:]


def _build_tables(doc: AnnDoc, page_tables: list[dict]) -> None:
    built: list[AnnTable] = []
    for pt in page_tables:
        rows = [[clean_cell(c) for c in r] for r in pt['rows']]
        rows = [r for r in rows if any(c for c in r)]
        if not rows:
            continue
        header, body = _split_header(rows)
        prev = built[-1] if built else None
        merge = False
        if prev is not None and pt['page'] - prev.pages[-1] == 1 and not prev.rows and prev.header_rows \
                and len(rows[0]) > prev.ncols and not rows[0][0].strip() and len(rows[0]) - prev.ncols <= prev.ncols:
            # 表头跨页且列数不同：上一页末尾的分组表头（如“已质押股份情况”）在下一页被拆成两个子列
            extra = len(rows[0]) - prev.ncols
            expanded = []
            for h in prev.header_rows:
                row = list(h) + [''] * (prev.ncols - len(h))
                head, tail = row[:prev.ncols - extra], row[prev.ncols - extra:]
                row = head + [c for cell in tail for c in (cell, '')]
                expanded.append(row)
            prev.header_rows = expanded
            prev.pages.append(pt['page'])
            header_extra = header
            prev.header_rows = prev.header_rows + header_extra
            prev.labels = prev._labels(prev.header_rows)
            prev.all_rows = prev.all_rows + header + body
            prev.rows = list(body)
            prev.row_pages = [pt['page']] * len(body)
            prev.continued = True
            continue
        if prev is not None and pt['page'] - prev.pages[-1] == 1 and len(rows[0]) == prev.ncols \
                and not prev.rows and prev.header_rows:
            # 上一页只剩表头、数据在下一页：表头碎片（如“（股）”）并入表头，表体接上
            prev.header_rows = prev.header_rows + header
            prev.labels = prev._labels(prev.header_rows)
            prev.all_rows = prev.all_rows + header + body
            prev.rows.extend(body)
            prev.row_pages.extend([pt['page']] * len(body))
            prev.pages.append(pt['page'])
            prev.continued = True
            continue
        if prev is not None and pt['page'] - prev.pages[-1] == 1 and len(rows[0]) == prev.ncols:
            if not header:
                merge = True                      # 无表头的跨页续表
            elif prev.header_rows and _norm_header(header[-1]) == _norm_header(prev.header_rows[-1]):
                merge = True                      # 表头重复的续表
        if merge:
            prev.all_rows = prev.all_rows + header + body
            prev.rows.extend(body)
            prev.row_pages.extend([pt['page']] * len(body))
            if pt['page'] not in prev.pages:
                prev.pages.append(pt['page'])
            prev.continued = True
            continue
        built.append(AnnTable(f'p{pt["page"]}t{sum(1 for t in built if t.page == pt["page"]) + 1}',
                              pt['page'], header, body, bbox=pt.get('bbox')))
    # 无表头且不是紧邻续表：沿用最近一张同列数表的表头（标记 inherited）
    for i, t in enumerate(built):
        if not t.header_rows:
            for j in range(i - 1, -1, -1):
                if built[j].labels and built[j].ncols == t.ncols:
                    t.labels = list(built[j].labels)
                    t.inherited_header = True
                    break
    doc.tables = built


def _join_lines(lines: list[str]) -> str:
    out = ''
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if out and re.match(r'[A-Za-z0-9]', out[-1]) and re.match(r'[A-Za-z]', ln[0]):
            out += ' ' + ln
        else:
            out += ln
    return out



def _table_sane(rows: list[list]) -> bool:
    """版面检测质量判据：整页边框被误当成一个大表时，单元格里会塞进整段正文。"""
    cells = [clean_cell(c) for r in rows for c in r]
    nonempty = [c for c in cells if c]
    if not nonempty:
        return False
    too_long = sum(1 for c in nonempty if len(c) > 90)
    empty_ratio = 1 - len(nonempty) / len(cells)
    return too_long == 0 and empty_ratio <= 0.5


def _best_tables(page, doc: 'AnnDoc', page_no: int) -> list:
    """同一页用多种线检测策略各跑一遍，取「合格表格最多且无不合格表格」的那个。

    实测：Word 转 PDF 的页面常把整页文本框当成一个大表（lines 策略），
    此时 lines_strict（只认真正的线段）才能切出正确的 11 列质押表。
    """
    best, best_key = [], (-1, 0)
    for strategy in ('lines', 'lines_strict'):
        try:
            tabs = [t for t in page.find_tables(strategy=strategy).tables
                    if t.row_count >= 1 and t.col_count >= 2]
        except Exception as err:
            doc.warnings.append(f'第{page_no}页表格检测({strategy})失败：{type(err).__name__}')
            continue
        sane = [t for t in tabs if _table_sane(t.extract())]
        key = (len(sane) - 2 * (len(tabs) - len(sane)), len(sane))
        if key > best_key:
            best, best_key = sane, key
    return best


def _split_notes(rows: list[list]) -> tuple[list[list], list[str]]:
    """表内只有一个长文本单元格的行是脚注/说明（如“注1：…”），移出表体回到正文。"""
    kept, notes = [], []
    for r in rows:
        ne = [clean_cell(c) for c in r if clean_cell(c)]
        if len(ne) == 1 and len(ne[0]) >= 14 and not is_data_cell(ne[0]):
            notes.append(ne[0])
        else:
            kept.append(r)
    return kept, notes


_HEADING = re.compile(r'^(?:[一二三四五六七八九十]+[、.．]|[（(][一二三四五六七八九十\d]+[）)]|\d{1,2}[、.．)）]|[●■□◆•·]|第[一二三四五六七八九十]+[节章条]|重要内容提示|特此公告)')


def _paragraphs(lines: list) -> list[str]:
    """把按行切碎的文字块还原成段落。

    判据（几何 + 标点）：一行“写满”（右端接近本页最右）说明它在段内换行，应与下一行拼接；
    行尾是句末标点、下一行以序号/项目符号开头、或本行没写满，则在此处分段。
    实测这类 PDF（Word 转出）每个视觉行是一个独立文字块，不做这一步，
    “项目中标/金额为…”这类跨行短语会被换行截断，正则全部失灵。
    """
    if not lines:
        return []
    widths = sorted(ln[2] for ln in lines if len(ln[4]) >= 15)
    right = widths[int(0.9 * (len(widths) - 1))] if widths else max(ln[2] for ln in lines)
    left = min(ln[0] for ln in lines)
    span = max(1.0, right - left)
    paras, cur, prev = [], '', None
    for x0, y0, x1, y1, txt in lines:
        if cur:
            long_line = (prev[2] - left) >= 0.72 * span
            ends = prev[4][-1] in '。；;！？!?：:'
            new_para = (not long_line) or ends or bool(_HEADING.match(txt))
            if (y0 - prev[3]) > 1.2 * (prev[3] - prev[1]):      # 大段间距也分段
                new_para = True
            if new_para:
                paras.append(cur)
                cur = txt
            else:
                if re.match(r'[A-Za-z0-9]', cur[-1]) and re.match(r'[A-Za-z]', txt[0]):
                    cur += ' ' + txt
                else:
                    cur += txt
        else:
            cur = txt
        prev = (x0, y0, x1, y1, txt)
    if cur:
        paras.append(cur)
    return paras


def load_pdf(path: str | Path, max_pages: int = 400) -> AnnDoc:
    import pymupdf
    path = Path(path)
    doc = AnnDoc(path.name, 'pdf_text')
    page_tables: list[dict] = []
    with pymupdf.open(path) as pdf:
        n = len(pdf)
        if n > max_pages:
            doc.warnings.append(f'PDF 共 {n} 页，仅解析前 {max_pages} 页')
        for index in range(min(n, max_pages)):
            page = pdf[index]
            tabs = _best_tables(page, doc, index + 1)
            rects = [pymupdf.Rect(t.bbox) for t in tabs]
            lines = []
            for blk in page.get_text('dict', sort=True)['blocks']:
                if blk.get('type') != 0:
                    continue
                for line in blk['lines']:
                    x0, y0, x1, y1 = line['bbox']
                    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                    if any(r.x0 - 1 <= cx <= r.x1 + 1 and r.y0 - 1 <= cy <= r.y1 + 1 for r in rects):
                        continue          # 落在表格内的行不进正文
                    txt = ''.join(sp['text'] for sp in line['spans']).strip()
                    if txt:
                        lines.append((x0, y0, x1, y1, txt))
            paras = _paragraphs(lines)
            raw = page.get_text('text', sort=True)
            doc.pages.append({'page': index + 1, 'text': '\n'.join(paras), 'raw': raw})
            notes = []
            for t in tabs:
                rows, note_rows = _split_notes(t.extract())
                notes.extend(note_rows)
                page_tables.append({'page': index + 1, 'bbox': [round(v, 1) for v in t.bbox],
                                    'rows': rows})
            if notes:
                doc.pages[-1]['text'] += '\n' + '\n'.join(notes)
    _build_tables(doc, page_tables)
    chars = sum(len(re.sub(r'\s', '', p['raw'])) for p in doc.pages)
    if chars < 60 * max(1, len(doc.pages)) * 0.25:
        doc.warnings.append('文字层几乎为空：疑似扫描件，需要 OCR')
        doc.source_kind = 'pdf_scanned'
    return doc


def load_text(text: str, filename: str = '') -> AnnDoc:
    doc = AnnDoc(filename, 'text')
    lines = str(text).splitlines()
    body, page_tables = [], []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith('|'):
            rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                s = lines[i].strip().replace('｜', '|')
                i += 1
                if re.fullmatch(r'\|[\s:\-|]+', s):
                    continue
                rows.append([c.strip() for c in s.strip('|').split('|')])
            if rows:
                page_tables.append({'page': 1, 'bbox': None, 'rows': rows})
            continue
        body.append(lines[i])
        i += 1
    doc.pages.append({'page': 1, 'text': '\n'.join(body), 'raw': text})
    _build_tables(doc, page_tables)
    return doc


def load_parsed_document(parsed: dict, filename: str = '', kind: str = 'ocr') -> AnnDoc:
    """从 financial_documents.parse_document 的结果（含 OCR 路径）转成 AnnDoc。"""
    doc = AnnDoc(filename or parsed.get('filename', ''), kind)
    page_tables = []
    by_page: dict[int, list] = {}
    for t in parsed.get('tables') or []:
        by_page.setdefault(t['page'], []).append(t)
    for p in parsed.get('pages') or []:
        text = '\n'.join(ln.strip() for ln in str(p.get('text') or '').splitlines() if ln.strip())
        doc.pages.append({'page': p['page'], 'text': text, 'raw': p.get('text') or ''})
        for t in by_page.get(p['page'], []):
            page_tables.append({'page': p['page'], 'bbox': t.get('bbox'), 'rows': t.get('rows')})
    _build_tables(doc, page_tables)
    doc.warnings = list(parsed.get('warnings') or [])
    return doc


def load_any(path: str | Path) -> AnnDoc:
    path = Path(path)
    ext = path.suffix.lower()
    if ext == '.pdf':
        doc = load_pdf(path)
        if doc.source_kind == 'pdf_scanned':
            from . import ocr_fallback
            return ocr_fallback.ocr_pdf_to_anndoc(path, doc)
        return doc
    if ext in {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp'}:
        from . import ocr_fallback
        return ocr_fallback.ocr_image_to_anndoc(path)
    data = path.read_bytes()
    for enc in ('utf-8-sig', 'gb18030'):
        try:
            return load_text(data.decode(enc), path.name)
        except UnicodeError:
            continue
    raise ValueError('无法确定文本编码，请另存为 UTF-8')
