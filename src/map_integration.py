"""智拓商机作战助手 · 真实地理拜访路线接入模块。

为什么需要这个模块
------------------
原 `智拓商机作战助手-完整版.html` 里的“拜访路线”是按客户档案里的
`province / city / area` 文本做“地理近邻”打分（`geographicProximity`），
并且 `routeMapGeometry` 用的是写死的坐标点（`anchors`），并不是真实地理数据。

本模块用腾讯地图 WebService 接口把“按企业位置生成拜访路线”升级为真实地理：
1. 地理编码（地址 -> 真实经纬度 GCJ-02）；
2. 驾车距离矩阵（站点之间的真实驾车距离 / 耗时）；
3. 以真实距离做最近邻 + 2-opt 路线优化；
4. 用腾讯静态地图接口渲染一张“真实位置 + 真实顺序”的地图（key 只在服务端使用）。

设计原则（呼应原 ZhiTuoNativeServer.py 的思路）
----------------------------------------------
- 仅使用 Python 标准库（urllib / http.server / json），无需 pip 安装；
- API Key 只在服务端读取和使用，绝不下发到浏览器；
- 跨域（CORS）已开启，原前端（8766）可直接 fetch 本服务（默认 8767）；
- 不修改任何源文件；本文件自带一个可独立运行的工具台页面。

运行方式
--------
    python map_integration.py                 # 启动工具台：http://127.0.0.1:8767/
    python map_integration.py --geocode "深圳市南山区科技中一路"   # 仅测地理编码
    python map_integration.py --route stops.json                  # 仅算路线（JSON 文件）

Key 读取顺序
-----------
1. 环境变量 TENCENT_MAP_KEY；
2. 同仓库 data/TenCentApiKey.txt（纯文本，首行即 key）。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
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
DEFAULT_KEY_FILE = REPO / "data" / "TenCentApiKey.txt"

GEO_ENDPOINT = "https://apis.map.qq.com/ws/geocoder/v1/"
DISTANCE_ENDPOINT = "https://apis.map.qq.com/ws/distance/v1/matrix"
STATICMAP_ENDPOINT = "https://apis.map.qq.com/ws/staticmap/v2/"

HOST = "127.0.0.1"
PORT = int(os.environ.get("ZHITUO_MAP_PORT", "8767"))
MAX_STOPS = int(os.environ.get("ZHITUO_MAP_MAX_STOPS", "25"))
CACHE_SECONDS = int(os.environ.get("ZHITUO_MAP_CACHE", "600"))
DISK_CACHE_DAYS = int(os.environ.get("ZHITUO_MAP_DISK_CACHE_DAYS", "30"))
REQUEST_TIMEOUT = 15

# --------------------------------------------------------------------------- #
# 缓存（避免重复点击产生不必要的计费）
# 进程内缓存 + 磁盘缓存：相同地址跨多次运行也复用，省腾讯配额
# --------------------------------------------------------------------------- #
_geo_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_cache_lock = threading.Lock()
GEO_DISK_CACHE_FILE = REPO / "data" / ".tencent_geocode_cache.json"


def _load_disk_cache() -> None:
    try:
        if not GEO_DISK_CACHE_FILE.is_file():
            return
        data = json.loads(GEO_DISK_CACHE_FILE.read_text(encoding="utf-8"))
        now = time.time()
        max_age = DISK_CACHE_DAYS * 86400
        for key, entry in data.items():
            if not (isinstance(entry, list) and len(entry) == 2):
                continue
            ts, geo = entry
            if now - float(ts) > max_age:
                continue
            _geo_cache[key] = (float(ts), geo)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass


def _save_disk_cache() -> None:
    try:
        data = {key: [ts, geo] for key, (ts, geo) in _geo_cache.items()}
        GEO_DISK_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


_load_disk_cache()


class MapApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "MAP_PROVIDER_ERROR",
        provider_code: str = "",
        http_status: int = HTTPStatus.BAD_GATEWAY,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_code = provider_code
        self.http_status = http_status


def load_tencent_key() -> str:
    env_key = os.environ.get("TENCENT_MAP_KEY", "").strip()
    if env_key:
        return env_key
    if DEFAULT_KEY_FILE.is_file():
        text = DEFAULT_KEY_FILE.read_text(encoding="utf-8", errors="replace")
        candidate = text.strip().splitlines()[0].strip() if text.strip() else ""
        if candidate:
            return candidate
    raise MapApiError(
        "未找到腾讯地图 API Key。",
        code="MAP_KEY_MISSING",
        http_status=HTTPStatus.SERVICE_UNAVAILABLE,
    )


# key 在进程内只读取一次即可（避免每次请求重复读盘）。
TENCENT_KEY = load_tencent_key()


# --------------------------------------------------------------------------- #
# 腾讯地图 WebService 底层调用
# --------------------------------------------------------------------------- #
def _tencent_get_json(url: str, retries: int = 3) -> dict[str, Any]:
    """调用腾讯 WebService JSON 接口，自动对 QPS 限速（status 120）做退避重试。"""
    last_exc: MapApiError | None = None
    for attempt in range(retries):
        request = urllib.request.Request(url, headers={"User-Agent": "ChinaMobile-Zhituo-Map/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise MapApiError(f"腾讯地图服务返回 HTTP {exc.code}。", code="MAP_HTTP_ERROR", provider_code=str(exc.code)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MapApiError("暂时无法连接腾讯地图服务，请检查网络。", code="MAP_UNREACHABLE") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MapApiError("腾讯地图返回了无法解析的数据。", code="MAP_BAD_RESPONSE") from exc

        status = payload.get("status")
        if status == 0:
            return payload
        message = str(payload.get("message") or "腾讯地图返回未知错误。")
        lowered = message.lower()
        # 每秒请求量(QPS)限速：退避后重试
        if status == 120 or "每秒请求量" in message or "频率" in message or "qps" in lowered:
            last_exc = MapApiError(
                message,
                code="MAP_RATE_LIMITED",
                provider_code=str(status),
                http_status=HTTPStatus.TOO_MANY_REQUESTS,
            )
            time.sleep(0.6 * (attempt + 1))
            continue
        # 每日调用量配额耗尽
        if status == 121 or "每日调用量" in message or "配额" in message or "调用量" in message:
            raise MapApiError(
                message,
                code="MAP_QUOTA_EXHAUSTED",
                provider_code=str(status),
                http_status=HTTPStatus.TOO_MANY_REQUESTS,
            )
        if "key" in lowered or status in (101, 102, 103, 104, 105, 112, 113):
            raise MapApiError(
                message,
                code="MAP_KEY_INVALID",
                provider_code=str(status),
                http_status=HTTPStatus.UNAUTHORIZED,
            )
        if status in (310, 311):
            raise MapApiError(
                "地址解析失败，请补充更完整的省/市/区信息。",
                code="MAP_GEOCODE_FAILED",
                provider_code=str(status),
                http_status=HTTPStatus.BAD_REQUEST,
            )
        raise MapApiError(message, code="MAP_PROVIDER_REJECTED", provider_code=str(status))
    raise last_exc or MapApiError("腾讯地图请求被限速，请稍后重试。", code="MAP_RATE_LIMITED", http_status=HTTPStatus.TOO_MANY_REQUESTS)


# --------------------------------------------------------------------------- #
# 1) 地理编码：地址 -> 真实经纬度（GCJ-02）
# --------------------------------------------------------------------------- #
def _build_geocode_query(item: dict[str, Any]) -> tuple[str, str]:
    """拼接一个干净的、便于地理编码的地址字符串。

    企查查导入的客户 address 通常已经带市/区；种子客户 address 可能只带园区名。
    这里做“缺哪补哪”，避免重复堆叠省市区。
    """
    address = str(item.get("address") or "").strip()
    city = str(item.get("city") or "").strip()
    province = str(item.get("province") or "").strip()
    region = city or province or ""

    if not address:
        address = str(item.get("area") or "").strip()
    if city and city not in address:
        address = city + address
    if province and province not in address:
        address = province + address
    if not address and item.get("name"):
        address = str(item["name"])
    return address, region


def geocode_address(address: str, region: str = "") -> dict[str, Any] | None:
    cache_key = f"{region}|{address}"
    now = time.time()
    with _cache_lock:
        cached = _geo_cache.get(cache_key)
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]
        # 磁盘缓存不限进程内 TTL，命中即复用（省配额）
        if cache_key in _geo_cache:
            return _geo_cache[cache_key][1]

    params = {"address": address, "key": TENCENT_KEY, "output": "json"}
    if region:
        params["region"] = region
    url = f"{GEO_ENDPOINT}?{urllib.parse.urlencode(params)}"
    payload = _tencent_get_json(url)
    result = payload.get("result") or {}
    loc = result.get("location") or {}
    if "lat" not in loc or "lng" not in loc:
        with _cache_lock:
            _geo_cache[cache_key] = (now, None)
        _save_disk_cache()
        return None

    geo = {
        "lat": float(loc["lat"]),
        "lng": float(loc["lng"]),
        "reliability": result.get("reliability"),
        "title": result.get("title", ""),
    }
    with _cache_lock:
        _geo_cache[cache_key] = (now, geo)
    _save_disk_cache()
    return geo


def geocode_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量地理编码，返回与输入等长的列表，每个元素带 lat/lng 与 ok 标记。"""
    out: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        address, region = _build_geocode_query(item)
        geo = geocode_address(address, region)
        out.append(
            {
                "index": index,
                "id": item.get("id") or str(index),
                "name": item.get("name") or f"站点{index + 1}",
                "query": address,
                "address": str(item.get("address") or ""),
                "ok": geo is not None,
                "lat": geo["lat"] if geo else None,
                "lng": geo["lng"] if geo else None,
                "reliability": geo.get("reliability") if geo else None,
                "title": geo.get("title", "") if geo else "",
            }
        )
        # 腾讯地理编码 QPS 较严，每次实际调用后稍作间隔，避免突发
        if index < len(items) - 1:
            time.sleep(0.25)
    return out


