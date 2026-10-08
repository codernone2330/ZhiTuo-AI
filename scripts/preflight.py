#!/usr/bin/env python
"""上线前自检 —— push / 部署前跑一遍，把"会整站起不来"的问题挡在前面。

覆盖四类检查（都可单独开关）：

  1. migrations  Alembic 迁移图：单根、单 head、无重复 revision、无悬空父节点。
                **多 head 会让容器启动时 `alembic upgrade head` 直接失败**
                （docker-compose 的启动命令是 `alembic upgrade head && seed && uvicorn`，
                第一步断则整站起不来）。纯文件解析，不需要连数据库、不需要 Docker。
  2. image       容器镜像里的文件是否与当前工作区**逐字节一致**。
                防的是「改了代码但忘了 `docker compose up -d --build`」——
                这类问题会让线上跑着旧逻辑，排查时极易误判。
  3. api         接口冒烟：健康检查 + 登录 + 关键端点可用（可选，需服务已启动）。
  4. tests       后端测试套件（可选，默认跳过以保持快速）。

用法：
    python scripts/preflight.py                 # 默认跑 migrations + image
    python scripts/preflight.py --all           # 再加 api + tests
    python scripts/preflight.py --tests         # 只加测试
    python scripts/preflight.py --no-image      # 跳过镜像比对（Docker 没开时）

退出码：全部通过 0；任一失败 1。可直接用于 pre-push hook 或 CI。
"""

from __future__ import annotations

import argparse
import collections
import glob
import hashlib
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_DIR = os.path.join(REPO_ROOT, "backend", "migrations", "versions")
FRONTEND_SOURCE = os.path.join(REPO_ROOT, "data", "智拓商机作战助手-开发版.html")
FRONTEND_SERVED = "/app/frontend/index.html"
DEFAULT_CONTAINER = "zhituo-ai-api-1"
API_BASE = "http://127.0.0.1:8000/api/v1"

REVISION_RE = re.compile(r"^revision(?:\s*:\s*str)?\s*=\s*[\"']([^\"']+)", re.M)
DOWN_RE = re.compile(
    r"^down_revision(?:\s*:[^=]+)?\s*=\s*(?:[\"']([^\"']+)[\"']|None)", re.M
)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


# --------------------------------------------------------------------------- #
# 1. 迁移图
# --------------------------------------------------------------------------- #
def check_migrations() -> tuple[str, list[str]]:
    lines: list[str] = []
    files = sorted(glob.glob(os.path.join(MIGRATION_DIR, "*.py")))
    if not files:
        return FAIL, [f"未找到迁移文件：{MIGRATION_DIR}"]

    parents: dict[str, str | None] = {}
    parsed: list[str] = []  # 保留全部（含重复），用于检测 revision 撞号
    for path in files:
        text = open(path, encoding="utf-8").read()
        hit = REVISION_RE.search(text)
        if not hit:
            lines.append(f"  [警告] 解析不出 revision：{os.path.basename(path)}")
            continue
        revision = hit.group(1)
        parsed.append(revision)
        down = DOWN_RE.search(text)
        parents[revision] = down.group(1) if (down and down.group(1)) else None

    duplicates = [r for r, n in collections.Counter(parsed).items() if n > 1]
    revisions = list(parents)
    roots = [r for r, p in parents.items() if p is None]
    referenced = {p for p in parents.values() if p}
    dangling = sorted(referenced - set(revisions))
    heads = [r for r in revisions if r not in referenced]

    lines.append(f"  迁移文件 {len(files)} 个 / revision {len(revisions)} 个")
    lines.append(f"  根节点 {roots}     head {heads}")

    problems = []
    if duplicates:
        problems.append(
            f"revision 撞号（多个文件用同一个编号）：{duplicates} —— "
            "Alembic 会报 duplicate revision"
        )
    if dangling:
        problems.append(f"悬空父节点（down_revision 指向不存在的版本）：{dangling}")
    if len(roots) != 1:
        problems.append(f"根节点应有且仅有 1 个，实际 {len(roots)} 个")
    if len(heads) != 1:
        problems.append(
            f"head 应有且仅有 1 个，实际 {len(heads)} 个 —— "
            "两个分支各自接了同一个父节点就会这样，容器启动会直接失败"
        )

    if problems:
        for item in problems:
            lines.append(f"  ✗ {item}")
        lines.append("  修复：alembic merge -m \"merge <A> <B>\" <revA> <revB>")
        return FAIL, lines
    lines.append("  ✓ 单根单 head，`alembic upgrade head` 不会因多 head 报错")
    return PASS, lines


# --------------------------------------------------------------------------- #
# 2. 镜像 vs 工作区
# --------------------------------------------------------------------------- #
def _md5(path: str) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_expected() -> dict[str, str]:
    """{容器内路径: 本地 md5}"""
    expected: dict[str, str] = {}
    mapping = [
        (os.path.join(REPO_ROOT, "backend", "app"), "/app/app"),
        (os.path.join(REPO_ROOT, "backend", "migrations"), "/app/migrations"),
    ]
    for local_root, container_root in mapping:
        for ext in ("*.py", "*.json"):
            for path in glob.glob(os.path.join(local_root, "**", ext), recursive=True):
                if "__pycache__" in path:
                    continue
                rel = os.path.relpath(path, local_root).replace("\\", "/")
                expected[f"{container_root}/{rel}"] = _md5(path)
    if os.path.isfile(FRONTEND_SOURCE):
        expected[FRONTEND_SERVED] = _md5(FRONTEND_SOURCE)
    return expected


