"""衡知 macOS 桌面版的后台服务入口。

与 start_integrated.py 逻辑相同，区别只有三点：

1. **不打开浏览器**——窗口由桌面应用自己提供；
2. **前台常驻**——桌面应用以子进程方式持有本进程，收到 SIGTERM/SIGINT
   （应用正常退出）时清理自己拉起的 Web 服务与本地推理服务；
3. **父进程看门狗**——万一应用被强制退出或崩溃，SIGTERM 就发不出来了，
   此时靠 ppid 变化察觉并自行清理，避免留下孤儿进程占着 17904 / 17907 端口。

命令行版 start_integrated.py 保持原样，Windows 版不受影响。
"""
from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:17904'
LOGS = ROOT / 'data' / 'logs'
LOGS.mkdir(parents=True, exist_ok=True)

stopping = False
children = []
parent_pid = os.getppid()


def ready():
    try:
        with urllib.request.urlopen(URL + '/finance/api/health', timeout=1) as r:
            d = json.load(r)
        return bool(d.get('local_only') and d.get('mineru_version'))
    except Exception:
        return False


def request_stop(*_):
    """信号处理器只置标志，真正的清理在主循环里做，避免在中断上下文里做事。"""
    global stopping
    stopping = True


def say(text):
    """尽量把消息写出去。应用退出后 stdout 管道已断，写不进去就忽略。"""
    try:
        print(text, flush=True)
    except Exception:
        pass


_streams_silenced = False


def silence_streams():
    """把标准流改到日志文件。

    应用一死，stdout / stderr 那条管道的读端就关了，之后任何写入都会抛
    BrokenPipeError；解释器退出时刷缓冲同样会再抛一次。改到日志文件最省心，
    顺便也让这些收尾信息留在 integrated.log 里可查。可重复调用。
    """
    global _streams_silenced
    if _streams_silenced:
        return
    _streams_silenced = True
    try:
        f = (LOGS / 'integrated.log').open('a', encoding='utf-8')
    except Exception:
        return
    sys.stdout = f
    sys.stderr = f


def watch_parent():
    """看门狗线程：父进程没了就自己收拾干净。

    父进程（桌面应用）一旦死亡，本进程会被系统改挂到 launchd 名下，
    os.getppid() 随之变化——这就是可以依赖的「应用已经没了」的信号。
    每秒查一次，代价可以忽略。
    """
    global stopping
    while not stopping:
        time.sleep(1.0)
        if os.getppid() != parent_pid:
            # 顺序要紧：必须先置位。此刻管道多半已断，打印会抛异常，
            # 要是把它排在置位之前，线程会当场死掉，清理就永远不会发生。
            stopping = True
            silence_streams()   # 先把输出接到日志文件，这条记录才留得住
            say('父进程已退出，正在清理后台服务…')
            return


def kill_pid_file(path):
    try:
        pid = int(path.read_text(encoding='ascii').strip())
    except Exception:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception:
        pass
    finally:
        path.unlink(missing_ok=True)


def cleanup():
    """先停 Web 服务，再停本地推理服务，最后回收直接子进程。"""
    silence_streams()
    kill_pid_file(LOGS / 'integrated.pid')
    time.sleep(0.4)
    kill_pid_file(LOGS / 'reasoning.pid')
    for p in children:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()


def main():
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    threading.Thread(target=watch_parent, daemon=True).start()

    from server.config import load
    cfg = load()
    endpoint = (cfg.get('base_url_overrides') or {}).get(cfg.get('provider'), '')
    if cfg.get('provider') == 'local_finance' or endpoint.rstrip('/') == 'http://127.0.0.1:17907/v1':
        from local_reasoning import ensure_started
        ensure_started()
    if stopping:
        cleanup()
        return

    if not ready():
        env = {**os.environ, 'FINAGENT_DATA_DIR': str(ROOT / 'data')}
        with (LOGS / 'integrated.log').open('a', encoding='utf-8') as log:
            p = subprocess.Popen(
                [sys.executable, '-X', 'utf8', '-m', 'uvicorn', 'server.app:app',
                 '--host', '127.0.0.1', '--port', '17904'],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        children.append(p)
        (LOGS / 'integrated.pid').write_text(str(p.pid), encoding='ascii')
        for _ in range(150):
            if ready() or stopping or p.poll() is not None:
                break
            time.sleep(0.2)

    if not ready():
        say('服务启动未完成，请查看 data/logs/integrated.log。')
        cleanup()
        raise SystemExit(1)

    say('READY')

    # 常驻。两种退出方式都会把 stopping 置位：
    #   正常退出 → 应用发来 SIGTERM；异常退出 → 看门狗发现父进程没了。
    while not stopping:
        time.sleep(0.5)
    cleanup()


if __name__ == '__main__':
    main()
