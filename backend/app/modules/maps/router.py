import base64
import importlib
import time
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.core.exceptions import AppError
from app.modules.auth.dependencies import CurrentIdentity

router = APIRouter()


class VisitRouteRequest(BaseModel):
    items: list[dict[str, Any]] = Field(min_length=1, max_length=10)
    start: dict[str, Any] | None = None
    mode: str = Field(default="driving", pattern=r"^(driving|walking|bicycling)$")


def _success(request: Request, data: Any) -> dict:
    return {"success": True, "data": data, "traceId": request.state.trace_id}


@router.get("/status")
def map_status(request: Request, identity: CurrentIdentity) -> dict:
    del identity
    try:
        integration = importlib.import_module("src.map_integration")
        integration.load_tencent_key()
        configured = True
    except Exception:
        configured = False
    return _success(
        request,
        {"provider": "腾讯地图", "configured": configured, "realRoute": configured},
    )


@router.post("/visit-route")
def visit_route(
    payload: VisitRouteRequest,
    request: Request,
    identity: CurrentIdentity,
) -> dict:
    del identity
    try:
        integration = importlib.import_module("src.map_integration")
        plan = integration.plan_visit_route(payload.items, payload.start, payload.mode)
        if plan.get("ok") and plan.get("stops"):
            points = [
                {
                    "lat": stop["lat"],
                    "lng": stop["lng"],
                    "label": str(stop["sequence"]),
                    "name": stop.get("name", ""),
                    "isStart": bool(stop.get("isStart")),
                }
                for stop in plan["stops"]
                if stop.get("lat") is not None and stop.get("lng") is not None
            ]
            try:
                time.sleep(0.5)
                image, content_type = integration.fetch_static_map(points)
                encoded = base64.b64encode(image).decode("ascii")
                plan["mapImageBase64"] = f"data:{content_type};base64,{encoded}"
            except integration.MapApiError as exc:
                plan["mapImageBase64"] = None
                plan["mapWarning"] = str(exc)
        return _success(request, plan)
    except Exception as exc:
        integration = importlib.import_module("src.map_integration")
        if isinstance(exc, integration.MapApiError):
            raise AppError(
                exc.code,
                str(exc),
                int(exc.http_status),
                {"provider": "腾讯地图", "providerCode": exc.provider_code},
            ) from exc
        raise
