from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import TraceIdMiddleware


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.debug)

    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="智拓商机作战助手模块化后端公共骨架",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url=f"{settings.api_v1_prefix}/openapi.json",
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials="*" not in settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Trace-Id"],
    )
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.trusted_host_list,
    )
    application.add_middleware(TraceIdMiddleware)

    register_exception_handlers(application)
    application.include_router(api_router, prefix=settings.api_v1_prefix)

    if settings.frontend_dir:
        frontend_dir = Path(settings.frontend_dir)
    else:
        frontend_dir = Path(__file__).resolve().parents[2] / "data"
    if frontend_dir.is_dir():
        frontend_index = frontend_dir / "index.html"
        if not frontend_index.is_file():
            frontend_index = frontend_dir / "智拓商机作战助手-开发版.html"

        @application.get("/app/", include_in_schema=False)
        def frontend() -> FileResponse:
            return FileResponse(frontend_index)

        application.mount("/app", StaticFiles(directory=frontend_dir, html=True), name="frontend")

    @application.get("/", include_in_schema=False)
    def service_info() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "version": "0.1.0",
            "environment": settings.app_env,
            "docs": "/docs",
            "frontend": "/app/",
        }

    return application


app = create_app()
