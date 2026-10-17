"""知识库：本地目录 → 文本分片 → 索引 → 检索。

检索采用 BM25（针对中文做了字符二元切分 + 英文/数字词切分），
纯 Python 实现、零外部依赖，适合本地离线运行，也便于在答辩时说明算法。
"""

import math
import hashlib
import json
from collections import Counter
from functools import lru_cache
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
    errors=[]; processed=0; versions={}
    files = [f for f in sorted(path.rglob("*"))
             if f.is_file() and f.suffix.lower() in INDEXABLE_EXTS and not f.name.startswith(".")]
    for f in files:
        try:
            parsed = parse_file(f)
            content=parsed.get("text") or ""
            if not content.strip():
                errors.append({"file":str(f.relative_to(path)),"error":"未提取到可索引文本"});continue
            processed+=1
            stat=f.stat()
            versions[str(f.relative_to(path))]={"size":stat.st_size,"mtime_ns":stat.st_mtime_ns,"sha256":hashlib.sha256(f.read_bytes()).hexdigest()}
            for i, ch in enumerate(chunk_text(content)):
                items.append((str(f.relative_to(path)), i, ch))
        except Exception as error:
            errors.append({"file":str(f.relative_to(path)),"error":type(error).__name__})
    db.replace_kb_chunks(name, items)
    record=_index_record(name);record.parent.mkdir(parents=True,exist_ok=True)
    temp=record.with_suffix(".tmp");temp.write_text(json.dumps(versions,ensure_ascii=False),encoding="utf-8");temp.replace(record)
    return {"ok": True, "kb": name, "files": len(files), "processed_files": processed, "chunks": len(items), "errors": errors}


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


SYNONYMS = {
    'wacc':'加权平均资本成本 折现率', 'dcf':'现金流折现 估值',
    'fcff':'企业自由现金流', 'fcfe':'股权自由现金流', 'eps':'每股收益 股数',
    'roe':'净资产收益率', 'roic':'投入资本回报率 再投资',
    'ifrs':'国际财务报告准则', 'cas':'企业会计准则',
    '三张报表':'资产负债表 利润表 现金流量表',
    '研报纠错':'研究报告 数字核验 引用 纠错',
    '研报质量':'研究报告 质量评价 证据 评分',
    '结构化提取':'结构化 数据提取 字段 单位 期间 证据',
}


def expand_query(query):
    lower=query.lower()
    extra=[value for key,value in SYNONYMS.items() if (re.search(r'(?<![a-z])'+re.escape(key)+r'(?![a-z])',lower) if key.isascii() else key in lower)]
    return query+' '+' '.join(extra)


@lru_cache(maxsize=8)
def _corpus(texts):
    counts=[Counter(tokenize(text)) for text in texts]
    avg=sum(sum(c.values()) for c in counts)/len(counts) if counts else 1
    postings={}
    for i,c in enumerate(counts):
        for token,count in c.items():postings.setdefault(token,[]).append((i,count))
    return counts,postings,avg or 1


def _rank(query,texts):
    counts,postings,avg=_corpus(texts);scores=[0.0]*len(texts);n=len(texts)
    lengths=[sum(c.values()) for c in counts]
    for token in set(tokenize(query)):
        matched=postings.get(token,[])
        if not matched:continue
        idf=math.log(1+(n-len(matched)+.5)/(len(matched)+.5))
        for i,tf in matched:scores[i]+=idf*tf*2.5/(tf+1.5*(1-.75+.75*lengths[i]/avg))
    return scores


def _knowledge_path(kb,filename):
    root=kb_dirs().get(kb)
    if not root:raise ValueError('知识库不存在')
    root=root.resolve();path=(root/filename).resolve()
    if not path.is_relative_to(root) or not path.is_file():raise ValueError('文件不属于指定知识库')
    if path.suffix.lower() not in INDEXABLE_EXTS:raise ValueError('不支持的知识文件格式')
    return path


@lru_cache(maxsize=1024)
def _metadata(path,size,mtime):
    if Path(path).suffix.lower() not in ('.md','.markdown','.txt'):return {'source_status':'未提供结构化来源元数据'}
    with Path(path).open(encoding='utf-8',errors='replace') as stream:head=stream.read(16000)
    out={}
    keys={'标题':'title','任务域':'domain','关键词':'keywords','适用范围':'scope','核验日期':'checked_at','来源':'source','资料性质':'material_type'}
    for line in head.splitlines()[:35]:
        clean=line.strip().replace('**','')
        for label,key in keys.items():
            match=re.match(r'^'+label+r'\s*[：:]\s*(.*)',clean)
            if match:out[key]=match.group(1).strip().strip('*')
    out['source_urls']=list(dict.fromkeys(re.findall(r'https?://[^\s<>\)\]，；]+',head)))
    if Path(path).name in ('01-竞赛方案要点.md','03-评测指标与目标值.md'):
        out.setdefault('material_type','项目设计草案与自定验收目标，非官方竞赛规则')
    out['source_status']='带来源的本地知识条目' if out['source_urls'] else '本地材料，未提供可核验外部来源'
    return out


