import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.modules.auth.models import Role, UserRole
from app.modules.organizations.models import Organization
from app.modules.users.models import User
from app.scripts.seed import seed_organizations, seed_roles


@pytest.fixture()
def identity_client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine, expire_on_commit=False)
    with testing_session.begin() as session:
        seed_roles(session)
        seed_organizations(session)
        group = session.scalar(select(Organization).where(Organization.code == "cmcc"))
        role = session.scalar(select(Role).where(Role.code == "group_admin"))
        admin = User(
            username="groupadmin",
            employee_no="TEST0001",
            display_name="测试集团管理员",
            password_hash=hash_password("test-admin-password"),
            organization_id=group.id,
            is_active=True,
        )
        session.add(admin)
        session.flush()
        session.add(UserRole(user_id=admin.id, role_id=role.id))
        super_role = session.scalar(select(Role).where(Role.code == "super_admin"))
        super_admin = User(
            username="szyd",
            employee_no="TEST-SUPER-001",
            display_name="测试超级管理员",
            password_hash=hash_password("test-super-password"),
            organization_id=group.id,
            is_active=True,
        )
        session.add(super_admin)
        session.flush()
        session.add(UserRole(user_id=super_admin.id, role_id=super_role.id))

    def override_db():
        with testing_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, testing_session
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)


def login(client: TestClient, username: str, password: str) -> str:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return response.json()["data"]["accessToken"]


def test_login_me_refresh_and_organization_tree(identity_client) -> None:
    client, _ = identity_client
    token = login(client, "groupadmin", "test-admin-password")
    headers = {"Authorization": f"Bearer {token}"}

    me = client.get("/api/v1/auth/me", headers=headers)
    tree = client.get("/api/v1/organizations/tree", headers=headers)
    refresh = client.post("/api/v1/auth/refresh")

    assert me.status_code == 200
    assert me.json()["data"]["roles"] == ["group_admin"]
    assert tree.status_code == 200
    assert tree.json()["data"][0]["code"] == "cmcc"
    assert refresh.status_code == 200

    employee_login = client.post(
        "/api/v1/auth/login",
        json={"username": "TEST0001", "password": "test-admin-password"},
    )
    assert employee_login.status_code == 200


def test_org_admin_cannot_create_user_in_sibling_city(identity_client) -> None:
    client, testing_session = identity_client
    super_token = login(client, "szyd", "test-super-password")
    super_headers = {"Authorization": f"Bearer {super_token}"}
    with testing_session() as session:
        shenzhen = session.scalar(select(Organization).where(Organization.code == "cmcc-gd-sz"))
        guangzhou = session.scalar(select(Organization).where(Organization.code == "cmcc-gd-gz"))

    create_admin = client.post(
        "/api/v1/users",
        headers=super_headers,
        json={
            "username": "szadmin",
            "employeeNo": "TEST-SZ-ADMIN",
            "displayName": "深圳管理员",
            "password": "test-admin-password",
            "organizationId": str(shenzhen.id),
            "roleCode": "org_admin",
        },
    )
    assert create_admin.status_code == 201
    shenzhen_token = login(client, "szadmin", "test-admin-password")

    forbidden = client.post(
        "/api/v1/users",
        headers={"Authorization": f"Bearer {shenzhen_token}"},
        json={
            "username": "gzadmin",
            "employeeNo": "TEST-GZ-ADMIN",
            "displayName": "广州管理员",
            "password": "test-admin-password",
            "organizationId": str(guangzhou.id),
            "roleCode": "org_admin",
        },
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "AUTH.FORBIDDEN"


def test_customer_manager_must_belong_to_enterprise_department(identity_client) -> None:
    client, testing_session = identity_client
    token = login(client, "szyd", "test-super-password")
    with testing_session() as session:
        general = session.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ba-general")
        )
    response = client.post(
        "/api/v1/users",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "username": "wrongdept",
            "employeeNo": "TEST-WRONG-DEPT",
            "displayName": "错误部门客户经理",
            "password": "test-admin-password",
            "organizationId": str(general.id),
            "roleCode": "customer_manager",
        },
    )
    assert response.status_code == 400


def test_cannot_deactivate_last_group_admin(identity_client) -> None:
    client, testing_session = identity_client
    token = login(client, "szyd", "test-super-password")
    with testing_session() as session:
        admin_id = session.scalar(select(User.id).where(User.username == "groupadmin"))
    response = client.delete(
        f"/api/v1/users/{admin_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 409


def test_group_admin_cannot_create_login_users(identity_client) -> None:
    client, testing_session = identity_client
    token = login(client, "groupadmin", "test-admin-password")
    with testing_session() as session:
        shenzhen = session.scalar(select(Organization).where(Organization.code == "cmcc-gd-sz"))
    response = client.post(
        "/api/v1/users",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "username": "shouldfail",
            "employeeNo": "TEST-SHOULD-FAIL",
            "displayName": "无权新增用户",
            "password": "test-admin-password",
            "organizationId": str(shenzhen.id),
            "roleCode": "org_admin",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "只有超级管理员可以管理登录用户"
