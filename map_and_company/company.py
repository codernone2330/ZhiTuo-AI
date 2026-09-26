"""智拓商机作战助手 · 企查查工商数据接入模块。

为什么需要这个模块
------------------
原 `智拓商机作战助手-完整版.html` 里的“商机”主要靠前端根据客户档案里的
`industry / scale / lastOrderAmount` 等字段做规则打分，并没有真实工商数据支撑，
也缺少合规的“外部线索采集”链路。

本模块用企查查开放平台接口，把“外部企业线索 → 待审核线索 → 商机评分”做成可解释、
可独立运行的真实数据链路：
1. 模糊搜索（关键词 -> 候选企业列表，自动剔除注销/吊销企业）；
2. 工商详情（经营状态 / 注册资本 / 成立日期 / 法定代表人 / 行业 / 参保人数等）；
3. 商机评分（基于工商数据给出 0-100 分、A/B/C/D 等级与风险标记，规则可解释）；
4. 输出既可直接喂给后端 `external_leads` 待审核池，也能在工具台里人工核验。

设计原则（呼应 ZhiTuoNativeServer.py / map_integration.py 的思路）
--------------------------------------------------------------
- 仅使用 Python 标准库（urllib / http.server / json），无需 pip 安装；
- 企查查 AppKey / SecretKey 只在服务端读取和使用，绝不下发到浏览器；
- 跨域（CORS）已开启，原前端（8766）可直接 fetch 本服务（默认 8768）；
- 带内存 + 磁盘缓存，避免重复搜索 / 详情查询产生不必要的计费；
- 不修改任何源文件；本文件自带一个可独立运行的工具台页面。

运行方式
--------
    python company.py                              # 启动工具台：http://127.0.0.1:8768/
    python company.py --search "中国移动"          # 仅做模糊搜索 + 评分
    python company.py --detail "中国移动深圳分公司"  # 仅查工商详情 + 评分

Key 读取顺序（SecretKey 同样只留在服务端）
--------------------------------------
1. 环境变量 QCC_APP_KEY / QCC_SECRET_KEY；
2. 与本文件同目录的 QccAppKey.txt / QccSecret.txt（纯文本，首行即值）。

⚠️ 安全：QccSecret.txt 切勿提交 Git。本仓库 .gitignore 已忽略这两个文件。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
DEFAULT_APPKEY_FILE = ROOT / "QccAppKey.txt"
DEFAULT_SECRET_FILE = ROOT / "QccSecret.txt"

SEARCH_ENDPOINT = "https://api.qichacha.com/FuzzySearch/GetList"
DETAIL_ENDPOINT = "https://api.qichacha.com/ECIV4/GetBasicDetailsByName"

HOST = "127.0.0.1"
PORT = int(os.environ.get("ZHITUO_QCC_PORT", "8768"))
MAX_SEARCH_RESULTS = int(os.environ.get("ZHITUO_QCC_MAX_RESULTS", "20"))
DETAIL_TOP_N = int(os.environ.get("ZHITUO_QCC_DETAIL_TOP_N", "5"))
CACHE_SECONDS = int(os.environ.get("ZHITUO_QCC_CACHE", "600"))
DISK_CACHE_DAYS = int(os.environ.get("ZHITUO_QCC_DISK_CACHE_DAYS", "30"))
REQUEST_TIMEOUT = 20

# 视为“已注销 / 不可用”的经营状态，直接剔除出线索池
_DEAD_STATUSES = ("注销", "吊销", "撤销", "清算", "停业", "吊销已注销")
# 战略 / 高价值行业关键词（命中给商机加分）
_STRATEGIC_INDUSTRY_KEYWORDS = (
    "电信", "通信", "移动", "联通", "运营商", "广电", "政企", "政府", "国企",
    "科技", "信息", "软件", "数据", "人工智能", "互联网", "智能制造", "制造",
    "金融", "银行", "证券", "保险", "能源", "电力", "石油", "医疗", "医药",
    "教育", "物流", "供应链",
)


# --------------------------------------------------------------------------- #
# 缓存（避免重复搜索 / 详情查询产生不必要的计费）
# --------------------------------------------------------------------------- #
_search_cache: dict[str, tuple[float, list[dict[str, Any]] | None]] = {}
_detail_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_cache_lock = threading.Lock()
DISK_CACHE_FILE = REPO / "data" / ".qcc_cache.json"


def _load_disk_cache() -> None:
    try:
        if not DISK_CACHE_FILE.is_file():
            return
        data = json.loads(DISK_CACHE_FILE.read_text(encoding="utf-8"))
        now = time.time()
        max_age = DISK_CACHE_DAYS * 86400
        for key, entry in data.items():
            if not (isinstance(entry, list) and len(entry) == 3):
                continue
            kind, ts, val = entry
            if now - float(ts) > max_age:
                continue
            if kind == "search":
                _search_cache[key] = (float(ts), val)
            elif kind == "detail":
                _detail_cache[key] = (float(ts), val)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass


def _save_disk_cache() -> None:
    try:
        data: dict[str, list[Any]] = {}
        for key, (ts, val) in _search_cache.items():
            data[f"search|{key}"] = ["search", ts, val]
        for key, (ts, val) in _detail_cache.items():
            data[f"detail|{key}"] = ["detail", ts, val]
        DISK_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


_load_disk_cache()


class QccApiError(RuntimeError):
    """企查查服务相关错误；带 code / provider_code / http_status 便于上层映射。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "QCC_PROVIDER_ERROR",
        provider_code: str = "",
        http_status: int = HTTPStatus.BAD_GATEWAY,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_code = provider_code
        self.http_status = http_status


