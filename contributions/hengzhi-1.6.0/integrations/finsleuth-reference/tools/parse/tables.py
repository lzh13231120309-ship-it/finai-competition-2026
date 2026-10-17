"""表格抽取：一个页面多种策略各跑一遍，按置信度择优，并记录证据位置。

置信度不是装饰，它是后面 validation_gate 的开关：
低于阈值的表格不许进入分析层，只能进人工复核队列。
"""
from __future__ import annotations

import re
from pathlib import Path

import pdfplumber

from domain.models import RawTable
from tools.normalize.units import parse_unit_scale, parse_cell_value
from tools.parse.router import route_pdf

STRATEGIES: list[tuple[str, dict | None]] = [
    ("lines", None),  # 有框线：直接用线框切格
    ("text", {"vertical_strategy": "text", "horizontal_strategy": "text"}),  # 无框线：靠文字对齐
]

_HEADER_HINT = re.compile(r"项目|期末|期初|余额|合计|单位|年度|月")


def _clean(cell) -> str:
    if cell is None:
        return ""
    return re.sub(r"\s+", "", str(cell))


def table_confidence(cells: list[list[str]]) -> float:
    """数值填充率 50% + 表头命中 30% + 行数规模 20%。"""
    if len(cells) < 2 or not cells[0] or len(cells[0]) < 2:
        return 0.2
    flat = [c for row in cells for c in row]
    non_empty = [c for c in flat if c]
    if not non_empty:
        return 0.1
    numeric = 0
    for c in non_empty:
        value, _, note = parse_cell_value(c)
        if value is not None and note not in {"no_number"}:
            numeric += 1
    numeric_ratio = numeric / len(non_empty)
    header_hit = 1.0 if any(_HEADER_HINT.search(c) for c in cells[0]) else 0.0
    size_score = min(1.0, len(cells) / 8.0)
    return round(0.5 * numeric_ratio + 0.3 * header_hit + 0.2 * size_score, 3)


def _unit_declaration_above(page, table_bbox) -> str:
    """真实年报把 "单位：元" 写在表格上方，不在单元格里，所以要去表外找。"""
    top = table_bbox[1]
    words = page.extract_words() or []
    line_words = [w for w in words if top - 45 <= w["bottom"] <= top + 2]
    text = "".join(w["text"] for w in sorted(line_words, key=lambda w: w["x0"]))
    return text if parse_unit_scale(text) else ""


def extract_tables(
    path: str | Path,
    doc_id: str | None = None,
    pages: list[int] | None = None,
) -> tuple[list[RawTable], object]:
    path = Path(path)
    plan = route_pdf(path, doc_id)
    doc_id = plan.doc_id
    targets = pages if pages is not None else plan.text_pages
    out: list[RawTable] = []

    with pdfplumber.open(str(path)) as pdf:
        for pno in targets:
            page = pdf.pages[pno - 1]
            best: tuple[str, float, list] | None = None
            for name, settings in STRATEGIES:
                try:
                    found = page.find_tables(settings) if settings else page.find_tables()
                except Exception:
                    continue
                for t in found:
                    cells = [[_clean(c) for c in row] for row in t.extract()]
                    conf = table_confidence(cells)
                    if best is None or conf > best[1]:
                        best = (name, conf, [(t, cells)])
                    elif conf == best[1]:
                        best[2].append((t, cells))
                if best and best[1] >= 0.75:
                    break
            if not best:
                continue
            for idx, (t, cells) in enumerate(best[2], start=1):
                bbox = tuple(float(v) for v in t.bbox)
                out.append(
                    RawTable(
                        table_id=f"{doc_id}:p{pno}:t{idx}",
                        doc_id=doc_id,
                        page=pno,
                        strategy=best[0],
                        confidence=best[1],
                        cells=cells,
                        unit_header_raw=_unit_declaration_above(page, t.bbox) or None,
                        bbox=bbox,
                    )
                )
    return out, plan
