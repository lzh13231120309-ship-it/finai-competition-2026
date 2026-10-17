"""扫描件表格重建：从 OCR 文本块按坐标还原「标签 + 附注号 + 多列数值」的行结构。

为什么需要这一层（实测得到）：
农夫山泉 2025 年报（230 页扫描件）第 117 页「综合财务状况表」，解析器给出的
表格 bbox 是 x∈[382, 453] —— 只框住了一列数值，48 行全是单列：

    ["24,719,336"] ["1,262,195"] ["80,894"] ...

科目名列整个丢了，于是提取阶段一个事实都认不出来。但同一页的 OCR 文本块其实
完整保留了全部信息：

    y=214.6 x= 70.9  '物業、廠房及設備'     <- 中文标签
    y=215.2 x=178.6  'Property, plant and equipment'  <- 英文标签
    y=215.2 x=356.4  '14'                    <- 附注号
    y=215.1 x=402.8  '24,719,336'            <- 2025 年数值
    y=215.2 x=473.7  '21,083,239'            <- 2024 年数值

所以：只要按 y 聚成视觉行、按 x 聚成列，就能把表还原回来，而且每个单元格
仍然带着原始 OCR 的 evidence_id，证据链不断。

本模块是纯几何重建，不含任何语义猜测；重建结果照常走科目词典与勾稽校验。
"""
from __future__ import annotations

import re
import statistics

DEGENERATE_MULTI_COLUMN_RATIO = 0.3   # 多列行占比低于这个值就判定为「表结构已损坏」
MIN_REBUILT_ROWS = 3


def _bbox(block):
    bb = block.get('bbox') or []
    if len(bb) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in bb)
    except (TypeError, ValueError):
        return None
    if x1 < x0 or y1 < y0:
        return None
    return (x0, y0, x1, y1)


def _yc(bb):
    return (bb[1] + bb[3]) / 2.0


def _xc(bb):
    return (bb[0] + bb[2]) / 2.0


def group_visual_rows(blocks, tol_ratio=0.55):
    """把文本块按纵向重叠聚成视觉行。同一行的中英文标签会落进同一组。"""
    items = []
    for b in blocks:
        text = str(b.get('text') or '').strip()
        bb = _bbox(b)
        if not text or bb is None:
            continue
        items.append((bb, text, b))
    if not items:
        return []
    heights = [bb[3] - bb[1] for bb, _, _ in items if bb[3] > bb[1]]
    median_h = statistics.median(heights) if heights else 10.0
    tol = max(median_h * tol_ratio, 2.0)

    items.sort(key=lambda it: (_yc(it[0]), it[0][0]))
    rows, current = [], []
    current_center = None
    for bb, text, block in items:
        yc = _yc(bb)
        if current and current_center is not None and abs(yc - current_center) <= tol:
            current.append((bb, text, block))
        else:
            if current:
                rows.append(current)
            current = [(bb, text, block)]
        if current:
            current_center = sum(_yc(it[0]) for it in current) / len(current)
    if current:
        rows.append(current)
    return rows


def dedupe_row(row, overlap_ratio=0.5):
    """同一视觉行内，被 OCR 重复检出的同一段文字只保留一个。

    扫描件的 OCR 常把同一段文字检两遍：一个"宽框"（几乎横跨整个数值列）
    加一个"紧框"。实测农夫山泉第 115 页几乎每个表头与数值块都出现两次，
    直接拼进单元格会变成「52,552,910 52,552,910」—— 这种串既不是合法单值，
    被数字防护拒掉之后，**整列 2025 年的数据会凭空消失**，而附註列里的小整数
    反而被当成收入。所以去重必须发生在拼单元格之前。

    判重条件故意收得很紧：文字完全相同，且横向区间显著重叠。
    只在"同一视觉行内"比较，因此不会把不同行里恰好同值的数字合并掉。
    """
    kept = []
    for item in row:
        bb, text, _ = item
        duplicate = False
        for other_bb, other_text, _ in kept:
            if other_text != text:
                continue
            overlap = min(bb[2], other_bb[2]) - max(bb[0], other_bb[0])
            if overlap <= 0:
                continue
            narrower = min(bb[2] - bb[0], other_bb[2] - other_bb[0])
            if narrower > 0 and overlap / narrower >= overlap_ratio:
                duplicate = True
                break
        if not duplicate:
            kept.append(item)
    return kept