def load_qcc_credentials() -> tuple[str, str]:
    app_key = os.environ.get("QCC_APP_KEY", "").strip()
    secret = os.environ.get("QCC_SECRET_KEY", "").strip()
    if not app_key and DEFAULT_APPKEY_FILE.is_file():
        app_key = _read_first_line(DEFAULT_APPKEY_FILE)
    if not secret and DEFAULT_SECRET_FILE.is_file():
        secret = _read_first_line(DEFAULT_SECRET_FILE)
    if not app_key or not secret:
        raise QccApiError(
            "未找到企查查凭证（QCC_APP_KEY / QCC_SECRET_KEY 环境变量，或同目录 QccAppKey.txt / QccSecret.txt）。",
            code="QCC_NOT_CONFIGURED",
            http_status=HTTPStatus.SERVICE_UNAVAILABLE,
        )
    return app_key, secret


def _read_first_line(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    return text.strip().splitlines()[0].strip() if text.strip() else ""


# 凭证在进程内只读取一次（避免每次请求重复读盘）
QCC_APP_KEY, QCC_SECRET = load_qcc_credentials()


# --------------------------------------------------------------------------- #
# 企查查开放平台底层调用（动态签名 + 限速退避重试）
# --------------------------------------------------------------------------- #
def _build_auth_headers() -> dict[str, str]:
    timespan = str(int(time.time()))
    token = hashlib.md5((QCC_APP_KEY + timespan + QCC_SECRET).encode("utf-8")).hexdigest().upper()
    return {"Accept": "application/json", "Timespan": timespan, "Token": token}


def _qcc_get_json(url: str, retries: int = 3) -> dict[str, Any]:
    """调用企查查接口，对频率限制做退避重试，失败抛出 QccApiError。"""
    last_exc: QccApiError | None = None
    for attempt in range(retries):
        request = urllib.request.Request(url, headers=_build_auth_headers())
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # 401/403 通常是签名或凭证问题
            if exc.code in (401, 403):
                raise QccApiError(
                    "企查查凭证无效或签名错误，请检查 AppKey / SecretKey。",
                    code="QCC_AUTH_FAILED",
                    provider_code=str(exc.code),
                    http_status=HTTPStatus.UNAUTHORIZED,
                ) from exc
            raise QccApiError(f"企查查返回 HTTP {exc.code}。", code="QCC_HTTP_ERROR", provider_code=str(exc.code)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise QccApiError("暂时无法连接企查查服务，请检查网络。", code="QCC_UNREACHABLE") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise QccApiError("企查查返回了无法解析的数据。", code="QCC_BAD_RESPONSE") from exc

        status = str(payload.get("Status") or payload.get("status") or "")
        if status == "200":
            return payload
        message = str(payload.get("Message") or payload.get("message") or "企查查返回未知错误。")
        # 频率限制：退避后重试
        if "频率" in message or "频繁" in message or "qps" in message.lower() or status == "303":
            last_exc = QccApiError(message, code="QCC_RATE_LIMITED", provider_code=status, http_status=HTTPStatus.TOO_MANY_REQUESTS)
            time.sleep(0.6 * (attempt + 1))
            continue
        if status in ("201", "202", "204") or "额度" in message or "余额" in message or "次数" in message:
            raise QccApiError("企查查调用额度不足，请充值或检查接口权限。", code="QCC_QUOTA_EXHAUSTED", http_status=HTTPStatus.PAYMENT_REQUIRED)
        raise QccApiError(message or "企查查未接受请求。", code="QCC_REJECTED", provider_code=status)
    raise last_exc or QccApiError("企查查请求被限速，请稍后重试。", code="QCC_RATE_LIMITED", http_status=HTTPStatus.TOO_MANY_REQUESTS)


# --------------------------------------------------------------------------- #
# 1) 模糊搜索：关键词 -> 候选企业列表
# --------------------------------------------------------------------------- #
def search_companies(term: str, top_n: int | None = None) -> list[dict[str, Any]]:
    """企查查模糊搜索。返回已剔除注销/吊销、按成立时间倒序的标准化企业列表。

    每个元素：{providerKey, name, status, establishedAt, address, creditCode, legalPerson?}
    """
    term = (term or "").strip()
    if not term:
        raise QccApiError("搜索关键词不能为空。", code="INVALID_REQUEST", http_status=HTTPStatus.BAD_REQUEST)

    cache_key = f"{term}|{top_n}"
    now = time.time()
    with _cache_lock:
        cached = _search_cache.get(cache_key)
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1] or []

    params = {"key": QCC_APP_KEY, "searchKey": term, "pageIndex": 1, "pageSize": max(1, min(top_n or MAX_SEARCH_RESULTS, 50))}
    url = f"{SEARCH_ENDPOINT}?{urllib.parse.urlencode(params)}"
    payload = _qcc_get_json(url)

    rows = payload.get("Result") or []
    if isinstance(rows, dict):
        rows = rows.get("Data") or rows.get("Items") or []
    items: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or "").strip()
        status = str(row.get("Status") or "")
        if not name:
            continue
        if any(word in status for word in _DEAD_STATUSES):
            continue  # 注销/吊销企业不进入线索池
        items.append(
            {
                "providerKey": str(row.get("KeyNo") or row.get("CreditCode") or name)[:120],
                "name": name[:200],
                "status": status,
                "establishedAt": str(row.get("StartDate") or row.get("EstablishDate") or ""),
                "address": str(row.get("Address") or "")[:500],
                "creditCode": str(row.get("CreditCode") or row.get("No") or ""),
                "legalPerson": str(row.get("OperName") or row.get("LegalPerson") or ""),
            }
        )
    items.sort(key=lambda r: r["establishedAt"], reverse=True)
    items = items[: (top_n or MAX_SEARCH_RESULTS)]

    with _cache_lock:
        _search_cache[cache_key] = (now, items)
    _save_disk_cache()
    return items


