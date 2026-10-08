"""智拓·政企企业评分模型模块。

分层（遵循 `backend/app/modules/README.md`）：
- `engine.py` / `lines.py` / `model_config.json`：纯引擎，无 IO、无第三方依赖，可被后端与
  独立脚本共用。
- `models.py` / `service.py` / `schemas.py` / `router.py`：FastAPI 与数据库集成层。

模型定位：**线索筛选级**（拜访优先级排名），不是成交概率预测。
"""

from . import engine, lines

__all__ = ["engine", "lines"]
