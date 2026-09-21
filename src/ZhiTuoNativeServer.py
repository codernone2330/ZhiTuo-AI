"""智拓商机作战助手本地服务。

用途：
1. 在 http://127.0.0.1:8766 提供单文件前端；
2. 在服务端调用企查查企业模糊搜索 API（ApiCode 886），避免把 SecretKey 写进 HTML；
3. 对成功请求做短时缓存，避免用户重复点击产生不必要的计费。

凭证读取顺序：
- 环境变量 QCC_APP_KEY / QCC_SECRET_KEY；
- 用户提供的《商企查找API(1).docx》（只在本机读取，不向浏览器返回凭证）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT.parent / "data"
HOST = "127.0.0.1"
PORT = int(os.environ.get("ZHITUO_PORT", "8766"))
DEFAULT_CREDENTIALS_DOCX = Path(
    r"D:\xwechat_files\wxid_ucjc0porus7522_3a65\msg\file\2026-08\商企查找API(1).docx"
)
QCC_ENDPOINT = "https://api.qichacha.com/FuzzySearch/GetList"
QCC_API_CODE = "886"
AI_PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "name": "DeepSeek",
        "endpoint": "https://api.deepseek.com/chat/completions",
        "models": {"deepseek-v4-flash", "deepseek-v4-pro"},
    },
    "qwen": {
        "name": "Qwen · 阿里云百炼",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "models": {"qwen3.8-flash", "qwen3.7-plus", "qwen3.8-max"},
    },
    "glm": {
        "name": "GLM · 智谱 AI",
        "endpoint": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "models": {"glm-4.5-flash", "glm-4.5-air", "glm-4.5"},
    },
    "kimi": {
        "name": "Kimi · Moonshot",
        "endpoint": "https://api.moonshot.cn/v1/chat/completions",
        "models": {"moonshot-v1-auto", "kimi-k2-turbo-preview", "moonshot-v1-128k"},
    },
}
MAX_BODY_BYTES = 64 * 1024
CACHE_SECONDS = 300
RESULT_LIMIT = 5
ACTIVE_STATUS_WORDS = ("存续", "在业", "开业", "正常", "有效")
INACTIVE_STATUS_WORDS = ("注销", "吊销", "撤销", "清算")


@dataclass(frozen=True)
class QccCredentials:
    app_key: str
    secret_key: str
    source: str


class CompanyApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "PROVIDER_ERROR",
        provider_code: str = "",
        http_status: int = HTTPStatus.BAD_GATEWAY,
        action: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_code = provider_code
        self.http_status = http_status
        self.action = action


class AiApiError(RuntimeError):
    def __init__(self, message: str, *, code: str = "AI_PROVIDER_ERROR", http_status: int = HTTPStatus.BAD_GATEWAY) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    return "\n".join(text for text in root.itertext() if text)


def load_qcc_credentials() -> QccCredentials:
    app_key = os.environ.get("QCC_APP_KEY", "").strip()
    secret_key = os.environ.get("QCC_SECRET_KEY", "").strip()
    if app_key and secret_key:
        return QccCredentials(app_key=app_key, secret_key=secret_key, source="environment")

    configured_path = os.environ.get("COMPANY_API_CREDENTIALS_DOCX", "").strip()
    credentials_path = Path(configured_path) if configured_path else DEFAULT_CREDENTIALS_DOCX
    if not credentials_path.is_file():
        raise CompanyApiError(
            "未找到企业数据 API 凭证。",
            code="CREDENTIALS_MISSING",
            http_status=HTTPStatus.SERVICE_UNAVAILABLE,
            action="请设置 QCC_APP_KEY 和 QCC_SECRET_KEY 环境变量后重启服务。",
        )
    try:
        values = re.findall(r"\b[0-9a-fA-F]{32}\b", _docx_text(credentials_path))
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise CompanyApiError(
            "无法读取企业数据 API 凭证文档。",
            code="CREDENTIALS_UNREADABLE",
            http_status=HTTPStatus.SERVICE_UNAVAILABLE,
            action="请检查凭证文档，或改用 QCC_APP_KEY / QCC_SECRET_KEY 环境变量。",
        ) from exc
    if len(values) < 2:
        raise CompanyApiError(
            "凭证文档中缺少完整的企查查 AppKey 或 SecretKey。",
            code="CREDENTIALS_INVALID",
            http_status=HTTPStatus.SERVICE_UNAVAILABLE,
            action="请在企查查开放平台重新生成凭证，并通过环境变量配置。",
        )
    return QccCredentials(app_key=values[0], secret_key=values[1], source="document")


def _qcc_signature(credentials: QccCredentials, timestamp: str) -> str:
    value = credentials.app_key + timestamp + credentials.secret_key
    return hashlib.md5(value.encode("utf-8")).hexdigest().upper()


def _provider_error(status: str, message: str) -> CompanyApiError:
    clean_message = message.strip() or "企业数据提供方返回未知错误。"
    if status == "214" or "未购买" in clean_message:
        return CompanyApiError(
            clean_message,
            code="PROVIDER_NOT_ENTITLED",
            provider_code=status,
            http_status=HTTPStatus.FAILED_DEPENDENCY,
            action="请在企查查开放平台为当前 AppKey 开通“企业模糊搜索（ApiCode 886）”后重试。",
        )
    if "余额" in clean_message or "次数" in clean_message:
        return CompanyApiError(
            clean_message,
            code="PROVIDER_QUOTA_EXHAUSTED",
            provider_code=status,
            http_status=HTTPStatus.FAILED_DEPENDENCY,
            action="请检查企查查套餐余额或剩余调用次数。",
        )
    return CompanyApiError(
        clean_message,
        code="PROVIDER_REJECTED",
        provider_code=status,
        action="请在企查查开放平台检查应用状态、接口权限与 IP 白名单。",
    )


def _is_active_company(status: str) -> bool:
    if any(word in status for word in INACTIVE_STATUS_WORDS):
        return False
    return not status or any(word in status for word in ACTIVE_STATUS_WORDS)


def _normalized_company(item: dict[str, Any]) -> dict[str, str] | None:
    name = str(item.get("Name") or "").strip()
    if not name:
        return None
    status = str(item.get("Status") or "").strip()
    if not _is_active_company(status):
        return None
    return {
        "externalId": str(item.get("KeyNo") or "").strip(),
        "name": name,
        "creditCode": str(item.get("CreditCode") or "").strip(),
        "establishedAt": str(item.get("StartDate") or "").strip(),
        "legalRepresentative": str(item.get("OperName") or "").strip(),
        "businessStatus": status or "状态待核验",
        "registrationNumber": str(item.get("No") or "").strip(),
        "address": str(item.get("Address") or "").strip(),
    }


def qcc_fuzzy_search(search_term: str, page_index: int = 1) -> dict[str, Any]:
    cache_key = f"{search_term}|{page_index}"
    now = time.time()
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and now - cached[0] < CACHE_SECONDS:
            return {**cached[1], "cached": True}

    credentials = load_qcc_credentials()
    timestamp = str(int(now))
    query = urllib.parse.urlencode(
        {"key": credentials.app_key, "searchKey": search_term, "pageIndex": page_index}
    )
    request = urllib.request.Request(
        f"{QCC_ENDPOINT}?{query}",
        headers={
            "Accept": "application/json",
            "Timespan": timestamp,
            "Token": _qcc_signature(credentials, timestamp),
            "User-Agent": "ChinaMobile-Zhituo/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise CompanyApiError(
            f"企查查服务返回 HTTP {exc.code}。",
            code="PROVIDER_HTTP_ERROR",
            provider_code=str(exc.code),
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CompanyApiError(
            "暂时无法连接企查查服务。",
            code="PROVIDER_UNREACHABLE",
            action="请检查本机网络、代理和防火墙设置后重试。",
        ) from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CompanyApiError(
            "企查查返回了无法解析的数据。", code="PROVIDER_BAD_RESPONSE"
        ) from exc

    status = str(payload.get("Status") or payload.get("status") or "")
    message = str(payload.get("Message") or payload.get("message") or "")
    if status != "200":
        raise _provider_error(status, message)

    raw_items = payload.get("Result")
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("Data") or raw_items.get("Items") or []
    if not isinstance(raw_items, list):
        raw_items = []
    companies = []
    for item in raw_items:
        if isinstance(item, dict):
            normalized = _normalized_company(item)
            if normalized:
                companies.append(normalized)

    companies.sort(
        key=lambda company: re.sub(r"\D", "", company.get("establishedAt", ""))[:8],
        reverse=True,
    )
    companies = companies[:RESULT_LIMIT]
    result = {
        "ok": True,
        "realData": True,
        "provider": "企查查",
        "apiCode": QCC_API_CODE,
        "query": search_term,
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "cached": False,
        "credentialSource": credentials.source,
        "resultLimit": RESULT_LIMIT,
        "items": companies,
    }
    with _cache_lock:
        _cache[cache_key] = (now, result)
    return result


def ai_chat(api_key: str, provider: str, model: str, messages: list[dict[str, str]]) -> dict[str, Any]:
    clean_key = str(api_key or "").strip()
    clean_provider = str(provider or "deepseek").strip().lower()
    provider_config = AI_PROVIDERS.get(clean_provider)
    if not provider_config:
        raise AiApiError("不支持所选 AI 平台。", code="INVALID_PROVIDER", http_status=HTTPStatus.BAD_REQUEST)
    provider_name = str(provider_config["name"])
    clean_model = str(model or "").strip()
    if len(clean_key) < 12 or len(clean_key) > 256:
        raise AiApiError(f"{provider_name} API Key 格式无效。", code="INVALID_API_KEY", http_status=HTTPStatus.BAD_REQUEST)
    if clean_model not in provider_config["models"]:
        raise AiApiError(f"{provider_name} 不支持所选模型或模型已下线。", code="INVALID_MODEL", http_status=HTTPStatus.BAD_REQUEST)
    if not isinstance(messages, list) or not messages:
        raise AiApiError("对话消息不能为空。", code="INVALID_MESSAGES", http_status=HTTPStatus.BAD_REQUEST)

    clean_messages: list[dict[str, str]] = []
    total_chars = 0
    for item in messages[-12:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        content = str(item.get("content") or "").strip()
        if role not in {"system", "user", "assistant"} or not content:
            continue
        content = content[:12000 if role == "system" else 4000]
        total_chars += len(content)
        if total_chars > 40000:
            break
        clean_messages.append({"role": role, "content": content})
    if not clean_messages:
        raise AiApiError("没有可发送的有效对话消息。", code="INVALID_MESSAGES", http_status=HTTPStatus.BAD_REQUEST)

    body = json.dumps(
        {"model": clean_model, "messages": clean_messages, "stream": False, "max_tokens": 1200},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        str(provider_config["endpoint"]),
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {clean_key}",
            "User-Agent": "ChinaMobile-Zhituo/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        provider_message = ""
        try:
            error_payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            error_value = error_payload.get("error")
            if isinstance(error_value, dict):
                provider_message = str(error_value.get("message") or error_value.get("msg") or "")
            else:
                provider_message = str(error_value or error_payload.get("message") or error_payload.get("msg") or "")
        except (json.JSONDecodeError, AttributeError, OSError, TypeError):
            provider_message = ""
        if exc.code in {401, 403}:
            raise AiApiError(f"{provider_name} API Key 无效，或该账号没有所选模型权限。", code="AI_AUTH_FAILED", http_status=HTTPStatus.UNAUTHORIZED) from exc
        if exc.code == 429:
            raise AiApiError(f"{provider_name} 请求过于频繁、额度耗尽或账户余额不足。", code="AI_RATE_LIMITED", http_status=HTTPStatus.TOO_MANY_REQUESTS) from exc
        if exc.code == 404:
            raise AiApiError(f"{provider_name} 未找到模型 {clean_model}；请确认 API Key 所属平台与模型选择一致。", code="AI_MODEL_NOT_FOUND", http_status=HTTPStatus.BAD_GATEWAY) from exc
        raise AiApiError(provider_message or f"{provider_name} 服务返回 HTTP {exc.code}。") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AiApiError(f"暂时无法连接 {provider_name}，请检查网络或代理设置后重试。", code="AI_UNREACHABLE") from exc

    try:
        payload = json.loads(raw)
        content = str(payload["choices"][0]["message"]["content"] or "").strip()
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise AiApiError(f"{provider_name} 返回了无法解析的响应。", code="AI_BAD_RESPONSE") from exc
    if not content:
        raise AiApiError(f"{provider_name} 本次没有返回有效内容。", code="AI_EMPTY_RESPONSE")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return {
        "ok": True,
        "provider": provider_name,
        "model": str(payload.get("model") or clean_model),
        "content": content,
        "usage": {
            "promptTokens": int(usage.get("prompt_tokens") or 0),
            "completionTokens": int(usage.get("completion_tokens") or 0),
            "totalTokens": int(usage.get("total_tokens") or 0),
        },
    }


class ZhituoHandler(SimpleHTTPRequestHandler):
    server_version = "ZhituoLocal/1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(DATA_ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise CompanyApiError(
                "请求长度无效。", code="INVALID_REQUEST", http_status=HTTPStatus.BAD_REQUEST
            ) from exc
        if size <= 0 or size > MAX_BODY_BYTES:
            raise CompanyApiError(
                "请求内容为空或过大。", code="INVALID_REQUEST", http_status=HTTPStatus.BAD_REQUEST
            )
        try:
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CompanyApiError(
                "请求不是有效的 JSON。", code="INVALID_REQUEST", http_status=HTTPStatus.BAD_REQUEST
            ) from exc
        if not isinstance(payload, dict):
            raise CompanyApiError(
                "请求格式无效。", code="INVALID_REQUEST", http_status=HTTPStatus.BAD_REQUEST
            )
        return payload

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health":
            try:
                credentials = load_qcc_credentials()
                configured = True
                source = credentials.source
            except CompanyApiError:
                configured = False
                source = "missing"
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "智拓真实企业数据服务",
                    "provider": "企查查",
                    "apiCode": QCC_API_CODE,
                    "resultLimit": RESULT_LIMIT,
                    "configured": configured,
                    "credentialSource": source,
                },
            )
            return
        if path == "/":
            self.path = "/" + urllib.parse.quote("智拓商机作战助手-开发版.html")
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path in {"/api/ai-chat", "/api/deepseek-chat"}:
            try:
                payload = self._read_json()
                provider = str(payload.get("provider") or "deepseek")
                model = str(payload.get("model") or "deepseek-v4-flash")
                if path == "/api/deepseek-chat":
                    provider = "deepseek"
                    if model == "deepseek-chat":
                        model = "deepseek-v4-flash"
                    elif model == "deepseek-reasoner":
                        model = "deepseek-v4-pro"
                self._send_json(
                    HTTPStatus.OK,
                    ai_chat(
                        str(payload.get("apiKey") or ""),
                        provider,
                        model,
                        payload.get("messages") if isinstance(payload.get("messages"), list) else [],
                    ),
                )
            except (AiApiError, CompanyApiError) as exc:
                self._send_json(
                    exc.http_status,
                    {"ok": False, "error": exc.code, "provider": "AI", "message": str(exc)},
                )
            return
        if path != "/api/company-opportunities":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "NOT_FOUND"})
            return
        try:
            payload = self._read_json()
            search_term = re.sub(r"\s+", " ", str(payload.get("searchTerm") or "")).strip()
            if len(search_term) < 2 or len(search_term) > 60:
                raise CompanyApiError(
                    "企业检索词长度应为 2 到 60 个字符。",
                    code="INVALID_SEARCH_TERM",
                    http_status=HTTPStatus.BAD_REQUEST,
                )
            page_index = max(1, min(20, int(payload.get("pageIndex") or 1)))
            self._send_json(HTTPStatus.OK, qcc_fuzzy_search(search_term, page_index))
        except (TypeError, ValueError):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "INVALID_REQUEST", "message": "页码或检索参数无效。"},
            )
        except CompanyApiError as exc:
            self._send_json(
                exc.http_status,
                {
                    "ok": False,
                    "error": exc.code,
                    "provider": "企查查",
                    "apiCode": QCC_API_CODE,
                    "providerCode": exc.provider_code,
                    "message": str(exc),
                    "action": exc.action,
                },
            )

    def log_message(self, format_string: str, *args: Any) -> None:
        # 本地日志只记录路径和状态，不记录上游凭证。
        super().log_message(format_string, *args)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), ZhituoHandler)
    print(f"智拓商机作战助手已启动：http://{HOST}:{PORT}/")
    print("真实企业数据通道：企查查企业模糊搜索（ApiCode 886）")
    print("AI 模型通道：DeepSeek / Qwen / GLM / Kimi")
    print("按 Ctrl+C 停止服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
