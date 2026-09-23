from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.scripts.seed import DEMO_USERS, build_organizations


def test_organization_seed_is_complete_and_unique() -> None:
    organizations = build_organizations()
    codes = {item.code for item in organizations}
    paths = {item.path for item in organizations}

    assert len(organizations) == len(codes) == len(paths) == 165
    assert sum(item.level == "district" for item in organizations) == 28
    assert sum(item.level == "department" for item in organizations) == 132
    enterprise = next(item for item in organizations if item.code == "cmcc-gd-sz-ba-enterprise")
    assert enterprise.path == "/cmcc/gd/sz/ba/enterprise"


def test_password_hash_and_access_token_round_trip() -> None:
    digest = hash_password("not-a-production-password")
    assert verify_password("not-a-production-password", digest)
    assert not verify_password("wrong-password", digest)

    token = create_access_token("user-001", {"org": "cmcc-gd-sz"})
    payload = decode_access_token(token)
    assert payload["sub"] == "user-001"
    assert payload["org"] == "cmcc-gd-sz"
    assert payload["type"] == "access"


def test_demo_users_cover_operating_levels_and_customer_managers() -> None:
    usernames = {item.username for item in DEMO_USERS}
    employee_numbers = {item.employee_no for item in DEMO_USERS}
    organization_codes = {item.organization_code for item in DEMO_USERS}

    assert len(DEMO_USERS) == len(usernames) == len(employee_numbers)
    assert "cmcc-gd" in organization_codes
    assert {"cmcc-gd-sz", "cmcc-gd-gz", "cmcc-gd-dg"} <= organization_codes
    assert any(
        item.role_code == "org_admin" and "-ft" in item.organization_code
        for item in DEMO_USERS
    )
    assert sum(item.role_code == "customer_manager" for item in DEMO_USERS) >= 3