# --------------------------------------------------------------------------- #
# 2) 工商详情：公司名 -> 工商照面信息
# --------------------------------------------------------------------------- #
def _first(*values: Any) -> Any:
    for v in values:
        if v not in (None, "", "None", "null"):
            return v
    return None


def get_company_detail(name: str) -> dict[str, Any] | None:
    """企查查工商详情。返回标准化工商信息；查不到返回 None。"""
    name = (name or "").strip()
    if not name:
        raise QccApiError("公司名称不能为空。", code="INVALID_REQUEST", http_status=HTTPStatus.BAD_REQUEST)

    now = time.time()
    with _cache_lock:
        cached = _detail_cache.get(name)
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]

    params = {"key": QCC_APP_KEY, "keyword": name}
    url = f"{DETAIL_ENDPOINT}?{urllib.parse.urlencode(params)}"
    payload = _qcc_get_json(url)
    result = payload.get("Result") or {}
    if isinstance(result, list):
        result = result[0] if result else {}

    status = str(_first(result.get("Status"), result.get("EntStatus"), result.get("RegStatus"), "") or "")
    if any(word in status for word in _DEAD_STATUSES):
        detail = None  # 注销/吊销企业不入库
    else:
        detail = {
            "providerKey": str(_first(result.get("KeyNo"), result.get("CreditCode"), name) or "")[:120],
            "name": str(_first(result.get("Name"), name) or "")[:200],
            "status": status,
            "creditCode": str(_first(result.get("CreditCode"), result.get("No"), result.get("RegNo")) or ""),
            "legalPerson": str(_first(result.get("LegalPerson"), result.get("OperName"), result.get("FrName")) or ""),
            "establishedAt": str(_first(result.get("EstablishDate"), result.get("StartDate"), result.get("EsDate")) or ""),
            "registeredCapitalRaw": str(_first(result.get("RegistCapi"), result.get("RegCapi"), result.get("RecCap")) or ""),
            "industry": str(_first(result.get("Industry"), result.get("IndustryCode"), result.get("CateCode")) or ""),
            "address": str(_first(result.get("Address"), result.get("RegAddr")) or "")[:500],
            "businessScope": str(_first(result.get("BusinessScope"), result.get("Scope")) or "")[:1000],
            "employees": _coerce_int(_first(result.get("SocialStaffNum"), result.get("Employees"), result.get("EmpNum"))),
            "companyType": str(_first(result.get("CompanyType"), result.get("EntType")) or ""),
        }

    with _cache_lock:
        _detail_cache[name] = (now, detail)
    _save_disk_cache()
    return detail


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 3) 商机评分：工商数据 -> 0-100 分 + 等级 + 风险标记（规则可解释）
# --------------------------------------------------------------------------- #
def _parse_year(established_at: str) -> int | None:
    if not established_at:
        return None
    match = re.search(r"(\d{4})", established_at)
    return int(match.group(1)) if match else None


