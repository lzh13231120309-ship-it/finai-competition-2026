"""文件解析：图片、xlsx、PDF、Word、txt、markdown、PPT 等。

输出统一结构，便于直接注入对话上下文：
{
  "name": 原文件名, "ext": 扩展名, "size": 字节数, "kind": "image|table|document|text",
  "text": 抽取出的文本, "pages": 页数或 None, "truncated": 是否截断,
  "error": 出错信息或 None, "data_url": 图片的 data URL（仅图片）
}
"""

import base64
import csv
import io
import json
import mimetypes
from pathlib import Path

MAX_TEXT = 80000          # 单文件最大注入字符数
MAX_TABLE_ROWS = 300      # 单个 sheet 最大行数
MAX_TABLE_COLS = 40
MAX_IMAGE_BYTES = 8 * 1024 * 1024

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic"}
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".log", ".ini", ".toml", ".xml", ".html", ".htm", ".py", ".js", ".ts",
    ".java", ".c", ".cpp", ".go", ".rs", ".sql", ".sh", ".r", ".m",
}
SUPPORTED_HINT = "图片 / PDF / Word / Excel / PPT / txt / markdown / csv / json / 代码文件"


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) > MAX_TEXT:
        return text[:MAX_TEXT] + f"\n\n…（内容过长，已截断，原长度 {len(text)} 字符）", True
    return text, False


def _read_text_file(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _table_to_md(rows: list[list[str]], caption: str = "") -> str:
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        return ""
    if len(rows) > MAX_TABLE_ROWS:
        note = f"\n…（仅显示前 {MAX_TABLE_ROWS} 行，共 {len(rows)} 行）"
        rows = rows[:MAX_TABLE_ROWS]
    else:
        note = ""
    width = min(max(len(r) for r in rows), MAX_TABLE_COLS)
    norm = []
    for r in rows:
        r = ["" if c is None else str(c).replace("\n", " ").replace("|", "\\|") for c in r]
        r = (r + [""] * width)[:width]
        norm.append(r)
    head, body = norm[0], norm[1:]
    out = [f"**{caption}**" if caption else ""]
    out.append("| " + " | ".join(head) + " |")
    out.append("|" + "---|" * width)
    for r in body:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out) + note


# ------------------------------------------------------------------ 各类型解析

def _parse_xlsx(path: Path) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    parts = []
    for ws in wb.worksheets:
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= MAX_TABLE_ROWS + 1:
                break
            rows.append(list(row))
        md = _table_to_md(rows, caption=f"工作表：{ws.title}")
        if md:
            parts.append(md)
    wb.close()
    return {"text": "\n\n".join(parts), "pages": None}


def _parse_pdf(path: Path) -> dict:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    pages = []
    for i, pg in enumerate(reader.pages):
        try:
            t = (pg.extract_text() or "").strip()
        except Exception as e:
            t = f"(第 {i+1} 页解析失败：{e})"
        pages.append(f"--- 第 {i+1} 页 ---\n{t}" if t else f"--- 第 {i+1} 页（无文字层，可能是扫描件）---")
        if sum(len(p) for p in pages) > MAX_TEXT:
            pages.append("…（后续页面已省略）")
            break
    text = "\n\n".join(pages)
    if not any(len(p) > 60 for p in pages):
        text += ("\n\n提示：该 PDF 似乎没有文字层（纯扫描图片）。"
                 "若模型支持视觉，建议直接把页面导出为图片后上传。")
    return {"text": text, "pages": len(reader.pages)}


def _parse_docx(path: Path) -> dict:
    import docx
    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for ti, table in enumerate(d.tables):
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        md = _table_to_md(rows, caption=f"表格 {ti+1}")
        if md:
            parts.append(md)
    return {"text": "\n".join(parts), "pages": None}


