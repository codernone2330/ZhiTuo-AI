"""多租户登录、组织与 RBAC 服务。

这个模块刻意不依赖 Web 框架；HTTP 层可将请求映射到 ``AuthRbacService``
的方法。所有数据库访问均以 tenant_id 为边界，并通过 audit_logs 留痕。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


DEFAULT_ROLES: dict[str, set[str]] = {
    "platform_admin": {"*"},
    "tenant_admin": {"tenant:manage", "org:manage", "user:manage", "role:manage", "audit:read"},
    "sales_manager": {"company:read", "company:write", "company:approve", "authorization:manage"},
    "sales": {"company:read", "company:write"},
    "auditor": {"company:read", "audit:read"},
}
TOKEN_TTL_SECONDS = 8 * 60 * 60
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES organizations(id), name TEXT NOT NULL, code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, code)
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    username TEXT NOT NULL, display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
    organization_id TEXT REFERENCES organizations(id), status TEXT NOT NULL DEFAULT 'active',
    failed_logins INTEGER NOT NULL DEFAULT 0, locked_until TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, username)
);
CREATE TABLE IF NOT EXISTS roles (
    id TEXT PRIMARY KEY, tenant_id TEXT REFERENCES tenants(id) ON DELETE CASCADE,
    code TEXT NOT NULL, name TEXT NOT NULL, permissions_json TEXT NOT NULL, is_system INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, UNIQUE(tenant_id, code)
);
CREATE TABLE IF NOT EXISTS user_roles (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY(user_id, role_id)
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY, tenant_id TEXT, actor_id TEXT, action TEXT NOT NULL, resource_type TEXT NOT NULL,
    resource_id TEXT, outcome TEXT NOT NULL, detail_json TEXT NOT NULL, ip TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_org_tenant_parent ON organizations(tenant_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_time ON audit_logs(tenant_id, created_at DESC);
"""


