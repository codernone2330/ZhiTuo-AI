from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_password
from app.modules.customers.models import Customer, CustomerEvent
from app.modules.organizations.models import Organization
from app.modules.users.models import User
from app.scripts.seed import seed_organizations, seed_roles


def test_existing_customer_receives_labeled_score_baseline(tmp_path, monkeypatch) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "score-migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    try:
        command.upgrade(config, "20260924_0006")
        engine = create_engine(database_url)
        @event.listens_for(engine, "connect")
        def sqlite_now(connection, _record):
            connection.create_function("now", 0, lambda: datetime.now(timezone.utc).isoformat())

        with Session(engine) as session:
            seed_roles(session)
            seed_organizations(session)
            group = session.scalar(select(Organization).where(Organization.code == "cmcc"))
            department = session.scalar(
                select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
            )
            actor = User(
                username="migration-actor", employee_no="MIGRATION-001",
                display_name="历史导入人", password_hash=hash_password("test-password"),
                organization_id=group.id, is_active=True,
            )
            session.add(actor)
            session.flush()
            customer = Customer(
                external_id="c-before-week5", name="迁移前企业有限公司",
                normalized_name="迁移前企业有限公司", organization_id=department.id,
                owner_name="历史经理", kind="新客", need="企业专线",
                stage="已联系", potential="高", score=87,
                imported_by=actor.id, extra_data={"reasons": ["旧评分原因"]},
            )
            session.add(customer)
            session.commit()
            customer_id = customer.id
        engine.dispose()

        command.upgrade(config, "head")
        engine = create_engine(database_url)
        with Session(engine) as session:
            events = session.scalars(
                select(CustomerEvent).where(CustomerEvent.customer_id == customer_id)
            ).all()
            assert len(events) == 1
            assert events[0].action == "score_baseline"
            assert events[0].after_data == {
                "score": 87, "reasons": ["旧评分原因"],
                "provenance": "migration_baseline",
                "ruleVersion": "unknown",
            }
        engine.dispose()
    finally:
        get_settings.cache_clear()