# --------------------------------------------------------------------------- #
# 2) 驾车距离矩阵：站点之间的真实距离 / 耗时
# --------------------------------------------------------------------------- #
def _numeric(value: Any) -> int | float | None:
    """兼容腾讯距离矩阵两种返回格式：{'value': 123} 或直接 123。"""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("value")
    return value if isinstance(value, (int, float)) else None


def driving_distance_matrix(
    from_pt: dict[str, Any], to_pts: list[dict[str, Any]], mode: str = "driving"
) -> list[dict[str, Any]]:
    """调用腾讯距离矩阵接口：一个起点 -> 多个终点。

    返回与 to_pts 等长，元素为 {distanceMeters, durationSeconds}（失败为 None）。
    """
    if not to_pts:
        return []
    frm = f"{from_pt['lat']},{from_pt['lng']}"
    to = ";".join(f"{p['lat']},{p['lng']}" for p in to_pts)
    params = {"mode": mode, "from": frm, "to": to, "key": TENCENT_KEY}
    url = f"{DISTANCE_ENDPOINT}?{urllib.parse.urlencode(params)}"
    payload = _tencent_get_json(url)
    rows = (payload.get("result") or {}).get("rows") or []
    elements = (rows[0].get("elements") if rows else []) or []

    out: list[dict[str, Any]] = []
    for el in elements:
        dist = _numeric(el.get("distance"))
        dur = _numeric(el.get("duration"))
        out.append(
            {
                "distanceMeters": int(dist) if dist is not None else None,
                "durationSeconds": int(dur) if dur is not None else None,
            }
        )
    # 补足缺失项
    while len(out) < len(to_pts):
        out.append({"distanceMeters": None, "durationSeconds": None})
    return out


