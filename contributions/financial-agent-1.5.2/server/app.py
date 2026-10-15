"""FastAPI 应用：REST 接口 + SSE 流式对话 + 静态前端。"""

import asyncio
import json
import shutil
import time
import uuid
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, config, db, knowledge, parsers, providers, skills, sysops, finance_service
from .llm import LLMError, list_models
from .mcp_client import manager as mcp_manager

ROOT = config.ROOT
WEB_DIR = ROOT / "web"

_cancel_events: dict[str, asyncio.Event] = {}
_running_tasks: set[str] = set()
_active_runs: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    skills.seed_financial_skills()
    skills.seed_knowledge_base()
    cfg = config.load()
    mcp_manager.sync_from_config(cfg.get("mcp_servers", []))
    auto = [s["name"] for s in (cfg.get("mcp_servers") or [])
            if s.get("enabled") and s.get("auto_start")]
    # 后台预热（不阻塞启动），但保留 task 引用以便停止时取消；
    # 真正取工具前 agent.py 还会走 ensure_ready()，所以即使预热没跑完也不会丢工具。
    if auto:
        task = asyncio.create_task(mcp_manager.start_enabled(auto))
        _app.state.mcp_prewarm = task
    yield
    producers = [run['producer'] for run in list(_active_runs.values())]
    for producer in producers: producer.cancel()
    if producers: await asyncio.gather(*producers, return_exceptions=True)
    prewarm = getattr(_app.state, "mcp_prewarm", None)
    if prewarm and not prewarm.done():
        prewarm.cancel()
    await mcp_manager.stop_all()




app = FastAPI(title="人工智能金融大赛本地 Agent", version="1.5.2", lifespan=lifespan)
@app.middleware("http")
async def _no_cache_static(request, call_next):
    """静态资源禁强缓存：WKWebView 常把旧 app.js 缓存到重启都不刷新。"""
    resp = await call_next(request)
    if request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp



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
    return {"ok": True, "providers": providers.public_registry(config.load()),
            "default": providers.DEFAULT_PROVIDER}


class CustomProvider(BaseModel):
    id: str = ""
    label: str
    base_url: str = ""
    models: list[str] = []


class CustomProvidersBody(BaseModel):
    providers: list[CustomProvider]


def _slugify(label: str, existing: set[str]) -> str:
    import re
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", label.strip().lower()).strip("-") or "custom"
    pid, i = f"custom_{base}", 2
    while pid in existing:
        pid = f"custom_{base}-{i}"
        i += 1
    return pid


