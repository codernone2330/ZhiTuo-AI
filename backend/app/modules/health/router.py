from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import engine

router = APIRouter()


def _payload(request: Request, database: str | None = None) -> dict:
    settings = get_settings()
    data = {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.app_env,
    }
    if database is not None:
        data["database"] = database
    return {
        "success": True,
        "data": data,
        "traceId": request.state.trace_id,
    }


@router.get("/live")
def live(request: Request) -> dict:
    """仅验证 API 进程可响应，不依赖外部组件。"""
    return _payload(request)


@router.get("/ready")
def ready(request: Request):
    """验证数据库可连接，用于容器编排和发布检查。"""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        payload = _payload(request, database="unavailable")
        payload["success"] = False
        payload["data"]["status"] = "not_ready"
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload)
    return _payload(request, database="ok")
