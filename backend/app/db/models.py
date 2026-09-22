"""集中导入模型，确保 Alembic 能发现所有 metadata。"""

from app.modules.auth.models import AuthSession, Role, UserRole
from app.modules.organizations.models import Organization
from app.modules.users.models import User

__all__ = ["AuthSession", "Organization", "Role", "User", "UserRole"]