def _parse_capital_wan(registered_capital_raw: str) -> float | None:
    """把注册资本原始字符串解析为「万元」数值。"""
    if not registered_capital_raw:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", registered_capital_raw)
    if not match:
        return None
    val = float(match.group(1))
    if "亿" in registered_capital_raw:
        val *= 10000
    # 单位若是「元」而非「万」，归一化为万元
    if "万" not in registered_capital_raw and "亿元" not in registered_capital_raw:
        val = val / 10000.0
    return round(val, 2)


def score_company(info: dict[str, Any]) -> dict[str, Any]:
    """对一条工商/搜索信息给出商机评分。

    info 需包含（任意缺失都会从总分中相应扣减并标注 missingFields）：
        status, establishedAt, registeredCapitalRaw(或 registeredCapitalWan),
        industry, employees, legalPerson, creditCode
    返回：{score, level, tier, reasons[], riskFlags[], missingFields[]}
    """
    info = info or {}
    reasons: list[str] = []
    risk_flags: list[str] = []
    missing: list[str] = []
    score = 0

    # —— 经营状态（基础门槛）——
    status = str(info.get("status") or "")
    if not status:
        missing.append("status")
        reasons.append("经营状态缺失，按中性处理")
    elif any(word in status for word in ("存续", "在业", "在营", "正常", "开业")):
        score += 30
        reasons.append(f"经营状态正常（{status}）+30")
    elif any(word in status for word in ("迁入", "迁出", "歇业", "筹建")):
        score += 12
        reasons.append(f"经营状态中性（{status}）+12")
    else:
        score += 4
        reasons.append(f"经营状态偏弱（{status}）+4")

    # —— 成立年限（稳定度）——
    year = _parse_year(str(info.get("establishedAt") or ""))
    if year is None:
        missing.append("establishedAt")
        reasons.append("成立日期缺失")
    else:
        age = max(0, 2026 - year)
        if age >= 3 and age <= 12:
            score += 14
            reasons.append(f"成立 {age} 年，处稳健经营期 +14")
        elif age > 12:
            score += 10
            reasons.append(f"成立 {age} 年，老牌企业 +10")
        elif age >= 1:
            score += 9
            reasons.append(f"成立 {age} 年，成长期 +9")
        else:
            score += 4
            risk_flags.append("成立不足 1 年，新设企业需谨慎核验资质")
            reasons.append("成立不足 1 年 +4")

    # —— 注册资本（规模潜力，边际递减）——
    capital_wan = info.get("registeredCapitalWan")
    if capital_wan is None:
        capital_wan = _parse_capital_wan(str(info.get("registeredCapitalRaw") or ""))
    if capital_wan is None:
        missing.append("registeredCapital")
        reasons.append("注册资本缺失")
    else:
        if capital_wan >= 5000:
            score += 18
            reasons.append(f"注册资本 {capital_wan:g} 万元，规模大 +18")
        elif capital_wan >= 1000:
            score += 14
            reasons.append(f"注册资本 {capital_wan:g} 万元，规模较大 +14")
        elif capital_wan >= 100:
            score += 9
            reasons.append(f"注册资本 {capital_wan:g} 万元，中等规模 +9")
        else:
            score += 4
            reasons.append(f"注册资本 {capital_wan:g} 万元，偏小 +4")
        # 壳公司预警：资本很大但无参保
        employees = info.get("employees")
        if capital_wan >= 1000 and (employees == 0):
            risk_flags.append("注册资本高但参保人数为 0，疑似空壳公司，建议尽调")

    # —— 行业匹配（战略价值）——
    industry = str(info.get("industry") or "")
    if not industry:
        missing.append("industry")
        reasons.append("行业信息缺失")
    elif any(kw in industry for kw in _STRATEGIC_INDUSTRY_KEYWORDS):
        score += 16
        reasons.append(f"行业命中战略方向（{industry}）+16")
    else:
        score += 6
        reasons.append(f"行业为（{industry}），非重点拓展方向 +6")

    # —— 参保人数（真实经营体量）——
    employees = info.get("employees")
    if employees is None:
        missing.append("employees")
        reasons.append("参保人数缺失")
    elif employees >= 200:
        score += 12
        reasons.append(f"参保 {employees} 人，体量较大 +12")
    elif employees >= 50:
        score += 8
        reasons.append(f"参保 {employees} 人，有稳定团队 +8")
    elif employees > 0:
        score += 4
        reasons.append(f"参保 {employees} 人 +4")

    # —— 法人 / 统一信用代码（合规完整度）——
    if str(info.get("legalPerson") or ""):
        score += 3
        reasons.append("法定代表人信息完整 +3")
    else:
        missing.append("legalPerson")
    if str(info.get("creditCode") or ""):
        score += 3
        reasons.append("统一社会信用代码完整 +3")
    else:
        missing.append("creditCode")

    score = max(0, min(100, score))

    if score >= 80:
        level, tier = "A", "高"
    elif score >= 65:
        level, tier = "B", "中高"
    elif score >= 50:
        level, tier = "C", "中"
    else:
        level, tier = "D", "低"

    return {
        "score": score,
        "level": level,
        "tier": tier,
        "reasons": reasons,
        "riskFlags": risk_flags,
        "missingFields": missing,
    }


