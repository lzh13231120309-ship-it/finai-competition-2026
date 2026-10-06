"""知识库：本地目录 → 文本分片 → 索引 → 检索。

检索采用 BM25（针对中文做了字符二元切分 + 英文/数字词切分），
纯 Python 实现、零外部依赖，适合本地离线运行，也便于在答辩时说明算法。
"""

import math
import re
from pathlib import Path

from . import config, db
from .parsers import parse_file

CHUNK_SIZE = 700
CHUNK_OVERLAP = 120
INDEXABLE_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".pdf",
    ".docx", ".pptx", ".xlsx", ".xlsm", ".html", ".htm", ".log",
}

_WORD_RE = re.compile(r"[A-Za-z]+|\d+(?:\.\d+)?")
_CN_RE = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    tokens = _WORD_RE.findall(text)
    cn = _CN_RE.findall(text)
    tokens.extend("".join(pair) for pair in zip(cn, cn[1:]))
    tokens.extend(cn)
    return tokens


def chunk_text(text: str) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if not text:
        return []
    chunks, start = [], 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        if end < len(text):
            for sep in ("\n\n", "\n", "。", "；", "，"):
                pos = text.rfind(sep, start + CHUNK_SIZE // 2, end)
                if pos > 0:
                    end = pos + len(sep)
                    break
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return [c for c in chunks if len(c) > 20]


def kb_dirs() -> dict[str, Path]:
    """返回 {知识库名: 目录}，含内置目录与用户额外添加的目录。"""
    cfg = config.load()
    out: dict[str, Path] = {}
    roots = [config.KNOWLEDGE_DIR] + [Path(p).expanduser() for p in cfg.get("extra_knowledge_dirs", [])]
    for root in roots:
        if not root.exists():
            continue
        if (root / "knowledge").exists():
            root = root / "knowledge"
        for child in sorted(root.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                out.setdefault(child.name, child)
        if root == config.KNOWLEDGE_DIR:
            for f in sorted(root.glob("*")):
                if f.is_file() and f.suffix.lower() in INDEXABLE_EXTS:
                    out.setdefault("默认知识库", root)
                    break
    return out


def list_kbs() -> list[dict]:
    cfg = config.load()
    enabled = cfg.get("knowledge_enabled", {}) or {}
    stats = db.kb_stats()
    out = []
    for name, path in kb_dirs().items():
        files = [f for f in path.rglob("*")
                 if f.is_file() and f.suffix.lower() in INDEXABLE_EXTS and not f.name.startswith(".")]
        out.append({
            "name": name,
            "path": str(path),
            "files": len(files),
            "file_list": [str(f.relative_to(path)) for f in files[:80]],
            "chunks": stats.get(name, {}).get("chunks", 0),
            "indexed": name in stats,
            "enabled": enabled.get(name, True),
        })
    return out


def index_kb(name: str) -> dict:
    dirs = kb_dirs()
    if name not in dirs:
        return {"ok": False, "error": f"知识库 {name} 不存在"}
    path = dirs[name]
    items: list[tuple[str, int, str]] = []
    files = [f for f in sorted(path.rglob("*"))
             if f.is_file() and f.suffix.lower() in INDEXABLE_EXTS and not f.name.startswith(".")]
    for f in files:
        try:
            parsed = parse_file(f)
            for i, ch in enumerate(chunk_text(parsed.get("text") or "")):
                items.append((str(f.relative_to(path)), i, ch))
        except Exception:
            continue
    db.replace_kb_chunks(name, items)
    return {"ok": True, "kb": name, "files": len(files), "chunks": len(items)}


def index_all() -> dict:
    results = [index_kb(name) for name in kb_dirs()]
    return {"ok": True, "results": results}


def _bm25(query_tokens: list[str], docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
    n = len(docs)
    if not n:
        return []
    avgdl = sum(len(d) for d in docs) / n or 1.0
    df: dict[str, int] = {}
    for d in docs:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    scores = [0.0] * n
    for t in set(query_tokens):
        if t not in df:
            continue
        idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
        for i, d in enumerate(docs):
            tf = d.count(t)
            if not tf:
                continue
            dl = len(d) or 1
            scores[i] += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))
    return scores


def search(query: str, kb: str | None = None, top_k: int = 6) -> dict:
    cfg = config.load()
    enabled = [k for k, v in (cfg.get("knowledge_enabled", {}) or {}).items() if v]
    names = [kb] if kb else (list(enabled) if enabled else None)
    # 未做启用配置时，检索全部已建索引的知识库
    if names is None:
        chunks = db.all_chunks()
    else:
        chunks = db.all_chunks([n for n in names if n]) or db.all_chunks()
    if not chunks:
        return {"ok": True, "hits": [], "note": "知识库尚未建立索引，请在设置页点击「重建索引」"}
    docs = [tokenize(c["text"]) for c in chunks]
    q = tokenize(query)
    if not q:
        return {"ok": True, "hits": []}
    scores = _bm25(q, docs)
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
    hits = []
    for i in order:
        if scores[i] <= 0:
            continue
        c = chunks[i]
        hits.append({
            "kb": c["kb"], "file": c["file"], "chunk_no": c["chunk_no"],
            "score": round(scores[i], 4), "text": c["text"],
        })
    return {"ok": True, "hits": hits}