def cluster_columns(rows, page_width=None):
    """按 x 中心聚类出列边界。返回 (边界列表, 中心列表)。"""
    centers = sorted(_xc(bb) for row in rows for bb, _, _ in row)
    if not centers:
        return [], []
    span = page_width or max(centers + [1.0])
    gap = max(span * 0.012, 6.0)
    clusters, cur = [], [centers[0]]
    for value in centers[1:]:
        if value - cur[-1] <= gap:
            cur.append(value)
        else:
            clusters.append(cur)
            cur = [value]
    clusters.append(cur)
    cluster_centers = [sum(c) / len(c) for c in clusters]
    boundaries = []
    for i, c in enumerate(cluster_centers):
        low = 0.0 if i == 0 else (cluster_centers[i - 1] + c) / 2.0
        high = float('inf') if i == len(cluster_centers) - 1 else (c + cluster_centers[i + 1]) / 2.0
        boundaries.append((low, high))
    return boundaries, cluster_centers


def build_table_from_blocks(page, table, y_padding=45.0, context=''):
    """把一页的 OCR 块重建为表格（结构对齐 financial_documents 的表格式）。

    仅在原表格结构已损坏时使用，返回值形状与原表格一致，可直接进入下游流程。
    """
    blocks = page.get('blocks') or []
    if not blocks:
        return None
    table_bbox = _bbox({'bbox': table.get('bbox') or []})
    page_height = page.get('height') or 0
    page_width = page.get('width') or 0

    if table_bbox:
        y0 = table_bbox[1] - y_padding
        y1 = table_bbox[3] + y_padding
    else:
        y0, y1 = 0.0, float('inf')

    scoped = []
    for b in blocks:
        bb = _bbox(b)
        if bb is None:
            continue
        yc = _yc(bb)
        if y0 <= yc <= y1:
            scoped.append(b)
    if not scoped:
        scoped = blocks

    rows = group_visual_rows(scoped)
    rows = [dedupe_row(row) for row in rows]
    rows = [row for row in rows if row]
    if len(rows) < MIN_REBUILT_ROWS:
        return None
    boundaries, centers = cluster_columns(rows, page_width or None)
    if len(boundaries) < 2:
        return None

    grid, bboxes, evidence = [], [], []
    for row in rows:
        cells = [''] * len(boundaries)
        cell_boxes = [None] * len(boundaries)
        cell_ev = [None] * len(boundaries)
        for bb, text, block in sorted(row, key=lambda it: it[0][0]):
            xc = _xc(bb)
            index = next((i for i, (low, high) in enumerate(boundaries) if low <= xc < high), None)
            if index is None:
                continue
            # 同一格里有多个块（如中文标签+英文标签）用空格拼接，
            # 下游的中英边界切分逻辑能正确处理
            cells[index] = (cells[index] + ' ' + text).strip() if cells[index] else text
            if cell_boxes[index] is None:
                cell_boxes[index] = list(bb)
            if cell_ev[index] is None and block.get('evidence_id'):
                cell_ev[index] = block.get('evidence_id')
        grid.append(cells)
        bboxes.append(cell_boxes)
        evidence.append(cell_ev)

    # 两遍结构修复，顺序不能反：先合列（列号归位），再合续行（数值补齐）。
    grid, bboxes, evidence = _merge_split_columns(grid, bboxes, evidence, centers, page_width)
    grid, bboxes, evidence = _merge_continuation_rows(grid, bboxes, evidence, context)

    confidences = [float(b.get('confidence') or 0) for b in scoped if b.get('confidence') is not None]    # 用块自身的最低 OCR 置信度，不再额外打折：
    # 几何重建是确定性的，其真实风险是"行列配错"，而那正是勾稽校验要抓的东西，
    # 用一个拍脑袋的系数把整张表压成 needs_review 只会让下游一个指标都算不出来。
    # 低置信 OCR 仍然照常进复核；来源标记记在 rebuilt_from_ocr_blocks 上。
    confidence = min(confidences) if confidences else 0.5
    flat_evidence = [e for row in evidence for e in (row or []) if e]

    return {
        'table_id': str(table.get('table_id') or f"rebuild:p{page.get('page')}") + ':rebuilt',
        'page': page.get('page'),
        'rows': grid,
        'bbox': list(table_bbox) if table_bbox else None,
        'method': 'ocr_row_rebuild',
        'confidence': confidence,
        'evidence_ids': flat_evidence[:200],
        'context': context or str(table.get('context') or ''),
        'context_evidence_ids': table.get('context_evidence_ids') or [],
        'cell_bboxes': bboxes,
        'cell_evidence_ids': evidence,
        'rebuilt_from_ocr_blocks': True,
    }


