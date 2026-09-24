"""集中导入模型，确保 Alembic 能发现所有 metadata。"""

from app.modules.auth.models import AuthSession, Role, UserRole
from app.modules.customers.models import (
    Customer,
    CustomerEvent,
    CustomerImportBatch,
    CustomerRequest,
)
from app.modules.organizations.models import Organization
from app.modules.users.models import User
from app.modules.visits.models import Visit

__all__ = [
    "AuthSession",
    "Customer",
    "CustomerEvent",
    "CustomerImportBatch",
    "CustomerRequest",
    "Organization",
    "Role",
    "User",
    "UserRole",
    "Visit",
]
