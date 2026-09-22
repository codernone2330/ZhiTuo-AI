import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Role(TimestampMixin, Base):
    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )


class AuthSession(TimestampMixin, Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
