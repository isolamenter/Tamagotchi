# Agent instructions

本文件是所有 AI Agent 的 canonical instructions。不要在 harness 专用文件复制另一套规则；如需入口文件，只引用这里。

## 入口与命令

- 先读 [README.md](../README.md) 获取环境、启动、测试和工具命令，再读 [ARCHITECTURE.md](../ARCHITECTURE.md)。
- 应用组装：`dafeiyu/app.py`；配置装载：`dafeiyu/config.py`；行为配置：`config/*.toml`。
- 在仓库根目录运行：`python -m unittest discover -s tests -v`、`ruff check .`、`git diff --check`。
- 安全 UI 预览：`python -m uvicorn tests.preview_server:create_preview --factory --host 127.0.0.1 --port 8765`。其他运行/构建命令以 README 为准。

## 核心约束

- 保持会话隔离、消息来源与事实/传闻/想象区分；不要恢复旧版养成数值或游戏动作。
- 运行时只接受 schema 3；旧库走离线迁移至新文件，不原地升级、覆盖或清洗用户数据。
- 不提交 `.env`、数据库、原始聊天导出或凭据；测试优先使用临时数据库和 fake 集成。
- `RuntimeState` 的锁与缓冲仅在单进程内有效；不要假设多 worker 安全。
- 真实启动、GM 操作、向量重建可能调用收费模型或向群聊发言；未经任务授权不要连接生产服务或真实数据。
- 保持改动与任务范围一致；提交、合并和部署分别遵循用户授权，不把审核分支当作 main 已接受的事实。

## 工作流

Understand → Inspect → Plan → Execute → Verify → Update Docs

1. **Understand**：明确目标、验收条件与禁止修改范围。
2. **Inspect**：读相关源码、配置、测试及下列文档；以当前 checkout 为准。
3. **Plan**：简单任务直接做；只有复杂、多阶段工作才在 `docs/plans/` 写计划，不为已完成工作补写。
4. **Execute**：最小必要改动，避免无关重构与重复事实来源。
5. **Verify**：运行相关测试及上述通用检查；报告实际通过、失败、未运行的部分，不把 mock 通过当线上验收。
6. **Update Docs**：同步受影响的长期行为/边界；没有变化则不制造文档。

## 文档路由

- 人类使用与开发入口 → `README.md`；模块/数据流/边界 → `ARCHITECTURE.md`。
- 功能目标、业务规则和约束 → `docs/specs/`。
- 有代码、配置或现有记录支持的架构决策 → `docs/decisions/`；原因不明写 `Unknown / Needs confirmation`，不得编造历史。
- 尚未完成的复杂工作计划 → `docs/plans/`；常量、字段和 API 明细优先引用源码，不重复抄写。
