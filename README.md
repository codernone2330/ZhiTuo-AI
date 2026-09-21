# 智拓 · 商机作战助手

当前仓库同时保留可直接演示的单页原型，以及用于多人并行开发的新后端公共骨架。

## 目录入口

- `data/智拓商机作战助手-开发版.html`：当前最新版前端原型。
- `src/ZhiTuoNativeServer.py`：现有 8766 演示服务，继续兼容企查查与多模型 AI 调用。
- `src/map_integration.py`：现有 8767 地图能力演示服务。
- `backend/`：FastAPI + PostgreSQL 模块化后端公共骨架。
- `docs/公共骨架搭建说明.md`：骨架结构、设计依据、启动方式与团队协作说明。

## 后端快速启动

```powershell
Copy-Item .env.example .env
docker compose up --build
```

启动后访问：

- API 文档：http://127.0.0.1:8000/docs
- 存活检查：http://127.0.0.1:8000/api/v1/health/live
- 就绪检查：http://127.0.0.1:8000/api/v1/health/ready

本地开发、数据库迁移和初始化数据命令请见
[`docs/公共骨架搭建说明.md`](docs/公共骨架搭建说明.md)。
