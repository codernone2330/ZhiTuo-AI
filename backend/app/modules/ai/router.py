import json
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppError, CommonErrorCode
from app.core.responses import PageData
from app.db.session import get_db
from app.integrations.llm.client import MODELS, ask_provider
from app.integrations.qichacha.client import search_companies
from app.modules.ai.models import ExternalLead, IntegrationAudit
from app.modules.auth.dependencies import CurrentIdentity, IdentityContext
from app.modules.customers.audit import record_score_event
from app.modules.customers.models import Customer
from app.modules.customers.service import (
    _assert_customer_organization,
    _customer_statement,
    normalize_customer_name,
)
from app.modules.organizations.models import Organization
from app.modules.visits.models import Visit

router = APIRouter()
Db = Annotated[Session, Depends(get_db)]


def ok(request: Request, data):
    return {"success": True, "data": data, "traceId": request.state.trace_id}


def audit(
    session: Session,
    identity: IdentityContext,
    action: str,
    target: str | None = None,
    detail: str | None = None,
    trace_id: str | None = None,
):
    session.add(
        IntegrationAudit(
            id=uuid.uuid4(),
            actor_id=identity.user.id,
            organization_id=identity.organization.id,
            action=action,
            target_id=target,
            detail=detail,
            trace_id=trace_id,
            created_at=datetime.now(timezone.utc),
        )
    )


class CaptureRequest(BaseModel):
    searchTerm: str = Field(min_length=2, max_length=50)
    organizationId: uuid.UUID


class LeadDecision(BaseModel):
    approve: bool
    reason: str = Field(min_length=3, max_length=300)
    version: int = Field(ge=1)


def _lead_data(lead: ExternalLead) -> dict:
    return {
        "id": str(lead.id),
        "provider": lead.provider,
        "providerKey": lead.provider_key,
        "name": lead.name,
        "establishedAt": lead.established_at.isoformat() if lead.established_at else None,
        "address": lead.address,
        "searchTerm": lead.search_term,
        "organizationId": str(lead.organization_id),
        "status": lead.status,
        "capturedBy": str(lead.captured_by),
        "reviewReason": lead.review_reason,
        "customerId": str(lead.customer_id) if lead.customer_id else None,
        "version": lead.version,
        "score": lead.score,
        "scoreDetail": _load_score_detail(lead.score_detail),
    }