def _container_hashes(container: str) -> dict[str, str] | None:
    script = (
        "find /app/app /app/migrations -type f \\( -name '*.py' -o -name '*.json' \\) "
        "-not -path '*__pycache__*' -print0 | xargs -0 md5sum; "
        f"md5sum {FRONTEND_SERVED}"
    )
    result = subprocess.run(
        ["docker", "exec", container, "sh", "-lc", script],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    hashes = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            hashes[parts[1].strip()] = parts[0]
    return hashes


def check_image(container: str) -> tuple[str, list[str]]:
    if subprocess.run(
        ["docker", "info"], capture_output=True, text=True
    ).returncode != 0:
        return SKIP, ["  Docker 未运行 —— 跳过（要检查请先启动 Docker Desktop）"]

    container_hashes = _container_hashes(container)
    if container_hashes is None:
        return SKIP, [f"  容器 {container} 不可用 —— 跳过（先 docker compose up -d）"]

    expected = _local_expected()
    mismatched = [p for p, h in expected.items() if container_hashes.get(p) != h]
    missing = [p for p in expected if p not in container_hashes]

    lines = [f"  比对 {len(expected)} 个文件（backend/app + migrations + 前端 HTML）"]
    if mismatched or missing:
        for path in sorted(mismatched)[:20]:
            lines.append(f"  ✗ 内容不一致：{path}")
        for path in sorted(missing)[:20]:
            lines.append(f"  ✗ 镜像里缺失：{path}")
        lines.append("  修复：docker compose up -d --build")
        return FAIL, lines
    lines.append("  ✓ 逐字节一致 —— 镜像就是从当前工作区构建的")
    return PASS, lines


# --------------------------------------------------------------------------- #
# 3. API 冒烟
# --------------------------------------------------------------------------- #
def check_api() -> tuple[str, list[str]]:
    import json
    import urllib.error
    import urllib.request

    def call(path: str, method: str = "GET", body=None, token: str | None = None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            API_BASE + path, data=data, headers=headers, method=method
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode())["data"]

    try:
        token = call(
            "/auth/login", "POST", {"username": "groupadmin", "password": "szyd123456"}
        )["accessToken"]
        config = call("/scoring/config", token=token)
        opportunities = call("/opportunities?page=1&pageSize=1", token=token)
    except (urllib.error.URLError, OSError, KeyError) as error:
        return SKIP, [f"  服务未就绪或不支持该调用 —— 跳过（{error}）"]

    lines = [
        "  登录 ✓",
        f"  /scoring/config ✓  基准日 asOf={config.get('asOf')} "
        f"已固定={config.get('asOfIsPinned')}",
        f"  /opportunities ✓  total={opportunities.get('total')}",
    ]
    return PASS, lines


# --------------------------------------------------------------------------- #
# 4. 后端测试
# --------------------------------------------------------------------------- #
def check_tests() -> tuple[str, list[str]]:
    backend = os.path.join(REPO_ROOT, "backend")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "-p", "no:warnings", "-q"],
        cwd=backend,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": "."},
    )
    tail = [line for line in result.stdout.strip().splitlines() if line.strip()]
    summary = tail[-1] if tail else "(无输出)"
    if result.returncode != 0:
        return FAIL, [f"  {summary}"] + [f"  {line}" for line in tail[-12:-1]]
    return PASS, [f"  {summary}"]


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="上线前自检")
    parser.add_argument("--container", default=DEFAULT_CONTAINER, help="api 容器名")
    parser.add_argument("--no-migrations", action="store_true", help="跳过迁移图检查")
    parser.add_argument("--no-image", action="store_true", help="跳过镜像一致性检查")
    parser.add_argument("--api", action="store_true", help="加跑接口冒烟（需服务已启动）")
    parser.add_argument("--tests", action="store_true", help="加跑后端测试套件")
    parser.add_argument("--all", action="store_true", help="跑全部检查")
    args = parser.parse_args()

    if args.all:
        args.api = args.tests = True

    checks: list[tuple[str, str, tuple[str, list[str]]]] = []
    if not args.no_migrations:
        checks.append(("migrations", "Alembic 迁移图", check_migrations()))
    if not args.no_image:
        checks.append(("image", "镜像 vs 工作区", check_image(args.container)))
    if args.api:
        checks.append(("api", "接口冒烟", check_api()))
    if args.tests:
        checks.append(("tests", "后端测试", check_tests()))

    print("=" * 74)
    print("智拓 · 上线前自检")
    print("=" * 74)
    failed = []
    for _, title, (status, lines) in checks:
        mark = {PASS: "✓ 通过", FAIL: "✗ 失败", SKIP: "- 跳过"}[status]
        print(f"\n[{mark}] {title}")
        for line in lines:
            print(line)
        if status == FAIL:
            failed.append(title)

    print("\n" + "=" * 74)
    if failed:
        print(f"结果：{len(failed)} 项失败 —— {'、'.join(failed)}")
        print("请先修复再 push / 部署。")
        return 1
    print(f"结果：全部通过（{len(checks)} 项检查）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
