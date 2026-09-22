"""智拓多租户业务后端：SQLite、认证鉴权、审批、审计和 API 密钥管理。

仅使用 Python 标准库，适合与现有 ``ZhiTuoNativeServer.py`` 一起部署。
生产环境必须设置 ZHITUO_JWT_SECRET、ZHITUO_BOOTSTRAP_PASSWORD 和
ZHITUO_API_KEY_PEPPER；开发环境可通过 --allow-dev-secrets 明确启用临时密钥。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("ZHITUO_DB_PATH", str(ROOT / "data" / "zhituo.db")))
TOKEN_TTL = int(os.getenv("ZHITUO_TOKEN_TTL_SECONDS", "28800"))
MAX_PAGE_SIZE = 100

ROLE_PERMISSIONS = {
    "platform_admin": {"*"},
    "tenant_admin": {"user:manage", "company:read", "company:write", "company:approve", "authorization:manage", "key:manage", "audit:read"},
    "sales_manager": {"company:read", "company:write", "company:approve", "authorization:manage"},
    "sales": {"company:read", "company:write"},
    "auditor": {"company:read", "audit:read"},
}

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), username TEXT NOT NULL,
  password_hash TEXT NOT NULL, display_name TEXT NOT NULL, department TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active', failed_logins INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, username)
);
CREATE TABLE IF NOT EXISTS user_roles (
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, role TEXT NOT NULL,
  PRIMARY KEY(user_id, role)
);
CREATE TABLE IF NOT EXISTS companies (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), external_id TEXT,
  name TEXT NOT NULL, credit_code TEXT, legal_representative TEXT, business_status TEXT,
  address TEXT, owner_id TEXT REFERENCES users(id), source TEXT NOT NULL DEFAULT 'manual',
  version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, credit_code)
);
CREATE TABLE IF NOT EXISTS company_changes (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), company_id TEXT NOT NULL REFERENCES companies(id),
  before_json TEXT NOT NULL, after_json TEXT NOT NULL, operation TEXT NOT NULL,
  actor_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), type TEXT NOT NULL,
  target_id TEXT NOT NULL, payload_json TEXT NOT NULL, requested_by TEXT NOT NULL REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'pending', reviewer_id TEXT REFERENCES users(id), comment TEXT,
  created_at TEXT NOT NULL, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS authorization_grants (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), user_id TEXT NOT NULL REFERENCES users(id),
  scope TEXT NOT NULL, resource_type TEXT, resource_id TEXT, granted_by TEXT NOT NULL REFERENCES users(id),
  expires_at TEXT, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), name TEXT NOT NULL,
  prefix TEXT NOT NULL, secret_hash TEXT NOT NULL, scopes_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  expires_at TEXT, last_used_at TEXT, created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_logs (
  id TEXT PRIMARY KEY, tenant_id TEXT, actor_id TEXT, request_id TEXT NOT NULL, action TEXT NOT NULL,
  resource_type TEXT NOT NULL, resource_id TEXT, outcome TEXT NOT NULL, ip TEXT, detail_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_company_tenant_name ON companies(tenant_id, name);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_created ON audit_logs(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_approval_tenant_status ON approvals(tenant_id, status);
"""

class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message

def now() -> str: return datetime.now(timezone.utc).isoformat()
def uid(prefix: str) -> str: return prefix + "_" + secrets.token_urlsafe(12)
def b64(data: bytes) -> str: return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
def unb64(data: str) -> bytes: return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))

