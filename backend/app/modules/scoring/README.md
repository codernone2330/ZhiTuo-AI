# 企业评分模型模块（scoring）

政企企业拜访优先级评分模型。输入企查查工商数据，输出 0–100 分 + S/A/B/C/D 分级 +
16 条移动政企方案线标签 + 联系方式。

**模型定位**：线索筛选级（拜访优先级排名），不是成交概率预测。

## 分层

| 文件 | 职责 | 依赖 |
|---|---|---|
| `model_config.json` | 全部业务参数的唯一真源（锚点/缺失策略/时效曲线/三版权重/分级阈值） | — |
| `lines.py` | 16 条方案线定义 + 行业→方案线映射 + 关键词 | 无 |
| `engine.py` | 纯引擎：解析、锚点、8 特征、三版打分、分级、映射自检 | 无（标准库） |
| `models.py` | `ScoreAnchorLock` 锚点锁表 | SQLAlchemy |
| `service.py` | 锚点锁持久化、批量评分、客户评分刷新 | SQLAlchemy |
| `schemas.py` | API 请求模型 | pydantic |
| `router.py` | API 路由 | FastAPI |

`engine.py` 与 `lines.py` 不依赖 FastAPI / SQLAlchemy / pandas，可被后端与独立脚本共用。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/scoring/config` | 模型配置摘要 + 当前锚点 + 方案线目录 |
| POST | `/api/v1/scoring/preview` | 对企业清单评分预览（不写客户库，仅首次落锚点锁） |
| POST | `/api/v1/scoring/refresh` | 按工商档案重算已入库客户评分并落库 |

自动集成：
- **客户导入**：`CustomerImportRow.profile`（工商字段）存在时，导入即按模型打分。
- **商机评分刷新**：`refresh_customer_scores` 写入 `Customer.score` 与 `extra_data.scoreDetail`。
- **推荐商机链路**：`opportunities.refresh_opportunities` 对带 `profile` 的客户走模型，
  仅对无档案客户回退旧规则 `_score()`；`serialize_opportunity` 向前端暴露
  `tier / solutionLine / features / scores / anchors`。
- **CRM 单条编辑**：带 `profile` 的客户在 `customers.update_customer` 中**保留模型分**，
  不被旧规则覆盖（重算走上面两个 refresh 接口）。

## 三条铁律

1. 任何归一化分母都不能硬编码 → 一律走 `model_config.json` 分位自适应锚点。
2. 文本特征绝不能混入「标签侧描述文本」→ `方案契合` 的 blob 只含企业自身信息。
3. 「文件重建了」≠「内容是最新的」→ 一切可算的数字动态派生。

## 运行方式

```bash
# A. 只出评分报告（不写库）
python scripts/score_enterprise.py [企业清单.xlsx] [--out DIR] [--as-of YYYY-MM-DD] [--no-lock]

# B. 清单入库到 PostgreSQL（入库即打分）；需后端已启动
python scripts/import_customers.py [企业清单.xlsx] [--org-code cmcc-gd-sz-ft-enterprise] [--dry-run]
```