def _parse_pptx(path: Path) -> dict:
    from pptx import Presentation
    prs = Presentation(str(path))
    parts = []
    for i, slide in enumerate(prs.slides):
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                texts.append(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False) and shape.has_table:
                rows = [[c.text for c in row.cells] for row in shape.table.rows]
                md = _table_to_md(rows, caption="表格")
                if md:
                    texts.append(md)
        parts.append(f"--- 第 {i+1} 页 ---\n" + "\n".join(texts))
    return {"text": "\n\n".join(parts), "pages": len(prs.slides)}


def _parse_csv(path: Path) -> dict:
    raw = _read_text_file(path)
    delim = "\t" if path.suffix.lower() == ".tsv" else ","
    try:
        rows = list(csv.reader(io.StringIO(raw), delimiter=delim))[:MAX_TABLE_ROWS + 1]
    except Exception:
        return {"text": raw, "pages": None}
    return {"text": _table_to_md(rows, caption=path.name), "pages": None}


def _image_data_url(path: Path) -> tuple[str, str | None]:
    size = path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        return "", f"图片过大（{size/1024/1024:.1f}MB），超过 8MB 限制"
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    if path.suffix.lower() == ".heic":
        return "", "HEIC 格式暂不支持直接识别，请先转为 PNG/JPG"
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{b64}", None


# ------------------------------------------------------------------ 主入口

def parse_file(path: str | Path) -> dict:
    path = Path(path)
    result = {
        "name": path.name, "ext": path.suffix.lower(), "size": 0,
        "kind": "text", "text": "", "pages": None,
        "truncated": False, "error": None, "data_url": None,
    }
    if not path.exists():
        result["error"] = "文件不存在"
        return result
    result["size"] = path.stat().st_size
    ext = result["ext"]

    try:
        if ext in IMAGE_EXTS:
            url, err = _image_data_url(path)
            result.update(kind="image", data_url=url or None,
                          text=f"(图片文件：{path.name}，{result['size']/1024:.0f}KB)")
            result["error"] = err
        elif ext in (".xlsx", ".xlsm"):
            r = _parse_xlsx(path)
            result.update(kind="table", **r)
        elif ext == ".xls":
            result.update(kind="table",
                          text="旧版 .xls 格式支持有限，建议另存为 .xlsx 后重新上传。")
        elif ext == ".pdf":
            r = _parse_pdf(path)
            result.update(kind="document", **r)
        elif ext == ".docx":
            r = _parse_docx(path)
            result.update(kind="document", **r)
        elif ext == ".doc":
            result.update(kind="document",
                          text="旧版 .doc 格式支持有限，建议另存为 .docx 后重新上传。")
        elif ext == ".pptx":
            r = _parse_pptx(path)
            result.update(kind="document", **r)
        elif ext in (".csv", ".tsv"):
            r = _parse_csv(path)
            result.update(kind="table", **r)
        elif ext in TEXT_EXTS:
            result.update(kind="text", text=_read_text_file(path))
        elif ext == ".json":
            result.update(kind="text",
                          text=json.dumps(json.loads(_read_text_file(path)),
                                          ensure_ascii=False, indent=2))
        else:
            try:
                result.update(kind="text", text=_read_text_file(path))
            except Exception:
                result.update(error=f"暂不支持的文件类型：{ext}")
    except Exception as e:
        result["error"] = f"解析失败：{type(e).__name__}: {e}"

    if result["kind"] != "image":
        result["text"], result["truncated"] = _truncate(result["text"] or "")
    return result


def build_attachment_block(parsed: dict, max_chars: int = 30000) -> str:
    """把解析结果拼成注入模型的文本块。"""
    head = f"【附件：{parsed['name']}】"
    if parsed.get("error"):
        return f"{head}\n（{parsed['error']}）"
    if parsed["kind"] == "image":
        return f"{head}\n（图片已作为多模态内容附加，请直接观察图片）"
    text = parsed.get("text") or "(空文件或未抽取到文本)"
    if parsed["pages"]:
        head += f"（共 {parsed['pages']} 页）"
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（后续内容已省略）"
    return f"{head}\n{text}"
