"""集中导入模型，确保 Alembic 能发现所有 metadata。"""

from app.modules.auth.models import Role, UserRole
from app.modules.organizations.models import Organization
from app.modules.users.models import User

__all__ = ["Organization", "Role", "User", "UserRole"]