def driving_distance_full_matrix(
    nodes: list[dict[str, Any]], mode: str = "driving"
) -> list[list[dict[str, Any] | None]]:
    """调用腾讯距离矩阵 /matrix：一次请求拿到所有节点之间的 N×N 距离/耗时。

    返回 matrix[i][j] = {distanceMeters, durationSeconds}，i/j 与 nodes 下标对应。
    """
    n = len(nodes)
    if n == 0:
        return []
    frm = ";".join(f"{nodes[i]['lat']},{nodes[i]['lng']}" for i in range(n))
    to = ";".join(f"{nodes[i]['lat']},{nodes[i]['lng']}" for i in range(n))
    params = {"mode": mode, "from": frm, "to": to, "key": TENCENT_KEY}
    url = f"{DISTANCE_ENDPOINT}?{urllib.parse.urlencode(params)}"
    payload = _tencent_get_json(url)
    rows = (payload.get("result") or {}).get("rows") or []

    matrix: list[list[dict[str, Any] | None]] = [[None] * n for _ in range(n)]
    for i, row in enumerate(rows):
        if i >= n:
            continue
        elements = row.get("elements") or []
        for j, el in enumerate(elements):
            if j >= n:
                continue
            dist = _numeric(el.get("distance"))
            dur = _numeric(el.get("duration"))
            matrix[i][j] = {
                "distanceMeters": int(dist) if dist is not None else None,
                "durationSeconds": int(dur) if dur is not None else None,
            }
    return matrix


