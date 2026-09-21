from dataclasses import dataclass

from sqlalchemy import select

from app.db.session import SessionLocal
from app.modules.auth.models import Role
from app.modules.organizations.models import Organization

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


def main() -> None:
    with SessionLocal.begin() as session:
        role_count = seed_roles(session)
        organization_count = seed_organizations(session)
    print(f"Seed completed: {organization_count} organizations, {role_count} roles created.")


if __name__ == "__main__":
    main()