_NUMERIC_CELL_RE = re.compile(r'^[（(]?\s*[-–—]?\s*[\d,]+(?:\.\d+)?\s*[）)]?$|^[–—\-]$')
_CJK_RE = re.compile(r'[\u3400-\u9fff]')


def _is_numeric_column(grid, index, ratio=0.6):
    """该列是否以数值为主。只合并数值列，避免把标签列（中文/英文折行）也并掉。"""
    values = [str(row[index] or '').strip() for row in grid]
    values = [v for v in values if v]
    if not values:
        return False
    hits = sum(1 for v in values if _NUMERIC_CELL_RE.match(v))
    return hits / len(values) >= ratio


def _merge_split_columns(grid, bboxes, evidence, centers, page_width, max_gap_ratio=0.05):
    """把被 OCR 拆开的同一个数值列合并回去。

    实测成因：OCR 有时把上下两行的数字并成一个"高块"（bbox 高度≈两行），
    它的 x 跨度随之变宽、中心偏移，聚类就把同一个数值列拆成两个相邻列，
    数字也被复制进两行。后果不止"多一列"这么轻 —— 列号整体错位，
    某一行只剩半个数值，而它的标签续行又匹配不上任何科目，
    整条比较期数据静默丢失（实测农夫山泉 2025 年报第 122 页
    「年末現金及現金等價物」的 2024 年值 2,416,380 千元）。

    合并判据故意收得很紧：
      1) 两列中心间距 < 页宽 5%（同表里 2025/2024 两列相距约 70pt，
         被拆开的列只相距 7–25pt）；
      2) 两列都必须是**数值列**（标签列不参与，避免把中英折行并掉）；
      3) **没有任何一行同时给出两个不同的非空值**。
    """
    if not grid or len(centers) < 2:
        return grid, bboxes, evidence
    limit = max(6.0, (page_width or 0.0) * max_gap_ratio)
    # 先定分组：组内用最左一列当代表列。先分组再取值，避免"并进一个
    # 自己也要被删掉的列"——那会把数值直接丢掉（这个 bug 实测踩过）。
    rep = list(range(len(centers)))
    for i in range(1, len(centers)):
        j = rep[i - 1]
        if abs(centers[i] - centers[j]) > limit:
            continue
        if not (_is_numeric_column(grid, i) and _is_numeric_column(grid, j)):
            continue
        if any(str(row[i] or '').strip() and str(row[j] or '').strip()
               and str(row[i]).strip() != str(row[j]).strip() for row in grid):
            continue
        rep[i] = j
    groups: dict[int, list[int]] = {}
    for i, r in enumerate(rep):
        groups.setdefault(r, []).append(i)
    if all(len(members) == 1 for members in groups.values()):
        return grid, bboxes, evidence
    keep = sorted(groups)
    new_grid, new_boxes, new_ev = [], [], []
    for r, row in enumerate(grid):
        cells, boxes, ev = [], [], []
        for root in keep:
            value, box, evid = '', None, None
            for member in groups[root]:
                cell = str(row[member] or '').strip()
                if cell and not value:
                    value = cell
                    box = bboxes[r][member]
                    evid = evidence[r][member]
            cells.append(value)
            boxes.append(box)
            ev.append(evid)
        new_grid.append(cells)
        new_boxes.append(boxes)
        new_ev.append(ev)
    return new_grid, new_boxes, new_ev


def _subject_lookup():
    """惰性取科目词典。

    financial_analysis 是在函数体内反向 import 本模块的（见 extract_financial_facts），
    模块级互相 import 会成环，所以这里也走惰性导入。取不到就返回 None，
    调用方按"保守不合并"处理。
    """
    try:
        from .financial_analysis import _lookup_subject
        return _lookup_subject
    except Exception:
        return None


