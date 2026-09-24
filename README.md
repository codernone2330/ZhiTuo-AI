# 智拓 · 商机作战助手

当前 `/app/` 是由 FastAPI 容器托管的前端页面，前端、API、PostgreSQL 和 Redis 由同一套 Compose 配置启动。仓库也保留旧演示服务供历史对照，但旧服务与当前共享数据库不自动同步。

## 目录入口

- `data/智拓商机作战助手-开发版.html`：当前最新版前端原型。
- `src/ZhiTuoNativeServer.py`：现有 8766 演示服务，继续兼容企查查与多模型 AI 调用。
- `src/map_integration.py`：现有 8767 地图能力演示服务。
- `backend/`：FastAPI + PostgreSQL 模块化后端公共骨架。
- `log_in/`、`src/zhituo_backend.py`：保留 `main` 分支原有的独立 SQLite/RBAC 原型；当前 `/app/` 页面使用 `backend/` 服务，两套数据不自动同步。
- `docs/公共骨架搭建说明.md`：骨架结构、设计依据、启动方式与团队协作说明。

## 后端快速启动

### Windows 一键启动

安装并启动 Docker Desktop 后，直接双击仓库根目录的：

```text
start.cmd
```

脚本会检查或打开 Docker Desktop、创建本地 `.env`、构建最新版前端和 API、
启动 PostgreSQL/Redis、执行数据库迁移与初始化，检查 API、页面、Logo 和关键业务路由后打开系统页面。
前端仅通过当前页面同源的 `/api/v1` 调用后端，团队部署时不再固定访问启动者电脑的 `127.0.0.1`。
当前 Compose 仍将 `8000` 端口只绑定在本机 `127.0.0.1`；跨电脑共享需另行配置受控网络入口、HTTPS、`TRUSTED_HOSTS` 和备份，不应直接开放数据库端口。

如果 Docker Desktop 弹出 `cannot find registry key SOFTWARE\\Docker Inc.\\Docker Desktop`，这是 Docker Desktop 的当前用户安装注册信息异常，不是系统页面报错。先确认 Docker 引擎是否仍可运行；若引擎无法启动，按 Docker 官方安装流程修复当前用户安装。不要在备份 PostgreSQL 数据卷前卸载或重置 Docker 数据。

停止服务时双击：

```text
stop.cmd
```

停止脚本不会删除 PostgreSQL 和 Redis 数据卷。

### 命令行启动

```powershell
Copy-Item .env.example .env
docker compose up --build
```

启动后访问：

- 系统页面：http://127.0.0.1:8000/app/
- API 文档：http://127.0.0.1:8000/docs
- 存活检查：http://127.0.0.1:8000/api/v1/health/live
- 就绪检查：http://127.0.0.1:8000/api/v1/health/ready

本地开发、数据库迁移和初始化数据命令请见
[`docs/公共骨架搭建说明.md`](docs/公共骨架搭建说明.md)。

身份认证、组织权限和用户管理接口见
[`docs/身份与组织接口说明.md`](docs/身份与组织接口说明.md)。

客户导入、分页查询、详情、共享范围和前端适配说明见
[`docs/客户读链路说明.md`](docs/客户读链路说明.md)。

拜访任务与推荐商机的后端接口和迁移规则见
[`docs/拜访商机后端链路说明.md`](docs/拜访商机后端链路说明.md)。

当前系统页面已接通后端登录、会话恢复、组织识别、用户管理、客户导入/查询、CRM 变更与删除审批、拜访任务、商机评分刷新、企查查待审核线索池、多模型 AI 网关、地图路线、周经分与共享文档。客户主数据、任务、审批、线索和文档由 PostgreSQL 保存。推荐商机由客户主档派生；企查查线索需另一名有权限的管理人员审核后才会入库，点击“AI 更新商机”本身只刷新现有商机评分。

企查查、腾讯地图和各 AI 模型需要在本机 `.env` 中配置有效凭证才能使用真实外部服务。不要把 `.env` 或 API Key 提交到 Git。首次共享测试前还需更改默认管理员口令和 JWT 密钥。
