from fastapi import APIRouter

from app.modules.ai.router import router as ai_router
from app.modules.auth.router import router as auth_router
from app.modules.customers.router import router as customers_router
from app.modules.documents.router import router as documents_router
from app.modules.health.router import router as health_router
from app.modules.maps.router import router as maps_router
from app.modules.opportunities.router import router as opportunities_router
from app.modules.organizations.router import router as organizations_router
from app.modules.reports.router import router as reports_router
from app.modules.users.router import router as users_router
from app.modules.visits.router import router as visits_router

api_router = APIRouter()
api_router.include_router(health_router, prefix="/health", tags=["health"])
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(organizations_router, prefix="/organizations", tags=["organizations"])
api_router.include_router(users_router, prefix="/users", tags=["users"])
api_router.include_router(maps_router, prefix="/maps", tags=["maps"])
api_router.include_router(customers_router, prefix="/customers", tags=["customers"])
api_router.include_router(visits_router, prefix="/visits", tags=["visits"])
api_router.include_router(opportunities_router, prefix="/opportunities", tags=["opportunities"])
api_router.include_router(ai_router, prefix="/ai", tags=["ai"])
api_router.include_router(documents_router, prefix="/documents", tags=["documents"])
api_router.include_router(reports_router, prefix="/reports", tags=["reports"])

# 新业务模块统一在此注册，禁止在 app.main 中散落路由：
