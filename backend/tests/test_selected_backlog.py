import base64
import io
from urllib.error import HTTPError, URLError

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.security import hash_password
from app.integrations.llm.client import ask_provider
from app.modules.auth.models import Role, UserRole
from app.modules.documents.models import SharedDocument
from app.modules.organizations.models import Organization
from app.modules.users.models import User
from app.scripts.seed import reject_legacy_demo_passwords
from tests.test_identity_api import login

pytest_plugins = ("tests.test_identity_api",)


def headers(client, user="groupadmin", password="test-admin-password"):
    return {"Authorization": "Bearer " + login(client, user, password)}


def test_shared_settings_reject_demo_defaults():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="staging")


def test_shared_seed_refuses_existing_demo_password(identity_client):
    _, sessions = identity_client
    with sessions.begin() as db:
        user = db.scalar(select(User).where(User.username == "groupadmin"))
        user.password_hash = hash_password("szyd123456")
    with sessions() as db, pytest.raises(RuntimeError, match="demonstration password"):
        reject_legacy_demo_passwords(db)


def test_viewer_cannot_write_document_and_versions_restore(identity_client):
    client, sessions = identity_client
    with sessions.begin() as db:
        department = db.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
        role = db.scalar(select(Role).where(Role.code == "viewer"))
        viewer = User(
            username="readonly",
            employee_no="VIEW001",
            display_name="只读用户",
            password_hash=hash_password("viewer-test-pass"),
            organization_id=department.id,
            is_active=True,
        )
        db.add(viewer)
        db.flush()
        db.add(UserRole(user_id=viewer.id, role_id=role.id))
        department_id = str(department.id)
    group = headers(client)
    readonly = headers(client, "readonly", "viewer-test-pass")
    body = {
        "organizationId": department_id,
        "name": "policy.txt",
        "contentBase64": base64.b64encode(b"first").decode(),
    }
    assert client.post("/api/v1/documents", headers=readonly, json=body).status_code == 403
    created = client.post("/api/v1/documents", headers=group, json=body)
    assert created.status_code == 201, created.text
    doc = created.json()["data"]
    assert client.put(
        f"/api/v1/documents/{doc['id']}",
        headers=readonly,
        json={"version": 1, "name": "policy.txt", "contentBase64": body["contentBase64"]},
    ).status_code == 403
    updated = client.put(
        f"/api/v1/documents/{doc['id']}",
        headers=group,
        json={
            "version": 1,
            "name": "policy.txt",
            "contentBase64": base64.b64encode(b"second").decode(),
        },
    )
    assert updated.status_code == 200, updated.text
    versions = client.get(f"/api/v1/documents/{doc['id']}/versions", headers=readonly)
    assert [v["version"] for v in versions.json()["data"]["items"]] == [2, 1]
    assert client.post(
        f"/api/v1/documents/{doc['id']}/restore", headers=readonly,
        json={"version": 1, "currentVersion": 2},
    ).status_code == 403
    assert client.delete(f"/api/v1/documents/{doc['id']}", headers=readonly).status_code == 403
    restored = client.post(
        f"/api/v1/documents/{doc['id']}/restore", headers=group,
        json={"version": 1, "currentVersion": 2},
    )
    assert restored.status_code == 200 and restored.json()["data"]["version"] == 3
    downloaded = client.get(f"/api/v1/documents/{doc['id']}/download", headers=readonly)
    assert downloaded.content == b"first"
    assert client.post(
        "/api/v1/documents", headers=group,
        json={**body, "name": "spoof.pdf"},
    ).status_code == 400


def test_document_upload_fails_closed_when_scanner_unavailable(identity_client, monkeypatch):
    client, sessions = identity_client
    with sessions() as db:
        group = db.scalar(select(Organization).where(Organization.code == "cmcc"))
        group_id = str(group.id)

    def unavailable(_):
        raise AppError("DOCUMENT.SCAN_UNAVAILABLE", "scanner unavailable", 503)

    monkeypatch.setattr("app.modules.documents.router._scan", unavailable)
    result = client.post(
        "/api/v1/documents", headers=headers(client),
        json={"organizationId": group_id, "name": "safe.txt",
              "contentBase64": base64.b64encode(b"hello").decode()},
    )
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "DOCUMENT.SCAN_UNAVAILABLE"


