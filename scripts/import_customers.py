#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""智拓 · 企业清单入库脚本（企查查 Excel → PostgreSQL，入库即由评分模型打分）。

流程：
    1. 登录后端，取当前人的组织树，定位承载政企客户的集客部（department，code 以 -enterprise 结尾）；
    2. 读取企查查「高级搜索」导出的 .xlsx（自动识别表头行，兼容表头在第 1/2 行两种导出）；
    3. 把 33 列映射为导入行 + profile（工商档案）；profile 存在时后端会用企业评分模型打分；
    4. 分批 POST /api/v1/customers/import（单批 ≤1000 行）；
    5. 调 POST /api/v1/scoring/refresh 复核，并打印等级分布与 Top 名单。

用法：
    python scripts/import_customers.py "企查查.xlsx"
    python scripts/import_customers.py "企查查.xlsx" --org-code cmcc-gd-sz-ft-enterprise
    python scripts/import_customers.py "企查查.xlsx" --limit 200 --dry-run     # 先试跑
    python scripts/import_customers.py "企查查.xlsx" --api-base http://127.0.0.1:8000/api/v1

依赖：pandas / openpyxl（HTTP 走标准库 urllib，无额外包）。
注意：导入要求目标是 department 级组织；重复客户（同信用代码 / 同企业名 / 同手机号）会自动跳过。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent

# 企查查导出列 → 用途（用不到的列直接不读）
NAME_COL = "企业名称"
COL_CREDIT = "统一社会信用代码"
COL_LEGAL = "法定代表人"
COL_CAPITAL = "注册资本"
COL_ESTABLISHED = "成立日期"
COL_ADDRESS = "注册地址"
COL_PROVINCE = "所属省份"
COL_CITY = "所属城市"
COL_AREA = "所属区县"
COL_MOBILE = "有效手机号"
COL_OTHER_PHONE = "更多电话"
COL_EMAIL = "邮箱"
COL_ENTITY = "企业(机构)类型"
COL_INSURED = "参保人数"
COL_STATUS = "登记状态"
COL_INDUSTRY_MAJOR = "国标行业大类"
COL_INDUSTRY_MID = "企查查行业中类"
COL_SCALE = "企业规模"
COL_WEBSITE = "官网网址"
COL_PROFILE = "企业简介"
COL_SCOPE = "经营范围"
COL_MAIL_ADDRESS = "通信地址"

# 传给后端 profile 的列（键名保持企查查中文列名，引擎的别名表能直接识别）
PROFILE_COLUMNS = [
    NAME_COL, COL_INSURED, COL_CAPITAL, COL_ESTABLISHED, COL_INDUSTRY_MAJOR,
    COL_INDUSTRY_MID, COL_ENTITY, COL_WEBSITE, COL_EMAIL, COL_OTHER_PHONE,
    COL_MOBILE, COL_ADDRESS, COL_SCOPE, COL_PROFILE,
    COL_STATUS, COL_SCALE, COL_MAIL_ADDRESS, COL_LEGAL,
]

_NULLISH = {"", "-", "nan", "none", "null", "/", "无", "--"}