def _load_score_detail(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _company_scorer():
    """惰性加载独立企查查模块（map_and_company/company.py），用于商机评分。

    未配置凭证或模块不可导入时返回 None，采集主流程不受影响。
    """
    try:
        from map_and_company import company  # noqa: PLC0415
    except Exception:
        return None
    return company


def _score_lead(row: dict, scorer) -> dict | None:
    """用企查查工商详情为一条线索评分；任何异常都降级为 None。"""
    if scorer is None:
        return None
    try:
        detail = scorer.get_company_detail(str(row.get("name") or "")) or {}
    except Exception:
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    try:
        return scorer.score_company({**row, **detail})
    except Exception:
        return None


def _lead_scope(identity: IdentityContext):
    statement = select(ExternalLead).join(
        Organization, ExternalLead.organization_id == Organization.id
    )
    if identity.is_super_admin or identity.is_group_admin:
        return statement
    if identity.is_org_admin:
        return statement.where(
            or_(
                Organization.path == identity.organization.path,
                Organization.path.startswith(identity.organization.path + "/"),
            )
        )
    return statement.where(ExternalLead.organization_id == identity.organization.id)


@router.post("/leads/capture", status_code=201)
def capture(payload: CaptureRequest, request: Request, identity: CurrentIdentity, session: Db):
    if not (
        identity.is_super_admin
        or identity.is_group_admin
        or identity.is_org_admin
        or "department_manager" in identity.role_codes
    ):
        raise AppError(CommonErrorCode.FORBIDDEN, "只有经营管理人员可以采集外部线索", 403)
    organization = _assert_customer_organization(session, identity, payload.organizationId)
    rows = search_companies(payload.searchTerm.strip())
    scorer = _company_scorer()
    created = []
    for row in rows:
        normalized = normalize_customer_name(row["name"])
        existing = session.scalar(
            select(ExternalLead.id).where(
                ExternalLead.organization_id == organization.id,
                or_(
                    ExternalLead.provider_key == row["providerKey"],
                    ExternalLead.normalized_name == normalized,
                ),
            )
        )
        if existing:
            continue
        try:
            established = (
                datetime.fromisoformat(row["establishedAt"].replace("/", "-").split(" ")[0])
                if row["establishedAt"]
                else None
            )
        except ValueError:
            established = None
        score_payload = _score_lead(row, scorer)
        lead = ExternalLead(
            id=uuid.uuid4(),
            provider="qichacha",
            provider_key=row["providerKey"],
            name=row["name"],
            normalized_name=normalized,
            established_at=established,
            address=row["address"],
            search_term=payload.searchTerm.strip(),
            organization_id=organization.id,
            status="pending",
            captured_by=identity.user.id,
            version=1,
            score=score_payload.get("score") if score_payload else None,
            score_detail=json.dumps(score_payload, ensure_ascii=False) if score_payload else None,
        )
        session.add(lead)
        created.append(_lead_data(lead))
    audit(
        session,
        identity,
        "qcc_capture",
        detail=f"term={payload.searchTerm.strip()}; fetched={len(rows)}; queued={len(created)}",
        trace_id=request.state.trace_id,
    )
    session.commit()
    return ok(request, {"fetched": len(rows), "queued": len(created), "items": created})


@router.get("/leads")
def leads(
    request: Request,
    identity: CurrentIdentity,
    session: Db,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
    status: Literal["pending", "approved", "rejected"] | None = None,
):
    statement = _lead_scope(identity)
    if status:
        statement = statement.where(ExternalLead.status == status)
    rows = session.scalars(
        statement.order_by(ExternalLead.created_at.desc())
        .offset((page - 1) * pageSize)
        .limit(pageSize)
    ).all()
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    return ok(
        request,
        PageData.build([_lead_data(row) for row in rows], page, pageSize, total).model_dump(),
    )


@router.post("/leads/{lead_id}/decision")
def decide(
    lead_id: uuid.UUID,
    payload: LeadDecision,
    request: Request,
    identity: CurrentIdentity,
    session: Db,
):
    if not (
        identity.is_super_admin
        or identity.is_group_admin
        or identity.is_org_admin
        or "department_manager" in identity.role_codes
    ):
        raise AppError(CommonErrorCode.FORBIDDEN, "只有经营管理人员可以审核外部线索", 403)
    lead = session.scalar(_lead_scope(identity).where(ExternalLead.id == lead_id).with_for_update())
    if lead is None:
        raise AppError(CommonErrorCode.NOT_FOUND, "线索不存在或不在授权范围内", 404)
    if lead.status != "pending" or lead.version != payload.version:
        raise AppError(CommonErrorCode.CONFLICT, "线索已处理或版本已变化", 409)
    if lead.captured_by == identity.user.id:
        raise AppError(CommonErrorCode.FORBIDDEN, "采集人不能审批自己的线索", 403)
    if payload.approve:
        duplicate = session.scalar(
            select(Customer.id).where(
                Customer.normalized_name == lead.normalized_name, Customer.deleted_at.is_(None)
            )
        )
        if duplicate:
            raise AppError(CommonErrorCode.CONFLICT, "客户库中已存在同名企业，请先核验", 409)
        organization = session.get(Organization, lead.organization_id)
        customer = Customer(
            id=uuid.uuid4(),
            external_id="QCC-" + uuid.uuid4().hex[:16].upper(),
            name=lead.name,
            normalized_name=lead.normalized_name,
            organization_id=lead.organization_id,
            owner_name="待分配",
            kind="新客",
            customer_type="政企客户",
            province=organization.province,
            city=organization.city,
            district=organization.district,
            address=lead.address,
            source="企查查·审核入库",
            imported_by=identity.user.id,
            source_created_at=lead.established_at,
            extra_data={
                "provider": "qichacha",
                "providerKey": lead.provider_key,
                "leadId": str(lead.id),
                "reasons": [],
            },
        )
        session.add(customer)
        session.flush()
        record_score_event(session, customer, identity.user, None, None, "企查查线索审核入库")
        lead.customer_id = customer.id
    lead.status = "approved" if payload.approve else "rejected"
    lead.review_reason = payload.reason.strip()
    lead.reviewed_by = identity.user.id
    lead.reviewed_at = datetime.now(timezone.utc)
    lead.version += 1
    audit(
        session,
        identity,
        "lead_approved" if payload.approve else "lead_rejected",
        str(lead.id),
        lead.review_reason,
        request.state.trace_id,
    )
    session.commit()
    return ok(request, _lead_data(lead))


class ChatRequest(BaseModel):
    provider: Literal["deepseek", "qwen", "glm", "kimi"]
    model: str = Field(min_length=2, max_length=80)
    apiKey: str | None = Field(None, min_length=12, max_length=256)
    question: str = Field(min_length=1, max_length=1000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=8)


@router.get("/models")
def models(request: Request, identity: CurrentIdentity):
    return ok(request, {name: sorted(values) for name, values in MODELS.items()})


@router.post("/chat")
def chat(payload: ChatRequest, request: Request, identity: CurrentIdentity, session: Db):
    settings = get_settings()
    configured = getattr(settings, payload.provider + "_api_key", None)
    key = payload.apiKey or (configured.get_secret_value() if configured else "")
    if len(key) < 12:
        raise AppError("AI.NOT_CONFIGURED", "请先配置所选 AI 平台的 API Key", 400)
    authorized_ids = _customer_statement(identity).with_only_columns(Customer.id).order_by(None)
    customer_count = (
        session.scalar(select(func.count()).select_from(authorized_ids.subquery())) or 0
    )
    kind_counts = dict(
        session.execute(
            select(Customer.kind, func.count())
            .where(Customer.id.in_(authorized_ids))
            .group_by(Customer.kind)
        ).all()
    )
    stages = dict(
        session.execute(
            select(Customer.stage, func.count())
            .where(Customer.id.in_(authorized_ids))
            .group_by(Customer.stage)
        ).all()
    )
    visit_scope = select(Visit).where(Visit.customer_id.in_(authorized_ids))
    if "customer_manager" in identity.role_codes:
        visit_scope = visit_scope.where(Visit.owner_name == identity.user.display_name)
    visit_ids = visit_scope.with_only_columns(Visit.id).order_by(None)
    visit_totals = dict(
        session.execute(
            select(Visit.status, func.count()).where(Visit.id.in_(visit_ids)).group_by(Visit.status)
        ).all()
    )
    deal_amount = session.scalar(
        select(func.coalesce(func.sum(Visit.deal_amount), 0)).where(
            Visit.id.in_(visit_ids), Visit.status == "completed"
        )
    )
    context = {
        "organization": identity.organization.name,
        "roleCodes": sorted(identity.role_codes),
        "customerCount": customer_count,
        "newCustomerCount": kind_counts.get("新客", 0),
        "hunterCount": kind_counts.get("猎户", 0),
        "stages": stages,
        "openVisits": visit_totals.get("pending", 0),
        "completedVisits": visit_totals.get("completed", 0),
        "dealAmount": str(deal_amount or 0),
    }
    words = [
        word
        for word in payload.question.replace("，", " ").replace("？", " ").split()
        if len(word) >= 2
    ][:8]
    relevance = (
        or_(
            *[
                or_(
                    Customer.name.ilike(f"%{word}%"),
                    Customer.need.ilike(f"%{word}%"),
                    Customer.industry.ilike(f"%{word}%"),
                )
                for word in words
            ]
        )
        if words
        else None
    )
    ranking = select(Customer).where(Customer.id.in_(authorized_ids))
    if relevance is not None:
        ranking = ranking.order_by(case((relevance, 0), else_=1), Customer.score.desc())
    else:
        ranking = ranking.order_by(Customer.score.desc())
    ranked = session.scalars(ranking.limit(20)).all()
    context["relevantCustomers"] = [
        {
            "name": row.name,
            "kind": row.kind,
            "industry": row.industry,
            "stage": row.stage,
            "score": row.score,
        }
        for row in ranked
    ]
    latest = session.execute(
        select(Visit, Customer.name)
        .join(Customer, Visit.customer_id == Customer.id)
        .where(Visit.id.in_(visit_ids))
        .order_by(Visit.created_at.desc())
        .limit(20)
    ).all()
    context["recentVisits"] = [
        {
            "customer": customer_name,
            "owner": row.owner_name,
            "status": row.status,
            "time": (row.completed_at or row.scheduled_at).isoformat(),
            "outcome": row.outcome,
            "dealAmount": str(row.deal_amount or 0),
        }
        for row, customer_name in latest
    ]
    messages = [
        {
            "role": "system",
            "content": "你是智拓 AI 助手。仅依据服务端授权数据回答。"
            "不得编造客户、联系人或电话。数据不足时说明。授权数据：" + str(context),
        }
    ]
    messages += [
        {"role": row.get("role"), "content": str(row.get("content") or "")[:1000]}
        for row in payload.history
        if row.get("role") in {"user", "assistant"}
    ]
    messages.append({"role": "user", "content": payload.question})
    result = ask_provider(payload.provider, payload.model, key, messages)
    audit(
        session,
        identity,
        "ai_chat",
        detail=(
            f"provider={payload.provider}; model={payload.model}; "
            f"authorizedCustomers={customer_count}"
        ),
        trace_id=request.state.trace_id,
    )
    session.commit()
    return ok(request, {**result, "provider": payload.provider, "model": payload.model})
