"""FastAPI 应用：REST 接口 + SSE 流式对话 + 静态前端。"""

import asyncio
import json
import shutil
import time
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, config, db, knowledge, parsers, providers, skills, sysops
from .llm import LLMError, list_models
from .mcp_client import manager as mcp_manager

ROOT = config.ROOT
WEB_DIR = ROOT / "web"

_cancel_events: dict[str, asyncio.Event] = {}
_running_tasks: set[str] = set()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    cfg = config.load()
    mcp_manager.sync_from_config(cfg.get("mcp_servers", []))
    auto = [s["name"] for s in (cfg.get("mcp_servers") or [])
            if s.get("enabled") and s.get("auto_start")]
    if auto:
        asyncio.create_task(mcp_manager.start_enabled(auto))
    yield
    await mcp_manager.stop_all()


app = FastAPI(title="人工智能金融大赛本地 Agent", version="1.0.0", lifespan=lifespan)


# ------------------------------------------------------------------ 基础接口

@app.get("/api/health")
async def health():
    cfg = config.load()
    return {
        "ok": True, "version": app.version, "time": time.time(),
        "provider": cfg.get("provider"), "model": cfg.get("model"),
        "has_key": bool((cfg.get("api_keys") or {}).get(cfg.get("provider"), "")),
    }


@app.get("/api/providers")
async def api_providers():
    return {"ok": True, "providers": providers.public_registry(),
            "default": providers.DEFAULT_PROVIDER}


class ConfigPatch(BaseModel):
    patch: dict


@app.get("/api/config")
async def api_config():
    cfg = config.load()
    masked = {k: config.mask_key(v) for k, v in (cfg.get("api_keys") or {}).items()}
    safe = dict(cfg)
    safe["api_keys"] = masked
    safe["has_keys"] = {k: bool(v) for k, v in (cfg.get("api_keys") or {}).items()}
    return {"ok": True, "config": safe}


@app.post("/api/config")
async def api_config_save(body: ConfigPatch):
    before = config.load()
    cfg = config.update(body.patch or {})
    if cfg.get("mcp_servers") != before.get("mcp_servers"):
        mcp_manager.sync_from_config(cfg.get("mcp_servers", []))
    masked = {k: config.mask_key(v) for k, v in (cfg.get("api_keys") or {}).items()}
    safe = dict(cfg)
    safe["api_keys"] = masked
    safe["has_keys"] = {k: bool(v) for k, v in (cfg.get("api_keys") or {}).items()}
    return {"ok": True, "config": safe}


class ApiKeyBody(BaseModel):
    provider: str
    api_key: str = ""


@app.post("/api/apikey")
async def api_set_key(body: ApiKeyBody):
    config.set_api_key(body.provider, body.api_key.strip())
    return {"ok": True}


class ModelQuery(BaseModel):
    provider: str
    base_url: str = ""
    api_key: str = ""


@app.post("/api/models")
async def api_models(q: ModelQuery):
    key = q.api_key.strip() or config.get_api_key(q.provider)
    base, _ = providers.resolve(q.provider, q.base_url)
    if not base:
        raise HTTPException(400, "该提供商未设置 base_url")
    try:
        models = await list_models(base, key)
    except LLMError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
    return {"ok": True, "models": models, "count": len(models)}


# ------------------------------------------------------------------ 任务

class TaskCreate(BaseModel):
    title: str = "新任务"


class TaskPatch(BaseModel):
    title: str | None = None
    provider: str | None = None
    model: str | None = None


@app.get("/api/tasks")
async def api_tasks():
    return {"ok": True, "tasks": db.list_tasks(), "running": sorted(_running_tasks)}


@app.post("/api/tasks")
async def api_task_create(body: TaskCreate):
    cfg = config.load()
    return {"ok": True, "task": db.create_task(body.title or "新任务",
                                               cfg.get("provider", ""), cfg.get("model", ""))}


@app.patch("/api/tasks/{tid}")
async def api_task_patch(tid: str, body: TaskPatch):
    if not db.get_task(tid):
        raise HTTPException(404, "任务不存在")
    db.touch_task(tid, title=body.title, provider=body.provider, model=body.model)
    return {"ok": True, "task": db.get_task(tid)}


@app.delete("/api/tasks/{tid}")
async def api_task_delete(tid: str):
    db.delete_task(tid)
    upload_dir = config.UPLOAD_DIR / tid
    if upload_dir.exists():
        shutil.rmtree(upload_dir, ignore_errors=True)
    return {"ok": True}


@app.get("/api/tasks/{tid}/messages")
async def api_task_messages(tid: str):
    if not db.get_task(tid):
        raise HTTPException(404, "任务不存在")
    return {"ok": True, "messages": db.list_messages(tid)}


