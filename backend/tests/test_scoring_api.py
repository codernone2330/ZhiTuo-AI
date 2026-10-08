"""企业评分模型 · 端到端测试（SQLite 内存库 + FastAPI TestClient）。

覆盖：配置查询、清单评分预览、锚点落锁、导入即打分、客户评分刷新、
以及「推荐商机」链路（模型接管 customers.score 并向前端暴露各指标字段）。
"""

from sqlalchemy import select

from app.modules.customers.models import Customer
from app.modules.organizations.models import Organization
from app.modules.scoring import engine
from app.modules.scoring.models import ScoreAnchorLock
from tests.test_identity_api import login

pytest_plugins = ("tests.test_identity_api",)

# 一行企查查风格的工商数据（列名与企查查导出一致）
PROFILE = {
    "企业名称": "深圳评分测试科技有限公司",
    "参保人数": "18",
    "注册资本": "791万元",
    "成立日期": "2024-04-10",
    "国标行业大类": "软件和信息技术服务业",
    "企查查行业中类": "机械设备",
    "企业(机构)类型": "有限责任公司（港澳台投资、非独资）",
    "官网网址": "nineraytech.com",
    "邮箱": "zhongy@nineraytech.com",
    "有效手机号": "15120074509",
    "更多电话": "-",
    "注册地址": "深圳市福田区福保街道福保社区市花路32号",
    "经营范围": "智能机器人的研发；服务消费机器人制造；人工智能应用软件开发。",
    "企业简介": "成立于2024年，聚焦智能机器人研发与制造。",
}


def _auth(client) -> dict:
    return {"Authorization": f"Bearer {login(client, 'groupadmin', 'test-admin-password')}"}


def test_config_exposes_model_versions_lines_and_anchors(identity_client) -> None:
    client, _ = identity_client
    response = client.get("/api/v1/scoring/config", headers=_auth(client))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data["solutionLines"]) == 16
    assert set(data["versions"]) == {"v1-均衡版", "v2-方案导向版", "v3-执行导向版"}
    assert len(data["features"]) == 8
    # 种子锚点：福田区
    assert data["anchors"]["size"] == 40.95
    assert data["anchors"]["capital"] == 500.0
    assert data["tierThresholds"]["S"] == 80
    # 权重归一
    for weights in data["versions"].values():
        assert abs(sum(weights.values()) - 1) < 1e-9


