"""企业评分模型 · 路由层。

- `GET  /api/v1/scoring/config`   模型配置摘要 + 当前区域锚点 + 16 条方案线目录
- `POST /api/v1/scoring/preview`  对一份企业清单评分预览（不写客户库，仅锁定锚点）
- `POST /api/v1/scoring/refresh`  按工商档案重算已入库客户的评分并落库
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.exceptions import AppError, CommonErrorCode
from app.db.session import get_db
from app.modules.auth.dependencies import CurrentIdentity, IdentityContext
from app.modules.scoring import engine
from app.modules.scoring.schemas import (
    ScorePreviewRequest,
    ScoreRefreshRequest,
)
from app.modules.scoring.service import (
    describe_config,
    refresh_customer_scores,
    score_companies,
)

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


def _success(request: Request, data) -> dict:
    return {"success": True, "data": data, "traceId": request.state.trace_id}


def _assert_can_score(identity: IdentityContext) -> None:
    if not (
        identity.is_super_admin
        or identity.is_group_admin
        or identity.is_org_admin
        or "department_manager" in identity.role_codes
        or "customer_manager" in identity.role_codes
    ):
        raise AppError(CommonErrorCode.FORBIDDEN, "当前角色不能使用企业评分模型", 403)


@router.get("/config")
def scoring_config(request: Request, identity: CurrentIdentity, session: DbSession) -> dict:
    _assert_can_score(identity)
    return _success(request, describe_config(session))


@router.post("/preview")
def scoring_preview(
    payload: ScorePreviewRequest,
    request: Request,
    identity: CurrentIdentity,
    session: DbSession,
) -> dict:
    """对一份即将导入的企业清单评分（试算）。

    不写入客户库；仅在系统内尚无该区域锚点锁时落锁，以保证后续评分稳定。
    输入行支持企查查导出列名（如「企业名称」「参保人数」「国标行业大类」）。
    """
    _assert_can_score(identity)
    config = engine.load_config()
    scored, meta = score_companies(
        session, payload.rows, region=payload.region, as_of=payload.asOf, config=config
    )
    session.commit()
    check = engine.self_check(
        [engine.normalize_company(row) for row in payload.rows], config
    )
    if payload.topN:
        scored = scored[: payload.topN]
    return _success(request, {"meta": meta, "selfCheck": check, "companies": scored})


@router.post("/refresh")
def scoring_refresh(
    request: Request,
    identity: CurrentIdentity,
    session: DbSession,
    payload: ScoreRefreshRequest | None = None,
) -> dict:
    """按工商档案（客户 extra_data.profile）重算评分并落库。

    仅对带有工商档案字段的客户生效；无档案的客户计入 skipped。
    评分基准日默认取 `model_config.json` 的 `as_of`（固定），可用 `asOf` 覆盖；
    基准日不变时重复调用是幂等的（changed 只反映真实分数变化）。
    请求体可省略（`POST` 空体等价于不限定组织、使用配置基准日）。
    """
    _assert_can_score(identity)
    organization_id = payload.organizationId if payload else None
    as_of = payload.asOf if payload else None
    result = refresh_customer_scores(session, identity, organization_id, as_of=as_of)
    return _success(request, result)
