"""Fixed-provider LLM transport; callers cannot supply arbitrary URLs."""

import json
from urllib import error, request

from app.core.exceptions import AppError

PROVIDERS = {
    "deepseek": "https://api.deepseek.com/chat/completions",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "glm": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    "kimi": "https://api.moonshot.cn/v1/chat/completions",
}
MODELS = {
    "deepseek": {"deepseek-v4-flash", "deepseek-v4-pro", "deepseek-flash"},
    "qwen": {"qwen-plus", "qwen-max", "qwen-turbo", "qwen3.8-flash", "qwen3.7-plus", "qwen3.8-max"},
    "glm": {"glm-4.5-flash", "glm-4.5-air", "glm-4.5"},
    "kimi": {"moonshot-v1-auto", "kimi-k2-turbo-preview", "moonshot-v1-128k"},
}


def ask_provider(provider: str, model: str, api_key: str, messages: list[dict]) -> dict:
    if provider not in PROVIDERS or model not in MODELS[provider]:
        raise AppError("AI.INVALID_MODEL", "平台或模型不受支持，请核对模型标识", 400)
    provider_model = (
        "deepseek-flash" if provider == "deepseek" and model == "deepseek-v4-flash" else model
    )
    payload = json.dumps(
        {"model": provider_model, "messages": messages, "stream": False, "max_tokens": 1200},
        ensure_ascii=False,
    ).encode()
    req = request.Request(
        PROVIDERS[provider],
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
    )
    try:
        with request.urlopen(req, timeout=45) as response:
            result = json.load(response)
        return {
            "content": str(result["choices"][0]["message"]["content"]),
            "usage": result.get("usage") or {},
        }
    except error.HTTPError as exc:
        if exc.code == 400:
            raise AppError("AI.REQUEST_REJECTED", "AI 平台拒绝请求，请检查模型和参数", 502) from exc
        if exc.code in (401, 403):
            raise AppError("AI.AUTH_FAILED", "AI 平台密钥无效或无模型权限", 502) from exc
        if exc.code == 402:
            raise AppError("AI.INSUFFICIENT_BALANCE", "AI 平台账户余额不足", 502) from exc
        if exc.code == 404:
            raise AppError("AI.MODEL_NOT_FOUND", "所选模型在平台上不可用，请检查型号", 502) from exc
        if exc.code == 429:
            raise AppError("AI.RATE_LIMITED", "AI 平台请求过于频繁，请稍后重试", 503) from exc
        if exc.code >= 500:
            raise AppError("AI.UPSTREAM_UNAVAILABLE", "AI 平台暂不可用，请稍后重试", 503) from exc
        raise AppError("AI.PROVIDER_ERROR", f"AI 平台返回 HTTP {exc.code}", 502) from exc
    except (
        error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
    ) as exc:
        raise AppError("AI.UNAVAILABLE", "AI 服务暂不可用", 502) from exc
