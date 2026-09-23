# 登录、用户、组织与 RBAC 模块

这是胡一骏负责范围的独立后端模块：多租户登录、用户、组织树、RBAC 与审计留痕。仅使用 Python 标准库和 SQLite，不会修改现有服务的路由或数据库。

## 已覆盖的交付

- 租户隔离：用户、组织和操作都绑定 `tenant_id`；跨租户的组织/用户引用会被拒绝。
- 登录安全：scrypt 密码哈希、HMAC 签名的 8 小时令牌、密码错误 5 次锁定 15 分钟、停用即时失效。
- 组织管理：支持任意层级的组织树，且组织编码在租户内唯一。
- RBAC：预置 `tenant_admin`、`sales_manager`、`sales`、`auditor` 和 `platform_admin`，支持创建租户自定义角色及替换式授权；每个敏感操作先进行权限校验。
- 审计：建租户、登录、建组织、建用户和停用用户均写入审计日志，且绝不记录明文密码或令牌。

## 本地验证

在本目录执行：

```powershell
python -m unittest test_auth_rbac_service.py -v
```

## 最小接入示例

```python
from pathlib import Path
from auth_rbac_service import AuthError, AuthRbacService

service = AuthRbacService(
    Path("data/auth.db"),
    token_secret="请从部署环境注入至少32位的随机密钥",
)

# 首次初始化一次即可
tenant = service.create_tenant("智拓", "admin", "至少12位的强密码", "胡一骏")
login = service.login(tenant["tenantId"], "admin", "至少12位的强密码", ip="127.0.0.1")

# 业务路由拿到 Authorization: Bearer <token> 后：
context = service.authenticate(login["accessToken"], "user:manage")
```

调用方应捕获 `AuthError`，将其中的 `status`、`code`、`message` 按项目统一响应格式返回。生产环境必须用机密管理器提供随机 `token_secret`，不要把它写入源代码或提交到仓库。