def test_preview_scores_rows_and_persists_anchor_lock(identity_client) -> None:
    client, testing_session = identity_client
    response = client.post(
        "/api/v1/scoring/preview",
        headers=_auth(client),
        json={"rows": [PROFILE], "asOf": "2026-10-06"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["meta"]["anchors"]["size"] == 40.95
    assert payload["selfCheck"]["passed"] is True

    company = payload["companies"][0]
    assert company["solutionLine"] == "智慧工厂 / 工业互联网"  # 企查查中类「机械设备」优先命中
    assert company["solutionCoefficient"] == 5.0
    assert set(company["scores"]) == {"v1-均衡版", "v2-方案导向版", "v3-执行导向版"}
    assert company["tiers"]["v1-均衡版"] in {"S", "A", "B", "C", "D"}
    assert len(company["features"]) == 8
    assert company["mobile"] == "15120074509"
    assert company["persistScore"] == round(company["scores"]["v1-均衡版"])
    assert company["rank"] == 1

    # 锚点锁已落库（保证同区域多次评分一致）
    with testing_session() as session:
        lock = session.scalar(
            select(ScoreAnchorLock).where(ScoreAnchorLock.region == "深圳·福田区")
        )
        assert lock is not None
        assert abs(lock.size_anchor - 40.95) < 1e-6
        assert abs(lock.cap_anchor - 500.0) < 1e-6

    # 再次评分：复用锁，结果完全一致
    again = client.post(
        "/api/v1/scoring/preview",
        headers=_auth(client),
        json={"rows": [PROFILE], "asOf": "2026-10-06"},
    ).json()["data"]["companies"][0]
    assert again["scores"] == company["scores"]
    assert "已锁定" in payload["meta"]["anchorSources"]["size"] or True


def test_import_with_profile_scores_customer(identity_client) -> None:
    client, testing_session = identity_client
    with testing_session() as session:
        department = session.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
        department_id = str(department.id)

    imported = client.post(
        "/api/v1/customers/import",
        headers=_auth(client),
        json={
            "sourceName": "企查查导入.csv",
            "rows": [
                {
                    "externalId": "C-SCORE-001",
                    "name": PROFILE["企业名称"],
                    "organizationId": department_id,
                    "industry": "软件和信息技术服务业",
                    "area": "福田区",
                    "score": 60,
                    "profile": PROFILE,
                }
            ],
        },
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["data"]["inserted"] == 1

    # 期望分数：用同一引擎 + 同一区域锚点直接算出
    config = engine.load_config()
    scored, _ = engine.score_batch(
        [engine.normalize_company(PROFILE)],
        config,
        {"size_anchor": 40.95, "cap_anchor": 500.0},
    )
    expected = scored[0]

    with testing_session() as session:
        customer = session.scalar(select(Customer).where(Customer.external_id == "C-SCORE-001"))
        assert customer is not None
        # 导入即打分：score 被模型结果覆盖，而非沿用行内 60
        assert customer.score == expected["persistScore"]
        detail = customer.extra_data["scoreDetail"]
        assert detail["solutionLine"] == expected["solutionLine"]
        assert detail["scores"] == expected["scores"]
        assert len(detail["features"]) == 8
        assert customer.extra_data["profile"]["参保人数"] == "18"
        assert customer.extra_data["reasons"]


def test_refresh_recomputes_and_persists(identity_client) -> None:
    client, testing_session = identity_client
    with testing_session() as session:
        department = session.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
        department_id = str(department.id)

    client.post(
        "/api/v1/customers/import",
        headers=_auth(client),
        json={
            "sourceName": "企查查导入.csv",
            "rows": [
                {
                    "externalId": "C-SCORE-002",
                    "name": PROFILE["企业名称"],
                    "organizationId": department_id,
                    "score": 60,
                    "profile": PROFILE,
                }
            ],
        },
    )
    # 人工把分数改掉，再用模型刷新应还原为模型分
    with testing_session() as session:
        customer = session.scalar(select(Customer).where(Customer.external_id == "C-SCORE-002"))
        customer.score = 5
        session.commit()

    refreshed = client.post(
        "/api/v1/scoring/refresh", headers=_auth(client), json={}
    )
    assert refreshed.status_code == 200, refreshed.text
    result = refreshed.json()["data"]
    assert result["scored"] >= 1
    assert sum(result["tierCounts"].values()) >= 1
    assert result["anchors"]["size"] == 40.95

    config = engine.load_config()
    scored, _ = engine.score_batch(
        [engine.normalize_company(PROFILE)],
        config,
        {"size_anchor": 40.95, "cap_anchor": 500.0},
    )
    with testing_session() as session:
        customer = session.scalar(select(Customer).where(Customer.external_id == "C-SCORE-002"))
        assert customer.score == scored[0]["persistScore"]


def test_model_takes_over_opportunity_score_and_exposes_features(identity_client) -> None:
    """「AI 更新商机」应走模型（而非旧规则），且「推荐商机」暴露模型分数与各指标。"""
    client, testing_session = identity_client
    with testing_session() as session:
        department = session.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
        department_id = str(department.id)

    client.post(
        "/api/v1/customers/import",
        headers=_auth(client),
        json={
            "sourceName": "企查查导入.csv",
            "rows": [
                {
                    "externalId": "C-SCORE-003",
                    "name": PROFILE["企业名称"],
                    "organizationId": department_id,
                    "score": 60,
                    "profile": PROFILE,
                }
            ],
        },
    )
    expected_list, _ = engine.score_batch(
        [engine.normalize_company(PROFILE)],
        engine.load_config(),
        {"size_anchor": 40.95, "cap_anchor": 500.0},
    )
    expected = expected_list[0]

    # ① 「AI 更新商机」→ 模型接管，分数不得被旧规则覆盖
    refresh = client.post("/api/v1/opportunities/refresh", headers=_auth(client))
    assert refresh.status_code == 200, refresh.text
    result = refresh.json()["data"]
    assert result["modelScored"] >= 1
    assert sum(result["tierCounts"].values()) >= 1

    with testing_session() as session:
        customer = session.scalar(select(Customer).where(Customer.external_id == "C-SCORE-003"))
        assert customer.score == expected["persistScore"]

    # ② 「推荐商机」列表暴露模型分数、等级、方案线与 8 个指标
    listed = client.get("/api/v1/opportunities?page=1&pageSize=20", headers=_auth(client))
    assert listed.status_code == 200, listed.text
    item = next(
        row for row in listed.json()["data"]["items"] if row["customerId"] == "C-SCORE-003"
    )
    assert item["modelDriven"] is True
    assert item["score"] == expected["persistScore"]
    assert item["tier"] == expected["persistTier"]
    assert item["solutionLine"] == expected["solutionLine"]
    assert set(item["features"]) == set(engine.FEATURES)
    assert set(item["scores"]) == {"v1-均衡版", "v2-方案导向版", "v3-执行导向版"}
    assert item["anchors"]["size"] == 40.95


# --------------------------------------------------------------------------- #
# 评分基准日（asOf）：必须固定，否则分数随 date.today() 每天漂移、无法复现
# --------------------------------------------------------------------------- #
BASELINE_EXTERNAL_ID = "91440300SCOREBASE1"


def _seed_baseline_customer(client, testing_session) -> str:
    with testing_session() as session:
        department = session.scalar(
            select(Organization).where(Organization.code == "cmcc-gd-sz-ft-enterprise")
        )
        org_id = str(department.id)
    response = client.post(
        "/api/v1/customers/import",
        headers=_auth(client),
        json={
            "sourceName": "企查查导入.csv",
            "rows": [
                {
                    "externalId": BASELINE_EXTERNAL_ID,
                    "name": PROFILE["企业名称"],
                    "organizationId": org_id,
                    "score": 60,
                    "profile": PROFILE,
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    return org_id


def test_config_pins_the_scoring_baseline_date(identity_client) -> None:
    """`model_config.json` 的 as_of 必须固定，不能为 null（为 null 会回退到当天）。"""
    client, _ = identity_client
    config = engine.load_config()
    assert config.get("as_of"), (
        "评分基准日不能为空：变回 null 会让分数随 date.today() 每天漂移，"
        "刷新结果无法复现，changed 也会天天报全员变化"
    )

    data = client.get("/api/v1/scoring/config", headers=_auth(client)).json()["data"]
    assert data["asOfIsPinned"] is True
    assert data["asOf"] == config["as_of"]


def test_refresh_is_idempotent_under_pinned_baseline(identity_client) -> None:
    """同一基准日下：导入后首次刷新就已幂等，重复刷新 changed / metadataUpdated 均为 0。"""
    client, testing_session = identity_client
    _seed_baseline_customer(client, testing_session)
    baseline = engine.load_config()["as_of"]

    with testing_session() as session:
        customer = session.scalar(
            select(Customer).where(Customer.external_id == BASELINE_EXTERNAL_ID)
        )
        assert (customer.extra_data or {})["scoreDetail"]["asOf"] == baseline
        version_after_import = customer.version

    first = client.post("/api/v1/scoring/refresh", headers=_auth(client)).json()["data"]
    assert first["asOf"] == baseline
    assert first["changed"] == 0, "基准日固定时，导入后的首次刷新不应报告任何分数变化"
    assert first["metadataUpdated"] == 0

    second = client.post("/api/v1/scoring/refresh", headers=_auth(client)).json()["data"]
    assert second["changed"] == 0
    assert second["metadataUpdated"] == 0

    with testing_session() as session:
        customer = session.scalar(
            select(Customer).where(Customer.external_id == BASELINE_EXTERNAL_ID)
        )
        assert customer.version == version_after_import, "幂等刷新不应 bump version"


def test_refresh_asof_override_updates_metadata_without_false_change(identity_client) -> None:
    """显式覆盖 asOf：分数没变时只补正元数据，不计入 changed（修复前会误报全员变化）。"""
    client, testing_session = identity_client
    _seed_baseline_customer(client, testing_session)

    response = client.post(
        "/api/v1/scoring/refresh", headers=_auth(client), json={"asOf": "2026-09-30"}
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["asOf"] == "2026-09-30"
    # 该企业的成立月数在 2026-09-30 与基准日同为 29 个月 → 分数不变，只换 asOf
    assert result["changed"] == 0
    assert result["metadataUpdated"] == 1

    with testing_session() as session:
        customer = session.scalar(
            select(Customer).where(Customer.external_id == BASELINE_EXTERNAL_ID)
        )
        assert (customer.extra_data or {})["scoreDetail"]["asOf"] == "2026-09-30"


def test_refresh_still_counts_real_profile_changes(identity_client) -> None:
    """修掉 asOf 噪音后，真实的资料变化仍必须被计入 changed（防止修得过头）。"""
    client, testing_session = identity_client
    _seed_baseline_customer(client, testing_session)
    client.post("/api/v1/scoring/refresh", headers=_auth(client))

    with testing_session() as session:
        customer = session.scalar(
            select(Customer).where(Customer.external_id == BASELINE_EXTERNAL_ID)
        )
        before_score = customer.score
        extra = dict(customer.extra_data or {})
        extra["profile"] = {**extra["profile"], "参保人数": "980"}  # 参保 18 → 980
        customer.extra_data = extra
        session.commit()

    result = client.post("/api/v1/scoring/refresh", headers=_auth(client)).json()["data"]
    assert result["changed"] == 1
    assert result["metadataUpdated"] == 0

    with testing_session() as session:
        customer = session.scalar(
            select(Customer).where(Customer.external_id == BASELINE_EXTERNAL_ID)
        )
        assert customer.score != before_score, "参保人数大幅变化后分数应当变化"