# --------------------------------------------------------------------------- #
# 对外主接口：搜索 + 评分（待审核线索 + 商机评分）
# --------------------------------------------------------------------------- #
def search_and_score(term: str, detail_top_n: int | None = None) -> dict[str, Any]:
    """关键词搜索，并对 Top-N 拉取工商详情做更精准的评分与风险标记。

    线索结构可直接写入后端 external_leads 待审核池。
    """
    top_n = detail_top_n or DETAIL_TOP_N
    if not (isinstance(term, str) and term.strip()):
        raise QccApiError("搜索关键词不能为空。", code="INVALID_REQUEST", http_status=HTTPStatus.BAD_REQUEST)

    candidates = search_companies(term)
    leads: list[dict[str, Any]] = []
    for cand in candidates:
        # 先基于搜索字段快速评分
        lead = dict(cand)
        lead["score"] = score_company(cand)
        leads.append(lead)

    # 对 Top-N 拉详情，重新精确评分（带缓存，省配额）
    for lead in leads[:top_n]:
        try:
            detail = get_company_detail(lead["name"])
        except QccApiError:
            detail = None
        if detail:
            lead.update(
                {
                    "status": detail.get("status") or lead.get("status"),
                    "creditCode": detail.get("creditCode") or lead.get("creditCode"),
                    "legalPerson": detail.get("legalPerson") or lead.get("legalPerson"),
                    "establishedAt": detail.get("establishedAt") or lead.get("establishedAt"),
                    "registeredCapitalRaw": detail.get("registeredCapitalRaw", ""),
                    "industry": detail.get("industry", ""),
                    "address": detail.get("address") or lead.get("address"),
                    "employees": detail.get("employees"),
                    "businessScope": detail.get("businessScope", ""),
                    "companyType": detail.get("companyType", ""),
                }
            )
            # 用详情重新精确评分（详情字段更全）
            lead["score"] = score_company(
                {
                    "status": detail.get("status"),
                    "establishedAt": detail.get("establishedAt"),
                    "registeredCapitalRaw": detail.get("registeredCapitalRaw"),
                    "industry": detail.get("industry"),
                    "employees": detail.get("employees"),
                    "legalPerson": detail.get("legalPerson"),
                    "creditCode": detail.get("creditCode"),
                }
            )

    # 按评分倒序；并列时新的在前
    leads.sort(key=lambda l: (l["score"]["score"], l.get("establishedAt", "")), reverse=True)
    return {
        "ok": True,
        "provider": "企查查",
        "term": term,
        "total": len(leads),
        "detailEnriched": min(top_n, len(leads)),
        "leads": leads,
    }