# --------------------------------------------------------------------------- #
# 3) 路线优化（最近邻 + 2-opt，基于真实距离）
# --------------------------------------------------------------------------- #
def _leg(matrix: list[list[dict[str, Any] | None]], i: int, j: int) -> float:
    cell = matrix[i][j]
    if not cell or cell.get("distanceMeters") is None:
        return float("inf")
    return float(cell["distanceMeters"])


def _route_cost(matrix: list[list[dict[str, Any] | None]], route: list[int]) -> float:
    return sum(_leg(matrix, route[k], route[k + 1]) for k in range(len(route) - 1))


def _optimize(matrix: list[list[dict[str, Any] | None]], customer_indices: list[int], origin: int) -> list[int]:
    unvisited = set(customer_indices)
    unvisited.discard(origin)
    route = [origin]
    current = origin
    # 最近邻
    while unvisited:
        nxt = min(unvisited, key=lambda j: _leg(matrix, current, j))
        route.append(nxt)
        unvisited.discard(nxt)
        current = nxt
    # 2-opt 微调（起点固定）
    improved = True
    while improved:
        improved = False
        for i in range(1, len(route) - 1):
            for k in range(i + 1, len(route) - 1):
                candidate = route[:i] + route[i : k + 1][::-1] + route[k + 1 :]
                if _route_cost(matrix, candidate) + 1e-9 < _route_cost(matrix, route):
                    route = candidate
                    improved = True
    return route


# --------------------------------------------------------------------------- #
# 对外主接口：生成真实拜访路线
# --------------------------------------------------------------------------- #
def plan_visit_route(
    items: list[dict[str, Any]],
    start: dict[str, Any] | None = None,
    mode: str = "driving",
) -> dict[str, Any]:
    """根据企业位置生成真实拜访路线。

    items: [{id?, name?, address, province?, city?, area?}, ...]
    start: 可选出发地 {lat, lng, name?}，如销售员当前位置 / 营业部
    返回: {ok, mode, stops:[真实顺序], totalDistanceKm, totalDurationMin, unresolved:[解析失败]}
    """
    if not isinstance(items, list) or not items:
        raise MapApiError("站点列表为空。", code="INVALID_REQUEST", http_status=HTTPStatus.BAD_REQUEST)
    if len(items) > MAX_STOPS:
        raise MapApiError(
            f"单次最多规划 {MAX_STOPS} 个站点。", code="TOO_MANY_STOPS", http_status=HTTPStatus.BAD_REQUEST
        )

    geocoded = geocode_items(items)
    resolved = [g for g in geocoded if g["ok"]]
    unresolved = [{"id": g["id"], "name": g["name"], "query": g["query"]} for g in geocoded if not g["ok"]]
    if not resolved:
        raise MapApiError(
            "所有地址都无法解析为坐标，请检查地址是否完整。",
            code="GEOCODE_FAILED",
            http_status=HTTPStatus.BAD_REQUEST,
        )

    # 组装节点：出发地(可选) + 已解析客户
    nodes: list[dict[str, Any]] = []
    if start and start.get("lat") is not None and start.get("lng") is not None:
        nodes.append(
            {
                "id": "start",
                "name": str(start.get("name") or "出发地"),
                "lat": float(start["lat"]),
                "lng": float(start["lng"]),
                "isStart": True,
            }
        )
    for g in resolved:
        nodes.append(
            {
                "id": g["id"],
                "name": g["name"],
                "address": g["address"],
                "query": g["query"],
                "lat": g["lat"],
                "lng": g["lng"],
                "reliability": g["reliability"],
                "isStart": False,
            }
        )

    n = len(nodes)
    # 计算全量距离矩阵：每次只传 1 个 from 点，按 from 点计 QPS，避免一次请求扣多个 QPS
    matrix: list[list[dict[str, Any] | None]] = [[None] * n for _ in range(n)]
    for i in range(n):
        to_indices = [j for j in range(n) if j != i]
        dists = driving_distance_matrix(nodes[i], [nodes[j] for j in to_indices], mode)
        for j, d in zip(to_indices, dists):
            matrix[i][j] = d
        if i < n - 1:
            time.sleep(0.6)

    customer_indices = [i for i in range(n) if not nodes[i].get("isStart")]
    origin = 0 if nodes[0].get("isStart") else customer_indices[0]
    order = _optimize(matrix, customer_indices, origin)

    # 组织成有真实距离/耗时的站点
    stops: list[dict[str, Any]] = []
    total_meters = 0
    total_seconds = 0
    cumulative = 0.0
    for seq, idx in enumerate(order, start=1):
        node = nodes[idx]
        prev_idx = order[seq - 2] if seq > 1 else None
        leg = matrix[prev_idx][idx] if prev_idx is not None else None
        leg_km = (leg["distanceMeters"] / 1000.0) if leg and leg.get("distanceMeters") is not None else None
        leg_min = (leg["durationSeconds"] / 60.0) if leg and leg.get("durationSeconds") is not None else None
        if leg_km is not None:
            total_meters += leg["distanceMeters"]
            cumulative += leg_km
        if leg_min is not None:
            total_seconds += leg["durationSeconds"]
        stops.append(
            {
                "sequence": seq,
                "id": node["id"],
                "name": node["name"],
                "address": node.get("address", ""),
                "resolvedAddress": node.get("query", ""),
                "lat": node["lat"],
                "lng": node["lng"],
                "reliability": node.get("reliability"),
                "isStart": bool(node.get("isStart")),
                "legDistanceKm": round(leg_km, 2) if leg_km is not None else None,
                "legDurationMin": round(leg_min, 1) if leg_min is not None else None,
                "cumulativeDistanceKm": round(cumulative, 2),
            }
        )

    return {
        "ok": True,
        "provider": "腾讯地图",
        "mode": mode,
        "realData": True,
        "resolvedCount": len(resolved),
        "unresolvedCount": len(unresolved),
        "unresolved": unresolved,
        "totalDistanceKm": round(total_meters / 1000.0, 2),
        "totalDurationMin": round(total_seconds / 60.0, 1),
        "stops": stops,
        "mapImagePath": "/api/map-image",
    }