def _merge_continuation_rows(grid, bboxes, evidence, context='', label_columns=3):
    """把"标签续行"并回上一行。

    多行科目名（中文名折两行、或中英各占一行）在坐标重建里会被拆成两行：
    上一行是标签首行 + 被复制过来的一个数值，下一行是标签续行 + 完整数值。
    续行的标签片段单独匹配不上任何科目，于是那一行的数值被整行丢掉 ——
    静默、无报错。实测第 122 页「年末現金及現金等價物 / AT END OF YEAR」
    的 2024 年值 2,416,380 千元就是这样消失的。

    合并判据（全部满足才合并）：
      1) 本行第 0 列为空；
      2) 本行**自己不带科目名**。判据是"第一个数值格之前的文字格"：
         那段文字就是本行的科目名（或科目名的续行片段）。中文一律算科目名
         （港式年报的科目名一定是中文，真正的续行片段只会是英文，
         如 'AT END OF YEAR'）；英文再问一次科目词典，这样
         'Cash at bank' 这类纯英文科目名也不会被误并。
         实测踩过的两个坑：① 第 115 页「年內溢利 / 母公司擁有人應佔」两行
         都是独立科目；② 第 171 页「其他收益 / Other gains」表头碎片
         与紧随其后的「匯兌收益淨額 75,092」是两回事 —— 只按前 3 列判、
         或只看候选行不看它自己带的科目名，都会把它们并成一条
         "收入 75,092" 的假事实。
      3) 确实补出了上一行为空的数值列，且两行重叠列的取值完全一致。
    """
    if not grid:
        return grid, bboxes, evidence
    lookup = _subject_lookup()
    out_grid, out_boxes, out_ev = [], [], []
    for row, boxes, ev in zip(grid, bboxes, evidence):
        merged = False
        if out_grid and not str(row[0] or '').strip():
            first_numeric = next((c for c, cell in enumerate(row)
                                  if _NUMERIC_CELL_RE.match(str(cell or '').strip())), None)
            head = row[:first_numeric] if first_numeric is not None else row[:label_columns]
            head = [str(c or '').strip() for c in head]
            owns_label = any(_CJK_RE.search(text) for text in head)
            if not owns_label and lookup is not None:
                owns_label = any(text and not _NUMERIC_CELL_RE.match(text) and lookup(text, context)
                                 for text in head)
            if not owns_label:
                prev, prev_boxes, prev_ev = out_grid[-1], out_boxes[-1], out_ev[-1]
                # 只有"补出了上一行为空的**数值**格"才值得合并 ——
                # 纯标签行（没有数值）并过去只会把标签文字挪个位置，
                # 却可能让上一行的科目名被拼成另一个科目，风险大于收益。
                adds_numeric, consistent = False, True
                for c, cell in enumerate(row):
                    val = str(cell or '').strip()
                    if not val:
                        continue
                    existing = str(prev[c] or '').strip()
                    if not existing:
                        if _NUMERIC_CELL_RE.match(val):
                            adds_numeric = True
                    elif existing != val:
                        consistent = False
                        break
                if adds_numeric and consistent:
                    for c, cell in enumerate(row):
                        if str(cell or '').strip() and not str(prev[c] or '').strip():
                            prev[c] = cell
                            if prev_boxes[c] is None:
                                prev_boxes[c] = boxes[c]
                            if prev_ev[c] is None:
                                prev_ev[c] = ev[c]
                    merged = True
        if not merged:
            out_grid.append(list(row))
            out_boxes.append(list(boxes))
            out_ev.append(list(ev))
    return out_grid, out_boxes, out_ev


def is_degenerate(table, ratio=DEGENERATE_MULTI_COLUMN_RATIO) -> bool:
    rows = table.get('rows') or []
    if not rows:
        return True
    multi = sum(1 for r in rows if len(r) >= 2)
    return (multi / len(rows)) < ratio


def upgrade_tables(document, tables):
    """把结构已损坏的表格替换为按 OCR 坐标重建的版本。返回 (表格列表, 说明列表)。"""
    pages = {p.get('page'): p for p in (document.get('pages') or [])}
    out, notes = [], []
    for table in tables or []:
        if not is_degenerate(table):
            out.append(table)
            continue
        page = pages.get(table.get('page'))
        if not page or not page.get('blocks'):
            out.append(table)
            notes.append(f"表 {table.get('table_id')} 只剩单列且无 OCR 文本块可用，未能重建，已保留原状并需人工核对。")
            continue
        rebuilt = build_table_from_blocks(page, table, context=str(table.get('context') or ''))
        if rebuilt and len(rebuilt['rows']) >= MIN_REBUILT_ROWS and len(rebuilt['rows'][0]) >= 2:
            out.append(rebuilt)
            notes.append(f"表 {table.get('table_id')}（第 {table.get('page')} 页）原始解析只剩单列，"
                         f"已按 OCR 坐标重建为 {len(rebuilt['rows'])} 行 × {len(rebuilt['rows'][0])} 列；"
                         f"重建表的行列对应关系由勾稽校验把关，请抽查原文。")
        else:
            out.append(table)
            notes.append(f"表 {table.get('table_id')}（第 {table.get('page')} 页）重建失败，已保留原状。")
    return out, notes
