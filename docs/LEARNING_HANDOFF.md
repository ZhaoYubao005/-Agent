# EcomOps Agent 源码学习与演示

## 从界面到业务调用

1. 打开 `/console`，用开发令牌验证身份并读取角色。
2. 查看 SKU 和仓库库存，核对 stocked、reserved、available 三个维度。
3. 运行库存检查。对杯子提案比较预留数量与可用库存，记录提案 ID。
4. Admin 批准提案。查询 Medusa，确认批准只改变提案状态。
5. Operator 在隔离环境确认实收。水瓶可先确认 12 件，重放原请求 ID 后仍为 12 件，
   再确认剩余 18 件。对照审计里的操作者、状态和数量。
6. 刷新订单告警。读出规则、证据和严重级别，确认告警后重复扫描，状态应保留。
7. 预览当天日报，保存已结束营业日的日报，再从历史查询并导出 CSV。
8. 查看报告中 LLM 或规则的来源，以及审计和调度状态。

## 建议阅读顺序

- `agent-service/ecomops/app.py` 定义 API、输入校验与角色限制。`auth.py` 解析账号与令牌。
- `agent-service/ecomops/tools.py` 用官方 MCP SDK 创建 stdio 会话、初始化并调用工具。
  `mcp-server/server.py` 提供只读业务工具。`medusa.py` 封装管理 API、分页和上游读取。
- `agent-service/ecomops/graph.py` 执行库存快照和策略节点。
  `operations_graph.py` 编排订单采样、告警判断、日报指标及 AI 解读。
- `agent-service/ecomops/operations.py` 是确定性业务规则入口。
  先阅读 SLA 计算，再看按 IANA 时区构建的半开统计区间、取消过滤和跨币种口径。
- `agent-service/ecomops/receipts.py` 执行受限的隔离库存写入。
  `database.py` 保留应用状态和同仓库并发领取约束。`audit.py` 记录状态变更主体。
- `agent-service/ecomops/scheduler.py` 记录每日任务的领取及处理状态。
  `advice.py` 发送去标识聚合数据并校验结构。
- `tests/test_live_flow.py`、`scripts/console-check.py` 和 `scripts/llm-check.py`
  对应业务流程、浏览器及真实本地模型验收。

## 面试可以讲清楚的实际问题

- **并发边界。** PostgreSQL 的事务级 advisory lock 保护 Agent 对同库存品、
  同仓库的提案领取。Medusa 2.22.3 的库存调整路径使用独立读写操作。
  因此演示写入需要隔离环境。回读用于发现不一致，无法阻止未遵守 Agent 约束的外部写入者。
- **幂等和恢复。** 批次请求 ID 与数量绑定。审批、发起收货与实际入库各有状态。
  Medusa 响应不明时进入人工核对。崩溃发生在跨服务提交之间时，用 Medusa 账和实物核对。
- **LLM 的责任范围。** 告警判定和统计由规则完成。LLM 只排序受聚合事实支持的核查建议。
  Pydantic 限制模型返回候选代号，代码渲染事实摘要与建议文字，MCP 不提供业务写工具。
- **指标口径。** 订单金额与已收款、退款、净营收分别解释。日期按当地时区切界，
  金额按币种保留，历史报告中的库存为生成时快照。
  Medusa 收款和退款按流水入账日汇总，订单金额按创建日汇总。
- **证明项目运行过。** CI 工件含提交 SHA、JUnit、库存前后差异和浏览器证据。
  浏览器时间是虚构小样本的单次观察，适合作回归基线。
  PostgreSQL 压力、长期锁等待及大订单量需要独立验收。

## 上游与范围

- [Medusa v2.22.3](https://github.com/medusajs/medusa/tree/v2.22.3) 及其 MIT 许可。
- [Medusa 库存位置写入路由](https://github.com/medusajs/medusa/blob/v2.22.3/packages/medusa/src/api/admin/inventory-items/%5Bid%5D/location-levels/%5Blocation_id%5D/route.ts)。
- [Psycopg](https://www.psycopg.org/psycopg3/docs/api/connections.html) 和
  [PostgreSQL 17 advisory lock](https://www.postgresql.org/docs/17/functions-admin.html)。
- [Ollama Qwen 2.5 0.5B 模型及许可](https://ollama.com/library/qwen2.5:0.5b)。
  项目提供运营 Agent、演示数据、只读 MCP 和验收脚本，容器构建仍从上游安装依赖。