# --------------------------------------------------------------------------- #
# 4) 静态地图（真实位置 + 真实顺序），key 仅在服务端拼接与代理
# --------------------------------------------------------------------------- #
def build_static_map_url(points: list[dict[str, Any]]) -> str:
    """points: 有序 [{lat, lng, label, name?, isStart?, isEnd?}]。返回腾讯静态地图 URL（含 key，仅供服务端代理）。

    注意：腾讯静态地图 marker 的 label 仅支持单个字母/数字（A-Z, 0-9），
    不支持中文，因此这里统一用序号数字做标注，靠颜色区分起点/终点/中间站。
    """
    if not points:
        return ""
    center = points[0]
    # size 上限 1024*1024，scale 上限 2；此处取安全值避免被腾讯拒绝返回空图
    parts: list[str] = [
        f"center={center['lat']},{center['lng']}",
        "zoom=12",
        "scale=2",
        "size=640*480",
    ]
    # 路径：穿过所有真实坐标点
    path_pts = ";".join(f"{p['lat']},{p['lng']}" for p in points)
    parts.append("paths=" + urllib.parse.quote(f"color:0x0067aa,weight:6|{path_pts}"))
    # 起点(绿) / 终点(红) / 中间站(蓝)，标注均用序号数字（与前端对照表一致）
    n = len(points)
    for i, p in enumerate(points):
        is_start = bool(p.get("isStart")) or i == 0
        is_end = bool(p.get("isEnd")) or i == n - 1
        if is_start and n > 1:
            color = "0x10b981"  # 绿色：出发地 / 首站
        elif is_end and i > 0:
            color = "0xef4444"  # 红色：最后一站
        else:
            color = "0x3b82f6"  # 蓝色：中间站点
        marker_label = str(p.get("label", i + 1))[:1]  # 仅取首字符，保证单字符且为数字/字母
        parts.append(
            "markers="
            + urllib.parse.quote(f"size:large|color:{color}|label:{marker_label}|{p['lat']},{p['lng']}")
        )
    return f"{STATICMAP_ENDPOINT}?{'&'.join(parts)}&key={TENCENT_KEY}"