class BackendApp:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self.jwt_secret = os.getenv("ZHITUO_JWT_SECRET", "")
        self.key_pepper = os.getenv("ZHITUO_API_KEY_PEPPER", "")
        self._rate: dict[str, list[float]] = {}
        self.init_database()

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn; conn.commit()
        except Exception:
            conn.rollback(); raise
        finally: conn.close()

    def init_database(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as conn: conn.executescript(SCHEMA)

    def bootstrap(self, tenant_name: str, username: str, password: str) -> dict[str, str]:
        if len(password) < 12: raise ApiError(400, "WEAK_PASSWORD", "初始密码至少需要 12 位。")
        with self.db() as conn:
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise ApiError(409, "BOOTSTRAP_DONE", "系统已初始化。")
            tenant, user = uid("ten"), uid("usr"); stamp = now()
            conn.execute("INSERT INTO tenants VALUES(?,?,?,?)", (tenant, tenant_name.strip(), "active", stamp))
            conn.execute("INSERT INTO users(id,tenant_id,username,password_hash,display_name,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (user, tenant, username.strip().lower(), self.password_hash(password), username.strip(), stamp, stamp))
            conn.execute("INSERT INTO user_roles VALUES(?,?)", (user, "platform_admin"))
        return {"tenantId": tenant, "userId": user}

    @staticmethod
    def password_hash(password: str) -> str:
        salt = secrets.token_bytes(16); digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        return "scrypt$" + b64(salt) + "$" + b64(digest)
    @staticmethod
    def verify_password(password: str, encoded: str) -> bool:
        try:
            _, salt, digest = encoded.split("$"); candidate = hashlib.scrypt(password.encode(), salt=unb64(salt), n=2**14, r=8, p=1)
            return hmac.compare_digest(candidate, unb64(digest))
        except (ValueError, TypeError): return False

    def sign_token(self, claims: dict[str, Any]) -> str:
        if not self.jwt_secret: raise ApiError(503, "SECURITY_NOT_CONFIGURED", "未设置 ZHITUO_JWT_SECRET。")
        payload = b64(json.dumps(claims, separators=(",", ":")).encode())
        sig = b64(hmac.new(self.jwt_secret.encode(), payload.encode(), hashlib.sha256).digest())
        return payload + "." + sig
    def token_claims(self, token: str) -> dict[str, Any]:
        try:
            payload, sig = token.split(".")
            expected = b64(hmac.new(self.jwt_secret.encode(), payload.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(sig, expected): raise ValueError
            claims = json.loads(unb64(payload));
            if int(claims["exp"]) < int(time.time()): raise ValueError
            return claims
        except Exception as exc: raise ApiError(401, "INVALID_TOKEN", "登录已失效或令牌无效。") from exc

    def _rate_limit(self, key: str) -> None:
        t = time.time(); values = [x for x in self._rate.get(key, []) if x > t - 60]
        if len(values) >= 60: raise ApiError(429, "RATE_LIMITED", "请求过于频繁，请稍后重试。")
        values.append(t); self._rate[key] = values

    def audit(self, conn: sqlite3.Connection, ctx: dict[str, Any] | None, action: str, resource: str, resource_id: str | None, outcome: str, detail: dict[str, Any]) -> None:
        conn.execute("INSERT INTO audit_logs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uid("aud"), (ctx or {}).get("tenant_id"), (ctx or {}).get("user_id"), (ctx or {}).get("request_id", uid("req")), action, resource, resource_id, outcome, (ctx or {}).get("ip"), json.dumps(detail, ensure_ascii=False), now()))

    def authenticate(self, headers: dict[str, str], permission: str | None = None) -> dict[str, Any]:
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer "): raise ApiError(401, "AUTH_REQUIRED", "请提供 Bearer Token。")
        claims = self.token_claims(auth[7:].strip())
        with self.db() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=? AND tenant_id=?", (claims.get("sub"), claims.get("tid"))).fetchone()
            if not row or row["status"] != "active": raise ApiError(401, "ACCOUNT_UNAVAILABLE", "账户不可用。")
            roles = {x[0] for x in conn.execute("SELECT role FROM user_roles WHERE user_id=?", (row["id"],))}
        perms = set().union(*(ROLE_PERMISSIONS.get(r, set()) for r in roles))
        if permission and permission not in perms and "*" not in perms: raise ApiError(403, "FORBIDDEN", "当前角色没有此操作权限。")
        return {"user_id": row["id"], "tenant_id": row["tenant_id"], "roles": roles, "permissions": perms}

    def login(self, data: dict[str, Any], ip: str) -> dict[str, Any]:
        tenant, username, password = str(data.get("tenantId", "")), str(data.get("username", "")).strip().lower(), str(data.get("password", ""))
        self._rate_limit("login:" + ip)
        with self.db() as conn:
            user = conn.execute("SELECT * FROM users WHERE tenant_id=? AND username=?", (tenant, username)).fetchone()
            if not user or user["status"] != "active" or (user["locked_until"] and user["locked_until"] > now()):
                self.audit(conn, None, "auth.login", "user", None, "denied", {"username": username}); raise ApiError(401, "LOGIN_FAILED", "用户名、租户或密码不正确。")
            if not self.verify_password(password, user["password_hash"]):
                failures = user["failed_logins"] + 1; lock = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat() if failures >= 5 else None
                conn.execute("UPDATE users SET failed_logins=?,locked_until=? WHERE id=?", (failures, lock, user["id"])); self.audit(conn, {"tenant_id": tenant,"user_id":user["id"]}, "auth.login", "user", user["id"], "denied", {}); raise ApiError(401, "LOGIN_FAILED", "用户名、租户或密码不正确。")
            conn.execute("UPDATE users SET failed_logins=0,locked_until=NULL,updated_at=? WHERE id=?", (now(), user["id"]))
            roles = [x[0] for x in conn.execute("SELECT role FROM user_roles WHERE user_id=?", (user["id"],))]
            self.audit(conn, {"tenant_id":tenant,"user_id":user["id"]}, "auth.login", "user", user["id"], "success", {})
        claims = {"sub": user["id"], "tid": tenant, "roles": roles, "iat": int(time.time()), "exp": int(time.time()) + TOKEN_TTL}
        return {"accessToken": self.sign_token(claims), "tokenType": "Bearer", "expiresIn": TOKEN_TTL, "user": {"id": user["id"], "name": user["display_name"], "roles": roles}}

    def list_companies(self, ctx: dict[str, Any], query: dict[str, str]) -> dict[str, Any]:
        size = max(1, min(MAX_PAGE_SIZE, int(query.get("pageSize", "20")))); offset = max(0, int(query.get("offset", "0"))); search = query.get("q", "").strip()
        where, args = ["tenant_id=?"], [ctx["tenant_id"]]
        if "*" not in ctx["permissions"] and "tenant_admin" not in ctx["roles"] and "sales_manager" not in ctx["roles"]: where.append("owner_id=?"); args.append(ctx["user_id"])
        if search: where.append("(name LIKE ? OR credit_code LIKE ?)"); args += ["%" + search + "%", "%" + search + "%"]
        with self.db() as conn:
            total = conn.execute("SELECT count(*) FROM companies WHERE " + " AND ".join(where), args).fetchone()[0]
            rows = conn.execute("SELECT * FROM companies WHERE " + " AND ".join(where) + " ORDER BY updated_at DESC LIMIT ? OFFSET ?", args + [size, offset]).fetchall()
        return {"items": [dict(r) for r in rows], "total": total, "pageSize": size, "offset": offset}

    def create_company(self, ctx: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        name = str(data.get("name", "")).strip()
        if not 2 <= len(name) <= 200: raise ApiError(400, "INVALID_NAME", "企业名称长度需为 2 至 200 个字符。")
        company = uid("cmp"); stamp = now(); credit = str(data.get("creditCode", "")).strip() or None
        with self.db() as conn:
            try:
                conn.execute("INSERT INTO companies(id,tenant_id,external_id,name,credit_code,legal_representative,business_status,address,owner_id,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (company, ctx["tenant_id"], str(data.get("externalId", "")), name, credit, str(data.get("legalRepresentative", "")), str(data.get("businessStatus", "")), str(data.get("address", "")), data.get("ownerId") or ctx["user_id"], str(data.get("source") or "manual"), stamp, stamp))
            except sqlite3.IntegrityError as exc: raise ApiError(409, "DUPLICATE_COMPANY", "该统一社会信用代码已存在。") from exc
            self.audit(conn, ctx, "company.create", "company", company, "success", {"name": name})
            return dict(conn.execute("SELECT * FROM companies WHERE id=?", (company,)).fetchone())

    def create_user(self, ctx: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        username, password = str(data.get("username", "")).strip().lower(), str(data.get("password", ""))
        role = str(data.get("role", "sales"))
        if not username or len(username) > 100 or role not in ROLE_PERMISSIONS: raise ApiError(400, "INVALID_USER", "用户名或角色无效。")
        if len(password) < 12: raise ApiError(400, "WEAK_PASSWORD", "用户密码至少需要 12 位。")
        user, stamp = uid("usr"), now()
        with self.db() as conn:
            try:
                conn.execute("INSERT INTO users(id,tenant_id,username,password_hash,display_name,department,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (user, ctx["tenant_id"], username, self.password_hash(password), str(data.get("displayName") or username), str(data.get("department") or ""), stamp, stamp))
                conn.execute("INSERT INTO user_roles VALUES(?,?)", (user, role))
            except sqlite3.IntegrityError as exc: raise ApiError(409, "DUPLICATE_USER", "该用户名已存在。") from exc
            self.audit(conn, ctx, "user.create", "user", user, "success", {"username": username, "role": role})
        return {"id": user, "username": username, "role": role}

    def request_approval(self, ctx: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        kind, target, payload = str(data.get("type", "")), str(data.get("targetId", "")), data.get("payload")
        if kind not in {"company_transfer", "authorization_grant", "company_delete"} or not target or not isinstance(payload, dict): raise ApiError(400, "INVALID_APPROVAL", "审批参数无效。")
        approval = uid("apr")
        with self.db() as conn:
            conn.execute("INSERT INTO approvals(id,tenant_id,type,target_id,payload_json,requested_by,created_at) VALUES(?,?,?,?,?,?,?)", (approval, ctx["tenant_id"], kind, target, json.dumps(payload, ensure_ascii=False), ctx["user_id"], now()))
            self.audit(conn, ctx, "approval.request", "approval", approval, "success", {"type": kind, "targetId": target})
        return {"id": approval, "status": "pending"}

    def decide_approval(self, ctx: dict[str, Any], approval_id: str, data: dict[str, Any]) -> dict[str, Any]:
        decision = str(data.get("decision", "")); comment = str(data.get("comment", ""))[:1000]
        if decision not in {"approved", "rejected"}: raise ApiError(400, "INVALID_DECISION", "decision 必须是 approved 或 rejected。")
        with self.db() as conn:
            item = conn.execute("SELECT * FROM approvals WHERE id=? AND tenant_id=?", (approval_id, ctx["tenant_id"])).fetchone()
            if not item: raise ApiError(404, "NOT_FOUND", "审批单不存在。")
            if item["status"] != "pending": raise ApiError(409, "ALREADY_DECIDED", "审批单已处理。")
            if item["requested_by"] == ctx["user_id"]: raise ApiError(403, "SELF_APPROVAL_DENIED", "不能审批本人提交的申请。")
            conn.execute("UPDATE approvals SET status=?,reviewer_id=?,comment=?,decided_at=? WHERE id=?", (decision, ctx["user_id"], comment, now(), approval_id))
            if decision == "approved" and item["type"] == "company_transfer":
                owner = json.loads(item["payload_json"]).get("ownerId"); conn.execute("UPDATE companies SET owner_id=?,version=version+1,updated_at=? WHERE id=? AND tenant_id=?", (owner, now(), item["target_id"], ctx["tenant_id"]))
            if decision == "approved" and item["type"] == "authorization_grant":
                payload = json.loads(item["payload_json"])
                conn.execute("INSERT INTO authorization_grants(id,tenant_id,user_id,scope,resource_type,resource_id,granted_by,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (uid("grt"), ctx["tenant_id"], payload.get("userId"), payload.get("scope", "company:read"), payload.get("resourceType"), payload.get("resourceId"), ctx["user_id"], payload.get("expiresAt"), now()))
            self.audit(conn, ctx, "approval." + decision, "approval", approval_id, "success", {"type": item["type"]})
        return {"id": approval_id, "status": decision}

    def create_api_key(self, ctx: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        if not self.key_pepper: raise ApiError(503, "SECURITY_NOT_CONFIGURED", "未设置 ZHITUO_API_KEY_PEPPER。")
        name, scopes = str(data.get("name", "")).strip(), data.get("scopes", [])
        if not name or not isinstance(scopes, list): raise ApiError(400, "INVALID_KEY", "密钥名称或权限范围无效。")
        raw, key_id = "ztk_" + secrets.token_urlsafe(32), uid("key"); digest = hmac.new(self.key_pepper.encode(), raw.encode(), hashlib.sha256).hexdigest()
        with self.db() as conn:
            conn.execute("INSERT INTO api_keys(id,tenant_id,name,prefix,secret_hash,scopes_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)", (key_id, ctx["tenant_id"], name, raw[:12], digest, json.dumps(scopes), ctx["user_id"], now()))
            self.audit(conn, ctx, "key.create", "api_key", key_id, "success", {"name": name})
        return {"id": key_id, "key": raw, "prefix": raw[:12], "scopes": scopes, "warning": "请立即保存该密钥；系统不会再次显示完整值。"}

    def handle(self, method: str, path: str, headers: dict[str, str], query: dict[str, str], body: dict[str, Any] | None, ip: str = "") -> tuple[int, dict[str, Any]]:
        request_id = headers.get("x-request-id", uid("req")); ctx = None
        try:
            if method == "POST" and path == "/api/v1/auth/login": return 200, {"ok": True, "data": self.login(body or {}, ip)}
            if method == "GET" and path == "/api/v1/health": return 200, {"ok": True, "data": {"database": "ready", "authConfigured": bool(self.jwt_secret), "time": now()}}
            permission = "company:read" if method == "GET" and path == "/api/v1/companies" else "company:write" if method == "POST" and path == "/api/v1/companies" else "user:manage" if method == "POST" and path == "/api/v1/users" else "company:approve" if path.startswith("/api/v1/approvals/") else "authorization:manage" if path == "/api/v1/approvals" else "key:manage" if path == "/api/v1/api-keys" else "audit:read" if path == "/api/v1/audit-logs" else None
            if not permission: raise ApiError(404, "NOT_FOUND", "接口不存在。")
            ctx = self.authenticate(headers, permission); ctx.update({"request_id": request_id, "ip": ip})
            if method == "GET" and path == "/api/v1/companies": result = self.list_companies(ctx, query)
            elif method == "POST" and path == "/api/v1/companies": result = self.create_company(ctx, body or {})
            elif method == "POST" and path == "/api/v1/users": result = self.create_user(ctx, body or {})
            elif method == "POST" and path == "/api/v1/approvals": result = self.request_approval(ctx, body or {})
            elif method == "POST" and path.startswith("/api/v1/approvals/") and path.endswith("/decision"): result = self.decide_approval(ctx, path.split("/")[4], body or {})
            elif method == "POST" and path == "/api/v1/api-keys": result = self.create_api_key(ctx, body or {})
            elif method == "GET" and path == "/api/v1/audit-logs":
                with self.db() as conn: result = {"items": [dict(r) for r in conn.execute("SELECT * FROM audit_logs WHERE tenant_id=? ORDER BY created_at DESC LIMIT 100", (ctx["tenant_id"],))]}
            else: raise ApiError(404, "NOT_FOUND", "接口不存在。")
            return 200 if method == "GET" else 201, {"ok": True, "requestId": request_id, "data": result}
        except ApiError as exc:
            return exc.status, {"ok": False, "requestId": request_id, "error": {"code": exc.code, "message": exc.message}}

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(); parser.add_argument("--init", action="store_true"); parser.add_argument("--tenant", default="智拓演示租户"); parser.add_argument("--username", default="admin"); parser.add_argument("--password", default=os.getenv("ZHITUO_BOOTSTRAP_PASSWORD", "")); args = parser.parse_args()
    app = BackendApp()
    if args.init:
        if not args.password: raise SystemExit("请用 ZHITUO_BOOTSTRAP_PASSWORD 或 --password 提供至少 12 位的初始密码。")
        print(json.dumps(app.bootstrap(args.tenant, args.username, args.password), ensure_ascii=False))
    else: print("数据库已就绪：", app.db_path)
