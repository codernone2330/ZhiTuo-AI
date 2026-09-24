"""Small QCC adapter. Credentials never enter responses or audit records."""

import hashlib
import json
import time
from urllib import error, parse, request

from app.core.config import get_settings
from app.core.exceptions import AppError

ENDPOINT = "https://api.qichacha.com/FuzzySearch/GetList"


def search_companies(term: str) -> list[dict]:
    settings = get_settings()
    if not settings.qcc_app_key or not settings.qcc_secret_key:
        raise AppError("QCC.NOT_CONFIGURED", "请先在服务端配置企查查凭证", 503)
    key = settings.qcc_app_key.get_secret_value()
    secret = settings.qcc_secret_key.get_secret_value()
    stamp = str(int(time.time()))
    token = hashlib.md5((key + stamp + secret).encode("utf-8")).hexdigest().upper()
    url = ENDPOINT + "?" + parse.urlencode({"key": key, "searchKey": term, "pageIndex": 1})
    req = request.Request(
        url, headers={"Accept": "application/json", "Timespan": stamp, "Token": token}
    )
    try:
        with request.urlopen(req, timeout=20) as response:
            body = json.load(response)
    except (error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise AppError("QCC.UNAVAILABLE", "企查查服务暂不可用，请稍后重试", 502) from exc
    if str(body.get("Status") or body.get("status")) != "200":
        raise AppError("QCC.REJECTED", "企查查未接受请求，请检查额度与接口权限", 502)
    rows = body.get("Result") or []
    if isinstance(rows, dict):
        rows = rows.get("Data") or rows.get("Items") or []
    items = []
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or "").strip()
        status = str(item.get("Status") or "")
        if not name or any(word in status for word in ("注销", "吊销", "撤销", "清算")):
            continue
        items.append(
            {
                "providerKey": str(item.get("KeyNo") or item.get("CreditCode") or name)[:120],
                "name": name[:200],
                "establishedAt": str(item.get("StartDate") or ""),
                "address": str(item.get("Address") or "")[:500],
            }
        )
    return sorted(items, key=lambda row: row["establishedAt"], reverse=True)[:20]
