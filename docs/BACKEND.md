# 智拓后端部署与接口说明

现有地图服务和其密钥文件保持原样。业务后端使用 `data/zhituo.db`，不保存第三方服务明文密钥。

## 初始化

PowerShell 中先加载环境变量（生产环境应由部署平台的机密管理器注入）：

```powershell
$env:ZHITUO_JWT_SECRET = "至少32位随机值"
$env:ZHITUO_API_KEY_PEPPER = "至少32位随机值"
$env:ZHITUO_BOOTSTRAP_PASSWORD = "至少12位的高强度初始密码"
python src/zhituo_backend.py --init --tenant "智拓" --username admin
```

首次初始化会创建平台管理员；再次运行会拒绝执行。密码使用 scrypt 派生存储；令牌和 API 密钥仅保存可验证摘要。

## 权限模型

`platform_admin` 拥有全权；`tenant_admin` 管理租户内用户、授权与密钥；`sales_manager` 可管理及审批企业；`sales` 只读写本人客户；`auditor` 只读企业和审计。企业转移、授权授予和删除必须创建审批单，申请人不能自行审批。

## 接口

所有业务接口使用 JSON，统一返回 `{ok, requestId, data}` 或 `{ok:false,error}`。除登录、健康检查外都需要 `Authorization: Bearer <token>`。

| 方法 | 地址 | 权限 | 用途 |
|---|---|---|---|
| POST | `/api/v1/auth/login` | 公开 | 登录并取得 8 小时令牌 |
| GET / POST | `/api/v1/companies` | 企业读 / 写 | 分页检索、录入或同步企业 |
| POST | `/api/v1/users` | 用户管理 | 创建租户用户并分配预定义角色 |
| POST | `/api/v1/approvals` | 授权管理 | 提交企业转移、授权或删除申请 |
| POST | `/api/v1/approvals/{id}/decision` | 企业审批 | 审批并执行企业转移 |
| POST | `/api/v1/api-keys` | 密钥管理 | 创建只展示一次的服务密钥 |
| GET | `/api/v1/audit-logs` | 审计读取 | 查询租户操作留痕 |

## 接入当前本地服务

在 `ZhiTuoNativeServer.py` 的请求处理器中，将 `/api/v1/` 请求转发给 `BackendApp.handle()`。保留现有企业查询和地图接口不变即可。生产部署时将服务监听在反向代理之后；代理应启用 HTTPS、严格 CORS、请求体限制及访问日志脱敏。
