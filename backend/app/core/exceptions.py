import logging
from enum import Enum
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class CommonErrorCode(str, Enum):
    INVALID_ARGUMENT = "COMMON.INVALID_ARGUMENT"
    UNAUTHORIZED = "AUTH.UNAUTHORIZED"
    FORBIDDEN = "AUTH.FORBIDDEN"
    NOT_FOUND = "COMMON.NOT_FOUND"
    CONFLICT = "COMMON.CONFLICT"
    INTERNAL_ERROR = "COMMON.INTERNAL_ERROR"


class AppError(Exception):
    def __init__(
        self,
        code: CommonErrorCode | str,
        message: str,
        http_status: int = status.HTTP_400_BAD_REQUEST,
        details: Any | None = None,
    ) -> None:
        self.code = code.value if isinstance(code, CommonErrorCode) else code
        self.message = message
        self.http_status = http_status
        self.details = details
        super().__init__(message)


def _error_payload(request: Request, code: str, message: str, details: Any = None) -> dict:
    return {
        "success": False,
        "error": {"code": code, "message": message, "details": details},
        "traceId": getattr(request.state, "trace_id", None),
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=_error_payload(request, exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_error_payload(
                request,
                CommonErrorCode.INVALID_ARGUMENT.value,
                "请求参数校验失败",
                exc.errors(),
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled application error", exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_payload(
                request,
                CommonErrorCode.INTERNAL_ERROR.value,
                "服务内部错误",
            ),
        )
