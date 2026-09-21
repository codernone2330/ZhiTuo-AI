from fastapi import APIRouter

from app.modules.health.router import router as health_router

api_router = APIRouter()
api_router.include_router(health_router, prefix="/health", tags=["health"])

# 新业务模块统一在此注册，禁止在 app.main 中散落路由：
# api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
# api_router.include_router(customer_router, prefix="/customers", tags=["customers"])
