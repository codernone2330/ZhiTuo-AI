# 业务模块边界

每个模块后续统一按 `models.py / schemas.py / repository.py / service.py / router.py`
分层。模块之间通过 service 或明确的领域事件协作，不直接跨模块修改数据表。

- `auth`：登录、Token、角色与授权入口。
- `organizations`：集团、省、市、区县、部门树及数据范围。
- `users`：用户、客户经理、组织岗位关系。
- `customers`：政企客户档案、归属和生命周期。
- `approvals`：客户变更、删除等统一审批流。
- `opportunities`：商机、评分、企查查线索归集。
- `visits`：拜访任务、行动清单与地图路线。
- `documents`：共享文档、版本和读写权限。
- `reports`：周经分、指标快照、同比环比。
- `ai`：模型网关、脱敏上下文、审计与用量。