def fetch_static_map(points: list[dict[str, Any]]) -> tuple[bytes, str]:
    """代理腾讯静态地图图片（key 不下发浏览器）。对 QPS 限速做退避重试。"""
    url = build_static_map_url(points)
    last_exc: MapApiError | None = None
    for attempt in range(3):
        request = urllib.request.Request(url, headers={"User-Agent": "ChinaMobile-Zhituo-Map/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                content_type = response.headers.get("Content-Type", "image/png")
                # 成功时返回图片；被限速时返回 JSON（含 status 120）
                if "image" in content_type:
                    return response.read(), content_type
                body = response.read().decode("utf-8", errors="replace")
                message = ""
                try:
                    message = str((json.loads(body) or {}).get("message") or "")
                except json.JSONDecodeError:
                    message = body
                if "每秒请求量" in message or "频率" in message:
                    last_exc = MapApiError(message, code="MAP_RATE_LIMITED", http_status=HTTPStatus.TOO_MANY_REQUESTS)
                    time.sleep(0.6 * (attempt + 1))
                    continue
                raise MapApiError(message or "静态地图返回异常。", code="MAP_IMAGE_FAILED")
        except urllib.error.HTTPError as exc:
            raise MapApiError(f"腾讯静态地图返回 HTTP {exc.code}。", code="MAP_IMAGE_FAILED", provider_code=str(exc.code)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MapApiError("静态地图获取失败，请检查网络。", code="MAP_IMAGE_FAILED") from exc
    raise last_exc or MapApiError("静态地图请求被限速，请稍后重试。", code="MAP_RATE_LIMITED", http_status=HTTPStatus.TOO_MANY_REQUESTS)


# --------------------------------------------------------------------------- #
# HTTP 服务（工具台 + API），CORS 已开启，可供原前端直接调用
# --------------------------------------------------------------------------- #
class MapHandler(SimpleHTTPRequestHandler):
    server_version = "ZhituoMap/1.0"

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
            raise MapApiError("请求长度无效。", code="INVALID_REQUEST", http_status=HTTPStatus.BAD_REQUEST) from exc
        if size <= 0 or size > 256 * 1024:
            raise MapApiError("请求内容为空或过大。", code="INVALID_REQUEST", http_status=HTTPStatus.BAD_REQUEST)
        try:
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MapApiError("请求不是有效的 JSON。", code="INVALID_REQUEST", http_status=HTTPStatus.BAD_REQUEST) from exc
        if not isinstance(payload, dict):
            raise MapApiError("请求格式无效。", code="INVALID_REQUEST", http_status=HTTPStatus.BAD_REQUEST)
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
                    "service": "智拓真实地理拜访路线服务",
                    "provider": "腾讯地图",
                    "keyConfigured": bool(TENCENT_KEY),
                    "endpoints": ["/api/geocode", "/api/visit-route", "/api/map-image"],
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
            if path == "/api/geocode":
                payload = self._read_json()
                items = payload.get("items") if isinstance(payload.get("items"), list) else []
                self._send_json(HTTPStatus.OK, {"ok": True, "provider": "腾讯地图", "items": geocode_items(items)})
                return
            if path == "/api/visit-route":
                payload = self._read_json()
                items = payload.get("items") if isinstance(payload.get("items"), list) else []
                start = payload.get("start") if isinstance(payload.get("start"), dict) else None
                mode = str(payload.get("mode") or "driving")
                plan = plan_visit_route(items, start, mode)
                if plan.get("ok") and plan.get("stops"):
                    try:
                        map_points = [
                            {
                                "lat": s["lat"],
                                "lng": s["lng"],
                                "label": str(s["sequence"]),
                                "name": s.get("name", ""),
                                "isStart": bool(s.get("isStart")),
                            }
                            for s in plan["stops"]
                            if s.get("lat") is not None and s.get("lng") is not None
                        ]
                        # 静态地图与前面的距离矩阵共用 QPS，稍作间隔避免突发
                        time.sleep(0.5)
                        img_data, content_type = fetch_static_map(map_points)
                        plan["mapImageBase64"] = (
                            "data:" + content_type + ";base64," + base64.b64encode(img_data).decode("ascii")
                        )
                    except Exception as exc:
                        print("[map] 静态地图获取失败，将无图返回：", exc, flush=True)
                        plan["mapImageBase64"] = None
                self._send_json(HTTPStatus.OK, plan)
                return
            if path == "/api/map-image":
                payload = self._read_json()
                points = payload.get("points") if isinstance(payload.get("points"), list) else []
                data, content_type = fetch_static_map(points)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
        except MapApiError as exc:
            self._send_json(
                exc.http_status,
                {"ok": False, "error": exc.code, "provider": "腾讯地图", "providerCode": exc.provider_code, "message": str(exc)},
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
# 工具台前端页面（真实地图 + 真实路线），由本服务在 / 提供
# --------------------------------------------------------------------------- #
CONSOLE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>智拓 · 真实地理拜访路线工具台</title>
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
  textarea{width:100%;min-height:120px;padding:10px;border:1px solid #cfd9e3;border-radius:10px;resize:vertical;font:13px/1.5 Consolas,Menlo,monospace}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:10px}
  button{background:var(--blue);color:#fff;border:0;border-radius:10px;padding:9px 16px;font-size:14px;cursor:pointer}
  button.ghost{background:#eaf4fb;color:var(--blue-d)}
  input{padding:8px 10px;border:1px solid #cfd9e3;border-radius:10px}
  .hint{color:var(--muted);font-size:12px;margin-top:6px}
  .route{list-style:none;margin:0;padding:0}
  .route li{display:grid;grid-template-columns:34px 1fr auto;gap:10px;align-items:center;padding:9px 0;border-top:1px solid #eef2f6}
  .seq{width:26px;height:26px;border-radius:50%;background:var(--blue);color:#fff;display:grid;place-items:center;font-weight:700;font-size:13px}
  .meta{color:var(--muted);font-size:12px}
  .leg{text-align:right;font-weight:600;color:var(--blue-d);white-space:nowrap}
  img.map{width:100%;border-radius:14px;border:1px solid #e3eaf0;display:block}
  .err{color:#c0341d;background:#fdecec;border:1px solid #f3c4be;border-radius:10px;padding:10px 12px}
  .ok{color:#3a7d1e}
  code{background:#eef3f7;padding:1px 5px;border-radius:5px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">智</div>
    <div><h1>真实地理拜访路线工具台</h1><div class="sub">接入腾讯地图 · 地址转坐标 / 驾车距离 / 路线优化</div></div>
  </header>

  <div class="card">
    <b>① 输入客户站点</b>
    <div class="hint">每行一个：<code>客户名, 省市区详细地址</code>，或粘贴 JSON 数组 <code>[{&quot;name&quot;:&quot;...&quot;,&quot;address&quot;:&quot;...&quot;,&quot;city&quot;:&quot;...&quot;}]</code></div>
    <textarea id="input">中国移动深圳分公司, 广东省深圳市南山区科技中一路
招商银行总行, 广东省深圳市福田区深南大道7088号
腾讯滨海大厦, 广东省深圳市南山区海天二路33号
深圳湾科技生态园, 广东省深圳市南山区高新南九道</textarea>
    <div class="row">
      <input id="start" placeholder="可选出发地：lat,lng（如营业部坐标）" style="flex:1;min-width:240px"/>
      <button id="go">生成真实拜访路线</button>
      <button id="sample" class="ghost">示例</button>
    </div>
  </div>

  <div id="result"></div>
</div>

<script>
const $ = (s)=>document.querySelector(s);
function parseInput(text){
  text=text.trim();
  if(text.startsWith("[")){ try{ return JSON.parse(text); }catch(e){ return null; } }
  return text.split(/\\n|\\r/).map(l=>l.trim()).filter(Boolean).map(l=>{
    const i=l.indexOf(",");
    if(i<0) return {name:l, address:l};
    return {name:l.slice(0,i).trim(), address:l.slice(i+1).trim()};
  });
}
async function call(path, body){
  const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  return r.json();
}
$("#go").onclick=async ()=>{
  const items=parseInput($("#input").value);
  if(!items||!items.length){ $("#result").innerHTML='<div class="err">请先输入客户站点。</div>'; return; }
  let start=null; const s=$("#start").value.trim();
  if(s){ const p=s.split(",").map(Number); if(p.length===2&&!isNaN(p[0])) start={lat:p[0],lng:p[1],name:"出发地"}; }
  $("#result").innerHTML='<div class="card">正在调用腾讯地图计算真实路线…</div>';
  try{
    const plan=await call("/api/visit-route",{items,start,mode:"driving"});
    if(!plan.ok){ $("#result").innerHTML='<div class="err">'+plan.message+'</div>'; return; }
    const pts=plan.stops.map((s,i)=>({lat:s.lat,lng:s.lng,label:i+1,name:s.name,isStart:s.isStart}));
    const total=plan.totalDistanceKm, dur=plan.totalDurationMin;
    let html='<div class="card"><b>路线概览</b><div class="hint">真实驾车总里程 <b class="ok">'+total+' km</b> · 预计耗时 <b class="ok">'+dur+' 分钟</b> · 已解析 '+plan.resolvedCount+' / '+(plan.resolvedCount+plan.unresolvedCount)+' 个站点</div>';
    if(plan.unresolved&&plan.unresolved.length) html+='<div class="err">未解析：'+plan.unresolved.map(u=>u.name).join("、")+'</div>';
    html+='<img class="map" alt="真实拜访路线地图" id="mapimg"/></div>';
    html+='<div class="card"><b>拜访顺序</b><ul class="route">';
    plan.stops.forEach(s=>{
      html+='<li><div class="seq">'+(s.isStart?"起":s.sequence)+'</div><div><div>'+s.name+(s.isStart?"（出发地）":"")+'</div><div class="meta">'+(s.address||s.resolvedAddress||"")+(s.legDistanceKm!=null?" · 距上一站 "+s.legDistanceKm+" km / "+s.legDurationMin+" 分钟":"")+'</div></div><div class="leg">'+(s.legDistanceKm!=null?s.legDistanceKm+" km":"")+'</div></li>';
    });
    html+='</ul></div>';
    $("#result").innerHTML=html;
    // 地图图片：坐标点回传服务端，由服务端用 key 调腾讯静态地图并代理返回（key 不下发浏览器）
    try{
      const r2=await fetch("/api/map-image",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({points:pts})});
      if(r2.ok){ const blob=await r2.blob(); $("#mapimg").src=URL.createObjectURL(blob); }
    }catch(e){ /* 地图失败不阻塞路线结果 */ }
  }catch(e){ $("#result").innerHTML='<div class="err">请求失败：'+e+'</div>'; }
};
$("#sample").onclick=()=>{ /* 示例已预填，点击聚焦即可 */ $("#input").focus(); };
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# 命令行 / 服务入口
# --------------------------------------------------------------------------- #
def _print_plan(plan: dict[str, Any]) -> None:
    print(f"\n真实拜访路线（{plan['mode']}）总里程 {plan['totalDistanceKm']} km，预计 {plan['totalDurationMin']} 分钟")
    for s in plan["stops"]:
        leg = f"距上一站 {s['legDistanceKm']}km/{s['legDurationMin']}min" if s["legDistanceKm"] is not None else "起点"
        print(f"  {s['sequence']:>2}. {s['name']}  ({s['lat']:.5f},{s['lng']:.5f})  {leg}")
    if plan["unresolved"]:
        print("未解析：", ", ".join(u["name"] for u in plan["unresolved"]))


def main() -> None:
    parser = argparse.ArgumentParser(description="智拓真实地理拜访路线接入")
    parser.add_argument("--geocode", help="仅测试地理编码，传入地址字符串")
    parser.add_argument("--route", help="仅计算路线，传入 JSON 文件路径（数组或 {items,start}）")
    parser.add_argument("--serve", action="store_true", help="启动 HTTP 工具台服务（默认行为）")
    args = parser.parse_args()

    if args.geocode:
        geo = geocode_address(args.geocode)
        print(json.dumps(geo, ensure_ascii=False))
        return
    if args.route:
        data = json.loads(Path(args.route).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            plan = plan_visit_route(data.get("items", []), data.get("start"))
        else:
            plan = plan_visit_route(data)
        _print_plan(plan)
        return

    server = ThreadingHTTPServer((HOST, PORT), MapHandler)
    print(f"智拓真实地理拜访路线工具台已启动：http://{HOST}:{PORT}/")
    print("真实地理通道：腾讯地图（地理编码 / 驾车距离 / 静态地图）")
    print("原前端可在 http://127.0.0.1:8766/ 直接 fetch 本服务（已开启 CORS）。按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
