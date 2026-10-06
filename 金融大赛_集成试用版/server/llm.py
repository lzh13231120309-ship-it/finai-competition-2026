"""OpenAI 兼容协议的流式调用客户端。

所有厂商（DeepSeek / Kimi / GLM / 通义 / 硅基流动 / Ollama / 中转站）共用这一份代码，
差异只在 base_url、api_key、model 三个参数，因此界面上切换模型不需要改任何代码。
"""

import json
from typing import Any, AsyncGenerator
from urllib.parse import urlparse

import httpx

# 本机服务（Ollama 等）不能走系统代理，否则会被代理拦掉
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class LLMError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def is_local(base_url: str) -> bool:
    try:
        return (urlparse(base_url).hostname or "") in _LOCAL_HOSTS
    except Exception:
        return False


def make_client(base_url: str, timeout) -> httpx.AsyncClient:
    """本地地址忽略系统代理；云端地址沿用系统代理设置。"""
    return httpx.AsyncClient(timeout=timeout, trust_env=not is_local(base_url))


def _headers(api_key: str) -> dict:
    h = {"Content-Type": "application/json"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


async def list_models(base_url: str, api_key: str, timeout: float = 20.0) -> list[str]:
    """请求 /models 拉取该服务商真实可用的模型列表。"""
    url = base_url.rstrip("/") + "/models"
    async with make_client(base_url, timeout) as client:
        try:
            r = await client.get(url, headers=_headers(api_key))
        except httpx.HTTPError as e:
            raise LLMError(f"无法连接 {url}：{e}") from e
        if r.status_code >= 400:
            raise LLMError(_extract_error(r), r.status_code)
        try:
            data = r.json()
        except Exception:
            raise LLMError("返回内容不是合法 JSON，请检查 base_url 是否正确")
    items = data.get("data") or data.get("models") or []
    ids = []
    for it in items:
        if isinstance(it, str):
            ids.append(it)
        elif isinstance(it, dict):
            mid = it.get("id") or it.get("name")
            if mid:
                ids.append(mid)
    return sorted(set(ids))


def _extract_error(r: httpx.Response) -> str:
    try:
        body = r.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                return err.get("message") or json.dumps(err, ensure_ascii=False)
            if isinstance(err, str):
                return err
            return body.get("message") or json.dumps(body, ensure_ascii=False)[:400]
    except Exception:
        pass
    text = (r.text or "")[:400]
    tips = {
        401: "API Key 无效或未填写",
        403: "无权限访问该模型，请检查 Key 与模型名",
        404: "接口地址或模型名不存在（注意智谱是 /api/paas/v4，通义要带 compatible-mode）",
        429: "请求过于频繁或额度不足",
    }
    tip = tips.get(r.status_code, "")
    return f"HTTP {r.status_code} {tip} {text}".strip()


def _looks_like_param_rejection(e: LLMError) -> bool:
    """判断 400 是否由不支持的请求参数引起（部分推理型模型会拒绝 temperature / max_tokens）。"""
    if e.status != 400:
        return False
    msg = str(e).lower()
    keys = ("max_tokens", "temperature", "unsupported", "not supported",
            "unknown parameter", "unrecognized", "unexpected parameter")
    return any(k in msg for k in keys)


async def _stream_once(
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    payload: dict,
    buffer: dict[int, dict],
) -> AsyncGenerator[dict, None]:
    async with client.stream("POST", url, headers=_headers(api_key), json=payload) as r:
        if r.status_code >= 400:
            await r.aread()
            raise LLMError(_extract_error(r), r.status_code)
        async for line in r.aiter_lines():
            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            if not line.startswith("{"):
                continue
            try:
                chunk = json.loads(line)
            except Exception:
                continue
            for piece in _parse_chunk(chunk, buffer):
                yield piece


async def stream_chat(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.3,
    max_tokens: int | None = None,
    timeout: float = 300.0,
    tool_choice: str | dict = 'auto',
) -> AsyncGenerator[dict, None]:
    """流式对话，逐块 yield 事件字典：
       {"type":"text","text":...}
       {"type":"reasoning","text":...}
       {"type":"tool_calls","tool_calls":[...]}   # 分片累积完成后的完整工具调用
       {"type":"usage","usage":{...}}
    """
    url = base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    # 部分推理型模型（如 OpenAI 新版）不接受 temperature / max_tokens。
    # 先用完整参数请求，若被参数校验拒绝，则去掉这两个参数重试一次。
    variants: list[dict] = [payload]
    if max_tokens:
        variants.append({k: v for k, v in payload.items()
                         if k not in ("temperature", "max_tokens")})

    async with make_client(base_url, httpx.Timeout(timeout, connect=30.0)) as client:
        for attempt, body in enumerate(variants):
            buffer: dict[int, dict] = {}
            emitted = False
            try:
                async for piece in _stream_once(client, url, api_key, body, buffer):
                    emitted = True
                    yield piece
                if buffer:
                    yield {"type": "tool_calls",
                           "tool_calls": [buffer[k] for k in sorted(buffer)]}
                return
            except httpx.HTTPError as e:
                raise LLMError(f"网络请求失败：{e}") from e
            except LLMError as e:
                if not emitted and attempt == 0 and len(variants) > 1 \
                        and _looks_like_param_rejection(e):
                    continue
                raise


def _parse_chunk(chunk: dict, buffer: dict) -> list[dict]:
    out: list[dict] = []
    usage = chunk.get("usage")
    if usage:
        out.append({"type": "usage", "usage": usage})

    choices = chunk.get("choices") or []
    if not choices:
        return out
    delta = choices[0].get("delta") or {}

    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
    if reasoning:
        out.append({"type": "reasoning", "text": reasoning})

    content = delta.get("content")
    if content:
        out.append({"type": "text", "text": content})

    for tc in delta.get("tool_calls") or []:
        idx = tc.get("index", 0)
        slot = buffer.setdefault(idx, {"id": "", "type": "function",
                                       "function": {"name": "", "arguments": ""}})
        if tc.get("id"):
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] = fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]
    return out


async def complete(
    base_url: str, api_key: str, model: str, messages: list[dict],
    temperature: float = 0.2, max_tokens: int = 256, timeout: float = 60.0,
) -> str:
    """非流式一次性生成（用于自动命名任务等小任务）。"""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens, "stream": False}
    lean = {"model": model, "messages": messages, "stream": False}
    last_error: LLMError | None = None

    async with make_client(base_url, timeout) as client:
        for attempt, body in enumerate([payload, lean]):
            try:
                r = await client.post(url, headers=_headers(api_key), json=body)
            except httpx.HTTPError as e:
                raise LLMError(f"网络请求失败：{e}") from e
            if r.status_code >= 400:
                err = LLMError(_extract_error(r), r.status_code)
                if attempt == 0 and _looks_like_param_rejection(err):
                    last_error = err
                    continue
                raise err
            data = r.json()
            try:
                return (data["choices"][0]["message"].get("content") or "").strip()
            except Exception:
                return ""
    if last_error:
        raise last_error
    return ""
