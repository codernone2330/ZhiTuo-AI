import base64
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select

from app.core.security import hash_password
from app.db import models  # noqa: F401
from app.db.base import Base
from app.integrations.llm.client import ask_provider
from app.integrations.qichacha.client import search_companies
from app.modules.auth.models import Role, UserRole
from app.modules.organizations.models import Organization
from app.modules.users.models import User
from tests.test_identity_api import login

pytest_plugins = ("tests.test_identity_api",)


def headers(client, user="groupadmin", password="test-admin-password"):
    return {"Authorization": "Bearer " + login(client, user, password)}


def test_external_lead_requires_independent_review_and_approval(identity_client, monkeypatch):
    client, sessions = identity_client
    with sessions() as db:
        dept = db.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
    monkeypatch.setattr(
        "app.modules.ai.router.search_companies",
        lambda term: [
            {
                "providerKey": "K-1",
                "name": "企查查采集测试有限公司",
                "establishedAt": "2026-01-01",
                "address": "深圳市福田区",
            }
        ],
    )
    group = headers(client)
    captured = client.post(
        "/api/v1/ai/leads/capture",
        headers=group,
        json={"searchTerm": "云计算", "organizationId": str(dept.id)},
    )
    assert captured.status_code == 201, captured.text
    lead = captured.json()["data"]["items"][0]
    assert client.get("/api/v1/customers", headers=group).json()["data"]["total"] == 0
    own = client.post(
        f"/api/v1/ai/leads/{lead['id']}/decision",
        headers=group,
        json={"approve": True, "reason": "核对企业信息", "version": 1},
    )
    assert own.status_code == 403
    super_admin = headers(client, "szyd", "test-super-password")
    approved = client.post(
        f"/api/v1/ai/leads/{lead['id']}/decision",
        headers=super_admin,
        json={"approve": True, "reason": "核对企业信息", "version": 1},
    )
    assert approved.status_code == 200, approved.text
    assert client.get("/api/v1/customers", headers=group).json()["data"]["total"] == 1
    assert (
        client.post(
            f"/api/v1/ai/leads/{lead['id']}/decision",
            headers=super_admin,
            json={"approve": True, "reason": "重复审核", "version": 1},
        ).status_code
        == 409
    )


def test_ai_gateway_uses_server_scope_and_never_sends_contact(identity_client, monkeypatch):
    client, sessions = identity_client
    group = headers(client)
    with sessions() as db:
        dept = db.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
    imported = client.post(
        "/api/v1/customers/import",
        headers=group,
        json={
            "rows": [
                {
                    "externalId": "AI-1",
                    "name": "AI 网关测试企业有限公司",
                    "organizationId": str(dept.id),
                    "contact": "私密联系人",
                    "phone": "13900000000",
                    "kind": "新客",
                }
            ]
        },
    )
    assert imported.status_code == 201
    sent = {}

    def fake_ask(provider, model, key, messages):
        sent.update(provider=provider, model=model, key=key, messages=messages)
        return {"content": "授权范围有 1 家客户", "usage": {}}

    monkeypatch.setattr("app.modules.ai.router.ask_provider", fake_ask)
    answer = client.post(
        "/api/v1/ai/chat",
        headers=group,
        json={
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "apiKey": "sk-test-not-real-123",
            "question": "有多少客户？",
            "history": [],
        },
    )
    assert answer.status_code == 200, answer.text
    assert "customerCount': 1" in sent["messages"][0]["content"]
    assert "私密联系人" not in str(sent["messages"])
    assert "13900000000" not in str(sent["messages"])
    assert "sk-test-not-real-123" not in str(
        client.get("/api/v1/reports/audit", headers=group).json()
    )


def test_weekly_report_has_comparison_and_drill_detail(identity_client):
    client, sessions = identity_client
    group = headers(client)
    with sessions() as db:
        dept = db.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
    created = client.post(
        "/api/v1/customers/import",
        headers=group,
        json={
            "rows": [
                {
                    "externalId": "REPORT-1",
                    "name": "报表测试企业有限公司",
                    "organizationId": str(dept.id),
                }
            ]
        },
    )
    assert created.status_code == 201
    planned = client.post(
        "/api/v1/visits",
        headers=group,
        json={
            "customerId": "REPORT-1",
            "ownerName": "报表经理",
            "time": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "method": "上门拜访",
            "purpose": "测试周经分",
        },
    )
    assert planned.status_code == 201, planned.text
    visit = planned.json()["data"]
    completed = client.post(
        f"/api/v1/visits/{visit['id']}/complete",
        headers=group,
        json={
            "version": visit["version"],
            "outcome": "已达成合作",
            "stage": "已成交",
            "notes": "已签约",
            "nextAction": "交付",
            "dealProduct": "企业专线",
            "dealAmount": "5000.00",
        },
    )
    assert completed.status_code == 200, completed.text
    report = client.get("/api/v1/reports/weekly", headers=group)
    assert report.status_code == 200, report.text
    data = report.json()["data"]
    assert data["current"]["dealAmount"] == "5000.00"
    assert data["comparisons"]["dealAmount"]["momPercent"] is None
    assert data["details"][0]["customerId"] == "REPORT-1"
    assert data["owners"][0]["owner"] == "报表经理"


