"""客户导入判重规则回归测试。

背景：某批 5000 家企查查清单信用代码全部唯一、同名 0 条，却有 235 家仅因
「与其他企业共用同一个联系电话」被判为重复并静默丢弃（同一集团下的多家主体
常共用联系人电话）。因此手机号判重只在导入行没有自带唯一外部标识时启用。
"""

from sqlalchemy import select

from app.modules.customers.models import Customer
from app.modules.organizations.models import Organization
from tests.test_identity_api import login

pytest_plugins = ("tests.test_identity_api",)


def _auth(client) -> dict:
    return {"Authorization": f"Bearer {login(client, 'groupadmin', 'test-admin-password')}"}


def _department_id(testing_session) -> str:
    with testing_session() as session:
        department = session.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
        return str(department.id)


def _import(client, org_id: str, rows: list[dict]):
    return client.post(
        "/api/v1/customers/import",
        headers=_auth(client),
        json={"sourceName": "企查查·高级搜索导入", "rows": rows},
    )


def test_shared_phone_with_distinct_credit_codes_is_kept(identity_client) -> None:
    """两家不同主体共用同一电话 → 都应入库，不得判重。"""
    client, testing_session = identity_client
    org_id = _department_id(testing_session)
    phone = "18160783362"
    response = _import(
        client,
        org_id,
        [
            {
                "externalId": "91440300MADJ21Q31M",
                "name": "深圳熙恩医疗管理合伙企业（有限合伙）",
                "organizationId": org_id,
                "phone": phone,
            },
            {
                "externalId": "91440300MAD9H2LT8G",
                "name": "深圳熙恩医疗美容门诊部",
                "organizationId": org_id,
                "phone": phone,
            },
        ],
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["inserted"] == 2, data
    assert data["duplicates"] == 0, data

    with testing_session() as session:
        names = set(session.scalars(select(Customer.name)).all())
    assert names == {"深圳熙恩医疗管理合伙企业（有限合伙）", "深圳熙恩医疗美容门诊部"}


def test_reimporting_same_credit_code_is_deduplicated(identity_client) -> None:
    """同一信用代码重复导入 → 仍应判重（唯一外部标识仍然生效）。"""
    client, testing_session = identity_client
    org_id = _department_id(testing_session)
    row = {
        "externalId": "91440300MAD284TK5Q",
        "name": "深圳赋智万物科技有限公司",
        "organizationId": org_id,
        "phone": "13530893855",
    }
    assert _import(client, org_id, [row]).json()["data"]["inserted"] == 1
    again = _import(client, org_id, [row])
    assert again.status_code == 201, again.text
    assert again.json()["data"]["inserted"] == 0
    assert again.json()["data"]["duplicates"] == 1


def test_phone_dedup_applies_to_interactive_import(identity_client) -> None:
    """交互式导入带的是前端本地占位 ID（c-xxxx），不是企业身份 → 手机号判重必须仍生效。

    回归背景：曾经用「有没有传 externalId」来判断是否豁免手机号判重，但前端
    `customerImportPayload` 一直会传自己的本地 id（形如 `c-m1abc2x3y`），
    结果交互式导入（新增线索 / CSV 导入 / 演示数据迁移）的手机号判重被整体关掉，
    同一组织内同一个电话可以重复录入且不报重复。
    """
    client, testing_session = identity_client
    org_id = _department_id(testing_session)
    phone = "13900000000"

    # ① 完全不传 externalId
    first = _import(
        client, org_id, [{"name": "深圳手工录入甲公司", "organizationId": org_id, "phone": phone}]
    )
    assert first.status_code == 201, first.text
    assert first.json()["data"]["inserted"] == 1

    # ② 传前端本地占位 ID —— 真实 UI 的行为，同样必须判重
    second = _import(
        client,
        org_id,
        [
            {
                "externalId": "c-m1abc2x3y",
                "name": "深圳手工录入乙公司",
                "organizationId": org_id,
                "phone": phone,
            }
        ],
    )
    assert second.status_code == 201, second.text
    assert second.json()["data"]["inserted"] == 0, second.json()["data"]
    assert second.json()["data"]["duplicates"] == 1


def test_business_identity_predicate() -> None:
    """只会把 18 位统一社会信用代码认作真实业务身份。"""
    from app.modules.customers.service import _has_business_identity

    assert _has_business_identity("91440300MADJ21Q31M") is True
    assert _has_business_identity("91440300madj21q31m") is True  # 小写同样认可
    assert _has_business_identity(" 91440300MADJ21Q31M ") is True
    assert _has_business_identity("c-m1abc2x3y") is False  # 前端本地占位 ID
    assert _has_business_identity("C-ABC123") is False
    assert _has_business_identity("91440300MADJ21Q31") is False  # 位数不足
    assert _has_business_identity("") is False
    assert _has_business_identity(None) is False
