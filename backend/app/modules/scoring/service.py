"""企业评分模型 · 服务层（FastAPI / SQLAlchemy 集成）。

职责：
- 锚点锁的**持久化**（DB 表 `scoring_anchor_locks`，按区域一条）——保证同区域多次评分一致。
- 批量评分（供导入路径直接调用）。
- 已入库客户的评分刷新，落库到 `Customer.score` 与 `extra_data["scoreDetail"]`。

引擎（特征/打分/分级）在 `engine.py`，此处只做装配与持久化，不含任何业务参数。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.auth.dependencies import IdentityContext
from app.modules.customers.audit import record_score_event
from app.modules.customers.models import Customer
from app.modules.customers.service import _customer_statement
from app.modules.organizations.models import Organization

from . import engine, lines
from .models import ScoreAnchorLock

# 客户 extra_data 中存放工商档案的键（导入时由 profile 字段写入）
PROFILE_KEY = "profile"
SCORE_DETAIL_KEY = "scoreDetail"


# --------------------------------------------------------------------------- #
# 锚点锁
# --------------------------------------------------------------------------- #
def _peek_lock(session: Session, region: str, config: dict) -> dict | None:
    """只读地取锚点：优先库中锁，其次配置种子锚点。不写库（供 GET 使用）。"""
    row = session.scalar(select(ScoreAnchorLock).where(ScoreAnchorLock.region == region))
    if row is not None:
        return row.as_lock()
    seed = config.get("seed_anchors") or {}
    if seed.get("region") == region and seed.get("size_anchor") and seed.get("cap_anchor"):
        return {
            "size_anchor": float(seed["size_anchor"]),
            "cap_anchor": float(seed["cap_anchor"]),
        }
    return None


def _load_lock(session: Session, region: str, config: dict) -> dict | None:
    """取该区域的锚点锁；若库里没有但配置带了种子锚点（且区域一致），落锁后返回。"""
    row = session.scalar(select(ScoreAnchorLock).where(ScoreAnchorLock.region == region))
    if row is not None:
        return row.as_lock()

    seed = config.get("seed_anchors") or {}
    if seed.get("region") == region and seed.get("size_anchor") and seed.get("cap_anchor"):
        _persist_lock(
            session,
            region,
            anchor_mode=config.get("anchor_mode", "auto"),
            size_anchor=float(seed["size_anchor"]),
            cap_anchor=float(seed["cap_anchor"]),
            size_pct=int(config["size_anchor"].get("pct", 95)),
            cap_pct=int(config["cap_anchor"].get("pct", 95)),
            sample_size=0,
        )
        return {"size_anchor": float(seed["size_anchor"]), "cap_anchor": float(seed["cap_anchor"])}
    return None


def _persist_lock(
    session: Session,
    region: str,
    *,
    anchor_mode: str,
    size_anchor: float,
    cap_anchor: float,
    size_pct: int,
    cap_pct: int,
    sample_size: int,
) -> ScoreAnchorLock:
    row = session.scalar(select(ScoreAnchorLock).where(ScoreAnchorLock.region == region))
    if row is None:
        row = ScoreAnchorLock(id=uuid.uuid4(), region=region)
        session.add(row)
    row.anchor_mode = anchor_mode
    row.size_anchor = float(size_anchor)
    row.cap_anchor = float(cap_anchor)
    row.size_pct = size_pct
    row.cap_pct = cap_pct
    row.sample_size = sample_size
    row.calibrated_at = datetime.now(timezone.utc)
    session.flush()
    return row


# --------------------------------------------------------------------------- #
# 批量评分
# --------------------------------------------------------------------------- #
def score_companies(
    session: Session,
    companies: list[dict],
    *,
    region: str | None = None,
    as_of=None,
    config: dict | None = None,
    persist_lock: bool = True,
    preserve_order: bool = False,
) -> tuple[list[dict], dict]:
    """批量评分。先取（或首次标定并落锁）区域锚点，再交引擎打分。

    preserve_order=True 时结果与传入 companies 顺序一致（用于导入/落库对齐）。
    """
    config = config or engine.load_config()
    region = region or config.get("region", "未知区域")
    lock = _load_lock(session, region, config)

    locked_region_config = dict(config)
    locked_region_config["region"] = region

    if lock is None:
        # 尚无锁：本次标定并落锁
        context = engine.build_context([engine.normalize_company(c) for c in companies], locked_region_config)
        if persist_lock:
            _persist_lock(
                session,
                region,
                anchor_mode=context["anchor_mode"],
                size_anchor=context["size_anchor"],
                cap_anchor=context["cap_anchor"],
                size_pct=int(config["size_anchor"].get("pct", 95)),
                cap_pct=int(config["cap_anchor"].get("pct", 95)),
                sample_size=len(companies),
            )
        lock = {"size_anchor": context["size_anchor"], "cap_anchor": context["cap_anchor"]}

    scored, meta = engine.score_batch(
        companies, locked_region_config, lock, as_of, preserve_order=preserve_order
    )
    meta["region"] = region
    return scored, meta


def _profile_to_company(customer: Customer) -> dict:
    """从客户档案 + extra_data.profile 组装引擎输入。"""
    extra = customer.extra_data or {}
    profile = dict(extra.get(PROFILE_KEY) or {})
    profile.setdefault("name", customer.name)
    if customer.industry and not profile.get("industry_major"):
        profile.setdefault("industry_major", customer.industry)
    if customer.address and not profile.get("address"):
        profile.setdefault("address", customer.address)
    return engine.normalize_company(profile)


def explain_scores(detail: dict) -> list[str]:
    """把评分结果压成客户经理看得懂的 3 条理由。"""
    features = detail.get("features", {})
    top = sorted(features.items(), key=lambda kv: kv[1], reverse=True)[:3]
    labels = {
        "真实规模": "真实规模", "资本实力": "资本实力", "方案线价值": "方案线价值",
        "方案契合": "方案契合", "数字足迹": "数字足迹", "触达可达": "触达可达",
        "拜访时效": "拜访时效", "企业性质": "企业性质",
    }
    reasons = [
        f"{labels.get(name, name)}得分 {value:.2f}（0-1）"
        for name, value in top
    ]
    reasons.insert(
        0,
        f"方案线：{detail.get('solutionLine')}（价值系数 {detail.get('solutionCoefficient')}）",
    )
    return reasons


def score_detail_payload(detail: dict, meta: dict, model_version: str = "v1") -> dict:
    """组装写入客户 extra_data.scoreDetail 的载荷（可解释性明细）。"""
    return {
        "solutionLine": detail["solutionLine"],
        "solutionCoefficient": detail["solutionCoefficient"],
        "typicalSolution": detail["typicalSolution"],
        "scores": detail["scores"],
        "tiers": detail["tiers"],
        "persistVersion": detail["persistVersion"],
        "persistScore": detail["persistScore"],
        "persistTier": detail["persistTier"],
        "features": detail["features"],
        "anchors": meta.get("anchors"),
        "region": meta.get("region"),
        "asOf": meta.get("asOf"),
        "modelVersion": model_version,
    }


# scoreDetail 里随时间漂移、但**不影响分数**的元数据字段。
# 判断「分数是否变化」时必须把它排除：否则跨天 asOf 必然不同 →
# 哪怕分数一字未改，也会把全部客户计入 changed（上千家"变化"其实没变）。
VOLATILE_DETAIL_KEYS = frozenset({"asOf"})


def _detail_content(payload: dict | None) -> dict:
    """取 scoreDetail 的『分数内容指纹』（剔除 asOf 等易变元数据）。"""
    if not payload:
        return {}
    return {key: value for key, value in payload.items() if key not in VOLATILE_DETAIL_KEYS}


def resolve_as_of(config: dict, as_of=None) -> str:
    """解析本次评分使用的基准日，优先级：显式参数 > 配置 `as_of` > 当天。

    返回 ISO 日期字符串（与引擎 meta["asOf"] 同口径），便于接口回显与前端展示。
    """
    explicit = as_of or engine.parse_date(config.get("as_of"))
    return (explicit or date.today()).isoformat()


def score_import_rows(
    session: Session, rows, *, region: str | None = None
) -> dict[int, tuple[dict, dict]]:
    """对导入行中带工商档案（profile）的行批量评分。

    返回 {行索引(0基): (评分明细, 元信息)}。无档案的行不参与。
    """
    config = engine.load_config()
    region = region or config.get("region")
    indexed = [(index, row) for index, row in enumerate(rows) if getattr(row, "profile", None)]
    if not indexed:
        return {}

    companies = []
    for _, row in indexed:
        company = engine.normalize_company(row.profile)
        if not company.get("name"):
            company["name"] = row.name
        if company.get("address") is None and row.address:
            company["address"] = row.address
        if company.get("industry_major") is None and row.industry:
            company["industry_major"] = row.industry
        companies.append(company)

    scored, meta = score_companies(
        session, companies, region=region, config=config, preserve_order=True
    )
    return {
        index: (detail, meta)
        for (index, _), detail in zip(indexed, scored)
    }


# --------------------------------------------------------------------------- #
# 已入库客户评分刷新
# --------------------------------------------------------------------------- #
def _refresh_scope(session: Session, identity: IdentityContext, org_id: uuid.UUID | None):
    statement = _customer_statement(identity)
    if org_id is None:
        return statement
    organization = session.get(Organization, org_id)
    if organization is None or not identity.organization_in_scope(organization):
        from app.core.exceptions import AppError, CommonErrorCode

        raise AppError(CommonErrorCode.FORBIDDEN, "无权刷新该组织的客户评分", 403)
    return statement.where(
        (Organization.path == organization.path)
        | Organization.path.startswith(organization.path + "/")
    )


def refresh_customer_scores(
    session: Session,
    identity: IdentityContext,
    org_id: uuid.UUID | None = None,
    *,
    region: str | None = None,
    as_of=None,
) -> dict:
    """按工商档案重算在授权范围内客户的评分，落库到 Customer.score / extra_data。"""
    customers = list(session.scalars(_refresh_scope(session, identity, org_id)).all())

    with_profile = [
        customer
        for customer in customers
        if (customer.extra_data or {}).get(PROFILE_KEY)
    ]
    config = engine.load_config()
    if not with_profile:
        return {
            "total": len(customers),
            "scored": 0,
            "changed": 0,
            "metadataUpdated": 0,
            "skipped": len(customers),
            "tierCounts": {},
            "anchors": {},
            "asOf": resolve_as_of(config, as_of),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }

    companies = [_profile_to_company(customer) for customer in with_profile]
    scored, meta = score_companies(
        session, companies, region=region, as_of=as_of, config=config, preserve_order=True
    )

    changed = 0
    metadata_updated = 0
    tier_counts = {"S": 0, "A": 0, "B": 0, "C": 0, "D": 0}
    for customer, detail in zip(with_profile, scored):
        tier_counts[detail["persistTier"]] = tier_counts.get(detail["persistTier"], 0) + 1
        new_score = detail["persistScore"]
        reasons = explain_scores(detail)
        extra = dict(customer.extra_data or {})
        detail_payload = score_detail_payload(detail, meta, config.get("model_version", "v1"))
        previous_payload = extra.get(SCORE_DETAIL_KEY)
        if customer.score == new_score and _detail_content(previous_payload) == _detail_content(
            detail_payload
        ):
            # 分数与内容都没变。若只是 asOf 等元数据过期，就地补正元数据，
            # 但**不计入 changed、不 bump version、不写评分事件** —— 避免无意义的审计噪音。
            if previous_payload != detail_payload:
                extra[SCORE_DETAIL_KEY] = detail_payload
                customer.extra_data = extra
                metadata_updated += 1
            continue
        before_score, before_reasons = customer.score, list(extra.get("reasons") or [])
        customer.score = new_score
        extra["reasons"] = reasons
        extra[SCORE_DETAIL_KEY] = detail_payload
        customer.extra_data = extra
        customer.version += 1
        record_score_event(
            session, customer, identity.user, before_score, before_reasons, "企业评分模型刷新"
        )
        changed += 1

    session.commit()
    return {
        "total": len(customers),
        "scored": len(with_profile),
        "changed": changed,
        "metadataUpdated": metadata_updated,
        "skipped": len(customers) - len(with_profile),
        "tierCounts": tier_counts,
        "anchors": meta["anchors"],
        "asOf": meta.get("asOf") or resolve_as_of(config, as_of),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------- #
# 配置 / 目录查询
# --------------------------------------------------------------------------- #
def describe_config(session: Session, config: dict | None = None) -> dict:
    """返回模型配置摘要 + 当前区域锚点（供前端「配置页」只读展示）。"""
    config = config or engine.load_config()
    region = config.get("region", "未知区域")
    lock = _peek_lock(session, region, config)
    anchors = (
        {"size": lock["size_anchor"], "capital": lock["cap_anchor"]}
        if lock
        else {"size": None, "capital": None}
    )
    return {
        "modelVersion": config.get("model_version", "v1"),
        "positioning": "线索筛选级（拜访优先级排名），非成交概率预测",
        # 实际生效的评分基准日：配置 as_of 固定 → 分数可复现；为 null 才回退到当天。
        "asOf": resolve_as_of(config),
        "asOfIsPinned": bool(config.get("as_of")),
        "region": region,
        "anchorMode": config.get("anchor_mode"),
        "anchors": anchors,
        "missingPolicy": config.get("missing_policy", {}),
        "timingCurve": config.get("timing_curve", {}).get("bands", []),
        "versions": {
            name: weights
            for name, weights in config.get("versions", {}).items()
            if not name.startswith("_")
        },
        "persistVersion": config.get("persist_version"),
        "tierThresholds": config.get("tier_thresholds", {}),
        "features": engine.FEATURES,
        "solutionLines": lines.solution_catalog(),
        "selfCheckRules": config.get("self_check", {}),
    }