class AuthError(Exception):
    """可由调用方转换为 HTTP 错误的领域异常。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class AuthRbacService:
    """SQLite 持久化的认证、组织树、用户及 RBAC 服务。"""

    def __init__(self, database: str | Path, token_secret: str) -> None:
        if len(token_secret) < 32:
            raise ValueError("token_secret 必须至少为 32 个字符。")
        self.database = Path(database)
        self.token_secret = token_secret.encode("utf-8")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._db() as conn:
            conn.executescript(SCHEMA)
            for code, permissions in DEFAULT_ROLES.items():
                # tenant_id NULL 表示平台内置角色；SQLite 的 UNIQUE 对 NULL 不做冲突判断，先查询再插入。
                if not conn.execute("SELECT 1 FROM roles WHERE tenant_id IS NULL AND code=?", (code,)).fetchone():
                    conn.execute(
                        "INSERT INTO roles VALUES(?,?,?,?,?,?,?)",
                        (_id("rol"), None, code, code, json.dumps(sorted(permissions)), 1, _now()),
                    )

    @staticmethod
    def hash_password(password: str) -> str:
        if len(password) < 12:
            raise AuthError(400, "WEAK_PASSWORD", "密码至少需要 12 个字符。")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
        return f"scrypt${_b64(salt)}${_b64(digest)}"

    @staticmethod
    def verify_password(password: str, stored: str) -> bool:
        try:
            algorithm, salt, digest = stored.split("$")
            if algorithm != "scrypt":
                return False
            candidate = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt), n=2**14, r=8, p=1)
            return hmac.compare_digest(candidate, _unb64(digest))
        except (TypeError, ValueError):
            return False

    def _audit(self, conn: sqlite3.Connection, *, tenant_id: str | None, actor_id: str | None,
               action: str, resource_type: str, resource_id: str | None, outcome: str,
               detail: dict[str, Any] | None = None, ip: str = "") -> None:
        conn.execute(
            "INSERT INTO audit_logs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (_id("aud"), tenant_id, actor_id, action, resource_type, resource_id, outcome,
             json.dumps(detail or {}, ensure_ascii=False), ip[:64], _now()),
        )

    def _system_role(self, conn: sqlite3.Connection, code: str) -> sqlite3.Row:
        role = conn.execute("SELECT * FROM roles WHERE tenant_id IS NULL AND code=?", (code,)).fetchone()
        if not role:
            raise RuntimeError(f"缺少内置角色：{code}")
        return role

    def create_tenant(self, name: str, admin_username: str, admin_password: str, admin_name: str | None = None) -> dict[str, str]:
        name, username = name.strip(), admin_username.strip().lower()
        if not name or not username:
            raise AuthError(400, "INVALID_INPUT", "租户名称和管理员账号不能为空。")
        tenant_id, user_id, stamp = _id("ten"), _id("usr"), _now()
        with self._db() as conn:
            try:
                conn.execute("INSERT INTO tenants VALUES(?,?,?,?)", (tenant_id, name, "active", stamp))
                conn.execute(
                    "INSERT INTO users(id,tenant_id,username,display_name,password_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (user_id, tenant_id, username, (admin_name or username).strip(), self.hash_password(admin_password), stamp, stamp),
                )
            except sqlite3.IntegrityError as exc:
                raise AuthError(409, "TENANT_EXISTS", "租户名称已存在。") from exc
            role = self._system_role(conn, "tenant_admin")
            conn.execute("INSERT INTO user_roles VALUES(?,?)", (user_id, role["id"]))
            self._audit(conn, tenant_id=tenant_id, actor_id=user_id, action="tenant.create", resource_type="tenant", resource_id=tenant_id, outcome="success")
        return {"tenantId": tenant_id, "adminUserId": user_id}

    def _issue_token(self, user_id: str, tenant_id: str) -> str:
        payload = _b64(json.dumps({"sub": user_id, "tid": tenant_id, "iat": int(time.time()), "exp": int(time.time()) + TOKEN_TTL_SECONDS}, separators=(",", ":")).encode())
        signature = _b64(hmac.new(self.token_secret, payload.encode("ascii"), hashlib.sha256).digest())
        return f"{payload}.{signature}"

    def _claims(self, token: str) -> dict[str, Any]:
        try:
            payload, signature = token.split(".")
            expected = _b64(hmac.new(self.token_secret, payload.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(expected, signature):
                raise ValueError
            claims = json.loads(_unb64(payload))
            if not isinstance(claims, dict) or int(claims["exp"]) <= int(time.time()):
                raise ValueError
            return claims
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuthError(401, "INVALID_TOKEN", "登录状态无效或已过期。") from exc

    def login(self, tenant_id: str, username: str, password: str, ip: str = "") -> dict[str, Any]:
        username = username.strip().lower()
        login_error: AuthError | None = None
        result: dict[str, Any] | None = None
        with self._db() as conn:
            user = conn.execute("SELECT * FROM users WHERE tenant_id=? AND username=?", (tenant_id, username)).fetchone()
            unusable = not user or user["status"] != "active" or (user["locked_until"] and user["locked_until"] > _now())
            if unusable:
                self._audit(conn, tenant_id=tenant_id or None, actor_id=user["id"] if user else None, action="auth.login", resource_type="user", resource_id=user["id"] if user else None, outcome="denied", detail={"username": username}, ip=ip)
                login_error = AuthError(401, "LOGIN_FAILED", "租户、用户名或密码不正确。")
            elif not self.verify_password(password, user["password_hash"]):
                failures = user["failed_logins"] + 1
                locked_until = (datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)).isoformat() if failures >= MAX_FAILED_LOGINS else None
                conn.execute("UPDATE users SET failed_logins=?, locked_until=?, updated_at=? WHERE id=?", (failures, locked_until, _now(), user["id"]))
                self._audit(conn, tenant_id=tenant_id, actor_id=user["id"], action="auth.login", resource_type="user", resource_id=user["id"], outcome="denied", detail={"username": username}, ip=ip)
                login_error = AuthError(401, "LOGIN_FAILED", "租户、用户名或密码不正确。")
            else:
                conn.execute("UPDATE users SET failed_logins=0, locked_until=NULL, updated_at=? WHERE id=?", (_now(), user["id"]))
                roles = self._role_codes(conn, user["id"])
                self._audit(conn, tenant_id=tenant_id, actor_id=user["id"], action="auth.login", resource_type="user", resource_id=user["id"], outcome="success", ip=ip)
                result = {"accessToken": self._issue_token(user["id"], tenant_id), "tokenType": "Bearer", "expiresIn": TOKEN_TTL_SECONDS, "user": {"id": user["id"], "name": user["display_name"], "roles": sorted(roles)}}
        if login_error:
            raise login_error
        assert result is not None
        return result

    def _role_codes(self, conn: sqlite3.Connection, user_id: str) -> set[str]:
        return {row[0] for row in conn.execute("SELECT r.code FROM roles r JOIN user_roles ur ON ur.role_id=r.id WHERE ur.user_id=?", (user_id,))}

    def authenticate(self, token: str, required_permission: str | None = None) -> dict[str, Any]:
        claims = self._claims(token.removeprefix("Bearer ").strip())
        with self._db() as conn:
            user = conn.execute("SELECT * FROM users WHERE id=? AND tenant_id=?", (claims.get("sub"), claims.get("tid"))).fetchone()
            if not user or user["status"] != "active":
                raise AuthError(401, "ACCOUNT_UNAVAILABLE", "账号已停用或不存在。")
            roles = self._role_codes(conn, user["id"])
            permissions: set[str] = set()
            for role in conn.execute("SELECT r.permissions_json FROM roles r JOIN user_roles ur ON ur.role_id=r.id WHERE ur.user_id=?", (user["id"],)):
                permissions.update(json.loads(role[0]))
        if required_permission and "*" not in permissions and required_permission not in permissions:
            raise AuthError(403, "FORBIDDEN", "当前账号没有该操作权限。")
        return {"userId": user["id"], "tenantId": user["tenant_id"], "roles": roles, "permissions": permissions}

    def _require(self, token: str, permission: str) -> dict[str, Any]:
        return self.authenticate(token, permission)

    def add_organization(self, token: str, name: str, code: str, parent_id: str | None = None) -> dict[str, str]:
        context = self._require(token, "org:manage")
        name, code = name.strip(), code.strip().upper()
        if not name or not code:
            raise AuthError(400, "INVALID_ORGANIZATION", "组织名称和编码不能为空。")
        org_id, stamp = _id("org"), _now()
        with self._db() as conn:
            if parent_id and not conn.execute("SELECT 1 FROM organizations WHERE id=? AND tenant_id=?", (parent_id, context["tenantId"])).fetchone():
                raise AuthError(404, "PARENT_NOT_FOUND", "上级组织不存在或不属于当前租户。")
            try:
                conn.execute("INSERT INTO organizations VALUES(?,?,?,?,?,?,?,?)", (org_id, context["tenantId"], parent_id, name, code, "active", stamp, stamp))
            except sqlite3.IntegrityError as exc:
                raise AuthError(409, "ORGANIZATION_EXISTS", "组织编码已存在。") from exc
            self._audit(conn, tenant_id=context["tenantId"], actor_id=context["userId"], action="organization.create", resource_type="organization", resource_id=org_id, outcome="success")
        return {"id": org_id, "name": name, "code": code, "parentId": parent_id or ""}

    def organization_tree(self, token: str) -> list[dict[str, Any]]:
        context = self.authenticate(token)
        with self._db() as conn:
            rows = [dict(row) for row in conn.execute("SELECT id,parent_id,name,code,status FROM organizations WHERE tenant_id=? ORDER BY name", (context["tenantId"],))]
        by_id = {row["id"]: {"id": row["id"], "name": row["name"], "code": row["code"], "status": row["status"], "children": []} for row in rows}
        roots: list[dict[str, Any]] = []
        for row in rows:
            item = by_id[row["id"]]
            if row["parent_id"] in by_id:
                by_id[row["parent_id"]]["children"].append(item)
            else:
                roots.append(item)
        return roots

    def create_user(self, token: str, username: str, password: str, display_name: str, role_code: str = "sales", organization_id: str | None = None) -> dict[str, str]:
        context = self._require(token, "user:manage")
        username, display_name = username.strip().lower(), display_name.strip()
        if not username or not display_name:
            raise AuthError(400, "INVALID_USER", "账号和姓名不能为空。")
        user_id, stamp = _id("usr"), _now()
        with self._db() as conn:
            role = conn.execute("SELECT * FROM roles WHERE code=? AND (tenant_id IS NULL OR tenant_id=?) ORDER BY tenant_id IS NOT NULL DESC LIMIT 1", (role_code, context["tenantId"])).fetchone()
            if not role:
                raise AuthError(400, "ROLE_NOT_FOUND", "指定角色不存在。")
            if organization_id and not conn.execute("SELECT 1 FROM organizations WHERE id=? AND tenant_id=?", (organization_id, context["tenantId"])).fetchone():
                raise AuthError(404, "ORGANIZATION_NOT_FOUND", "组织不存在或不属于当前租户。")
            try:
                conn.execute("INSERT INTO users(id,tenant_id,username,display_name,password_hash,organization_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (user_id, context["tenantId"], username, display_name, self.hash_password(password), organization_id, stamp, stamp))
                conn.execute("INSERT INTO user_roles VALUES(?,?)", (user_id, role["id"]))
            except sqlite3.IntegrityError as exc:
                raise AuthError(409, "USER_EXISTS", "该租户内账号已存在。") from exc
            self._audit(conn, tenant_id=context["tenantId"], actor_id=context["userId"], action="user.create", resource_type="user", resource_id=user_id, outcome="success", detail={"role": role_code})
        return {"id": user_id, "username": username, "role": role_code}

    def create_custom_role(self, token: str, code: str, name: str, permissions: list[str]) -> dict[str, str]:
        """创建仅在当前租户有效的角色；内置角色不会被覆盖。"""
        context = self._require(token, "role:manage")
        code, name = code.strip().lower(), name.strip()
        clean_permissions = sorted({item.strip() for item in permissions if isinstance(item, str) and item.strip()})
        if not code.replace("_", "").isalnum() or not name or not clean_permissions or "*" in clean_permissions:
            raise AuthError(400, "INVALID_ROLE", "角色编码、名称或权限列表无效。")
        with self._db() as conn:
            # 禁止用租户自定义角色伪装为平台内置角色，避免权限语义混淆。
            if conn.execute("SELECT 1 FROM roles WHERE tenant_id IS NULL AND code=?", (code,)).fetchone():
                raise AuthError(409, "SYSTEM_ROLE_RESERVED", "不能覆盖内置角色。")
            role_id = _id("rol")
            try:
                conn.execute("INSERT INTO roles VALUES(?,?,?,?,?,?,?)", (role_id, context["tenantId"], code, name, json.dumps(clean_permissions), 0, _now()))
            except sqlite3.IntegrityError as exc:
                raise AuthError(409, "ROLE_EXISTS", "角色编码已存在。") from exc
            self._audit(conn, tenant_id=context["tenantId"], actor_id=context["userId"], action="role.create", resource_type="role", resource_id=role_id, outcome="success", detail={"code": code})
        return {"id": role_id, "code": code, "name": name}

    def assign_role(self, token: str, user_id: str, role_code: str) -> None:
        """以替换方式设置角色，确保用户权限不会因历史角色残留而扩大。"""
        context = self._require(token, "role:manage")
        with self._db() as conn:
            user = conn.execute("SELECT 1 FROM users WHERE id=? AND tenant_id=?", (user_id, context["tenantId"])).fetchone()
            role = conn.execute("SELECT * FROM roles WHERE code=? AND (tenant_id IS NULL OR tenant_id=?) ORDER BY tenant_id IS NOT NULL DESC LIMIT 1", (role_code, context["tenantId"])).fetchone()
            if not user:
                raise AuthError(404, "USER_NOT_FOUND", "用户不存在或不属于当前租户。")
            if not role:
                raise AuthError(404, "ROLE_NOT_FOUND", "角色不存在。")
            conn.execute("DELETE FROM user_roles WHERE user_id=?", (user_id,))
            conn.execute("INSERT INTO user_roles VALUES(?,?)", (user_id, role["id"]))
            self._audit(conn, tenant_id=context["tenantId"], actor_id=context["userId"], action="user.role.assign", resource_type="user", resource_id=user_id, outcome="success", detail={"role": role_code})

    def set_user_status(self, token: str, user_id: str, status: str) -> None:
        context = self._require(token, "user:manage")
        if status not in {"active", "disabled"}:
            raise AuthError(400, "INVALID_STATUS", "账号状态只能为 active 或 disabled。")
        if user_id == context["userId"] and status == "disabled":
            raise AuthError(400, "SELF_DISABLE_DENIED", "不能停用当前登录账号。")
        with self._db() as conn:
            cursor = conn.execute("UPDATE users SET status=?,updated_at=? WHERE id=? AND tenant_id=?", (status, _now(), user_id, context["tenantId"]))
            if not cursor.rowcount:
                raise AuthError(404, "USER_NOT_FOUND", "用户不存在或不属于当前租户。")
            self._audit(conn, tenant_id=context["tenantId"], actor_id=context["userId"], action="user.status.update", resource_type="user", resource_id=user_id, outcome="success", detail={"status": status})
