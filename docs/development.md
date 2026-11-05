# 开发与重复验证

## 基本约定

云任务已经隔离，使用现有 `/workspace/-Agent` 检出；不需要新建 Git worktree。Windows 的 `D:\AI_develop\EcomOpsAgent` 是你的目标本地路径，云环境无法直接访问，推送后可在该目录克隆或拉取。

`bash scripts/setup.sh` 使用冻结 npm lock 安装、固定 Python 依赖、生成本地随机凭据、启动 PostgreSQL、执行官方迁移、搜索索引迁移、创建管理员、幂等演示种子以及前后端构建。数据库存在时保留数据；再次执行种子不重置已补货库存。不要同时运行多个 setup。修改业务源码后构建需重跑。

`python3 scripts/services.py start` 启动 Medusa 开发模式和一个 FastAPI worker，保存本机 PID 与日志在 `.runtime/`；重复 start 复用服务。仅停止自己管理的进程组。发布快照不保留活进程，新任务需重新启动 Docker 容器和应用。

## 常用验证

```bash
# 在根目录运行真实集成测试：会将 ECOMOPS 演示库存恢复为 5/50/0 再批准补货。
python3 scripts/run-with-env.py .venv/bin/pytest -q --junitxml=docs/evidence/pytest.xml
# 真实浏览器后台登录、商品页、订单页
python3 scripts/run-with-env.py .venv/bin/python scripts/browser-check.py
# 规则测试，不需要服务
.venv/bin/pytest -q tests/test_policy.py
# Medusa（在 commerce/apps/backend）
npm run build
npm run lint
```

集成测试仅操作 `ECOMOPS-*` 虚构 SKU，并验证两个订单各预留一个杯子：杯子账面 5、预留 2、可用 3；袋子 50；水瓶 0。正常验收后杯子账面 32、预留 2、可用 30，水瓶可用 30。测试会保存业务 JSON 和 JUnit XML，不能在生产库运行。

## 调用 Agent

```bash
python3 scripts/run-with-env.py .venv/bin/python - <<'PY'
import os, httpx
headers = {'Authorization': 'Bearer ' + os.environ['ECOMOPS_API_TOKEN']}
with httpx.Client(base_url='http://127.0.0.1:8100', trust_env=False, timeout=90) as client:
    result = client.post('/inventory/check', json={'threshold': 10, 'target': 30}, headers=headers)
    result.raise_for_status()
    print(result.json())
    # 审阅建议后显式调用 /recommendations/{id}/approve；不要自动批准全部建议。
PY
```

MCP 可独立运行：`python3 scripts/run-with-env.py .venv/bin/python mcp-server/server.py`。使用标准 stdio MCP 客户端；stdout 保留协议流，不添加调试 print。MCP 仅提供读工具。库存写入经 FastAPI 审批端点执行，并需 `ECOMOPS_ALLOW_REPLENISHMENT=1`，本地开发包装器明确开启。

## 本地凭据与安全

`.runtime/credentials.json` 和 `commerce/apps/backend/.env` 均忽略提交。凭据文件权限 0600。管理员凭据、JWT/Cookie 密钥和数据库密码随机生成；示例模板不含运行中的真实秘密。服务只用于开发；Agent 绑定回环地址并要求 Bearer Token。管理 API 通过 Medusa 正常登录认证，不绕过权限。

修改凭据时必须同步 PostgreSQL 用户密码与 Medusa `.env`；不要删除 credentials 文件而保留旧数据库卷后直接重建。不要提交数据库卷、SQLite 审计库、日志或 `.env`。

## 排错

- 云环境 shell 沙箱可能阻止访问代理、Docker socket 和回环服务；使用平台允许的网络/执行权限，保留代理和 TLS 校验，不绕过网络策略。
- Medusa `db:migrate` 卡住时先检查 `.env` 中 DATABASE_URL 是否存在。脚手架必须完全结束后再生成本地配置。
- 后台搜索需迁移并由 shared worker 启动时回填索引；日志出现 active version 后再查询。
- 本地 Event Bus / 内存锁 / 无 Redis 是开发模式选择。生产部署需外部持久化队列/锁和独立发布规划。
- 入库失败或进程崩溃：先读取建议审计与 Medusa 实际库存，避免重复收货。当前没有自动重试不确定写入。
