import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError, CommonErrorCode
from app.modules.auth.dependencies import IdentityContext
from app.modules.customers.models import Customer
from app.modules.customers.service import _customer_statement
from app.modules.organizations.models import Organization

STAGES = ("线索入池", "已联系", "已拜访", "方案沟通", "商机立项", "已成交")
PRODUCTS = (
    ("企业专线", ("专线", "带宽", "网络稳定")),
    ("云网融合", ("上云", "云网", "云主机", "云电脑")),
    ("5G专网", ("5G", "工业互联网", "低时延")),
    ("物联网卡", ("物联网", "设备联网", "车联网")),
    ("云安全", ("安全", "等保", "防护")),
    ("全光组网", ("组网", "园区网络", "全光")),
    ("视频监控", ("监控", "安防", "远程巡检")),
    ("云视讯", ("视频会议", "云视讯", "协同办公")),
    ("国际专线", ("跨境", "海外", "国际网络")),
    ("数据备份", ("备份", "容灾", "灾备")),
)


def _scope_org(session: Session, identity: IdentityContext, org_id: uuid.UUID | None):
    if org_id is None:
        return None
    organization = session.get(Organization, org_id)
    if organization is None or not identity.organization_in_scope(organization):
        raise AppError(CommonErrorCode.FORBIDDEN, "无权刷新该组织的商机", 403)
    return organization


def _statement(identity: IdentityContext, organization: Organization | None = None):
    statement = _customer_statement(identity).where(Customer.stage != "已成交")
    if organization is not None:
        statement = statement.where(
            (Organization.path == organization.path)
            | Organization.path.startswith(organization.path + "/")
        )
    return statement


def _last_contact(customer: Customer) -> datetime | None:
    raw = (customer.extra_data or {}).get("lastContact")
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _score(customer: Customer) -> int:
    score = 35 + {"高": 25, "中": 15}.get(customer.potential, 7)
    score += 12 if customer.need else 0
    score += 7 if customer.contact_phone else 0
    sources = (customer.extra_data or {}).get("sources") or [customer.source]
    score += 8 if len(sources) > 1 else 4
    last = _last_contact(customer)
    days = (
        max(0, int((datetime.now(timezone.utc) - last).total_seconds() // 86400))
        if last
        else None
    )
    score += 8 if days is None else min(8, max(0, days - 2))
    score += max(0, STAGES.index(customer.stage) if customer.stage in STAGES else 0) * 2
    return min(99, max(1, score))


def _reasons(customer: Customer, organization: Organization) -> list[str]:
    last = _last_contact(customer)
    stale = last is None or (datetime.now(timezone.utc) - last).days > 7
    return [
        "新客待核验与首触" if customer.kind == "新客" else "异网猎户待转化",
        f"归属{organization.name}，行业为{customer.industry}",
        f"需求方向：{customer.need or '待识别'}",
        "超过 7 天未跟进，建议尽快触达" if stale else "近期已有互动，建议推进下一阶段",
    ]


def _products(customer: Customer) -> list[dict]:
    text = f"{customer.need} {customer.industry}".lower()
    matches = []
    for name, words in PRODUCTS:
        found = [word for word in words if word.lower() in text]
        if found:
            matches.append({"name": name, "basis": "、".join(found)})
    return matches[:3]


def serialize_opportunity(customer: Customer, organization: Organization) -> dict:
    extra = customer.extra_data or {}
    return {
        "customerId": customer.external_id,
        "customerName": customer.name,
        "organizationId": str(customer.organization_id),
        "organizationCode": organization.code,
        "kind": customer.kind,
        "stage": customer.stage,
        "score": customer.score,
        "reasons": extra.get("reasons") or _reasons(customer, organization),
        "nextAction": customer.next_action,
        "productDirections": _products(customer),
        "updatedAt": customer.updated_at.isoformat(),
    }


def list_opportunities(
    session: Session,
    identity: IdentityContext,
    page: int,
    page_size: int,
    org_id: uuid.UUID | None,
) -> tuple[list[dict], int]:
    organization = _scope_org(session, identity, org_id)
    statement = _statement(identity, organization)
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    customers = session.scalars(
        statement.order_by(Customer.score.desc(), Customer.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    orgs = {
        item.id: item
        for item in session.scalars(
            select(Organization).where(
                Organization.id.in_({customer.organization_id for customer in customers})
            )
        ).all()
    }
    return [serialize_opportunity(item, orgs[item.organization_id]) for item in customers], total


def get_opportunity(session: Session, identity: IdentityContext, customer_ref: str) -> dict:
    customer = session.scalar(
        _statement(identity).where(Customer.external_id == customer_ref)
    )
    if customer is None:
        raise AppError(CommonErrorCode.NOT_FOUND, "商机不存在或不在授权范围内", 404)
    return serialize_opportunity(customer, session.get(Organization, customer.organization_id))


def refresh_opportunities(
    session: Session, identity: IdentityContext, org_id: uuid.UUID | None
) -> dict:
    if not (
        identity.is_super_admin
        or identity.is_group_admin
        or identity.is_org_admin
        or "department_manager" in identity.role_codes
        or "customer_manager" in identity.role_codes
    ):
        raise AppError(CommonErrorCode.FORBIDDEN, "当前角色不能更新商机", 403)
    organization = _scope_org(session, identity, org_id)
    customers = session.scalars(_statement(identity, organization).with_for_update()).all()
    orgs = {
        item.id: item
        for item in session.scalars(
            select(Organization).where(
                Organization.id.in_({customer.organization_id for customer in customers})
            )
        ).all()
    }
    changed = 0
    for customer in customers:
        score = _score(customer)
        reasons = _reasons(customer, orgs[customer.organization_id])
        extra = dict(customer.extra_data or {})
        if customer.score != score or extra.get("reasons") != reasons:
            customer.score = score
            extra["reasons"] = reasons
            customer.extra_data = extra
            customer.version += 1
            changed += 1
    session.commit()
    ranked = sorted(customers, key=lambda item: item.score, reverse=True)
    return {
        "total": len(customers),
        "changed": changed,
        "high": sum(item.score >= 80 for item in customers),
        "stale": sum(
            _last_contact(item) is None
            or (datetime.now(timezone.utc) - _last_contact(item)).days > 7
            for item in customers
        ),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "top": [serialize_opportunity(item, orgs[item.organization_id]) for item in ranked[:5]],
    }
