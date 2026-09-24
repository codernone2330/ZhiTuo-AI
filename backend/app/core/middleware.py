import logging
import uuid
from datetime import datetime, timezone

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger(__name__)


def _audit_request(request: Request, status_code: int, trace_id: str) -> None:
    if not request.url.path.startswith("/api/v1/") or (
        request.method == "GET" and status_code < 400
    ):
        return
    from app.core.security import decode_token
    from app.db.session import SessionLocal
    from app.modules.audit.models import ApiAudit
    from app.modules.users.models import User

    actor_id = organization_id = None
    bearer = request.headers.get("authorization", "")
    if bearer.lower().startswith("bearer "):
        try:
            actor_id = uuid.UUID(decode_token(bearer[7:], "access")["sub"])
        except (jwt.PyJWTError, ValueError, KeyError):
            actor_id = None
    try:
        with SessionLocal() as session:
            if actor_id:
                user = session.get(User, actor_id)
                organization_id = user.organization_id if user else None
            session.add(
                ApiAudit(
                    actor_id=actor_id,
                    organization_id=organization_id,
                    action="api_failure" if status_code >= 400 else "api_write",
                    method=request.method,
                    path=request.url.path[:300],
                    status_code=status_code,
                    trace_id=trace_id[:80],
                    created_at=datetime.now(timezone.utc),
                )
            )
            session.commit()
    except Exception:
        logger.exception("Unable to persist API audit event trace=%s", trace_id)


class TraceIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
        request.state.trace_id = trace_id
        try:
            response = await call_next(request)
        except Exception:
            _audit_request(request, 500, trace_id)
            raise
        _audit_request(request, response.status_code, trace_id)
        response.headers["X-Trace-Id"] = trace_id
        return response