@app.post("/api/custom_providers")
async def api_custom_providers_save(body: CustomProvidersBody):
    """整体保存自定义提供者清单。已有 id 的按 id 更新，无 id 的新建并分配 id。"""
    cfg = config.load()
    saved = {p.get("id"): p for p in (cfg.get("custom_providers") or [])}
    taken = set(saved.keys())
    out: list[dict] = []
    for item in body.providers:
        label = (item.label or "").strip()
        if not label:
            continue
        pid = (item.id or "").strip()
        if pid and pid in saved:
            out.append({
                "id": pid,
                "label": label,
                "base_url": (item.base_url or "").strip(),
                "models": [m.strip() for m in item.models if m.strip()],
            })
            continue
        pid = _slugify(label, taken)
        taken.add(pid)
        out.append({
            "id": pid,
            "label": label,
            "base_url": (item.base_url or "").strip(),
            "models": [m.strip() for m in item.models if m.strip()],
        })
    config.update({"custom_providers": out})
    return {"ok": True, "providers": providers.public_registry(config.load()),
            "saved": out}


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
    if "speed_mode" in (body.patch or {}) and body.patch["speed_mode"] not in ("fast", "balanced", "deep"):
        raise HTTPException(400, "速度档位仅支持fast/balanced/deep")
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
    base, _ = providers.resolve(q.provider, q.base_url, config.load())
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
    parsed = await asyncio.to_thread(parsers.parse_file, dest)
    return {"ok": True, "file": {
        "name": dest.name, "kind": parsed["kind"], "size": parsed["size"],
        "pages": parsed.get("pages"), "truncated": parsed.get("truncated"),
        "error": parsed.get("error"),
        "preview": (parsed.get("text") or "")[:400],
        "chars": len(parsed.get("text") or ""),
        'ocr_pages': parsed.get('ocr_pages', []), 'table_count': parsed.get('table_count'),
        'warnings': parsed.get('warnings', []),
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

class FinancialBody(BaseModel):
    task_id: str
    filenames: list[str] = []
    company: str = ''
    period: str = ''
    scope: str = 'auto'


@app.post('/api/financial/analyze')
async def api_financial_analyze(body: FinancialBody):
    if not db.get_task(body.task_id):
        raise HTTPException(404, '任务不存在')
    try:
        result = await asyncio.to_thread(finance_service.analyze_financial_report, body.task_id, body.model_dump())
        db.add_message(body.task_id, 'assistant', '本地财报核验完成。\n\n' + agent._artifact_footer(result))
        return result
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc


class ValuationModelBody(BaseModel):
    task_id: str
    run_id: str | None = None
    filenames: list[str] = []
    options: dict = {}
    overrides: dict = {}
    forecast: dict = {}
    market: dict = {}
    equity_model: dict = {}
    industry: str = ''
    market_symbol: str = ''
    benchmark: str = ''
    peers: list[dict] = []
    forensic_run_id: str | None = None
    valuation_date: str | None = None
    export_formats: list[str] | None = None
    allow_illustrative_model: bool = False
    allow_estimated_shares: bool = False
    monte_carlo: dict = {}
    reverse_dcf: dict = {}


@app.post('/api/financial/valuation-model')
async def api_financial_valuation_model(body: ValuationModelBody):
    if not db.get_task(body.task_id):
        raise HTTPException(404, '任务不存在')
    try:
        return await asyncio.to_thread(finance_service.build_valuation_model,
                                       body.task_id, body.model_dump(exclude_none=True, exclude_unset=True))
    except Exception as exc:
        raise HTTPException(400, '自动估值未完成，请检查财报、模型假设和输入数据。') from exc


class ReportReviewBody(BaseModel):
    task_id: str
    text: str = ''
    filename: str = ''
    run_id: str | None = None
    valuation_run_id: str | None = None
    forensic_run_id: str | None = None


@app.get('/api/financial/report-sources/{task_id}')
async def api_report_sources(task_id: str):
    if not db.get_task(task_id):
        raise HTTPException(404, '任务不存在')
    folder = config.UPLOAD_DIR / finance_service._id(task_id)
    files = sorted(p.name for p in folder.glob('*') if p.is_file() and not p.is_symlink()
                   and p.suffix.lower() in {'.pdf', '.docx', '.md', '.txt'}) if folder.exists() else []
    return {'files': files, 'runs': finance_service.list_runs(task_id)}


@app.post('/api/financial/report-review')
async def api_financial_report_review(body: ReportReviewBody):
    if not db.get_task(body.task_id):
        raise HTTPException(404, '任务不存在')
    try:
        return await asyncio.to_thread(finance_service.audit_report, body.task_id,
                                       body.model_dump(exclude_none=True))
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc


class FinanceWorkflowBody(BaseModel):
    task_id: str
    run_id: str
    goals: list[str] = ['extraction','financial_analysis','valuation','report_check','quality']
    financial_filenames: list[str] = []
    announcement_filenames: list[str] = []
    analysis_run_id: str | None = None
    report_filename: str = ''
    report_text: str = ''
    valuation_options: dict = {}
    company: str = ''
    period: str = ''
    scope: str = ''


_workflow_controls = {}


@app.get('/api/financial/workflow-inputs/{task_id}')
async def api_workflow_inputs(task_id: str):
    if not db.get_task(task_id):raise HTTPException(404,'任务不存在')
    folder=config.UPLOAD_DIR/finance_service._id(task_id)
    names=sorted(p.name for p in folder.glob('*') if p.is_file() and not p.is_symlink() and p.suffix.lower() in finance_service.SUPPORTED)
    return {'files':names,'runs':finance_service.list_runs(task_id)}


@app.post('/api/financial/workflow')
async def api_finance_workflow(body: FinanceWorkflowBody):
    import threading
    from .finance_workflow import run_workflow
    if not db.get_task(body.task_id):raise HTTPException(404,'任务不存在')
    try:finance_service._id(body.run_id)
    except ValueError as exc:raise HTTPException(400,str(exc)) from exc
    if any(key[0]==body.task_id for key in _workflow_controls):raise HTTPException(409,'当前任务已有联动流程运行')
    key=(body.task_id,body.run_id);control=threading.Event();_workflow_controls[key]=control
    try:
        return await asyncio.to_thread(run_workflow,body.task_id,body.model_dump(exclude_none=True),control)
    except (ValueError,FileNotFoundError) as exc:raise HTTPException(400,str(exc)) from exc
    finally:
        control.set();_workflow_controls.pop(key,None)


class WorkflowCancelBody(BaseModel):
    task_id: str
    run_id: str


@app.post('/api/financial/workflow/cancel')
async def api_workflow_cancel(body: WorkflowCancelBody):
    control=_workflow_controls.get((body.task_id,body.run_id))
    if control:control.set()
    return {'ok':True,'status':'stopping_after_current_stage' if control else 'not_running'}


@app.get('/api/financial/runs/{task_id}')
async def api_financial_runs(task_id: str):
    if not db.get_task(task_id):
        raise HTTPException(404, '任务不存在')
    return {'ok': True, 'runs': finance_service.list_runs(task_id)}


class FinancialConfirmBody(BaseModel):
    task_id: str
    run_id: str
    fact_ids: list[str]


@app.post('/api/financial/confirm')
async def api_financial_confirm(body: FinancialConfirmBody):
    if not db.get_task(body.task_id):
        raise HTTPException(404, '任务不存在')
    try:
        return await asyncio.to_thread(finance_service.confirm_financial_facts, body.task_id, body.model_dump())
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get('/api/financial/runs/{task_id}/{run_id}')
async def api_financial_run(task_id: str, run_id: str):
    try:
        run = finance_service.load_run(task_id, run_id)
        return {'ok': True, 'run': run, 'summary': finance_service._summary(run)}
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get('/api/financial/artifacts/{task_id}/{run_id}/{filename}')
async def api_financial_artifact(task_id: str, run_id: str, filename: str):
    if filename not in {'run.json', '报告.xlsx', '报告.docx', '报告.json', '报告.md', 'records.json', 'findings.json', 'valuation_pro.json', 'valuation_pro.xlsx', 'quality_report.html'}:
        raise HTTPException(404, '文件不存在')
    try:
        path = finance_service.run_dir(task_id, run_id) / filename
        if not path.is_file():
            if filename.startswith('报告.'):
                await asyncio.to_thread(finance_service.export_financial_report, task_id,
                    {'run_id': run_id, 'format': filename.rsplit('.', 1)[1]})
            else:
                raise FileNotFoundError('运行结果不存在')
        headers={'Content-Security-Policy': "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; form-action 'none'"} if filename.endswith('.html') else None
        return FileResponse(path, filename=filename, headers=headers)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get('/api/financial/evidence/{task_id}/{document_id}')
async def api_financial_evidence(task_id: str, document_id: str, evidence_id: str):
    import html
    from urllib.parse import quote
    try:
        doc, evidence, path = await asyncio.to_thread(finance_service.find_evidence, task_id, document_id, evidence_id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    title = html.escape(doc['filename'])
    location = f"第 {evidence['page']} 页"
    if evidence.get('sheet'):
        location += ' · ' + html.escape(str(evidence['sheet']))
    image_url = f'/api/financial/evidence-image/{task_id}/{document_id}?evidence_id={quote(evidence_id, safe="")}'
    image_html = f'<img src="{html.escape(image_url)}" alt="原文页及证据位置">' if path.suffix.lower() in {'.pdf', '.png', '.jpg', '.jpeg', '.webp'} else ''
    return HTMLResponse(f'''<!doctype html><html lang="zh-CN"><meta charset="UTF-8"><title>证据 · {title}</title>
    <style>body{{font:16px system-ui;max-width:1100px;margin:30px auto;padding:0 20px;color:#18384a}}pre{{white-space:pre-wrap;background:#eef4f7;padding:18px;border-radius:12px}}img{{max-width:100%;border:1px solid #ccd9df}}small{{color:#536f80}}</style>
    <h1>{title}</h1><p>{location}</p><small>证据编号：{html.escape(evidence_id)} · 文件SHA256：{doc['sha256']}</small>
    <pre>{html.escape(evidence['text'])}</pre>{image_html}</html>''')


@app.get('/api/financial/evidence-image/{task_id}/{document_id}')
async def api_financial_evidence_image(task_id: str, document_id: str, evidence_id: str):
    try:
        doc, evidence, path = await asyncio.to_thread(finance_service.find_evidence, task_id, document_id, evidence_id)
        def render():
            import fitz
            with fitz.open(path) as document:
                page = document[evidence['page'] - 1]
                bbox = evidence.get('bbox')
                if bbox:
                    page.draw_rect(fitz.Rect(bbox), color=(.9, .35, .1), width=2, overlay=True)
                return page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False).tobytes('png')
        return Response(await asyncio.to_thread(render), media_type='image/png')
    except (ValueError, FileNotFoundError, IndexError) as exc:
        raise HTTPException(404, str(exc)) from exc

class ChatBody(BaseModel):
    speed_mode: str | None = None
    task_id: str
    content: str = ""
    attachments: list[str] = []
    provider: str | None = None
    model: str | None = None
    run_id: str | None = None


@app.post("/api/chat/cancel")
async def api_chat_cancel(body: dict):
    tid = str(body.get("task_id", ""))
    run = _active_runs.get(tid)
    if not run or (body.get("run_id") and body["run_id"] != run["id"]):
        return {"ok": True, "cancelled": False}
    run["cancel"].set()
    producer = run["producer"]
    if not producer.done():
        # A second click must not cancel cleanup a second time.
        if not run.get("stopping"):
            run["stopping"] = True
            producer.cancel()
        await asyncio.wait({producer}, timeout=1.0)
    return {"ok": True, "cancelled": True, "stopping": not producer.done()}


@app.post("/api/chat")
async def api_chat(body: ChatBody):
    if body.speed_mode is not None and body.speed_mode not in {"fast", "balanced", "deep"}:
        raise HTTPException(400, "未知速度档位")
    if not db.get_task(body.task_id):
        raise HTTPException(404, "任务不存在")
    if body.task_id in _active_runs:
        raise HTTPException(409, "上一轮正在执行或停止，请稍后接续。")
    if body.provider or body.model:
        patch = {}
        if body.provider: patch["provider"] = body.provider
        if body.model: patch["model"] = body.model
        cfg = config.update(patch)
        db.touch_task(body.task_id, provider=cfg.get("provider"), model=cfg.get("model"))

    queue = asyncio.Queue()
    run = {"id": body.run_id or uuid.uuid4().hex, "cancel": asyncio.Event()}
    _active_runs[body.task_id] = run
    _cancel_events[body.task_id] = run["cancel"]
    _running_tasks.add(body.task_id)

    def finish_run():
        if run.get("closed"):
            return
        run["closed"] = True
        if _active_runs.get(body.task_id) is run:
            _active_runs.pop(body.task_id, None)
            _cancel_events.pop(body.task_id, None)
            _running_tasks.discard(body.task_id)
        queue.put_nowait({"type": "close"})

    async def produce():
        user_saved = False
        try:
            queue.put_nowait({"type": "open", "task_id": body.task_id})
            attachments = []
            upload_dir = config.UPLOAD_DIR / body.task_id
            for name in body.attachments:
                p = upload_dir / Path(name).name
                if p.exists():
                    queue.put_nowait({"type": "status", "text": "正在读取附件…"})
                    attachments.append(await asyncio.to_thread(parsers.parse_file, p))
            async for ev in agent.run_agent(body.task_id, body.content, attachments, run["cancel"], speed_mode=body.speed_mode):
                if ev.get("type") == "user_saved": user_saved = True
                queue.put_nowait(ev)
        except asyncio.CancelledError:
            if not user_saved:
                # Preserve a prompt cancelled during attachment parsing too.
                db.add_message(body.task_id, "user", body.content,
                    attachments=[{"name": Path(name).name, "kind": "file", "size": 0} for name in body.attachments] or None)
            queue.put_nowait({"type": "cancelled"})
        except Exception as exc:
            note = f"运行暂停：{type(exc).__name__}: {exc}。可补充要求或发送「继续」。"
            db.add_message(body.task_id, "assistant", note)
            queue.put_nowait({"type": "paused", "text": note, "reason": "runtime_error"})
        finally:
            # An old connection must never remove a newly registered run.
            finish_run()

    run["producer"] = asyncio.create_task(produce())
    # Task.cancel before the coroutine's first instruction skips its finally.
    # Release the registry and SSE connection in that race as well.
    def producer_done(task):
        if not run.get("closed") and task.cancelled():
            db.add_message(body.task_id, "user", body.content,
                attachments=[{"name": Path(name).name, "kind": "file", "size": 0} for name in body.attachments] or None)
            queue.put_nowait({"type": "cancelled"})
        finish_run()
    run["producer"].add_done_callback(producer_done)

    async def event_stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10)
                except asyncio.TimeoutError:
                    # Keep the connection alive while a tool works; do not
                    # count heartbeats as meaningful model output.
                    yield ": heartbeat\n\n"
                    continue
                yield _sse({**event, "run_id": run["id"]})
                if event["type"] == "close": break
        finally:
            producer = run["producer"]
            if not producer.done():
                run["cancel"].set()
                if not run.get("stopping"):
                    run["stopping"] = True
                    producer.cancel()
                # Shield cleanup from the client's disconnected response scope.
                try:
                    await asyncio.shield(producer)
                except asyncio.CancelledError:
                    pass

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

@app.get("/api/offline/readiness")
async def api_offline_readiness():
    from .offline_readiness import status
    return await asyncio.to_thread(status)


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


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