def test_documents_version_and_audit(identity_client):
    client, sessions = identity_client
    group = headers(client)
    with sessions() as db:
        dept = db.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
    content = base64.b64encode(b"report data").decode()
    payload = {
        "organizationId": str(dept.id),
        "name": "政策资料.txt",
        "category": "政策",
        "mimeType": "text/plain",
        "contentBase64": content,
    }
    created = client.post("/api/v1/documents", headers=group, json=payload)
    assert created.status_code == 201, created.text
    doc = created.json()["data"]
    assert client.get("/api/v1/documents", headers=group).json()["data"]["total"] == 1
    download = client.get(f"/api/v1/documents/{doc['id']}/download", headers=group)
    assert download.content == b"report data"
    stale = client.put(
        f"/api/v1/documents/{doc['id']}", headers=group, json={**payload, "version": 2}
    )
    assert stale.status_code == 409
    updated = client.put(
        f"/api/v1/documents/{doc['id']}", headers=group, json={**payload, "version": 1}
    )
    assert updated.status_code == 200
    actions = [
        row["action"]
        for row in client.get(f"/api/v1/documents/{doc['id']}/events", headers=group).json()["data"]
    ]
    assert set(actions) == {"created", "downloaded", "updated"}


def test_shared_document_blocks_sibling_city(identity_client):
    client, sessions = identity_client
    group = headers(client)
    with sessions.begin() as db:
        shenzhen = db.scalar(select(Organization).where(Organization.code == "cmcc-gd-sz"))
        guangzhou = db.scalar(select(Organization).where(Organization.code == "cmcc-gd-gz"))
        role = db.scalar(select(Role).where(Role.code == "org_admin"))
        manager = User(
            username="sz-document-admin",
            employee_no="TEST-DOC-SZ",
            display_name="深圳资料管理员",
            password_hash=hash_password("test-doc-password"),
            organization_id=shenzhen.id,
            is_active=True,
        )
        db.add(manager)
        db.flush()
        db.add(UserRole(user_id=manager.id, role_id=role.id))
    created = client.post(
        "/api/v1/documents",
        headers=group,
        json={
            "organizationId": str(guangzhou.id),
            "name": "广州资料.txt",
            "category": "政策",
            "mimeType": "text/plain",
            "contentBase64": base64.b64encode(b"secret").decode(),
        },
    )
    assert created.status_code == 201, created.text
    doc_id = created.json()["data"]["id"]
    shenzhen_headers = headers(client, "sz-document-admin", "test-doc-password")
    assert client.get("/api/v1/documents", headers=shenzhen_headers).json()["data"]["total"] == 0
    assert (
        client.get(f"/api/v1/documents/{doc_id}/download", headers=shenzhen_headers).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/v1/documents/{doc_id}",
            headers=shenzhen_headers,
            json={
                "name": "广州资料.txt",
                "category": "政策",
                "mimeType": "text/plain",
                "contentBase64": base64.b64encode(b"bad").decode(),
                "version": 1,
            },
        ).status_code
        == 403
    )


def test_week6_week7_migration_creates_all_new_tables():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    new_names = {"external_leads", "integration_audits", "shared_documents", "document_events"}
    with engine.begin() as conn:
        Base.metadata.create_all(
            conn,
            tables=[table for table in Base.metadata.sorted_tables if table.name not in new_names],
        )
        path = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "versions"
            / "20260924_0008_collection_reports_docs.py"
        )
        spec = importlib.util.spec_from_file_location("migration_week6_week7", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        module.op = Operations(MigrationContext.configure(conn))
        module.upgrade()
        assert new_names.issubset(inspect(conn).get_table_names())


def test_qcc_adapter_signs_request_and_filters_inactive(monkeypatch):
    from types import SimpleNamespace

    from pydantic import SecretStr

    monkeypatch.setattr(
        "app.integrations.qichacha.client.get_settings",
        lambda: SimpleNamespace(
            qcc_app_key=SecretStr("test-public-key"), qcc_secret_key=SecretStr("test-secret-key")
        ),
    )
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def fake_open(req, timeout):
        seen["url"] = req.full_url
        seen["token"] = req.headers.get("Token")
        assert timeout == 20
        return FakeResponse()

    monkeypatch.setattr("app.integrations.qichacha.client.request.urlopen", fake_open)
    monkeypatch.setattr(
        "app.integrations.qichacha.client.json.load",
        lambda response: {
            "Status": "200",
            "Result": [
                {"KeyNo": "A", "Name": "有效企业", "Status": "存续", "StartDate": "2026-06-01"},
                {"KeyNo": "B", "Name": "已注销企业", "Status": "注销"},
            ],
        },
    )
    result = search_companies("企业专线")
    assert len(result) == 1
    assert result[0]["providerKey"] == "A"
    assert "searchKey=" in seen["url"] and seen["token"]
    assert "test-secret-key" not in seen["url"]


def test_deepseek_display_alias_uses_current_provider_model(monkeypatch):
    import json

    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def fake_open(req, timeout):
        seen["body"] = json.loads(req.data)
        seen["url"] = req.full_url
        assert timeout == 45
        return FakeResponse()

    monkeypatch.setattr("app.integrations.llm.client.request.urlopen", fake_open)
    monkeypatch.setattr(
        "app.integrations.llm.client.json.load",
        lambda response: {"choices": [{"message": {"content": "连接成功"}}]},
    )
    result = ask_provider(
        "deepseek", "deepseek-v4-flash", "test-key-123456", [{"role": "user", "content": "测试"}]
    )
    assert result["content"] == "连接成功"
    assert seen["body"]["model"] == "deepseek-flash"
    assert seen["url"].startswith("https://api.deepseek.com/")