# --------------------------------------------------------------------------- #
# HTTP 服务（工具台 + API），CORS 已开启，可供原前端直接调用
# --------------------------------------------------------------------------- #
class QccHandler(SimpleHTTPRequestHandler):
    server_version = "ZhituoQCC/1.0"

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("X-Content-Type-Options", "nosniff")
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
            raise QccApiError("请求长度无效。", code="INVALID_REQUEST", http_status=HTTPStatus.BAD_REQUEST) from exc
        if size <= 0 or size > 256 * 1024:
            raise QccApiError("请求内容为空或过大。", code="INVALID_REQUEST", http_status=HTTPStatus.BAD_REQUEST)
        try:
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QccApiError("请求不是有效的 JSON。", code="INVALID_REQUEST", http_status=HTTPStatus.BAD_REQUEST) from exc
        if not isinstance(payload, dict):
            raise QccApiError("请求格式无效。", code="INVALID_REQUEST", http_status=HTTPStatus.BAD_REQUEST)
        return payload

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "智拓企查查工商数据接入服务",
                    "provider": "企查查",
                    "keyConfigured": bool(QCC_APP_KEY and QCC_SECRET),
                    "endpoints": ["/api/search", "/api/detail"],
                },
            )
            return
        if path == "/":
            self._send_html(HTTPStatus.OK, CONSOLE_HTML)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "NOT_FOUND"})

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/search":
                payload = self._read_json()
                term = str(payload.get("term") or "")
                top_n = payload.get("topN")
                top_n = int(top_n) if isinstance(top_n, int) else None
                result = search_and_score(term, top_n)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/detail":
                payload = self._read_json()
                name = str(payload.get("name") or "")
                detail = get_company_detail(name)
                if not detail:
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": False, "error": "NOT_FOUND", "message": "未查询到该企业的工商信息（可能已注销/吊销）。"},
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "provider": "企查查", "detail": detail, "score": score_company(detail)},
                )
                return
        except QccApiError as exc:
            self._send_json(
                exc.http_status,
                {"ok": False, "error": exc.code, "provider": "企查查", "providerCode": exc.provider_code, "message": str(exc)},
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "NOT_FOUND"})

    def _send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *args: Any) -> None:
        super().log_message(format_string, *args)


