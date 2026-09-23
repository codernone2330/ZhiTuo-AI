from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.modules.organizations.models import Organization
from tests.test_identity_api import login

pytest_plugins = ("tests.test_identity_api",)


def _headers(client, username="groupadmin", password="test-admin-password"):
    return {"Authorization": f"Bearer {login(client, username, password)}"}


def _customer(client, testing_session, headers, ref="c-visit-test"):
    with testing_session() as session:
        department = session.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
    response = client.post(
        "/api/v1/customers/import",
        headers=headers,
        json={
            "rows": [
                {
                    "externalId": ref,
                    "name": f"拜访测试企业{ref}有限公司",
                    "organizationId": str(department.id),
                    "ownerName": "测试经理",
                    "kind": "新客",
                    "need": "企业专线与云网融合",
                    "score": 73,
                }
            ]
        },
    )
    assert response.status_code == 201, response.text
    return ref, department


def _visit_payload(customer_id, **kwargs):
    return {
        "customerId": customer_id,
        "ownerName": "测试经理",
        "time": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "method": "上门拜访",
        "purpose": "确认企业专线需求",
        "notes": "准备产品方案",
        **kwargs,
    }


def test_opportunity_is_derived_from_authorized_customer(identity_client):
    client, testing_session = identity_client
    headers = _headers(client)
    ref, department = _customer(client, testing_session, headers)
    listed = client.get("/api/v1/opportunities", headers=headers)
    assert listed.status_code == 200, listed.text
    item = listed.json()["data"]["items"][0]
    assert item["customerId"] == ref
    assert item["productDirections"][0]["name"] == "企业专线"
    detail_opportunity = client.get(f"/api/v1/opportunities/{ref}", headers=headers)
    assert detail_opportunity.json()["data"]["customerId"] == ref
    refreshed = client.post(
        f"/api/v1/opportunities/refresh?organizationId={department.id}", headers=headers
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["data"]["total"] == 1
    assert refreshed.json()["data"]["top"][0]["customerId"] == ref
    detail = client.get(f"/api/v1/customers/{ref}", headers=headers)
    assert detail.json()["data"]["score"] == refreshed.json()["data"]["top"][0]["score"]


def test_visit_create_duplicate_override_edit_cancel_and_reload(identity_client):
    client, testing_session = identity_client
    headers = _headers(client)
    ref, _ = _customer(client, testing_session, headers)
    created = client.post("/api/v1/visits", headers=headers, json=_visit_payload(ref))
    assert created.status_code == 201, created.text
    task = created.json()["data"]
    assert task["customerId"] == ref
    assert client.get("/api/v1/visits", headers=headers).json()["data"]["total"] == 1
    duplicate = client.post("/api/v1/visits", headers=headers, json=_visit_payload(ref))
    assert duplicate.status_code == 409
    override = client.post(
        "/api/v1/visits",
        headers=headers,
        json=_visit_payload(ref, overrideReason="客户要求两组分别拜访"),
    )
    assert override.status_code == 201, override.text
    assert override.json()["data"]["conflictTaskId"] == task["id"]
    changed = client.patch(
        f"/api/v1/visits/{task['id']}",
        headers=headers,
        json={
            "version": task["version"],
            "ownerName": "测试经理",
            "time": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
            "method": "视频会议",
            "purpose": "确认带宽和预算",
            "notes": "方案已备齐",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["data"]["version"] == 2
    stale = client.patch(
        f"/api/v1/visits/{task['id']}",
        headers=headers,
        json={
            "version": 1,
            "ownerName": "测试经理",
            "time": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
            "method": "视频会议",
            "purpose": "确认带宽和预算",
        },
    )
    assert stale.status_code == 409
    canceled = client.post(
        f"/api/v1/visits/{task['id']}/cancel",
        headers=headers,
        json={"version": 2, "reason": "客户主动申请改期"},
    )
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["data"]["status"] == "canceled"
    reloaded = client.get(f"/api/v1/visits/{task['id']}", headers=headers)
    assert reloaded.json()["data"]["status"] == "canceled"


def test_visit_completion_updates_customer_and_deal_atomically(identity_client):
    client, testing_session = identity_client
    headers = _headers(client)
    ref, _ = _customer(client, testing_session, headers)
    created = client.post("/api/v1/visits", headers=headers, json=_visit_payload(ref))
    task = created.json()["data"]
    completed = client.post(
        f"/api/v1/visits/{task['id']}/complete",
        headers=headers,
        json={
            "version": task["version"],
            "outcome": "已达成合作",
            "stage": "已成交",
            "notes": "客户确认采购企业专线",
            "nextAction": "安排交付",
            "dealProduct": "企业专线",
            "dealAmount": "120000.00",
            "aiStructured": {"need": "企业专线"},
        },
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["data"]["visit"]["dealAmount"] == 120000.0
    customer = client.get(f"/api/v1/customers/{ref}", headers=headers).json()["data"]
    assert customer["stage"] == "已成交"
    assert customer["extraData"]["lastContact"]
    assert customer["extraData"]["stageHistory"][-1]["source"] == "拜访结果回填"
    assert "近期已有互动" in customer["extraData"]["reasons"][-1]
    assert client.get("/api/v1/opportunities", headers=headers).json()["data"]["total"] == 0
    again = client.post(
        f"/api/v1/visits/{task['id']}/complete",
        headers=headers,
        json={
            "version": task["version"],
            "outcome": "已达成合作",
            "stage": "已成交",
            "notes": "再次提交",
            "dealProduct": "企业专线",
            "dealAmount": "120000.00",
        },
    )
    assert again.status_code == 409


def test_visit_import_is_idempotent(identity_client):
    client, testing_session = identity_client
    headers = _headers(client)
    ref, _ = _customer(client, testing_session, headers)
    payload = {"rows": [{"externalId": "legacy-visit-1", **_visit_payload(ref)}]}
    first = client.post("/api/v1/visits/import", headers=headers, json=payload)
    second = client.post("/api/v1/visits/import", headers=headers, json=payload)
    assert first.status_code == 201, first.text
    assert first.json()["data"]["inserted"] == 1
    assert second.status_code == 201
    assert second.json()["data"]["duplicates"] == 1


def test_department_customer_manager_sees_only_own_tasks_and_cannot_edit_other_owner(
    identity_client,
):
    client, testing_session = identity_client
    super_headers = _headers(client, "szyd", "test-super-password")
    group_headers = _headers(client)
    ref, department = _customer(client, testing_session, group_headers)
    manager = client.post(
        "/api/v1/users",
        headers=super_headers,
        json={
            "username": "visit-manager",
            "employeeNo": "VISIT-MANAGER",
            "displayName": "测试经理",
            "password": "test-admin-password",
            "organizationId": str(department.id),
            "roleCode": "customer_manager",
        },
    )
    assert manager.status_code == 201, manager.text
    group_task = client.post("/api/v1/visits", headers=group_headers, json=_visit_payload(ref))
    assert group_task.status_code == 201, group_task.text
    manager_headers = _headers(client, "visit-manager", "test-admin-password")
    listed = client.get("/api/v1/visits", headers=manager_headers)
    assert listed.json()["data"]["total"] == 1
    wrong_owner = client.post(
        "/api/v1/visits",
        headers=manager_headers,
        json=_visit_payload(ref, ownerName="其他人", overrideReason="客户要求另外分配人员"),
    )
    assert wrong_owner.status_code == 403
    own_opportunities = client.get("/api/v1/opportunities", headers=manager_headers)
    assert own_opportunities.json()["data"]["total"] == 1


def test_org_scope_blocks_sibling_opportunity_refresh(identity_client):
    client, testing_session = identity_client
    super_headers = _headers(client, "szyd", "test-super-password")
    with testing_session() as session:
        shenzhen = session.scalar(select(Organization).where(Organization.code == "cmcc-gd-sz"))
        guangzhou = session.scalar(select(Organization).where(Organization.code == "cmcc-gd-gz"))
    created = client.post(
        "/api/v1/users",
        headers=super_headers,
        json={
            "username": "sz-visit-admin",
            "employeeNo": "SZ-VISIT-ADMIN",
            "displayName": "深圳拜访管理员",
            "password": "test-admin-password",
            "organizationId": str(shenzhen.id),
            "roleCode": "org_admin",
        },
    )
    assert created.status_code == 201
    headers = _headers(client, "sz-visit-admin", "test-admin-password")
    response = client.post(
        f"/api/v1/opportunities/refresh?organizationId={guangzhou.id}", headers=headers
    )
    assert response.status_code == 403
