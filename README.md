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
