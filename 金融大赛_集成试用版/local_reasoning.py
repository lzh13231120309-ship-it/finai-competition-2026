"""Serve the official Qwen model locally using the already installed llama.cpp."""
import json
import os
import re
from pathlib import Path
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
STAGE = ROOT.parent / '第一阶段_金融提取'
MODEL = ROOT.parent / 'runtime/models/Qwen3-4B-GGUF/Qwen3-4B-Q4_K_M.gguf'
URL = 'http://127.0.0.1:17907'
MODEL_NAME = 'finai-qwen3-4b'

def executable_path():
    import sysconfig
    return Path(sysconfig.get_path('purelib')) / 'mineru_llama_cpp' / 'bin' / 'llama-server.exe'


def request(path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(URL + path, timeout=2) as response:
        return json.load(response)


def ready():
    try:
        return any(x.get('id') == MODEL_NAME for x in request('/v1/models').get('data', []))
    except Exception:
        return False


def ensure_started():
    if ready():
        return
    if not MODEL.is_file():
        raise RuntimeError('本地推理模型尚未下载完成。请查看使用说明；当前不能运行真正的Agent。')
    executable = executable_path()
    model = MODEL
    logs = ROOT / 'data' / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    probe = subprocess.run([str(executable), '--list-devices'], cwd=executable.parent,
        capture_output=True, text=True, errors='replace', timeout=15,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    device = re.search(r'^\s*(Vulkan\d+):\s*NVIDIA', probe.stdout, re.M)
    device_args = ['--device', device.group(1)] if device else []
    with (logs / 'reasoning.log').open('a', encoding='utf-8') as stream:
        process = subprocess.Popen([str(executable), '-m', str(model), '--alias', MODEL_NAME,
            '--host', '127.0.0.1', '--port', '17907', '-c', '16384', '-np', '1', '-ngl', '99',
            '--jinja', '--reasoning', 'off', '--reasoning-budget', '0', '--chat-template-kwargs', '{"enable_thinking":false}',
            '--no-context-shift', '--threads', '6', *device_args], cwd=executable.parent,
            stdout=stream, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    (logs / 'reasoning.pid').write_text(str(process.pid), encoding='ascii')
    for _ in range(180):
        if ready():
            return
        if process.poll() is not None:
            break
        time.sleep(.5)
    raise RuntimeError('本地推理模型启动失败或尚未就绪，请查看 data/logs/reasoning.log；未回退到云端。')


if __name__ == '__main__':
    ensure_started()
    print('本地Qwen推理模型已就绪，文件与工具数据仅在本机处理。')
