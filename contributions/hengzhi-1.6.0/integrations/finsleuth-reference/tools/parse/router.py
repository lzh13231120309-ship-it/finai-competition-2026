"""解析器路由：先判断这份 PDF 该走哪条解析路径，再决定用哪个工具。

判定只看两个客观信号：文字层密度、页面图像覆盖率。
决策必须落进 RoutePlan，因为它决定了后面所有数字的来源（可追溯）。
"""
from __future__ import annotations

import re
from pathlib import Path

from domain.models import RoutePlan

MIN_CHARS_PER_PAGE = 30      # 一页有效文字少于 30 字 -> 视为扫描页
SCANNED_RATIO_TRIGGER = 0.30  # 扫描页占比超过 30% -> 整份走 OCR


def page_texts(path: str | Path) -> list[str]:
    """取每页文字层。优先 pypdf，缺失时回退 PyMuPDF。"""
    try:
        from pypdf import PdfReader

        return [(p.extract_text() or "") for p in PdfReader(str(path)).pages]
    except Exception:
        import pymupdf as fitz

        with fitz.open(str(path)) as doc:
            return [p.get_text() for p in doc]


def route_pdf(path: str | Path, doc_id: str | None = None) -> RoutePlan:
    path = Path(path)
    texts = page_texts(path)
    text_pages: list[int] = []
    scanned_pages: list[int] = []
    for i, t in enumerate(texts, start=1):
        chars = len(re.sub(r"\s", "", t or ""))
        (text_pages if chars >= MIN_CHARS_PER_PAGE else scanned_pages).append(i)

    total = max(1, len(texts))
    ratio = len(scanned_pages) / total
    if ratio <= 0.0:
        decision, reason = "text", f"全部 {total} 页有文字层，走文本+表格解析"
    elif ratio < SCANNED_RATIO_TRIGGER:
        decision, reason = "mixed", f"{len(scanned_pages)}/{total} 页无文字层，仅这些页走 OCR"
    elif ratio < 1.0:
        decision, reason = "mixed", f"{len(scanned_pages)}/{total} 页无文字层，文本页与 OCR 混跑"
    else:
        decision, reason = "ocr", f"全部 {total} 页无文字层，必须走 OCR（Vision/PaddleOCR）"

    return RoutePlan(
        doc_id=doc_id or path.stem,
        pages=total,
        text_pages=text_pages,
        scanned_pages=scanned_pages,
        decision=decision,
        reason=reason,
    )
