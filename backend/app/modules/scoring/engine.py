"""智拓·政企企业评分引擎（纯函数，无 IO、无第三方依赖）。

口径来源：政企客户拜访评分系统 handoff 包
- `01_产品与模型规格.md`（8 特征 / 3 版本 / 锚点自适应 / 分级）
- `03_决策记录与已知问题.md`（三条铁律 + 踩坑档案）
- `code/model_3ver.py`（参考实现）

三条铁律（违反会得到看似正常、实则错误的结果）：
1. 任何归一化分母都不能硬编码 → 一律走 config 分位自适应锚点。
2. 文本特征绝不能混入「标签侧描述文本」→ `方案契合` 的 blob 只含企业自身信息。
3. 「文件重建了」≠「内容是最新的」→ 一切可算的数字动态派生。

公开接口：
    load_config(path=None)                       -> dict
    normalize_company(raw)                       -> dict     # 中英文键 → 规范键
    build_context(companies, config, lock=None)  -> dict     # 锚点 + 批级填充中位数
    score_company(company, config, context, as_of) -> dict
    score_batch(companies, config, lock, as_of)    -> (list[dict], dict)
    self_check(companies, config)                  -> dict
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from . import lines as line_map

# --------------------------------------------------------------------------- #
# 常量与特征顺序
# --------------------------------------------------------------------------- #
FEATURES: list[str] = [
    "真实规模", "资本实力", "方案线价值", "方案契合",
    "数字足迹", "触达可达", "拜访时效", "企业性质",
]

_CONFIG_PATH = Path(__file__).resolve().parent / "model_config.json"

# 中英文键别名 —— 兼容企查查导出列名、规整键名与前端/工商接口传入键名
ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "企业名称"),
    "insured_count": ("insured_count", "参保人数", "SocialStaffNum", "employees"),
    "registered_capital": (
        "registered_capital", "注册资本", "registeredCapitalRaw", "registeredCapitalWan",
    ),
    "established_at": ("established_at", "成立日期", "establishedAt"),
    "industry_major": ("industry_major", "国标行业大类"),
    "industry_mid": ("industry_mid", "企查查行业中类"),
    "entity_type": ("entity_type", "企业(机构)类型", "companyType"),
    "website": ("website", "官网网址"),
    "email": ("email", "邮箱"),
    "other_phone": ("other_phone", "更多电话"),
    "mobile": ("mobile", "有效手机号"),
    "address": ("address", "注册地址"),
    "business_scope": ("business_scope", "经营范围"),
    "profile": ("profile", "企业简介"),
    "scale": ("scale", "企业规模"),
}

_NULLISH = {"", "-", "nan", "none", "null", "/", "无", "--"}


def load_config(path: str | Path | None = None) -> dict:
    """读取 model_config.json（业务参数唯一真源）。"""
    target = Path(path) if path else _CONFIG_PATH
    with open(target, encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------- #
# 基础解析工具
# --------------------------------------------------------------------------- #
def clean(value: Any) -> str:
    """把任意值规整为去空白的字符串；空值/占位符返回空串。"""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in _NULLISH else text


def _to_float(value: Any) -> float | None:
    text = clean(value)
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_capital_wan(value: Any) -> float | None:
    """注册资本 → 万元。含「亿」则 ×10000；单位是「元」则 ÷10000。"""
    text = clean(value)
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    amount = float(match.group(1))
    if "亿" in text:
        amount *= 10000
    elif "万" not in text:
        amount /= 10000.0
    return amount


def parse_date(value: Any) -> date | None:
    """解析多种日期写法，返回 date；无法解析返回 None。"""
    text = clean(value)
    if not text:
        return None
    match = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})?", text)
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2))
    day = int(match.group(3)) if match.group(3) else 1
    try:
        return date(year, month, day)
    except ValueError:
        try:
            return date(year, month, 1)
        except ValueError:
            return None


def months_since(start: date | None, ref: date) -> int | None:
    """整月数（不足一月不进位）。"""
    if start is None:
        return None
    months = (ref.year - start.year) * 12 + (ref.month - start.month)
    if ref.day < start.day:
        months -= 1
    return max(0, months)


def percentile(values: Iterable[float], pct: float) -> float | None:
    """线性插值分位（等价 numpy.percentile 默认方法），纯 Python 实现。"""
    arr = sorted(float(v) for v in values if v is not None)
    if not arr:
        return None
    if len(arr) == 1:
        return arr[0]
    k = (len(arr) - 1) * (pct / 100.0)
    low, high = math.floor(k), math.ceil(k)
    if low == high:
        return arr[int(k)]
    return arr[low] + (arr[high] - arr[low]) * (k - low)


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------- #
# 归一化输入
# --------------------------------------------------------------------------- #
def normalize_company(raw: dict) -> dict:
    """把一行（企查查列名 / 规整键名 / 前端键名）规整为规范键字典。幂等。"""
    data: dict[str, Any] = {}
    for key, names in ALIASES.items():
        for alias in names:
            if alias in raw and raw[alias] not in (None, ""):
                data[key] = raw[alias]
                break
        else:
            data.setdefault(key, None)
    return data


# --------------------------------------------------------------------------- #
# 锚点解析（分位自适应 + 可配置覆盖 + 标定冻结）
# --------------------------------------------------------------------------- #
def resolve_anchor(
    spec: dict, values: list[float], anchor_mode: str, lock_value: float | None
) -> tuple[float, str]:
    """解析单个锚点，返回 (锚点值, 来源说明)。

    优先级（与参考实现一致）：
      1) anchor_mode == "fixed"  → 用 spec.fixed（未设置则报错）
      2) spec.fixed 有值          → 配置覆盖
      3) lock_value 有值          → 复用锁定值（保证同一区域多次运行一致）
      4) 否则                     → 从当前数据算分位
    """
    if anchor_mode == "fixed":
        if spec.get("fixed") is None:
            raise ValueError("anchor_mode=fixed 但 spec.fixed 未设置")
        return float(spec["fixed"]), "fixed(配置指定)"
    if spec.get("fixed") is not None:
        return float(spec["fixed"]), "auto(配置覆盖)"
    if lock_value is not None:
        return float(lock_value), "auto(已锁定)"
    arr = [v for v in values if v is not None]
    if spec.get("scope") == "nonzero":
        nonzero = [v for v in arr if v > 0]
        # 非零样本足够多时才采用 nonzero 口径，避免小样本塌缩
        if len(nonzero) >= 30:
            arr = nonzero
    raw = percentile(arr, float(spec.get("pct", 95)))
    floor = float(spec.get("floor", 0) or 0)
    if raw is None:
        return floor if floor else 1.0, "auto(无数据·用下限)"
    return max(raw, floor), "auto(本次标定)"


def build_context(
    companies: list[dict], config: dict, lock: dict | None = None
) -> dict:
    """构造批级上下文：两个锚点 + 缺失填充值。

    ⚠️ 关键：`资本实力` 的中位数填充必须**批级**计算（整批企业的注册资本中位数），
    不能逐行计算——逐行时单值缺失会退化为 0，违背 missing_policy.cap=neutral 口径。
    """
    lock = lock or {}
    mode = config.get("anchor_mode", "auto")
    policy = config.get("missing_policy", {})

    size_values = [_to_float(c.get("insured_count")) for c in companies]
    cap_values = [parse_capital_wan(c.get("registered_capital")) for c in companies]

    size_anchor, size_source = resolve_anchor(
        config["size_anchor"], [v for v in size_values if v is not None], mode, lock.get("size_anchor")
    )
    cap_anchor, cap_source = resolve_anchor(
        config["cap_anchor"], [v for v in cap_values if v is not None], mode, lock.get("cap_anchor")
    )

    cap_present = [v for v in cap_values if v is not None]
    cap_median = percentile(cap_present, 50)
    cap_fill = cap_median if policy.get("cap") == "neutral" and cap_median is not None else 0.0
    size_fill = 0.0  # missing_policy.size == "zero"

    return {
        "region": config.get("region"),
        "anchor_mode": mode,
        "size_anchor": round(size_anchor, 4),
        "cap_anchor": round(cap_anchor, 4),
        "size_source": size_source,
        "cap_source": cap_source,
        "size_fill": size_fill,
        "cap_fill": round(float(cap_fill), 4) if cap_fill else 0.0,
    }


# --------------------------------------------------------------------------- #
# 8 个特征
# --------------------------------------------------------------------------- #
def _corp_email(email: str, free_domains: tuple[str, ...]) -> bool:
    if not email:
        return False
    for part in re.split(r"[;,，；\s]+", email):
        if "@" in part and not any(domain in part.lower() for domain in free_domains):
            return True
    return False


def _timing_score(months: int | None, config: dict) -> float:
    curve = config.get("timing_curve", {})
    if months is None:
        return float(curve.get("missing", 0.5))
    bands = curve.get("bands", [])
    for band in bands:
        cap = band.get("max")
        if cap is None or months < cap:
            return float(band["score"])
    return float(bands[-1]["score"]) if bands else 0.5


def _type_score(entity_type: str) -> float:
    text = entity_type or ""
    if "港澳台" in text or "外国" in text or "外商" in text:
        return 1.00
    if "法人独资" in text or "集团" in text:
        return 0.75
    if "合伙" in text:
        return 0.30
    if "自然人独资" in text:
        return 0.40
    return 0.50


def _fit_score(company: dict, keywords: str, config: dict) -> float:
    """方案契合 —— blob 只含企业自身信息，绝不含方案线『典型方案』（防 target leakage）。"""
    blob = " ".join(
        clean(company.get(field))
        for field in ("name", "industry_mid", "industry_major", "business_scope", "profile")
    )
    weights = config["fit_weights"]
    gen_keywords = config["generic_tech_keywords"]
    n1 = sum(1 for word in str(keywords).split("|") if word and word in blob)
    n2 = sum(1 for word in gen_keywords if word in blob)
    return (
        min(1.0, n1 / float(weights["domain_saturation"])) * float(weights["domain_weight"])
        + min(1.0, n2 / float(weights["generic_saturation"])) * float(weights["generic_weight"])
    )


def build_features(company: dict, config: dict, context: dict, as_of: date | None = None) -> dict:
    """构造 8 个特征（全部归一到 0-1）。context 由 build_context 产生。"""
    if as_of is None:
        as_of = date.today()

    size_anchor = context["size_anchor"]
    cap_anchor = context["cap_anchor"]

    insured = _to_float(company.get("insured_count"))
    capital = parse_capital_wan(company.get("registered_capital"))
    insured = context["size_fill"] if insured is None else insured
    capital = context["cap_fill"] if capital is None else capital

    x_size = _clip(math.log1p(insured) / math.log1p(size_anchor)) if size_anchor > 0 else 0.0
    x_cap = _clip(math.log1p(capital) / math.log1p(cap_anchor)) if cap_anchor > 0 else 0.0

    line, coefficient, _typical, keywords = line_map.assign_solution_line(
        clean(company.get("industry_major")), clean(company.get("industry_mid"))
    )
    x_line = _clip((coefficient - line_map.LINE_MIN) / (line_map.LINE_MAX - line_map.LINE_MIN))

    x_fit = _fit_score(company, keywords, config)

    digital = config["digital_footprint"]
    free = tuple(config["free_email_domains"])
    x_digital = (
        (1.0 if clean(company.get("website")) else 0.0) * float(digital["website_weight"])
        + (1.0 if _corp_email(clean(company.get("email")), free) else 0.0) * float(digital["corp_email_weight"])
        + (1.0 if clean(company.get("profile")) else 0.0) * float(digital["profile_weight"])
    )

    reach_w = config["reach_weights"]
    address = clean(company.get("address"))
    addr_score = 0.0
    for tier in config.get("region_tiers", {}).get("tiers", []):
        if tier["keyword"] in address:
            addr_score = max(addr_score, float(tier["score"]))
    x_reach = _clip(
        (1.0 if clean(company.get("mobile")) else 0.0) * float(reach_w["mobile"])
        + (1.0 if clean(company.get("email")) else 0.0) * float(reach_w["email"])
        + (1.0 if clean(company.get("other_phone")) else 0.0) * float(reach_w["other_phone"])
        + addr_score
    )

    months = months_since(parse_date(company.get("established_at")), as_of)
    x_timing = _timing_score(months, config)

    x_type = _type_score(clean(company.get("entity_type")))

    return {
        "真实规模": round(x_size, 4),
        "资本实力": round(x_cap, 4),
        "方案线价值": round(x_line, 4),
        "方案契合": round(x_fit, 4),
        "数字足迹": round(x_digital, 4),
        "触达可达": round(x_reach, 4),
        "拜访时效": round(x_timing, 4),
        "企业性质": round(x_type, 4),
        "_raw": {
            "insured_count": insured,
            "capital_wan": capital,
            "months": months,
            "line": line,
            "coefficient": coefficient,
        },
    }


# --------------------------------------------------------------------------- #
# 打分与分级
# --------------------------------------------------------------------------- #
def tier_of(score: float, config: dict) -> str:
    thresholds = config["tier_thresholds"]
    if score >= thresholds["S"]:
        return "S"
    if score >= thresholds["A"]:
        return "A"
    if score >= thresholds["B"]:
        return "B"
    if score >= thresholds["C"]:
        return "C"
    return "D"


def active_versions(config: dict) -> dict:
    """过滤掉以 _ 开头的说明键，并校验权重归一。"""
    versions = {k: v for k, v in config["versions"].items() if not k.startswith("_")}
    for name, weights in versions.items():
        total = sum(weights.values())
        if abs(total - 1) > 1e-9:
            raise ValueError(f"版本 {name} 的权重之和为 {total}，必须为 1")
    return versions


def score_company(company: dict, config: dict, context: dict, as_of: date | None = None) -> dict:
    """对单家企业打分，返回完整结果（含 8 特征明细与三版分数/分级）。"""
    company = normalize_company(company)
    features = build_features(company, config, context, as_of)
    raw = features.pop("_raw")

    scores: dict[str, float] = {}
    tiers: dict[str, str] = {}
    for name, weights in active_versions(config).items():
        total = sum(float(weights[feature]) * features[feature] for feature in FEATURES)
        value = round(total * 100, 1)
        scores[name] = value
        tiers[name] = tier_of(value, config)

    persist_version = config.get("persist_version", "v1-均衡版")
    return {
        "name": clean(company.get("name")),
        "industryMajor": clean(company.get("industry_major")),
        "industryMid": clean(company.get("industry_mid")),
        "entityType": clean(company.get("entity_type")),
        "insuredCount": raw["insured_count"],
        "registeredCapital": clean(company.get("registered_capital")),
        "capitalWan": raw["capital_wan"],
        "establishedAt": clean(company.get("established_at")),
        "foundedMonths": raw["months"],
        "solutionLine": raw["line"],
        "solutionCoefficient": raw["coefficient"],
        "typicalSolution": line_map.LINE_INFO.get(raw["line"], (0, "", ""))[1],
        "scores": scores,
        "tiers": tiers,
        "persistVersion": persist_version,
        "persistScore": int(round(scores.get(persist_version, 0))),
        "persistTier": tiers.get(persist_version, "D"),
        "features": features,
        "mobile": clean(company.get("mobile")),
        "email": clean(company.get("email")),
        "website": clean(company.get("website")),
        "address": clean(company.get("address")),
    }


def score_batch(
    companies: list[dict],
    config: dict | None = None,
    lock: dict | None = None,
    as_of: date | None = None,
    preserve_order: bool = False,
) -> tuple[list[dict], dict]:
    """批量打分。返回 (结果列表, 元信息)。结果带 rank。

    preserve_order=False（默认）时按 persist_version 分数降序返回（用于报告/名单）；
    preserve_order=True 时保持传入顺序（用于需要行对齐的导入/落库场景）。
    """
    config = config or load_config()
    active_versions(config)  # 触发权重归一校验
    normalized = [normalize_company(c) for c in companies]
    context = build_context(normalized, config, lock)
    if as_of is None:
        as_of_value = config.get("as_of")
        if as_of_value:
            as_of = parse_date(as_of_value)
    effective_as_of = as_of or date.today()

    scored = [score_company(n, config, context, effective_as_of) for n in normalized]
    persist_version = config.get("persist_version", "v1-均衡版")
    order = sorted(
        range(len(scored)),
        key=lambda i: scored[i]["scores"].get(persist_version, 0),
        reverse=True,
    )
    for position, index in enumerate(order, start=1):
        scored[index]["rank"] = position
    if not preserve_order:
        scored = [scored[index] for index in order]

    meta = {
        "region": config.get("region"),
        "anchorMode": config.get("anchor_mode"),
        "anchors": {"size": context["size_anchor"], "capital": context["cap_anchor"]},
        "anchorSources": {"size": context["size_source"], "capital": context["cap_source"]},
        "missingPolicy": config.get("missing_policy"),
        "asOf": effective_as_of.isoformat(),
        "total": len(scored),
        "persistVersion": persist_version,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
    }
    return scored, meta


# --------------------------------------------------------------------------- #
# 映射自检（handoff 02 第四节）
# --------------------------------------------------------------------------- #
def self_check(companies: list[dict], config: dict | None = None) -> dict:
    """映射覆盖率 + 兜底桶占比自检，防止行业漏配导致方案线塌缩。"""
    config = config or load_config()
    thresholds = config.get("self_check", {})
    total = len(companies)
    unmapped: dict[str, int] = {}
    fallback = 0
    line_counts: dict[str, int] = {}
    for company in companies:
        major = clean(company.get("industry_major"))
        mid = clean(company.get("industry_mid"))
        if not mid and major and major not in line_map.MAJOR2LINE:
            unmapped[major] = unmapped.get(major, 0) + 1
        line, *_ = line_map.assign_solution_line(major, mid)
        line_counts[line] = line_counts.get(line, 0) + 1
        if line == line_map.FALLBACK_LINE:
            fallback += 1
    unmapped_count = sum(unmapped.values())
    max_unmapped = thresholds.get("max_unmapped_ratio", 0.15)
    max_fallback = thresholds.get("max_fallback_ratio", 0.20)
    return {
        "total": total,
        "unmappedCount": unmapped_count,
        "unmappedRatio": round(unmapped_count / total, 4) if total else 0.0,
        "unmapped": unmapped,
        "fallbackCount": fallback,
        "fallbackRatio": round(fallback / total, 4) if total else 0.0,
        "lineCounts": line_counts,
        "maxUnmappedRatio": max_unmapped,
        "maxFallbackRatio": max_fallback,
        "passed": (
            (unmapped_count / total if total else 0) <= max_unmapped
            and (fallback / total if total else 0) <= max_fallback
        ),
    }
