"""扫描件/图片 OCR 后备通道：把"引擎自己的解析管线"接到公告解析上。

为什么要有这一层
----------------
`announcement_doc.load_any()` 对扫描版 PDF 和图片要一条 OCR 通路，但引擎里
**已经有一条**了：`financial_documents.parse_document()` 会在 PDF 文本层不足时
自动调本机 OCR（`server/native/finocr`，macOS Vision），图片则直接走 OCR。
再写一套 OCR 只会产生两份不一致的结果（同输入不同输出），违反项目纪律。

所以这里的做法是：**复用 parse_document，再把它转成 AnnDoc**。
- 本机有 OCR（macOS + native/finocr，或装了可选的 rapidocr-onnxruntime）：正常出文字。
- 本机一个后端都没有（例如 Linux 且未装可选依赖）：`parse_document` 会退回文本层并在
  `warnings` 里写明"本页需要 OCR 但本机 OCR 不可用"，我们把这些警告原样带到
  AnnDoc 上，绝不静默把空白页当成"原文没有"。

调用方不应假设一定有 OCR 结果，必须先看 `doc.warnings`。
后端选择与可用性判断在 `financial_documents.OCR_BACKENDS` / `ocr_backends()` 里，只有一处真源。
"""
from __future__ import annotations

from pathlib import Path


def _ann_from_parsed(parsed: dict, filename: str):
    from .announcement_doc import load_parsed_document
    return load_parsed_document(parsed, filename=filename, kind='ocr')


def ocr_pdf_to_anndoc(path, doc=None):
    """扫描版 PDF -> AnnDoc。

    path : 原始 PDF 路径
    doc  : load_pdf 已经读出的 AnnDoc（可能带部分文本层），只用来继承 warnings。
    """
    path = Path(path)
    try:
        from .financial_documents import parse_document
        parsed = parse_document(path)
    except Exception as error:  # 解析层异常要变成可读提示，而不是裸 traceback
        raise RuntimeError(
            f'扫描版 PDF 需要 OCR，但本机 OCR 管线不可用：{error}。'
            '请在有 macOS Vision（server/native/finocr）的环境重试，'
            '或先手工把该 PDF 转成带文本层的版本。'
        ) from error
    out = _ann_from_parsed(parsed, path.name)
    if doc is not None and getattr(doc, 'warnings', None):
        out.warnings = list(dict.fromkeys(list(doc.warnings) + list(out.warnings)))
    if not out.text.strip():
        out.warnings.append('OCR 未产出任何文字：请检查该文件是否为空页或本机 OCR 是否可用')
    return out


def ocr_image_to_anndoc(path):
    """单张图片（png/jpg/webp…）-> AnnDoc。"""
    path = Path(path)
    try:
        from .financial_documents import parse_document
        parsed = parse_document(path)
    except Exception as error:
        raise RuntimeError(
            f'图片需要 OCR，但本机 OCR 管线不可用：{error}。'
            '请在有 macOS Vision（server/native/finocr）的环境重试。'
        ) from error
    out = _ann_from_parsed(parsed, path.name)
    if not out.text.strip():
        out.warnings.append('OCR 未产出任何文字：请检查该图片是否清晰、本机 OCR 是否可用')
    return out


def ocr_available() -> tuple[bool, str]:
    """本机 OCR 是否可用，以及原因（供 /health 或工具说明展示）。

    可用性判断只有一处真源：`financial_documents.ocr_backends()`。这里不重复实现，
    否则"能不能 OCR"会出现两个互相矛盾的答案。
    """
    from .financial_documents import ocr_backends
    backends = ocr_backends()
    ready = [b for b in backends if b['available']]
    if ready:
        return True, '；'.join(f"{b['backend']}：{b['reason']}" for b in ready)
    return False, '；'.join(f"{b['backend']}：{b['reason']}" for b in backends)
