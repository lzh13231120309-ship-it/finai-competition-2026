"""Reproducible official local model download, never a document-upload client."""
from pathlib import Path
import hashlib
import json
import time
from modelscope.hub.file_download import model_file_download

ROOT = Path(__file__).resolve().parent
STAGE = ROOT.parent / '第一阶段_金融提取'
EXPECTED = '7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5'
TARGET = ROOT.parent / 'runtime/models/Qwen3-4B-GGUF'
TARGET.mkdir(parents=True, exist_ok=True)
started = time.time()
path = Path(model_file_download('Qwen/Qwen3-4B-GGUF', 'Qwen3-4B-Q4_K_M.gguf',
    local_dir=str(TARGET), cache_dir=str(ROOT.parent / 'runtime/modelscope-cache')))
digest = hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()
if digest != EXPECTED or path.stat().st_size != 2497280256:
    raise RuntimeError('官方模型指纹不匹配，拒绝启用该文件。')
record = {'repo': 'Qwen/Qwen3-4B-GGUF', 'file': path.name, 'sha256': digest,
    'official_sha256': EXPECTED, 'official_hash_verified': True, 'bytes': path.stat().st_size,
    'download_seconds': round(time.time() - started, 2), 'license': 'Apache-2.0',
    'source': 'https://modelscope.cn/models/Qwen/Qwen3-4B-GGUF',
    'model_card': 'https://huggingface.co/Qwen/Qwen3-4B-GGUF'}
(TARGET / 'source.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
print('模型下载和官方SHA256校验完成。')
