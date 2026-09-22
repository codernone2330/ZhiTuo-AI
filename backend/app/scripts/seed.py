from dataclasses import dataclass

from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password, verify_password
from app.db.session import SessionLocal
from app.modules.auth.models import Role, UserRole
from app.modules.organizations.models import Organization
from app.modules.users.models import User

DEPARTMENTS = [
    ("general", "综合部"),
    ("enterprise", "集客部"),
    ("network", "网络部"),
    ("market", "市场部"),
]

CITY_DISTRICTS = {
    "sz": (
        "深圳分公司",
        "深圳市",
        [
            ("ba", "宝安分公司", "宝安区"),
            ("lg", "龙岗分公司", "龙岗区"),
            ("lh", "龙华分公司", "龙华区"),
            ("ns", "南山分公司", "南山区"),
            ("ft", "福田分公司", "福田区"),
            ("ll", "罗湖分公司", "罗湖区"),
            ("gm", "光明分公司", "光明区"),
            ("ps", "坪山分公司", "坪山区"),
            ("yt", "盐田分公司", "盐田区"),
            ("ss", "深汕合作区分公司", "深汕合作区"),
            ("qh", "前海蛇口自贸区分公司", "前海蛇口自贸区"),
        ],
    ),
    "gz": (
        "广州分公司",
        "广州市",
        [
            ("by", "白云分公司", "白云区"),
            ("th", "天河分公司", "天河区"),
            ("py", "番禺分公司", "番禺区"),
            ("hz", "海珠分公司", "海珠区"),
            ("hd", "花都分公司", "花都区"),
            ("zc", "增城分公司", "增城区"),
            ("yx", "越秀分公司", "越秀区"),
            ("hp", "黄埔分公司", "黄埔区"),
            ("lw", "荔湾分公司", "荔湾区"),
            ("ns", "南沙分公司", "南沙区"),
            ("ch", "从化分公司", "从化区"),
        ],
    ),
    "dg": (
        "东莞分公司",
        "东莞市",
        [
            ("cq", "城区分公司", "城区"),
            ("dq", "东区分公司", "东区"),
            ("nq", "南区分公司", "南区"),
            ("xq", "西区分公司", "西区"),
            ("bq", "北区分公司", "北区"),
            ("zq", "中区分公司", "中区"),
        ],
    ),
}


@dataclass(frozen=True)
class OrgSeed:
    code: str
    name: str
    level: str
    parent_code: str | None
    path: str
    province: str | None = None
    city: str | None = None
    district: str | None = None
    sort_order: int = 0


@dataclass(frozen=True)
class UserSeed:
    username: str
    employee_no: str
    display_name: str
    organization_code: str
    role_code: str


DEMO_USERS = [
    UserSeed("szyd", "CMCCSUPER001", "超级管理员", "cmcc", "super_admin"),
    UserSeed("gdmanager", "CMCC440001", "陈晨", "cmcc-gd", "org_admin"),
    UserSeed("huangkai", "CMCC440301", "黄凯", "cmcc-gd-sz", "org_admin"),
    UserSeed("luoyali", "CMCC440101", "罗雅丽", "cmcc-gd-gz", "org_admin"),
    UserSeed("linzhiyuan", "CMCC441901", "林志远", "cmcc-gd-dg", "org_admin"),
    UserSeed("futianleader", "CMCC440304", "周颖", "cmcc-gd-sz-ft", "org_admin"),
    UserSeed("tianheleader", "CMCC440106", "郑雅文", "cmcc-gd-gz-th", "org_admin"),
    UserSeed("dongguanleader", "CMCC441904", "陈宇", "cmcc-gd-dg-cq", "org_admin"),
    UserSeed(
        "futianenterprise",
        "CMCC440305",
        "林珊",
        "cmcc-gd-sz-ft-enterprise",
        "department_manager",
    ),
    UserSeed(
        "huyijun",
        "CMCC440306",
        "胡一骏",
        "cmcc-gd-sz-ft-enterprise",
        "customer_manager",
    ),
    UserSeed(
        "wuweichao",
        "CMCC440107",
        "伍炜超",
        "cmcc-gd-gz-th-enterprise",
        "customer_manager",
    ),
    UserSeed(
        "caomeiling",
        "CMCC441905",
        "曹美玲",
        "cmcc-gd-dg-cq-enterprise",
        "customer_manager",
    ),
]


def build_organizations() -> list[OrgSeed]:
    result = [
        OrgSeed("cmcc", "中国移动集团", "group", None, "/cmcc", sort_order=1),
        OrgSeed(
            "cmcc-gd",
            "广东分公司",
            "province",
            "cmcc",
            "/cmcc/gd",
            province="广东省",
            sort_order=1,
        ),
    ]
    units = list(result)
    for city_order, (city_code, city_config) in enumerate(CITY_DISTRICTS.items(), start=1):
        city_name, city, districts = city_config
        code = f"cmcc-gd-{city_code}"
        city_seed = OrgSeed(
            code,
            city_name,
            "city",
            "cmcc-gd",
            f"/cmcc/gd/{city_code}",
            province="广东省",
            city=city,
            sort_order=city_order,
        )
        result.append(city_seed)
        units.append(city_seed)
        for district_order, (short_code, name, district) in enumerate(districts, start=1):
            district_code = f"{code}-{short_code}"
            district_seed = OrgSeed(
                district_code,
                name,
                "district",
                code,
                f"{city_seed.path}/{short_code}",
                province="广东省",
                city=city,
                district=district,
                sort_order=district_order,
            )
            result.append(district_seed)
            units.append(district_seed)

    for unit in units:
        for department_order, (short_code, name) in enumerate(DEPARTMENTS, start=1):
            department_code = f"{unit.code}-{short_code}"
            result.append(
                OrgSeed(
                    department_code,
                    name,
                    "department",
                    unit.code,
                    f"{unit.path}/{short_code}",
                    unit.province,
                    unit.city,
                    unit.district,
                    department_order,
                )
            )
    return result