@app.post("/api/tasks/{tid}/clear")
async def api_task_clear(tid: str):
    db.clear_messages(tid)
    return {"ok": True}


# ------------------------------------------------------------------ 上传

@app.post("/api/upload")
async def api_upload(task_id: str = Form(...), file: UploadFile = File(...)):
    if not db.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    upload_dir = config.UPLOAD_DIR / task_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "unnamed").name
    dest = upload_dir / safe_name
    stem, suffix, i = dest.stem, dest.suffix, 1
    while dest.exists():
        dest = upload_dir / f"{stem}({i}){suffix}"
        i += 1
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    parsed = parsers.parse_file(dest)
    return {"ok": True, "file": {
        "name": dest.name, "kind": parsed["kind"], "size": parsed["size"],
        "pages": parsed.get("pages"), "truncated": parsed.get("truncated"),
        "error": parsed.get("error"),
        "preview": (parsed.get("text") or "")[:400],
        "chars": len(parsed.get("text") or ""),
    }}


@app.get("/api/supported")
async def api_supported():
    return {"ok": True, "hint": parsers.SUPPORTED_HINT,
            "exts": sorted(parsers.IMAGE_EXTS | parsers.TEXT_EXTS |
                           {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx", ".xls", ".doc", ".tsv", ".csv"})}


@app.get("/api/audit")
async def api_audit(limit: int = 100):
    """本机操作审计日志：谁在什么时候写了哪个文件、跑了什么命令。"""
    path = config.LOG_DIR / "audit.jsonl"
    if not path.exists():
        return {"ok": True, "count": 0, "records": []}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    recs = []
    for line in lines[-max(1, min(int(limit or 100), 1000)):]:
        try:
            recs.append(json.loads(line))
        except Exception:
            continue
    recs.reverse()
    return {"ok": True, "count": len(recs), "records": recs,
            "output_dir": str(sysops.output_dir())}


# ------------------------------------------------------------------ 对话（SSE）

class ChatBody(BaseModel):
    task_id: str
    content: str = ""
    attachments: list[str] = []
    provider: str | None = None
    model: str | None = None


@app.post("/api/chat/cancel")
async def api_chat_cancel(body: dict):
    tid = str(body.get("task_id", ""))
    ev = _cancel_events.get(tid)
    if ev:
        ev.set()
        return {"ok": True, "cancelled": True}
    return {"ok": True, "cancelled": False}


@app.post("/api/chat")
async def api_chat(body: ChatBody):
    if not db.get_task(body.task_id):
        raise HTTPException(404, "任务不存在")

    if body.provider or body.model:
        cfg = config.load()
        patch = {}
        if body.provider:
            patch["provider"] = body.provider
        if body.model:
            patch["model"] = body.model
        config.update(patch)
        db.touch_task(body.task_id, provider=config.load().get("provider"),
                      model=config.load().get("model"))

    attachments = []
    upload_dir = config.UPLOAD_DIR / body.task_id
    for name in body.attachments:
        p = upload_dir / Path(name).name
        if p.exists():
            attachments.append(parsers.parse_file(p))

    cancel_event = asyncio.Event()
    _cancel_events[body.task_id] = cancel_event

    async def event_stream():
        _running_tasks.add(body.task_id)
        try:
            yield _sse({"type": "open", "task_id": body.task_id})
            async for ev in agent.run_agent(body.task_id, body.content, attachments, cancel_event):
                yield _sse(ev)
        except asyncio.CancelledError:
            yield _sse({"type": "cancelled"})
        except Exception as e:
            yield _sse({"type": "error", "error": f"{type(e).__name__}: {e}"})
        finally:
            _running_tasks.discard(body.task_id)
            _cancel_events.pop(body.task_id, None)
            yield _sse({"type": "close"})

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


# ------------------------------------------------------------------ MCP

class MCPServer(BaseModel):
    name: str
    command: str
    args: list[str] = []
    env: dict = {}
    cwd: str = ""
    enabled: bool = True
    auto_start: bool = False


@app.get("/api/mcp")
async def api_mcp_list():
    cfg = config.load()
    saved = {s.get("name"): s for s in (cfg.get("mcp_servers") or [])}
    out = []
    for name, s in saved.items():
        client = mcp_manager.get(name)
        out.append({
            "config": s,
            "status": client.status_dict() if client else
                      {"name": name, "status": "未启动", "tools": [], "error": "", "running": False},
        })
    # 补充已连接但配置里没有的
    for name, client in mcp_manager.clients.items():
        if name not in saved:
            out.append({"config": {"name": name, "enabled": True}, "status": client.status_dict()})
    return {"ok": True, "servers": out}


class MCPSave(BaseModel):
    servers: list[MCPServer]


@app.post("/api/mcp/save")
async def api_mcp_save(body: MCPSave):
    servers = [s.model_dump() for s in body.servers]
    config.update({"mcp_servers": servers})
    mcp_manager.sync_from_config(servers)
    return {"ok": True}


class MCPAction(BaseModel):
    name: str
    restart: bool = True


@app.post("/api/mcp/test")
async def api_mcp_test(body: MCPAction):
    client = mcp_manager.get(body.name)
    if not client:
        raise HTTPException(404, "该 MCP 服务器未保存，请先保存配置")
    if body.restart and client.running:
        await client.stop()
    try:
        await client.start()
    except Exception as e:
        return {"ok": False, "status": client.status_dict(), "error": str(e)}
    return {"ok": True, "status": client.status_dict()}


@app.post("/api/mcp/stop")
async def api_mcp_stop(body: MCPAction):
    client = mcp_manager.get(body.name)
    if client:
        await client.stop()
    return {"ok": True}


@app.get("/api/mcp/tools")
async def api_mcp_tools():
    cfg = config.load()
    names = [s["name"] for s in (cfg.get("mcp_servers") or []) if s.get("enabled")]
    tools = await mcp_manager.all_tools(names)
    return {"ok": True, "count": len(tools),
            "tools": [{"name": t["function"]["name"],
                       "description": t["function"]["description"]} for t in tools]}


class MCPCall(BaseModel):
    server: str
    tool: str
    arguments: dict = {}


@app.post("/api/mcp/call")
async def api_mcp_call(body: MCPCall):
    """直接调用某个 MCP 工具（用于在设置页排查问题）。"""
    result = await mcp_manager.call(body.server, body.tool, body.arguments or {})
    return {"ok": not result.startswith("错误："), "result": result}


# ------------------------------------------------------------------ Skills

@app.get("/api/skills")
async def api_skills():
    return {"ok": True, "skills": skills.list_skills(),
            "dirs": [str(d) for d in skills.skill_dirs()]}


@app.get("/api/skills/{name}")
async def api_skill_detail(name: str):
    s = skills.get_skill(name)
    if not s:
        raise HTTPException(404, "技能不存在")
    return {"ok": True, "skill": s, "text": skills.read_skill_text(name)}


class SkillToggle(BaseModel):
    name: str
    enabled: bool


@app.post("/api/skills/toggle")
async def api_skill_toggle(body: SkillToggle):
    skills.set_enabled(body.name, body.enabled)
    return {"ok": True}


class SkillImport(BaseModel):
    path: str


@app.post("/api/skills/import")
async def api_skill_import(body: SkillImport):
    return skills.import_skill(body.path)


class SkillCreate(BaseModel):
    name: str
    description: str = ""
    content: str = ""


@app.post("/api/skills/create")
async def api_skill_create(body: SkillCreate):
    return skills.create_skill(body.name, body.description, body.content)


@app.delete("/api/skills/{name}")
async def api_skill_delete(name: str):
    return skills.delete_skill(name)


# ------------------------------------------------------------------ 知识库

@app.get("/api/knowledge")
async def api_knowledge():
    return {"ok": True, "bases": knowledge.list_kbs(),
            "dir": str(config.KNOWLEDGE_DIR)}


class KBName(BaseModel):
    name: str


@app.post("/api/knowledge/index")
async def api_kb_index(body: KBName):
    return knowledge.index_kb(body.name)


@app.post("/api/knowledge/index_all")
async def api_kb_index_all():
    return knowledge.index_all()


@app.post("/api/knowledge/toggle")
async def api_kb_toggle(body: dict):
    name, enabled = str(body.get("name", "")), bool(body.get("enabled", True))
    cfg = config.load()
    m = dict(cfg.get("knowledge_enabled", {}) or {})
    m[name] = enabled
    config.update({"knowledge_enabled": m})
    return {"ok": True}


@app.post("/api/knowledge/create")
async def api_kb_create(body: KBName):
    name = body.name.strip().replace("/", "_") or "新知识库"
    d = config.KNOWLEDGE_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "name": name, "path": str(d)}


@app.post("/api/knowledge/upload")
async def api_kb_upload(kb: str = Form(...), file: UploadFile = File(...)):
    d = config.KNOWLEDGE_DIR / kb
    d.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "unnamed").name
    dest = d / safe_name
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    res = knowledge.index_kb(kb)
    return {"ok": True, "file": dest.name, "index": res}


@app.post("/api/knowledge/search")
async def api_kb_search(body: dict):
    return knowledge.search(str(body.get("query", "")),
                            body.get("kb") or None,
                            int(body.get("top_k") or 6))


# ------------------------------------------------------------------ 前端

@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html",
                        headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(WEB_DIR / "favicon.ico", media_type="image/x-icon",
                        headers={"Cache-Control": "max-age=86400"})


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