# --------------------------------------------------------------------------- #
# 工具台前端页面（企查查搜索 + 商机评分），由本服务在 / 提供
# --------------------------------------------------------------------------- #
CONSOLE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>智拓 · 企查查工商数据工具台</title>
<style>
  :root{--blue:#0085d0;--blue-d:#0067aa;--ink:#10203a;--muted:#5b6b7d;--bg:#f3f7fa}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.6 -apple-system,"Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink)}
  .wrap{max-width:980px;margin:0 auto;padding:22px}
  header{display:flex;align-items:center;gap:12px;margin-bottom:14px}
  .logo{width:38px;height:38px;border-radius:12px;background:linear-gradient(145deg,#00a2e2,var(--blue-d));color:#fff;display:grid;place-items:center;font-weight:800}
  h1{font-size:18px;margin:0}
  .sub{color:var(--muted);font-size:12px}
  .card{background:#fff;border:1px solid #e3eaf0;border-radius:16px;padding:16px 18px;margin-bottom:14px;box-shadow:0 8px 24px rgba(16,24,40,.04)}
  input[type=text]{width:100%;padding:10px;border:1px solid #cfd9e3;border-radius:10px}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:10px}
  button{background:var(--blue);color:#fff;border:0;border-radius:10px;padding:9px 16px;font-size:14px;cursor:pointer}
  button.ghost{background:#eaf4fb;color:var(--blue-d)}
  .hint{color:var(--muted);font-size:12px;margin-top:6px}
  .lead{border-top:1px solid #eef2f6;padding:12px 0}
  .lead:first-child{border-top:0}
  .top{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .name{font-weight:700;font-size:15px}
  .badge{padding:2px 9px;border-radius:999px;color:#fff;font-weight:700;font-size:12px}
  .meta{color:var(--muted);font-size:12px;margin-top:4px}
  .score{font-weight:800;font-size:18px}
  .reasons{margin:8px 0 0;padding-left:18px;color:#3a4a5c;font-size:12px}
  .risk{color:#c0341d;background:#fdecec;border:1px solid #f3c4be;border-radius:10px;padding:8px 10px;margin-top:8px;font-size:12px}
  .err{color:#c0341d;background:#fdecec;border:1px solid #f3c4be;border-radius:10px;padding:10px 12px}
  .ok{color:#3a7d1e}
  code{background:#eef3f7;padding:1px 5px;border-radius:5px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">企</div>
    <div><h1>企查查工商数据工具台</h1><div class="sub">外部线索采集 · 待审核线索 · 商机评分（数据仅服务端调用）</div></div>
  </header>

  <div class="card">
    <b>① 输入关键词搜索企业</b>
    <div class="hint">例如 <code>中国移动</code> <code>华为技术</code> <code>某区政企客户</code></div>
    <input id="term" type="text" placeholder="输入企业名称或关键词" value="中国移动"/>
    <div class="row">
      <input id="topn" type="text" placeholder="拉取详情的 Top-N（默认 5）" style="max-width:220px" value="5"/>
      <button id="go">搜索并评分</button>
      <button id="sample" class="ghost">示例</button>
    </div>
  </div>

  <div id="result"></div>
</div>

<script>
const $=(s)=>document.querySelector(s);
const tierColor=(t)=>({高:"#10b981",中高:"#3b82f6",中:"#f59e0b",低:"#ef4444"}[t]||"#94a3b8");
async function call(path,body){
  const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  return r.json();
}
$("#go").onclick=async ()=>{
  const term=$("#term").value.trim();
  if(!term){ $("#result").innerHTML='<div class="err">请先输入搜索关键词。</div>'; return; }
  const topN=parseInt($("#topn").value,10)||5;
  $("#result").innerHTML='<div class="card">正在调用企查查检索并评分…</div>';
  try{
    const data=await call("/api/search",{term,topN});
    if(!data.ok){ $("#result").innerHTML='<div class="err">'+data.message+'</div>'; return; }
    let html='<div class="card"><b>线索结果</b><div class="hint">共 '+data.total+' 条 · 已对 Top '+data.detailEnriched+' 拉取工商详情精评</div>';
    if(!data.leads.length){ html+='<div class="err">未检索到有效企业（注销/吊销已自动剔除）。</div></div>'; $("#result").innerHTML=html; return; }
    html+='<div style="margin-top:10px">';
    data.leads.forEach((l,i)=>{
      const s=l.score||{score:0,level:"?",tier:"?",reasons:[],riskFlags:[]};
      html+='<div class="lead"><div class="top"><span class="name">'+(i+1)+'. '+l.name+'</span>'
        +'<span class="score" style="color:'+tierColor(s.tier)+'">'+s.score+'分</span>'
        +'<span class="badge" style="background:'+tierColor(s.tier)+'">'+s.level+'·'+s.tier+'</span>'
        +'<span class="meta">'+ (l.status||"") +'</span></div>';
      html+='<div class="meta">成立 '+(l.establishedAt||"未知")+' · 注册资本 '+(l.registeredCapitalRaw||"未知")+' · 行业 '+(l.industry||"未知")+(l.employees!=null?" · 参保 "+l.employees+" 人":"")+'</div>';
      if(s.reasons&&s.reasons.length){ html+='<ul class="reasons">'+s.reasons.map(r=>'<li>'+r+'</li>').join('')+'</ul>'; }
      if(s.riskFlags&&s.riskFlags.length){ html+='<div class="risk">⚠ '+s.riskFlags.join('；')+'</div>'; }
      html+='</div>';
    });
    html+='</div></div>';
    $("#result").innerHTML=html;
  }catch(e){ $("#result").innerHTML='<div class="err">请求失败：'+e+'</div>'; }
};
$("#sample").onclick=()=>$("#term").focus();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# 命令行 / 服务入口
# --------------------------------------------------------------------------- #
def _print_leads(result: dict[str, Any]) -> None:
    print(f"\n企查查线索（{result['term']}）：共 {result['total']} 条，已精评 Top {result['detailEnriched']}")
    for i, l in enumerate(result["leads"], start=1):
        s = l["score"]
        print(f"  {i:>2}. [{s['level']}/{s['tier']}] {s['score']:>3}分  {l['name']}  ({l.get('status','')}, 成立 {l.get('establishedAt','')})")
        for r in s["reasons"][:4]:
            print(f"        - {r}")
        for risk in s["riskFlags"]:
            print(f"        ⚠ {risk}")


def main() -> None:
    parser = argparse.ArgumentParser(description="智拓企查查工商数据接入")
    parser.add_argument("--search", help="模糊搜索关键词并评分")
    parser.add_argument("--detail", help="查询指定公司名的工商详情并评分")
    parser.add_argument("--topn", type=int, default=DETAIL_TOP_N, help="搜索时拉取详情精评的 Top-N")
    parser.add_argument("--serve", action="store_true", help="启动 HTTP 工具台服务（默认行为）")
    args = parser.parse_args()

    if args.detail:
        detail = get_company_detail(args.detail)
        if not detail:
            print("未查询到该企业的工商信息（可能已注销/吊销）。")
            return
        print(json.dumps({"detail": detail, "score": score_company(detail)}, ensure_ascii=False, indent=2))
        return
    if args.search:
        _print_leads(search_and_score(args.search, args.topn))
        return

    server = ThreadingHTTPServer((HOST, PORT), QccHandler)
    print(f"智拓企查查工商数据工具台已启动：http://{HOST}:{PORT}/")
    print("真实数据通道：企查查开放平台（模糊搜索 / 工商详情 / 商机评分）")
    print("原前端可在 http://127.0.0.1:8766/ 直接 fetch 本服务（已开启 CORS）。按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
