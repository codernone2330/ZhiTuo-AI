from fastapi import APIRouter

from app.modules.auth.router import router as auth_router
from app.modules.health.router import router as health_router
from app.modules.maps.router import router as maps_router
from app.modules.organizations.router import router as organizations_router
from app.modules.users.router import router as users_router

api_router = APIRouter()
api_router.include_router(health_router, prefix="/health", tags=["health"])
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(organizations_router, prefix="/organizations", tags=["organizations"])
api_router.include_router(users_router, prefix="/users", tags=["users"])
api_router.include_router(maps_router, prefix="/maps", tags=["maps"])

# 新业务模块统一在此注册，禁止在 app.main 中散落路由：
# api_router.include_router(customer_router, prefix="/customers", tags=["customers"])
