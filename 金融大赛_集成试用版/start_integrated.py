"""Launch the integrated teammate frontend with isolated data and deployed parsing environment."""
from pathlib import Path
import os, subprocess, sys, time, json, urllib.request, webbrowser
root=Path(__file__).resolve().parent
url='http://127.0.0.1:17904'
logs=root/'data/logs'
logs.mkdir(parents=True,exist_ok=True)
# Only start the local reasoning server when the saved configuration selects it.
from server.config import load
cfg = load()
endpoint = (cfg.get("base_url_overrides") or {}).get(cfg.get("provider"), "")
if cfg.get("provider") == "local_finance" or endpoint.rstrip("/") == "http://127.0.0.1:17907/v1":
    from local_reasoning import ensure_started
    ensure_started()
def ready():
    try:
        with urllib.request.urlopen(url+'/finance/api/health',timeout=1) as r:
            d=json.load(r)
        return d.get('local_only') and d.get('mineru_version')
    except Exception:return False
if not ready():
    env={**os.environ,'FINAGENT_DATA_DIR':str(root/'data')}
    with (logs/'integrated.log').open('a',encoding='utf-8') as log:
        p=subprocess.Popen([sys.executable,'-X','utf8','-m','uvicorn','server.app:app','--host','127.0.0.1','--port','17904'],cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    (logs/'integrated.pid').write_text(str(p.pid),encoding='ascii')
    for _ in range(100):
        if ready():break
        if p.poll() is not None:break
        time.sleep(.2)
if not ready():
    print('集成版启动未完成，请查看 data/logs/integrated.log。')
    sys.exit(1)
webbrowser.open(url)
print('衡知已打开，可在输入框上方选择公告解读、财报分析或估值推演；详细结果可下载Word，估值方案可选。')
