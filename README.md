# EcomOps Agent

第一阶段：基于 Medusa 官方开源应用的电商运营 Agent。已实现真实商品、订单、库存 API 读取，以及通过 MCP / LangGraph 完成的库存预警、补货建议、显式批准、入库与回读审计。

## 结构

- `commerce/apps/backend/`：官方 create-medusa-app 生成的 Medusa 2.22.3，含管理后台。
- `agent-service/ecomops/`：FastAPI、LangGraph、Medusa API 适配与 SQLite 审计。
- `mcp-server/server.py`：stdio MCP 2.x 工具。
- `scripts/`：本地凭据生成、重复安装、服务启动及浏览器验证。
- `tests/`：库存规则测试及真实服务集成测试。
- `docs/`：架构、开发说明、来源与执行证据。

## 快速启动（Linux / WSL2）

要求 Docker Engine/Desktop 可用，Node.js >=22.23.4、npm 11、Python 3.12、Bash。Windows 工程可克隆到 `D:\AI_develop\EcomOpsAgent`；以下脚本在 WSL2 中运行。当前交付实际构建和运行于云端 `/workspace/-Agent`，不会自动写入 Windows 磁盘。

```bash
git clone https://github.com/ZhaoYubao005/-Agent.git EcomOpsAgent
cd EcomOpsAgent
bash scripts/setup.sh
python3 scripts/services.py start
python3 scripts/run-with-env.py .venv/bin/pytest -q --junitxml=docs/evidence/pytest.xml
python3 scripts/run-with-env.py .venv/bin/python scripts/browser-check.py
```

浏览器检查需要 Chromium（云环境已提供 `/usr/bin/chromium`）；其他机器安装系统 Chromium，或 `.venv/bin/playwright install --with-deps chromium`。`setup.sh` 不重复安装宿主机 Docker/Node，缺失时须按系统官方方式先安装。

管理后台使用 9000 端口的 `/app`；Agent 使用 8100 端口。管理员邮箱 `admin@ecomops.test`。密码和 Agent Token 仅在本机 `.runtime/credentials.json` 中，由脚本随机生成；不要上传、分享或提交该文件。程序调用请通过 `scripts/run-with-env.py` 注入，示例见 [开发说明](docs/development.md)。

```bash
python3 scripts/services.py status
python3 scripts/services.py stop
```

停止应用不会删除数据库。PostgreSQL 使用命名卷 `ecomops-postgres`；禁止在有真实数据的环境运行演示测试。

## 验收与边界

查看 [第一阶段报告](docs/phase1-report.md)、[测试证据](docs/evidence/phase1-live.json)、[架构](docs/architecture.md) 和 [开源来源](docs/open-source.md)。这是单实例开发演示，不是生产部署：规则为确定性库存阈值，尚未接入付费 LLM、真实供应商、采购支付或多仓优化。

### Inventory approval and receipt

Approval moves a proposal to `approved` without modifying stock. Confirm
physical receipt via `POST /recommendations/{id}/receive`. Before receipt,
a proposed or approved request can be cancelled. A pending request can be
rejected. A receipt with an uncertain result requires manual reconciliation.

The current Medusa Admin API adapter uses separate snapshot and update calls.
An external writer can change stock between those calls. The development
wrapper therefore disables receipts by default. The isolated CI runner enables
`ECOMOPS_ISOLATED_FIXTURES=1` for its disposable stack. The adapter limits these
writes to the three seeded fixture SKUs. Keep fixture receipt mode disabled
for shared or production Medusa instances.

## EcomOps 运营控制台

Agent 控制台位于 `http://127.0.0.1:8100/console`，Medusa 后台位于
`http://127.0.0.1:9000/app`。浏览器中的个人访问令牌只保存在当前页面，
由服务端识别账户及角色。以环境变量 `ECOMOPS_ACCOUNTS` 配置命名账户，
值为 JSON 对象，账户条目包含 `role` 和 `token`。角色如下。

- Viewer 查询库存、订单异常、日报、导出和任务。
- Operator 创建补货提案、确认已批准的实物收货、处理告警和生成日报。
- Admin 增加批准、拒绝、人工核对和查看操作审计的权限。
- 未提供命名账户配置时，`ECOMOPS_API_TOKEN` 用于本机开发管理员。

以 `docker compose up -d --build --wait --wait-timeout 240` 启动。
配置字段见 [环境变量示例](.env.example)，其中的密码和令牌必须独立生成。
Medusa 与 Agent 端口默认仅绑定本机。Compose 内部通过私有网络访问数据库，
MCP 作为 Agent 启动的标准 stdio 子进程运行，无独立公开端口。

`ECOMOPS_DATABASE_URL` 可启用 Agent 的 PostgreSQL `ecomops_agent` schema。
SQLite 仍可用于单机。迁移时先停止 Agent 并制作离线 SQLite 备份，设置目标
数据库连接串，再运行 `PYTHONPATH=agent-service python scripts/migrate-operations.py <备份文件>`。
迁移以空 schema 为目标，并保留业务 ID、状态和审计顺序。PostgreSQL
Agent 行为与 Medusa 商业数据库事务分开。

每日指定时区及小时由 `ECOMOPS_REPORT_ZONE` 和 `ECOMOPS_REPORT_HOUR`
控制，两个都未配置时调度关闭。调度器保存上一已完成营业日，
按日期和时区去重。当天使用预览接口。历史快照保留生成时间，JSON 与 CSV
导出可核对统计窗口。订单金额为订单口径，并按币种分开。净营收与退款金额
缺少完整的支付与退款明细时显示不可用。库存与告警概况使用生成时的现场快照。

LLM 默认关闭。配置 `ECOMOPS_LLM_PROTOCOL=ollama`、Ollama 主机 URL 和模型名
可使用本地原生接口。配置 `ECOMOPS_LLM_PROTOCOL=openai`、带 `/v1` 的
兼容 API 基址、模型名及服务端密钥可使用兼容 API。两种模式均校验 JSON
结构，仅发送聚合数值。失败时仍生成确定性日报和来源标记。
详见 [Ollama API](https://docs.ollama.com/api/chat) 和 [兼容 API](https://docs.ollama.com/api/openai-compatibility)。

- [架构与边界](docs/architecture.md)
- [教学交接和演示](docs/LEARNING_HANDOFF.md)
- [实时回归](.github/workflows/live.yml)、[Compose 验收](.github/workflows/compose.yml)、
  [真实本地模型验收](.github/workflows/llm.yml)

共享环境禁用 Agent 入库。隔离演示须明确开启写入开关，且收货期间没有其他库存
写入者。一个提案可以分批收货，每批请求 ID 只使用一次。若请求结果不确定，
通过 Medusa 记录和实物盘点核对，由 Admin 记录核对结果后重新提出差额提案。
若进程在收货中断，先停止可能仍在执行的实例，再处理遗留的 `applying` 状态。

对异常中断后遗留的 `applying` 记录，维护员停掉所有 Agent 写入实例，
核对实物和 Medusa 后使用 `scripts/reconcile-receipt.py` 写入人工处置记录。
该脚本只结束 Agent 的本地状态。实际差额需创建并批准独立的新提案。