def _index_record(kb):
    return config.DATA_DIR / "knowledge-index" / (hashlib.sha256(kb.encode()).hexdigest()+".json")


@lru_cache(maxsize=64)
def _versions(path,mtime):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def document_metadata(kb,filename):
    try:
        path=_knowledge_path(kb,filename);stat=path.stat()
        meta=dict(_metadata(str(path),stat.st_size,stat.st_mtime_ns))
        record=_index_record(kb)
        if record.exists():
            version=_versions(str(record),record.stat().st_mtime_ns).get(filename) or {}
            meta["indexed_sha256"]=version.get("sha256")
            meta["index_status"]="current" if version.get("size")==stat.st_size and version.get("mtime_ns")==stat.st_mtime_ns else "source_changed_reindex_required"
        else:meta["index_status"]="legacy_index_without_version_record"
        return meta
    except (ValueError,OSError):return {'source_status':'源文件缺失或已移除，应重建索引'}


def read_document(kb,filename,offset=0,limit=10000):
    if (config.load().get('knowledge_enabled') or {}).get(kb,True) is False:
        return {'ok':False,'error':'知识库已停用'}
    path=_knowledge_path(kb,filename);text=parse_file(path).get('text') or ''
    offset=max(0,int(offset));limit=min(20000,max(100,int(limit)))
    return {'ok':True,'kb':kb,'file':filename,'metadata':document_metadata(kb,filename),
        'content_sha256':hashlib.sha256(text.encode()).hexdigest(),'offset':offset,'total_chars':len(text),
        'text':text[offset:offset+limit],'next_offset':offset+limit if offset+limit<len(text) else None,
        'note':'知识是方法依据，不是本次公司的财务事实；来源链接仅作溯源，读取不联网。'}


def search(query: str, kb: str | None = None, top_k: int = 6) -> dict:
    cfg=config.load();enabled=cfg.get('knowledge_enabled') or {}
    if kb and enabled.get(kb,True) is False:return {'ok':True,'hits':[],'note':'指定知识库已停用'}
    chunks=db.all_chunks([kb] if kb else None)
    chunks=[c for c in chunks if enabled.get(c['kb'],True) is not False]
    if not chunks:return {'ok':True,'hits':[],'note':'指定范围没有可用索引，请重建索引或启用知识库'}
    if not tokenize(query):return {'ok':True,'hits':[]}
    expanded=expand_query(query);scores=_rank(expanded,tuple(c['text'] for c in chunks))
    metadata_by_file={}
    for c in chunks:
        key=(c['kb'],c['file'])
        if key not in metadata_by_file:metadata_by_file[key]=document_metadata(*key)
    titles=tuple((metadata_by_file[(c['kb'],c['file'])].get('title') or Path(c['file']).stem)+' '+metadata_by_file[(c['kb'],c['file'])].get('keywords','') for c in chunks)
    title_scores=_rank(query,titles)
    scores=[value+1.2*title_scores[i] for i,value in enumerate(scores)]
    top_k=min(20,max(1,int(top_k)))
    order=sorted(range(len(scores)),key=lambda i:scores[i],reverse=True)
    hits=[];per_file={}
    for i in order:
        if scores[i]<=0:break
        c=chunks[i];key=(c['kb'],c['file'])
        if per_file.get(key,0)>=2:continue
        per_file[key]=per_file.get(key,0)+1
        hits.append({'kb':c['kb'],'file':c['file'],'chunk_no':c['chunk_no'],'score':round(scores[i],4),
            'text':c['text'],'metadata':metadata_by_file[key],
            'citation_id':hashlib.sha256((c['kb']+'/'+c['file']+'/'+str(c['chunk_no'])+'/'+c['text']).encode()).hexdigest()[:16]})
        if len(hits)>=top_k:break
    return {'ok':True,'hits':hits,'expanded_query':expanded.strip(),
        'note':'本地检索，无外部请求；得分是检索排序，不是事实置信度。' if hits else '指定范围未命中相关知识'}
