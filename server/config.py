"""配置持久化。

所有配置保存在 data/config.json，API Key 以明文保存于本机（仅本机使用），
保存后立刻 chmod 600，避免同机其他用户读取。
"""

import json
import os
import stat
from pathlib import Path
from threading import RLock

from .providers import DEFAULT_MODEL, DEFAULT_PROVIDER, DEPRECATED_MODELS

ROOT = Path(__file__).resolve().parent.parent
# 数据目录默认在项目内；可用环境变量 FINAGENT_DATA_DIR 指定到别处
DATA_DIR = Path(os.environ.get("FINAGENT_DATA_DIR") or (ROOT / "data")).expanduser()
UPLOAD_DIR = DATA_DIR / "uploads"
SKILLS_DIR = DATA_DIR / "skills"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
LOG_DIR = DATA_DIR / "logs"
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "agent.db"

for _d in (DATA_DIR, UPLOAD_DIR, SKILLS_DIR, KNOWLEDGE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

_lock = RLock()

DEFAULT_CONFIG = {
    "provider": DEFAULT_PROVIDER,
    "model": DEFAULT_MODEL,
    "api_keys": {},
    "base_url_overrides": {},
    "temperature": 0.3,
    "max_tokens": 8192,
    "context_rounds": 20,
    "system_prompt": (
        "你是一个专业、严谨的 AI 智能体助手。请使用中文回答。"
        "涉及数字与计算时，必须使用工具完成计算，不要凭记忆口算。"
        "引用资料时请标明来源文件名。"
    ),
    "theme": "light",
    "auto_title": True,
    # 扩展能力
    "mcp_servers": [],          # [{name, command, args[], env{}, enabled, auto_start}]
    "skills_enabled": {},       # {skill_name: bool}
    "knowledge_enabled": {},    # {kb_name: bool}
    "extra_skill_dirs": [],     # 额外的技能扫描目录
    "extra_knowledge_dirs": [],  # 额外的知识库扫描目录
    "tools_enabled": {
        "calculator": True,
        "knowledge_search": True,
        "skill_loader": True,
        "mcp": True,
        "file_reader": True,
        # 本机操作（读写文件 / 执行命令 / 删除）。关闭后模型完全看不到这些工具。
        "system_access": True,
    },
    "default_output_dir": "~/Desktop/AI产出",
    "agent_max_steps": 50,
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    with _lock:
        if not CONFIG_PATH.exists():
            save(DEFAULT_CONFIG)
            return dict(DEFAULT_CONFIG)
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        merged = _deep_merge(DEFAULT_CONFIG, raw)
        # 历史配置里若残留已下线的模型名（如 deepseek-chat），自动迁移到当前推荐模型
        stale = (merged.get("model") or "").strip()
        replacement = DEPRECATED_MODELS.get(stale)
        if replacement:
            merged["model"] = replacement
            _write(merged)
        return merged


def _write(merged: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def save(cfg: dict) -> dict:
    with _lock:
        merged = _deep_merge(DEFAULT_CONFIG, cfg or {})
        _write(merged)
        return merged


def update(patch: dict) -> dict:
    with _lock:
        return save(_deep_merge(load(), patch or {}))


def get_api_key(provider: str) -> str:
    cfg = load()
    return (cfg.get("api_keys", {}) or {}).get(provider, "") or ""


def set_api_key(provider: str, key: str) -> dict:
    cfg = load()
    keys = dict(cfg.get("api_keys", {}) or {})
    if key:
        keys[provider] = key
    else:
        keys.pop(provider, None)
    return save(_deep_merge(cfg, {"api_keys": keys}))


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:4]}{'•' * 8}{key[-4:]}"
