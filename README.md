# 智拓 · 商机作战助手

当前仓库同时保留可直接演示的单页原型，以及用于多人并行开发的新后端公共骨架。

## 目录入口

- `data/智拓商机作战助手-开发版.html`：当前最新版前端原型。
- `src/ZhiTuoNativeServer.py`：现有 8766 演示服务，继续兼容企查查与多模型 AI 调用。
- `src/map_integration.py`：现有 8767 地图能力演示服务。
- `backend/`：FastAPI + PostgreSQL 模块化后端公共骨架。
- `docs/公共骨架搭建说明.md`：骨架结构、设计依据、启动方式与团队协作说明。

## 后端快速启动

### Windows 一键启动

安装并启动 Docker Desktop 后，直接双击仓库根目录的：

```text
start.cmd
```

脚本会自动检查 Docker、创建本地 `.env`、构建并启动 API/PostgreSQL/Redis、
执行数据库迁移与初始化、等待健康检查通过，并打开 API 文档。

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

当前系统页面已经接通后端登录、会话恢复、组织识别和用户管理；客户、商机、拜访等
业务数据仍保留在前端原型中，后续按模块迁移到数据库。
