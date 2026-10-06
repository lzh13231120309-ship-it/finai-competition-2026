"""Loopback-only financial extraction workspace with persistent local jobs."""
from pathlib import Path
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import subprocess
import sys
import threading
import uuid

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
JOBS = ROOT / 'local_data' / 'jobs'
JOBS.mkdir(parents=True, exist_ok=True)
POOL = ThreadPoolExecutor(max_workers=1)
LOCK = threading.RLock()
app = FastAPI(title='金融文件提取 · 本地工作台', version='0.1.0')

def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat()

def location(job_id):
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise HTTPException(404, '任务不存在')
    folder = JOBS / job_id
    if not folder.is_dir():
        raise HTTPException(404, '任务不存在')
    return folder

def save_state(folder, state):
    with LOCK:
        p = folder / 'state.json'
        tmp = folder / 'state.tmp'
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, p)

def load_state(folder):
    with LOCK:
        return json.loads((folder / 'state.json').read_text(encoding='utf-8'))

def execute(folder):
    state = load_state(folder)
    state.update(status='running', started_at=now())
    save_state(folder, state)
    worker_python = Path(sys.executable)
    cmd = [str(worker_python), '-X', 'utf8', '-m', 'finance_extract.worker', str(folder / ('input' + state['suffix'])),
           '--output', str(folder / 'result'), '--tier', state['tier'], '--ocr', state['ocr'],
           '--pages', state['pages'], '--display-name', state['filename']]
    try:
        with (folder / 'worker.log').open('w', encoding='utf-8') as log:
            process = subprocess.run(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=3600,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if process.returncode:
            trace_path = folder / 'result' / 'trace.json'
            detail = json.loads(trace_path.read_text(encoding='utf-8')).get('error', '') if trace_path.exists() else ''
            raise RuntimeError(detail or '本地解析未完成，请查看运行日志')
        state.update(status='done', finished_at=now())
    except subprocess.TimeoutExpired:
        state.update(status='failed', error='任务超过一小时，已停止。建议先选择少量页码。', finished_at=now())
    except Exception as exc:
        state.update(status='failed', error=str(exc), finished_at=now())
    save_state(folder, state)

@app.middleware('http')
async def local_access(request: Request, call_next):
    origin = request.headers.get('origin')
    from urllib.parse import urlsplit
    if origin and urlsplit(origin).hostname not in ('127.0.0.1', 'localhost', '::1'):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail': '仅接受本机页面操作'}, status_code=403)
    if request.method in ('POST', 'PUT', 'DELETE', 'PATCH') and request.headers.get('x-finai-local') != '1':
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail': '请从本地工作台提交'}, status_code=403)
    return await call_next(request)

@app.get('/api/health')
def health():
    from importlib.metadata import version
    return {'ok': True, 'local_only': True, 'mineru_version': version('mineru'), 'uploads_to_github': False}

@app.get('/api/jobs')
def jobs():
    states = []
    for path in JOBS.glob('*/state.json'):
        try:
            states.append(load_state(path.parent))
        except (ValueError, OSError):
            continue
    return sorted(states, key=lambda x: x['created_at'], reverse=True)

@app.post('/api/jobs')
async def submit(file: UploadFile = File(...), tier: str = Form('flash'), pages: str = Form(''), ocr: str = Form('txt')):
    if tier not in ('auto', 'flash', 'basic', 'standard', 'advanced') or ocr not in ('txt', 'ocr', 'auto'):
        raise HTTPException(400, '解析选项不正确')
    pages = pages.strip()
    if pages and not re.fullmatch(r'\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*', pages):
        raise HTTPException(400, '页码格式示例：1-3 或 1,4,6-8')
    name = (file.filename or '').replace('\\', '/').split('/')[-1]
    suffix = Path(name).suffix.lower()
    if suffix not in ('.pdf', '.docx', '.xlsx', '.png', '.jpg', '.jpeg','.csv','.tsv','.txt','.md'):
        raise HTTPException(400, '当前接受 PDF、DOCX、XLSX、PNG、JPEG、CSV、TSV及文本')
    if suffix in ('.docx', '.xlsx'):
        ocr, pages = 'auto', ''
        if tier != 'auto':
            tier = 'flash'
    if suffix in ('.png', '.jpg', '.jpeg') and ocr == 'txt' and tier != 'auto':
        raise HTTPException(400, '图片不能选择文字层模式，请选择 OCR')
    job_id = uuid.uuid4().hex
    folder = JOBS / job_id
    folder.mkdir()
    total = 0
    with (folder / ('input' + suffix)).open('wb') as stream:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > 70 * 1024 * 1024:
                # Never delete existing user files; this folder was created for this upload.
                stream.close()
                (folder / ('input' + suffix)).unlink()
                folder.rmdir()
                raise HTTPException(413, '本地试用版单文件上限 70MB')
            stream.write(chunk)
    if total == 0:
        (folder / ('input' + suffix)).unlink()
        folder.rmdir()
        raise HTTPException(400, '文件为空')
    state = {'id': job_id, 'filename': name, 'suffix': suffix, 'bytes': total, 'tier': tier, 'ocr': ocr,
             'pages': pages, 'status': 'queued', 'created_at': now(), 'owner_pid':os.getpid()}
    save_state(folder, state)
    POOL.submit(execute, folder)
    return state

@app.get('/api/jobs/{job_id}')
def job(job_id: str):
    folder = location(job_id)
    state = load_state(folder)
    if state['status'] == 'done':
        state['result'] = json.loads((folder / 'result' / 'financial.json').read_text(encoding='utf-8'))
        reviews = folder / 'reviews.jsonl'
        state['reviews'] = [json.loads(line) for line in reviews.read_text(encoding='utf-8').splitlines()] if reviews.exists() else []
    return state

@app.get('/api/jobs/{job_id}/source')
def source(job_id: str):
    folder = location(job_id)
    state = load_state(folder)
    media = 'application/pdf' if state['suffix'] == '.pdf' else None
    return FileResponse(folder / ('input' + state['suffix']), media_type=media, content_disposition_type='inline')

@app.get('/api/jobs/{job_id}/download/{name}')
def download(job_id: str, name: str):
    folder = location(job_id)
    allowed = {'financial.json', 'fields.csv', 'mineru_middle.json', 'mineru_original.md', 'trace.json'}
    path = folder / 'result' / name if name in allowed else folder / 'worker.log' if name == 'worker.log' else folder / 'reviews.jsonl' if name == 'reviews.jsonl' else None
    if path is None or not path.is_file():
        raise HTTPException(404, '文件暂不可下载')
    return FileResponse(path, filename=name)

class Review(BaseModel):
    field_id: str = Field(max_length=100)
    action: str
    note: str = Field(default='', max_length=2000)
    corrected_value: str | None = Field(default=None, max_length=100)
    corrected_unit: str | None = Field(default=None, max_length=100)
    corrected_period: str | None = Field(default=None, max_length=100)

@app.post('/api/jobs/{job_id}/reviews')
def review(job_id: str, body: Review):
    folder = location(job_id)
    if body.action not in ('confirmed', 'needs_review', 'corrected'):
        raise HTTPException(400, '复核动作无效')
    data = json.loads((folder / 'result' / 'financial.json').read_text(encoding='utf-8')) if (folder / 'result' / 'financial.json').exists() else None
    if not data:
        raise HTTPException(409, '等待提取完成后再复核')
    ids = {f'f{i}' for i in range(len(data['fields']))}
    ids |= {f'e{i}:{key}' for i, e in enumerate(data['events']) for key in e['fields']}
    if body.field_id not in ids:
        raise HTTPException(400, '字段不存在')
    entry = {'timestamp': now(), **body.model_dump()}
    if body.action == 'corrected' and not body.note.strip():
        raise HTTPException(400, '修正须说明原始依据')
    with LOCK:
        with (folder / 'reviews.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    return entry

@app.get('/')
def home():
    return FileResponse(ROOT / 'web' / 'index.html')

app.mount('/static', StaticFiles(directory=ROOT / 'web'), name='static')

def owner_alive(pid):
    """Read process state without sending signals; Windows kill(pid, 0) is unsafe."""
    if not isinstance(pid,int) or pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:
            return ctypes.get_last_error()==5
        code=wintypes.DWORD()
        try:
            return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid,0)
        return True
    except ProcessLookupError:return False
    except PermissionError:return True

# Preserve another live entry's jobs; only interrupted owners are retired.
for previous in JOBS.glob('*/state.json'):
    s = load_state(previous.parent)
    if s.get('status') in ('queued', 'running') and not owner_alive(s.get('owner_pid')):
        s.update(status='failed', error='上次服务退出导致任务中断，请重新提交。')
        save_state(previous.parent, s)