def seed_roles(session) -> int:
    definitions = [
        ("super_admin", "超级管理员", "管理全系统登录用户、角色与组织归属"),
        ("group_admin", "集团经营管理员", "管理集团及全部下级组织"),
        ("org_admin", "组织管理员", "管理本组织及全部下级组织"),
        ("department_manager", "部门负责人", "管理本部门及下属客户经理工作"),
        ("customer_manager", "客户经理", "管理本人负责的政企客户与任务"),
        ("viewer", "只读用户", "按授权范围只读访问"),
    ]
    count = 0
    for code, name, description in definitions:
        role = session.scalar(select(Role).where(Role.code == code))
        if role is None:
            session.add(Role(code=code, name=name, description=description))
            count += 1
        else:
            role.name = name
            role.description = description
    return count


def seed_organizations(session) -> int:
    created = 0
    by_code = {item.code: item for item in session.scalars(select(Organization)).all()}
    for item in build_organizations():
        parent = by_code.get(item.parent_code) if item.parent_code else None
        organization = by_code.get(item.code)
        values = {
            "name": item.name,
            "level": item.level,
            "parent_id": parent.id if parent else None,
            "path": item.path,
            "province": item.province,
            "city": item.city,
            "district": item.district,
            "sort_order": item.sort_order,
            "is_active": True,
        }
        if organization is None:
            organization = Organization(code=item.code, **values)
            session.add(organization)
            session.flush()
            by_code[item.code] = organization
            created += 1
        else:
            for key, value in values.items():
                setattr(organization, key, value)
    return created


def seed_bootstrap_admin(session) -> bool:
    settings = get_settings()
    if settings.app_env.lower() not in {"local", "development", "test"}:
        return False
    if settings.bootstrap_admin_password is None:
        return False
    password = settings.bootstrap_admin_password.get_secret_value()
    if len(password) < 8:
        raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD must contain at least 8 characters")
    organization = session.scalar(select(Organization).where(Organization.code == "cmcc"))
    role = session.scalar(select(Role).where(Role.code == "group_admin"))
    if organization is None or role is None:
        raise RuntimeError("Organizations and roles must be seeded before the bootstrap admin")
    user = session.scalar(select(User).where(User.username == settings.bootstrap_admin_username))
    created = user is None
    if user is None:
        user = User(
            username=settings.bootstrap_admin_username,
            employee_no=settings.bootstrap_admin_employee_no,
            display_name=settings.bootstrap_admin_display_name,
            password_hash=hash_password(password),
            organization_id=organization.id,
            is_active=True,
        )
        session.add(user)
        session.flush()
    assignment = session.scalar(
        select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id)
    )
    if assignment is None:
        session.add(UserRole(user_id=user.id, role_id=role.id))
    return created


def seed_demo_users(session, password: str) -> int:
    """Create local demonstration identities without overwriting managed users."""
    if len(password) < 8:
        raise RuntimeError("BOOTSTRAP_DEMO_USER_PASSWORD must contain at least 8 characters")
    organizations = {
        item.code: item for item in session.scalars(select(Organization)).all()
    }
    roles = {item.code: item for item in session.scalars(select(Role)).all()}
    created = 0
    for item in DEMO_USERS:
        if session.scalar(select(User).where(User.username == item.username)) is not None:
            continue
        organization = organizations.get(item.organization_code)
        role = roles.get(item.role_code)
        if organization is None or role is None:
            raise RuntimeError(
                f"Demo user {item.username} references missing organization or role"
            )
        user = User(
            username=item.username,
            employee_no=item.employee_no,
            display_name=item.display_name,
            password_hash=hash_password(password),
            organization_id=organization.id,
            is_active=True,
        )
        session.add(user)
        session.flush()
        session.add(UserRole(user_id=user.id, role_id=role.id))
        created += 1
    return created


def unify_local_user_passwords(session, password: str) -> int:
    """Keep every local demonstration account on the agreed shared password."""
    if len(password) < 8:
        raise RuntimeError("The unified local password must contain at least 8 characters")
    updated = 0
    for user in session.scalars(select(User)).all():
        if verify_password(password, user.password_hash):
            continue
        user.password_hash = hash_password(password)
        updated += 1
    return updated


def main() -> None:
    settings = get_settings()
    with SessionLocal.begin() as session:
        role_count = seed_roles(session)
        session.flush()
        organization_count = seed_organizations(session)
        admin_created = seed_bootstrap_admin(session)
        demo_user_count = 0
        password_sync_count = 0
        if (
            settings.app_env.lower() in {"local", "development", "test"}
            and settings.bootstrap_demo_users
            and settings.bootstrap_demo_user_password is not None
        ):
            demo_user_count = seed_demo_users(
                session, settings.bootstrap_demo_user_password.get_secret_value()
            )
            password_sync_count = unify_local_user_passwords(
                session, settings.bootstrap_demo_user_password.get_secret_value()
            )
    print(
        f"Seed completed: {organization_count} organizations, {role_count} roles created, "
        f"bootstrap admin {'created' if admin_created else 'unchanged'}, "
        f"{demo_user_count} demo users created, "
        f"{password_sync_count} local passwords synchronized。"
    )


if __name__ == "__main__":
    main()
