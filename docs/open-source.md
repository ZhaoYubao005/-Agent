# 开源来源与许可证

- 电商核心上游：<https://github.com/medusajs/medusa>，MIT。
- 使用 npm 正式发行 `create-medusa-app@2.22.3`，安装参数 `--version 2.22.3 --skip-db --no-browser --use-npm`，店面提示选择 No。
- 该版本脚手架默认应用模板是官方 <https://github.com/medusajs/dtc-starter>。生成的是后端应用工作区，不是复制 Medusa 核心 monorepo。
- 保留 `commerce/LICENSE`、官方 README、AGENTS.md 和源码说明；根 LICENSE 同样保留 Medusa 原有许可声明。新增 EcomOps 代码采用 MIT。
- `commerce/package-lock.json` 固定实际依赖树；Medusa 依赖明确为 2.22.3。Python 依赖由 `requirements.lock` 固定，MCP SDK 2.3.0。
- PostgreSQL 来自官方 Docker 镜像 `postgres:17-bookworm`，setup 脚本固定此次验证的镜像摘要。第三方软件分别遵循各自原始许可证，根 MIT 不替代第三方许可。

EcomOps 新增内容：根目录 Python 服务和 MCP；`commerce/apps/backend/src/scripts/seed-ecomops.ts`、`ensure-demo-admin.ts`；开发数据库连接池设置；本地随机凭据、服务管理、测试与文档。没有修改已安装的 Medusa 核心包。
