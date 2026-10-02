"""模型提供商注册表。

设计原则：
1. 所有厂商统一走 OpenAI 兼容协议（/chat/completions），因此核心调用代码只有一份。
2. base_url 内置，界面上选择厂商即自动填充，无需手动输入。
3. models 是"当前在售推荐清单"；真实可用模型以「拉取模型列表」按钮请求 /models 的结果为准，
   厂商更新模型名后本文件过期也不影响使用。

清单更新：2026-10-01 全量核对（上一版大面积下线：
deepseek-chat / deepseek-reasoner 于 2026-07-24 停用，
moonshot-v1 全系与 kimi-k2.5 于 2026-08-31 停用）。
"""

PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek 深度求索",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-flash"],
        "hint": "旗舰 deepseek-v4-pro；性价比 deepseek-v4-flash；deepseek-flash 为最新 V4.1-Flash，原生支持图片。key 在 platform.deepseek.com 获取",
        "vision": True,
    },
    "moonshot": {
        "label": "Kimi 月之暗面",
        "base_url": "https://api.moonshot.cn/v1",
        "models": [
            "kimi-k3",
            "kimi-k2.7-code-highspeed",
            "kimi-k2.7-code",
            "kimi-k2.6",
        ],
        "hint": "旗舰 kimi-k3（1M 上下文、原生视觉）；写代码用 kimi-k2.7-code-highspeed。key 在 platform.kimi.com 获取",
        "vision": True,
        "alt_base_url": "https://api.moonshot.ai/v1",
    },
    "zhipu": {
        "label": "GLM 智谱",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1"],
        "hint": "旗舰 glm-5.3；glm-5.3-flash 原生多模态（图/视频/文件）而价格低一个数量级。注意路径是 /api/paas/v4",
        "vision": True,
        "alt_base_url": "https://api.z.ai/api/paas/v4",
    },
    "qwen": {
        "label": "通义千问（阿里云百炼）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen3.8-max", "qwen-plus", "qwen-flash", "qwen-long", "qwen-vl-max"],
        "hint": "旗舰 qwen3.8-max；日常 qwen-plus / qwen-flash；超长文本 qwen-long（10M）。base_url 必须带 compatible-mode",
        "vision": True,
    },
    "siliconflow": {
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": [
            "deepseek-ai/DeepSeek-V4-Pro",
            "deepseek-ai/DeepSeek-V4-Flash",
            "zai-org/GLM-5.3",
            "zai-org/GLM-5.3-Flash",
            "moonshotai/Kimi-K2.7-Code",
        ],
        "hint": "聚合平台，模型 ID 带斜杠；上架型号变动频繁，建议点「拉取列表」取实时清单",
        "vision": False,
    },
    "ollama": {
        "label": "Ollama 本地模型",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["qwen3:8b", "qwen3:4b", "llama3.2:3b"],
        "hint": "本地部署、无需真实 Key（随便填即可）；能用哪些取决于本机已 pull 的模型，用 ollama list 查看",
        "vision": False,
    },
    "openai": {
        "label": "OpenAI 官方",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.4"],
        "hint": "需合规网络接入。新版推理模型可能不接受 temperature / max_tokens，程序会自动降级重试",
        "vision": True,
    },
    "custom": {
        "label": "自定义（OpenAI 兼容）",
        "base_url": "",
        "models": [],
        "hint": "填入任意 OpenAI 兼容服务的 base_url 与模型名，例如中转站",
        "vision": True,
    },
}

DEFAULT_PROVIDER = "deepseek"
DEFAULT_MODEL = "deepseek-v4-pro"

# 已下线 / 更名的旧模型 → 当前推荐模型。
# 用于自动迁移历史配置，避免升级后因模型名失效而报 404。
DEPRECATED_MODELS = {
    # DeepSeek：2026-07-24 停用 chat / reasoner
    "deepseek-chat": "deepseek-v4-pro",
    "deepseek-reasoner": "deepseek-v4-pro",
    "deepseek-v3": "deepseek-v4-pro",
    "deepseek-v3.1": "deepseek-v4-pro",
    "deepseek-v3.2": "deepseek-v4-pro",
    "deepseek-ai/DeepSeek-V3": "deepseek-ai/DeepSeek-V4-Pro",
    "deepseek-ai/DeepSeek-V3.2": "deepseek-ai/DeepSeek-V4-Pro",
    # Kimi：2026-08-31 停用 moonshot-v1 全系与 k2.5
    "moonshot-v1-8k": "kimi-k3",
    "moonshot-v1-32k": "kimi-k3",
    "moonshot-v1-128k": "kimi-k3",
    "kimi-k2.5": "kimi-k3",
    "kimi-k2-0711-preview": "kimi-k3",
    "kimi-k2-0905-preview": "kimi-k3",
    "kimi-k2-thinking": "kimi-k3",
    # GLM：4.x 系列
    "glm-4": "glm-5.3",
    "glm-4-plus": "glm-5.3",
    "glm-4.5": "glm-5.3",
    "glm-4.5-air": "glm-5.3-flash",
    "glm-4.5-flash": "glm-5.3-flash",
    "glm-4-flash": "glm-5.3-flash",
    "glm-4.6": "glm-5.3",
    # 通义
    "qwen-max": "qwen3.8-max",
    "qwen3-max": "qwen3.8-max",
    "qwen-turbo": "qwen-flash",
    # OpenAI
    "gpt-4o": "gpt-5.4",
    "gpt-4o-mini": "gpt-5.4-mini",
    "gpt-4.1": "gpt-5.4",
    "gpt-5": "gpt-5.4",
}

# 各家"最推荐"的模型，前端首次进入时优先选它
PREFERRED_MODEL = {
    "deepseek": "deepseek-v4-pro",
    "moonshot": "kimi-k3",
    "zhipu": "glm-5.3",
    "qwen": "qwen3.8-max",
    "siliconflow": "deepseek-ai/DeepSeek-V4-Pro",
    "openai": "gpt-6-sol",
}


def public_registry():
    """给前端用的提供商清单（不含任何密钥）。"""
    out = {}
    for pid, info in PROVIDERS.items():
        out[pid] = {
            "label": info["label"],
            "base_url": info["base_url"],
            "models": list(info.get("models", [])),
            "preferred": PREFERRED_MODEL.get(pid, ""),
            "hint": info.get("hint", ""),
            "vision": bool(info.get("vision", False)),
            "alt_base_url": info.get("alt_base_url", ""),
        }
    return out


def resolve(provider_id: str, override_base_url: str | None = None):
    """返回 (base_url, provider_meta)，override 优先。"""
    info = PROVIDERS.get(provider_id) or PROVIDERS["custom"]
    base = (override_base_url or "").strip() or info["base_url"]
    return base.rstrip("/"), info
