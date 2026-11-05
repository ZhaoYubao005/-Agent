# EcomOps Agent 第一阶段验收

实际工作目录 `/workspace/-Agent`；目标 GitHub `ZhaoYubao005/-Agent`。此仓库原为空，已直接在云端创建应用；没有要求用户上传源码，也没有向 Medusa 上游推送。

## 已交付

1. 官方 `create-medusa-app@2.22.3` 生成的 Medusa 应用，正式版依赖 2.22.3；保留 MIT 许可证与来源。
2. PostgreSQL 17 持久化容器、真实连接与迁移；保存 SQL 查询证据。
3. 三个 EcomOps 虚构商品，两个 `pending` 订单及真实库存预留。另保留官方初始种子的四个商品。
4. 商品、订单明细、库存 Admin API 真实请求通过；后台经 Chromium 实际登录并渲染商品及订单页。
5. 标准 stdio MCP 的初始化、工具发现与工具调用，读取同一 Medusa 实例的业务事实。
6. FastAPI + LangGraph 库存规则，建议保存、令牌认证、显式批准、真实库存入库、回读及 SQLite 审计。
7. 重复批准不重复入库；过期库存快照拒绝写入，标记需要核对。
8. 可重复安装、启动、重复启动、停止/重启脚本；不提交 `.env`、凭据或数据库。

## 真实业务结果

| SKU | 初始账面 | 预留 | 初始可用 | 建议补货 | 入库后可用 |
| --- | ---: | ---: | ---: | ---: | ---: |
| ECOMOPS-MUG | 5 | 2 | 3 | 27 | 30 |
| ECOMOPS-BAG | 50 | 0 | 50 | 无 | 50 |
| ECOMOPS-BOTTLE | 0 | 0 | 0 | 30 | 30 |

阈值 10、目标 30。批准后重新运行分析，预警列表为空。订单依然存在、杯子的两个预留未被覆盖。

## 验证与证据

- `bash scripts/setup.sh`：完整重跑成功，保留既有数据。
- Medusa build：lint 无警告，后端与管理前端构建成功，见 [build.log](evidence/build.log)。
- pytest：5/5 通过，0 跳过，见 [JUnit XML](evidence/pytest.xml)。包含 4 组规则边界案例和 1 个完整真实服务集成测试。
- 集成测试覆盖真实 API、MCP、LangGraph、无凭据拒绝、错误 Token 拒绝、无效参数、重复批准、旧快照拒绝及预警清空；见 [业务 JSON](evidence/phase1-live.json)。
- Chromium：后台实际登录、商品/订单页面及 0 页面错误，见 [browser.json](evidence/browser.json) 和同目录截图。
- PostgreSQL：见 [postgres.json](evidence/postgres.json)。
- 测试证据只包含虚构业务数据，不含运行凭据。时间字段使用 UTC；截图为当前云端运行结果。

## 限制与未完成项

第一阶段开发演示已交付。尚未建设生产部署、多实例锁/事务、外部消息队列、供应商采购、支付履约、需求预测或 LLM 对话能力。当前使用确定性 LangGraph 库存策略，不冒充 LLM 推理。收货为演示数据库实际库存更新，不代表真实采购到货。

本地 SQLite/内存锁只支持单 worker；其他写入方并发修改库存时存在读写竞争，详见架构文档。`applying`/`needs_review` 必须人工核对真实库存，不做危险的自动重试。

云端不能直接写入 Windows 路径。远程同步后，可在 `D:\AI_develop\EcomOpsAgent` 克隆或拉取；Windows 原生运行未验证，推荐 WSL2。未发布云环境快照，保存配置不等于发布，也未验证新任务恢复。