def test_demo_batch_requires_explicit_rollback(identity_client):
    client, sessions = identity_client
    with sessions() as db:
        department = db.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
        department_id = str(department.id)
    group = headers(client)
    result = client.post(
        "/api/v1/customers/import", headers=group,
        json={"sourceName": "现有HTML客户数据手动迁移", "rows": [
            {
                "externalId": "DEMO-ROLLBACK-1",
                "name": "演示回滚企业",
                "organizationId": department_id,
            }
        ]},
    )
    assert result.status_code == 201, result.text
    batch = result.json()["data"]["batchId"]
    rollback = client.post(f"/api/v1/customers/imports/{batch}/rollback", headers=group)
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()["data"]["rolledBackCustomers"] == 1
    assert client.get("/api/v1/customers", headers=group).json()["data"]["total"] == 0
    second_rollback = client.post(f"/api/v1/customers/imports/{batch}/rollback", headers=group)
    assert second_rollback.status_code == 404


def test_cross_module_audit_records_failure_without_request_secret(identity_client, monkeypatch):
    client, sessions = identity_client
    monkeypatch.setattr("app.db.session.SessionLocal", sessions)
    group = headers(client)
    response = client.post(
        "/api/v1/documents", headers=group,
        json={"organizationId": "00000000-0000-0000-0000-000000000000",
              "name": "secret.txt", "contentBase64": base64.b64encode(b"api-key-private").decode()},
    )
    assert response.status_code == 403
    audit = client.get("/api/v1/reports/audit", headers=group)
    assert audit.status_code == 200
    rows = audit.json()["data"]
    assert any(row["action"] == "api_failure" and row["targetId"] == "/api/v1/documents"
               and "HTTP 403" in row["detail"] for row in rows)
    assert "api-key-private" not in str(rows)


def test_documents_page_beyond_500(identity_client):
    client, sessions = identity_client
    with sessions.begin() as db:
        user = db.scalar(select(User).where(User.username == "groupadmin"))
        docs = [
            SharedDocument(
                organization_id=user.organization_id,
                name=f"batch-{index:03}.txt",
                mime_type="text/plain",
                category="综合资料",
                version=1,
                content=b"x",
                size_bytes=1,
                created_by=user.id,
                updated_by=user.id,
            )
            for index in range(501)
        ]
        db.add_all(docs)
    result = client.get("/api/v1/documents?page=26&pageSize=20", headers=headers(client))
    assert result.status_code == 200
    assert result.json()["data"]["total"] == 501
    assert len(result.json()["data"]["items"]) == 1


@pytest.mark.parametrize("status,code", [(400, "AI.REQUEST_REJECTED"), (401, "AI.AUTH_FAILED"),
    (402, "AI.INSUFFICIENT_BALANCE"), (404, "AI.MODEL_NOT_FOUND"),
    (429, "AI.RATE_LIMITED"), (503, "AI.UPSTREAM_UNAVAILABLE")])
def test_model_provider_errors_are_specific(monkeypatch, status, code):
    def fail(*args, **kwargs):
        raise HTTPError("https://api.deepseek.com", status, "failure", None, io.BytesIO())
    monkeypatch.setattr("app.integrations.llm.client.request.urlopen", fail)
    with pytest.raises(AppError) as error:
        ask_provider(
            "deepseek", "deepseek-v4-flash", "test-not-real",
            [{"role": "user", "content": "hello"}],
        )
    assert error.value.code == code


def test_model_provider_network_timeout(monkeypatch):
    def timeout(*args, **kwargs):
        raise URLError("timed out")

    monkeypatch.setattr("app.integrations.llm.client.request.urlopen", timeout)
    with pytest.raises(AppError) as error:
        ask_provider("qwen", "qwen3.8-flash", "test-not-real", [{"role": "user", "content": "hi"}])
    assert error.value.code == "AI.UNAVAILABLE"
