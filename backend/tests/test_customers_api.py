from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.modules.organizations.models import Organization
from tests.test_identity_api import login

pytest_plugins = ("tests.test_identity_api",)


def _row(organization_id: str, external_id: str, name: str, owner: str = "待分配") -> dict:
    return {
        "externalId": external_id,
        "name": name,
        "organizationId": organization_id,
        "ownerName": owner,
        "kind": "新客",
        "industry": "软件与信息服务",
        "area": "福田区",
        "need": "企业专线与云服务",
        "score": 82,
        "isQianBaiWanGroup": "是",
        "isKeyAccount": "否",
    }


def test_import_list_and_detail_preserve_external_id(identity_client) -> None:
    client, testing_session = identity_client
    token = login(client, "groupadmin", "test-admin-password")
    headers = {"Authorization": f"Bearer {token}"}
    with testing_session() as session:
        department = session.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )

    imported = client.post(
        "/api/v1/customers/import",
        headers=headers,
        json={
            "sourceName": "pytest.csv",
            "rows": [_row(str(department.id), "c-test-001", "深圳测试云网有限公司")],
        },
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["data"]["inserted"] == 1

    listed = client.get("/api/v1/customers?page=1&pageSize=20", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["data"]["total"] == 1
    assert listed.json()["data"]["items"][0]["id"] == "c-test-001"

    detail = client.get("/api/v1/customers/c-test-001", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["name"] == "深圳测试云网有限公司"
    assert detail.json()["data"]["extraData"]["reasons"] == []


def test_duplicate_import_is_audited_without_second_customer(identity_client) -> None:
    client, testing_session = identity_client
    token = login(client, "groupadmin", "test-admin-password")
    headers = {"Authorization": f"Bearer {token}"}
    with testing_session() as session:
        department = session.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
    payload = {
        "sourceName": "duplicate.csv",
        "rows": [_row(str(department.id), "c-duplicate", "重复客户有限公司")],
    }
    first = client.post("/api/v1/customers/import", headers=headers, json=payload)
    assert first.status_code == 201, first.text
    duplicate = client.post("/api/v1/customers/import", headers=headers, json=payload)
    assert duplicate.status_code == 201
    assert duplicate.json()["data"]["inserted"] == 0
    assert duplicate.json()["data"]["duplicates"] == 1


def test_org_admin_cannot_import_into_sibling_city(identity_client) -> None:
    client, testing_session = identity_client
    super_token = login(client, "szyd", "test-super-password")
    with testing_session() as session:
        shenzhen = session.scalar(select(Organization).where(Organization.code == "cmcc-gd-sz"))
        guangzhou_department = session.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-gz-th-enterprise")
        )
    created = client.post(
        "/api/v1/users",
        headers={"Authorization": f"Bearer {super_token}"},
        json={
            "username": "customer-sz-admin",
            "employeeNo": "CUSTOMER-SZ-ADMIN",
            "displayName": "深圳客户管理员",
            "password": "test-admin-password",
            "organizationId": str(shenzhen.id),
            "roleCode": "org_admin",
        },
    )
    assert created.status_code == 201
    token = login(client, "customer-sz-admin", "test-admin-password")
    response = client.post(
        "/api/v1/customers/import",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "sourceName": "forbidden.csv",
            "rows": [
                _row(str(guangzhou_department.id), "c-forbidden", "广州越权测试有限公司")
            ],
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH.FORBIDDEN"


def test_customer_manager_only_reads_owned_customer(identity_client) -> None:
    client, testing_session = identity_client
    super_token = login(client, "szyd", "test-super-password")
    super_headers = {"Authorization": f"Bearer {super_token}"}
    with testing_session() as session:
        department = session.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
    created = client.post(
        "/api/v1/users",
        headers=super_headers,
        json={
            "username": "customer-owner",
            "employeeNo": "CUSTOMER-OWNER",
            "displayName": "客户归属经理",
            "password": "test-admin-password",
            "organizationId": str(department.id),
            "roleCode": "customer_manager",
        },
    )
    assert created.status_code == 201
    group_token = login(client, "groupadmin", "test-admin-password")
    imported = client.post(
        "/api/v1/customers/import",
        headers={"Authorization": f"Bearer {group_token}"},
        json={
            "sourceName": "ownership.csv",
            "rows": [
                _row(str(department.id), "c-owned", "本人客户有限公司", "客户归属经理"),
                _row(str(department.id), "c-other", "他人客户有限公司", "其他经理"),
            ],
        },
    )
    assert imported.status_code == 201, imported.text
    owner_token = login(client, "customer-owner", "test-admin-password")
    listed = client.get(
        "/api/v1/customers?page=1&pageSize=20",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert listed.status_code == 200
    assert listed.json()["data"]["total"] == 1
    assert listed.json()["data"]["items"][0]["id"] == "c-owned"


def test_customer_manager_imports_only_own_customer(identity_client) -> None:
    client, testing_session = identity_client
    super_token = login(client, "szyd", "test-super-password")
    with testing_session() as session:
        department = session.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
    created = client.post(
        "/api/v1/users",
        headers={"Authorization": f"Bearer {super_token}"},
        json={
            "username": "import-owner",
            "employeeNo": "IMPORT-OWNER",
            "displayName": "导入经理",
            "password": "test-admin-password",
            "organizationId": str(department.id),
            "roleCode": "customer_manager",
        },
    )
    assert created.status_code == 201
    headers = {
        "Authorization": f"Bearer {login(client, 'import-owner', 'test-admin-password')}"
    }
    own = client.post(
        "/api/v1/customers/import",
        headers=headers,
        json={"rows": [_row(str(department.id), "c-self", "本人导入有限公司", "导入经理")]},
    )
    assert own.status_code == 201, own.text
    assert own.json()["data"]["inserted"] == 1
    other = client.post(
        "/api/v1/customers/import",
        headers=headers,
        json={"rows": [_row(str(department.id), "c-other-import", "他人导入有限公司", "他人经理")]},
    )
    assert other.status_code == 403


def test_customer_edit_persists_and_rejects_stale_version(identity_client) -> None:
    client, testing_session = identity_client
    token = login(client, "groupadmin", "test-admin-password")
    headers = {"Authorization": f"Bearer {token}"}
    with testing_session() as session:
        department = session.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
    created = client.post(
        "/api/v1/customers/import",
        headers=headers,
        json={"rows": [_row(str(department.id), "c-edit", "编辑前企业有限公司")]},
    )
    assert created.status_code == 201
    original = client.get("/api/v1/customers/c-edit", headers=headers).json()["data"]
    payload = {
        "version": original["version"],
        "name": "编辑后企业有限公司",
        "industry": "信息服务",
        "contact": "张经理",
        "phone": "13800001234",
        "need": "企业专线",
        "stage": "已联系",
        "potential": "高",
        "score": 90,
        "nextAction": "预约拜访",
        "isQianBaiWanGroup": "是",
        "isKeyAccount": "否",
        "stageHistory": [{"toStage": "已联系"}],
        "reasons": ["测试更新"],
        "stageReason": "客户已通过电话联系",
    }
    updated = client.patch("/api/v1/customers/c-edit", headers=headers, json=payload)
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["version"] == original["version"] + 1
    reloaded = client.get("/api/v1/customers/c-edit", headers=headers).json()["data"]
    assert reloaded["name"] == payload["name"]
    assert reloaded["extraData"]["stageHistory"][-1]["operator"] == "测试集团管理员"
    assert reloaded["extraData"]["stageHistory"][-1]["fromStage"] == "线索入池"
    assert reloaded["extraData"]["reasons"] != payload["reasons"]
    assert {event["action"] for event in reloaded["auditEvents"]} >= {
        "customer_edited", "stage_changed"
    }
    stale = client.patch("/api/v1/customers/c-edit", headers=headers, json=payload)
    assert stale.status_code == 409


def test_ownership_requires_direct_supervisor_and_survives_reload(identity_client) -> None:
    client, testing_session = identity_client
    super_headers = {"Authorization": f"Bearer {login(client, 'szyd', 'test-super-password')}"}
    group_headers = {
        "Authorization": f"Bearer {login(client, 'groupadmin', 'test-admin-password')}"
    }
    with testing_session() as session:
        department = session.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
    for username, display_name, role in [
        ("requesting-manager", "申请经理", "customer_manager"),
        ("approving-manager", "直属主管", "department_manager"),
    ]:
        response = client.post(
            "/api/v1/users",
            headers=super_headers,
            json={
                "username": username,
                "employeeNo": username.upper(),
                "displayName": display_name,
                "password": "test-admin-password",
                "organizationId": str(department.id),
                "roleCode": role,
            },
        )
        assert response.status_code == 201, response.text
    imported = client.post(
        "/api/v1/customers/import",
        headers=group_headers,
        json={
            "rows": [
                _row(str(department.id), "c-approval", "审批测试企业有限公司", "申请经理")
            ]
        },
    )
    assert imported.status_code == 201
    applicant = {
        "Authorization": f"Bearer {login(client, 'requesting-manager', 'test-admin-password')}"
    }
    supervisor = {
        "Authorization": f"Bearer {login(client, 'approving-manager', 'test-admin-password')}"
    }
    visit = client.post(
        "/api/v1/visits",
        headers=applicant,
        json={
            "customerId": "c-approval",
            "ownerName": "申请经理",
            "time": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "method": "电话沟通",
            "purpose": "确认下一步安排",
        },
    )
    assert visit.status_code == 201, visit.text
    created = client.post(
        "/api/v1/customers/c-approval/requests",
        headers=applicant,
        json={
            "kind": "ownership",
            "reason": "客户经理职责调整",
            "targetOrganizationId": str(department.id),
            "targetOwnerName": "新负责人",
        },
    )
    assert created.status_code == 201, created.text
    request_id = created.json()["data"]["id"]
    assert client.get("/api/v1/customers/requests", headers=supervisor).json()["data"]
    self_review = client.post(
        f"/api/v1/customers/requests/{request_id}/decision",
        headers=applicant,
        json={"approve": True},
    )
    assert self_review.status_code == 403
    reviewed = client.post(
        f"/api/v1/customers/requests/{request_id}/decision",
        headers=supervisor,
        json={"approve": True, "comment": "同意调整"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["data"]["status"] == "approved"
    detail = client.get("/api/v1/customers/c-approval", headers=group_headers)
    assert detail.json()["data"]["ownerName"] == "新负责人"
    assert {event["action"] for event in detail.json()["data"]["auditEvents"]} == {
        "ownership_requested", "ownership_approved"
    }
    assert detail.json()["data"]["extraData"]["crmFlow"][-1]["toLabel"] == "新负责人"
    assert client.get("/api/v1/customers/c-approval", headers=applicant).status_code == 404
    transferred = client.get(f"/api/v1/visits/{visit.json()['data']['id']}", headers=group_headers)
    assert transferred.json()["data"]["owner"] == "新负责人"


def test_delete_requires_approval_and_is_soft_deleted(identity_client) -> None:
    client, testing_session = identity_client
    group_headers = {
        "Authorization": f"Bearer {login(client, 'groupadmin', 'test-admin-password')}"
    }
    super_headers = {"Authorization": f"Bearer {login(client, 'szyd', 'test-super-password')}"}
    with testing_session() as session:
        department = session.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
    imported = client.post(
        "/api/v1/customers/import",
        headers=group_headers,
        json={"rows": [_row(str(department.id), "c-soft-delete", "待删企业有限公司")]},
    )
    assert imported.status_code == 201
    created = client.post(
        "/api/v1/customers/c-soft-delete/requests",
        headers=group_headers,
        json={"kind": "delete", "reason": "企业主体已注销，申请归档"},
    )
    assert created.status_code == 201
    assert client.get("/api/v1/customers/c-soft-delete", headers=group_headers).status_code == 200
    request_id = created.json()["data"]["id"]
    approved = client.post(
        f"/api/v1/customers/requests/{request_id}/decision",
        headers=super_headers,
        json={"approve": True, "comment": "同意归档"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["reviewComment"] == "同意归档"
    assert client.get("/api/v1/customers/c-soft-delete", headers=group_headers).status_code == 404
    assert client.get("/api/v1/customers", headers=group_headers).json()["data"]["total"] == 0


def test_crm_edit_and_ownership_request_are_atomic(identity_client) -> None:
    client, testing_session = identity_client
    headers = {"Authorization": f"Bearer {login(client, 'groupadmin', 'test-admin-password')}"}
    with testing_session() as session:
        department = session.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
    assert client.post(
        "/api/v1/customers/import", headers=headers,
        json={"rows": [_row(str(department.id), "c-atomic", "原始客户有限公司")]},
    ).status_code == 201
    original = client.get("/api/v1/customers/c-atomic", headers=headers).json()["data"]
    edit = {
        "version": original["version"], "name": "已编辑客户有限公司",
        "industry": "信息技术", "contact": "张经理", "phone": "13800001111",
        "need": "云网融合", "stage": "已联系", "stageReason": "电话确认了初步需求",
        "potential": "高", "score": 99, "nextAction": "安排拜访",
        "isQianBaiWanGroup": "否", "isKeyAccount": "否",
        "stageHistory": [{"operator": "伪造审批人", "toStage": "已成交"}],
        "reasons": ["伪造评分原因"],
        "ownershipRequest": {
            "targetOrganizationId": str(department.id),
            "targetOwnerName": original["ownerName"],
            "reason": "客户经理分工重新调整",
        },
    }
    failed = client.patch("/api/v1/customers/c-atomic", headers=headers, json=edit)
    assert failed.status_code == 400
    unchanged = client.get("/api/v1/customers/c-atomic", headers=headers).json()["data"]
    assert unchanged["name"] == original["name"]
    assert unchanged["version"] == original["version"]
    assert unchanged["auditEvents"] == []
    assert client.get("/api/v1/customers/requests", headers=headers).json()["data"] == []

    edit["ownershipRequest"]["targetOwnerName"] = "新负责人"
    saved = client.patch("/api/v1/customers/c-atomic", headers=headers, json=edit)
    assert saved.status_code == 200, saved.text
    detail = client.get("/api/v1/customers/c-atomic", headers=headers).json()["data"]
    assert detail["name"] == "已编辑客户有限公司"
    assert detail["ownerName"] == original["ownerName"]
    assert len(detail["extraData"]["stageHistory"]) == 1
    assert detail["extraData"]["stageHistory"][0]["operator"] == "测试集团管理员"
    assert detail["score"] != 99
    assert detail["extraData"]["reasons"] != ["伪造评分原因"]
    assert {event["action"] for event in detail["auditEvents"]} == {
        "customer_edited", "stage_changed", "ownership_requested"
    }
    requests = client.get("/api/v1/customers/requests", headers=headers).json()["data"]
    assert len(requests) == 1
    assert requests[0]["status"] == "pending"