def clean(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in _NULLISH else text


def first_phone(value) -> str:
    """企查查的「有效手机号」可能是分号分隔的多个号码，CRM 的联系电话只取第一个。

    完整号码串仍会写进 profile，模型（触达可达）不受影响。
    """
    text = clean(value)
    if not text:
        return ""
    for part in re.split(r"[;,，；\s/、]+", text):
        part = part.strip()
        if part and part.lower() not in _NULLISH:
            return part[:50]
    return ""


def clamp(text: str, limit: int) -> str:
    return text[:limit] if len(text) > limit else text


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def call(api_base: str, path: str, *, method: str = "GET", body: dict | None = None,
         token: str | None = None, timeout: int = 180) -> dict:
    url = api_base.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise SystemExit(f"[错误] {method} {path} → HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"[错误] 无法连接后端 {api_base}：{exc}。请先启动服务（start.cmd）。"
        ) from exc
    if isinstance(payload, dict) and payload.get("success") is False:
        raise SystemExit(f"[错误] {path}：{payload}")
    return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload


def login(api_base: str, username: str, password: str) -> str:
    data = call(api_base, "/auth/login", method="POST", body={"username": username, "password": password})
    token = (data or {}).get("accessToken")
    if not token:
        raise SystemExit("[错误] 登录未返回 accessToken。")
    return token


def flatten_orgs(nodes: list[dict], out: list[dict] | None = None) -> list[dict]:
    out = out if out is not None else []
    for node in nodes or []:
        out.append(node)
        flatten_orgs(node.get("children") or [], out)
    return out


def resolve_org(api_base: str, token: str, org_code: str) -> dict:
    tree = call(api_base, "/organizations/tree", token=token)
    orgs = flatten_orgs(tree or [])
    for org in orgs:
        if org.get("code") == org_code:
            if org.get("level") != "department":
                raise SystemExit(f"[错误] {org_code} 不是部门级（department）组织，不能承载政企客户。")
            return org
    codes = [o.get("code") for o in orgs if str(o.get("code", "")).endswith("-enterprise")]
    raise SystemExit(
        f"[错误] 未找到组织 {org_code}。可用的集客部（-enterprise）：{codes}\n"
        f"       请用 --org-code 指定其中之一。"
    )


# --------------------------------------------------------------------------- #
# 读取与映射
# --------------------------------------------------------------------------- #
def read_frame(src: Path) -> tuple[pd.DataFrame, int]:
    raw = pd.read_excel(src, header=None, dtype=object)
    header_row = 0
    for index in range(min(8, len(raw))):
        values = [str(v) for v in raw.iloc[index].tolist()]
        if any(v.strip() == NAME_COL for v in values):
            header_row = index
            break
    frame = pd.read_excel(src, header=header_row, dtype=object)
    frame = frame.dropna(how="all")
    frame = frame[frame[NAME_COL].map(lambda v: bool(clean(v)))]
    return frame, header_row


def row_to_payload(row: pd.Series, org_id: str, source: str) -> dict | None:
    name = clamp(clean(row.get(NAME_COL)), 200)
    if len(name) < 2:  # CustomerImportRow.name 要求 2..200
        return None
    profile = {}
    for column in PROFILE_COLUMNS:
        value = clean(row.get(column))
        if value:
            profile[column] = value
    credit = clamp(clean(row.get(COL_CREDIT)), 100)
    return {
        "externalId": credit or None,
        "name": name,
        "organizationId": org_id,
        "ownerName": "待分配",
        "kind": "新客",
        "customerType": "政企客户",
        "industry": clamp(clean(row.get(COL_INDUSTRY_MAJOR)) or "待分类", 100),
        "province": clamp(clean(row.get(COL_PROVINCE)), 50) or None,
        "city": clamp(clean(row.get(COL_CITY)), 50) or None,
        "area": clamp(clean(row.get(COL_AREA)), 50) or None,
        "address": clamp(clean(row.get(COL_ADDRESS)), 500) or None,
        "contact": clamp(clean(row.get(COL_LEGAL)), 80) or None,
        "phone": first_phone(row.get(COL_MOBILE)) or None,
        "need": "需求待识别",
        "stage": "线索入池",
        "potential": "中",
        "score": 60,  # 占位；带 profile 时后端会用企业评分模型覆盖
        "nextAction": "完成首次触达",
        "source": source,
        "sources": [source],
        "tags": ["企查查导入", "政企客户"],
        "profile": profile or None,
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="智拓·企业清单入库（企查查 Excel → PostgreSQL）")
    parser.add_argument("src", help="企查查导出的 xlsx 路径")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--username", default="groupadmin")
    parser.add_argument("--password", default="szyd123456")
    parser.add_argument("--org-code", default="cmcc-gd-sz-ft-enterprise", help="承载客户的集客部 code")
    parser.add_argument("--batch", type=int, default=500, help="每批行数（≤1000）")
    parser.add_argument("--limit", type=int, default=0, help="最多导入多少行（0=全部）")
    parser.add_argument("--source", default="企查查·高级搜索导入")
    parser.add_argument("--dry-run", action="store_true", help="只解析与预览，不写库")
    parser.add_argument("--no-refresh", action="store_true", help="导入后不调用评分复核")
    args = parser.parse_args()

    src = Path(args.src)
    if not src.is_file():
        print(f"[错误] 找不到输入文件：{src}")
        return 2
    batch_size = max(1, min(int(args.batch), 1000))

    print("=" * 72)
    print("智拓 · 企业清单入库（入库即由企业评分模型打分）")
    print("=" * 72)

    frame, header_row = read_frame(src)
    if args.limit:
        frame = frame.head(args.limit)
    print(f"读取 {src.name}：表头第 {header_row + 1} 行，共 {len(frame)} 家企业")

    if args.dry_run:
        org_id = "00000000-0000-0000-0000-000000000000"
        sample = row_to_payload(frame.iloc[0], org_id, args.source)
        print("\n[dry-run] 不写库。首行映射预览：")
        print(json.dumps(sample, ensure_ascii=False, indent=1)[:1600])
        return 0

    token = login(args.api_base, args.username, args.password)
    org = resolve_org(args.api_base, token, args.org_code)
    print(f"登录成功 · 目标组织：{org.get('name')}（{org.get('code')}）")

    rows = [row_to_payload(r, org["id"], args.source) for _, r in frame.iterrows()]
    skipped_invalid = sum(1 for r in rows if not r)
    rows = [r for r in rows if r]
    print(f"待导入 {len(rows)} 条（每批 {batch_size}）"
          + (f"；跳过无效行 {skipped_invalid} 条" if skipped_invalid else "") + "\n")

    inserted = duplicates = rejected = 0
    batches: list[str] = []
    for start in range(0, len(rows), batch_size):
        chunk = rows[start:start + batch_size]
        result = call(
            args.api_base, "/customers/import", method="POST",
            body={"sourceName": args.source, "rows": chunk}, token=token,
        )
        inserted += result.get("inserted", 0)
        duplicates += result.get("duplicates", 0)
        rejected += result.get("rejected", 0)
        if result.get("batchId"):
            batches.append(result["batchId"])
        print(f"  第 {start // batch_size + 1:>2} 批：提交 {len(chunk):>4} · "
              f"新增 {result.get('inserted', 0):>4} · 重复 {result.get('duplicates', 0):>4}")

    print(f"\n导入完成：新增 {inserted} · 重复跳过 {duplicates} · 拒绝 {rejected}")
    if batches:
        print(f"导入批次（可回滚）：{batches[0]}" + (f" … 共 {len(batches)} 个批次" if len(batches) > 1 else ""))

    if not args.no_refresh:
        print("\n调用评分复核（POST /scoring/refresh）…")
        result = call(args.api_base, "/scoring/refresh", method="POST", body={}, token=token)
        tier_counts = result.get("tierCounts") or {}
        anchors = result.get("anchors") or {}
        print(f"  参与评分 {result.get('scored')} 家 · 分数变化 {result.get('changed')} 家")
        print(f"  锚点：参保 {anchors.get('size')} 人 / 注册资本 {anchors.get('capital')} 万元")
        print("  等级分布：" + " · ".join(f"{t} {tier_counts.get(t, 0)}" for t in ("S", "A", "B", "C", "D")))

        top = call(args.api_base, "/opportunities?page=1&pageSize=10", token=token)
        items = (top or {}).get("items") or []
        if items:
            print("\n  推荐商机 Top 10（模型分）：")
            for index, item in enumerate(items, start=1):
                print(f"    {index:>2}. [{item.get('tier') or '-'}] {item.get('score'):>3} 分  "
                      f"{item.get('customerName')}  ·  {item.get('solutionLine') or '—'}")

    print("\n完成。请打开 http://127.0.0.1:8000/app/ → 「推荐商机」查看模型评分与各指标。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
