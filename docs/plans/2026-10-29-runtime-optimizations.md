# 运行时稳定性与召回性能优化执行计划

状态：P0、P1（`fb6b0e0`）、P3 已完成；P2 按 P0 结论延后。2026-10-29 根据源码（`2bfdf56`）审查整理。日志可观测性另见 [2026-10-29-observability.md](2026-10-29-observability.md)，本计划不重复。

## 范围

| 编号 | 事项 | 优先级 | 涉及 schema / 数据 |
| --- | --- | --- | --- |
| P0 | 生产只读测量：记忆规模、向量维度、依赖版本 | 前置 | 只读 |
| P1 | 回复与普通插话的生成阶段加超时（含 embedding） | 高 | 否；新增一项 TOML 配置 |
| P2 | 召回向量扫描合并为一次并减少重复计算 | 中（视 P0 数据） | 否 |
| P3 | 生产依赖锁定版本 | 中 | 否 |

不在范围：日志落盘与错误原因（归 observability 计划）、迁移、向量重建、调度语义变化、部署。

复核后不纳入：

- **Webhook 改用 `runtime.spawn`**：Starlette 的 BackgroundTasks 在响应发出后执行，uvicorn 优雅关闭会等待它完成；改为 `spawn` 会在 lifespan 退出时被取消，反而可能在飞书发送途中中断。未处理异常的日志一致性归 observability 计划处理。
- **tenant token 内存缓存**：每次飞书调用多一次 SQLite 读，当前流量下收益很小。
- **`sys_cache` 过期行清理、`runtime` 锁字典回收**：key 有限且多为覆盖写，暂无增长问题。

## P0 生产只读测量

需要用户单独授权连接 `gcp-vps`；只读，不重启、不调用模型、不发消息、不打印聊天正文或密钥。

1. 从 `systemctl cat/show tamagotchi` 与环境文件解析实际 `STATE_DB` 路径和发布目录。
2. 从生产配置确认当前 `llm_provider` 与 `embed_model`（召回 SQL 用这两项加查询向量长度过滤）。
3. 以只读模式（`file:...?mode=ro` URI）查询，过滤条件与 `MemoryRepository._score_cards` 一致：`memory_embeddings JOIN memories`，`superseded_by IS NULL`、未过期、`provider/model` 为当前值。按 `conversation_id, dimension` 分组，记录：
   - 可被召回的有效向量数（全部 / `subject_ids='[]'` / 有主体）；
   - 同会话有效记忆总数，用于看缺向量或只有旧 provider/model 向量的数量（仅作参考，不计入扫描规模）。
4. 在当前发布目录的 Python 环境执行 `pip freeze`，只记录 `requirements.txt` 中直接依赖的版本。
5. 结论写回本文件：取最大会话的有效向量数及其维度。若明显低于 2000 × 3072（例如 < 200 条），P2 降级为可选；若有会话超过 `recall_scan_max`，记录无主体卡占比，用于评估 P2 的行为差异。

**结论（2026-10-29 只读测量）**：

- 运行发布 `releases/2bfdf56`，环境文件 `shared/production-947ec05.env`，`STATE_DB=/home/akiya/tamagotchi/data/dafeiyu-0cfa307.db`（schema 3），`LLM_PROVIDER=gemini`、`EMBED_MODEL=gemini-embedding-2`，`recall_scan_max = 2000`。
- `memory_embeddings` 仅一组：gemini / gemini-embedding-2 / 3072 维，147 行。
- 只有会话 21 有有效记忆：147 条，全部有当前向量，全部为无主体卡（有主体 0 条），人物限定查询目前恒为空。
- 147 × 3072 低于阈值，**P2 降级为可选，本轮不做**。
- 生产 Python 3.11.2；所有发布共用 `/home/akiya/tamagotchi/.venv-v2-6d307d4`，并非每次在发布目录重新安装（见 P3）。

## P1 模型调用超时

**问题**：直接回复与普通插话在会话 `chat_lock` 内完成整段生成，外层没有超时。生成阶段除 `llm.chat_json` 外，还包括记忆召回与风格检索的 `embed_text`（`MemoryService.recall_relevant_cards`、`StyleService.render_examples_block`）。OpenAI SDK 默认单次 600 秒且重试 2 次，Gemini SDK 默认无超时；任一调用挂起，该会话后续回复全部排队。

`AutonomousService.prepare_reply` 与 `participation_decision` 同时被 `ScheduledService` 复用，后者已在调用边界分别加了 `scheduled_generation_timeout_sec`（90 秒）与 `scheduled_evaluation_timeout_sec`（15 秒）。因此超时不能加在这两个共享方法内部，否则会把梦境生成压到更短的限制。记忆整理、日记梦境、身份路由、梗图复核已各自有超时，不改。

**改动**：

1. `config/settings.toml` 的 `[llm]` 增加 `reply_timeout_sec`（默认值待确认，见文末），`config.py` 读取并校验范围，例如 (0, 120]。含义是“一次回复/插话生成阶段的总时限”，覆盖历史加载、身份路由、召回与风格 embedding、`chat_json`，不覆盖发送。
2. 只在普通回复/插话专用的调用边界加 `asyncio.timeout(reply_timeout_sec)`，共享方法本身不变：
   - 直接回复：`ReplyService._reply_batch_inner` 中包住 `self.generate(...)`；
   - 插话评估：`AutonomousService.evaluate_proactive_speaking` 中包住 `self.participation_decision(rows, state)`；
   - 插话生成：`AutonomousService._speak` 中包住 `self.prepare_reply(...)`（`_speak` 只由插话与 GM 发言经 `_proactive_locked` 调用），不包 `send_reply`，避免在飞书发送途中取消。
   
   不改 `LLMClient` 的 SDK 客户端级超时，避免影响已有更长超时（整理/生成 90 秒、生图 120 秒）的调用。
3. 超时后的行为沿用现有异常路径，不新增分支：
   - 直接回复：进入 `reply generation failed` → 发送 `fallback_replies["empty_llm"]`；`generate` 现有的 `BaseException` 分支会取消召回与风格任务；
   - 插话评估：`interjection evaluation failed` → 本次不说话；
   - 插话生成：异常上抛，由后台任务回调记录；GM 手动触发走 `app.py` 的 502 处理。

**测试**：fake LLM 用 `asyncio.Event` 永不 set 模拟挂起，配置极小超时：

- 直接回复：分别让 `embed_text` 与 `chat_json` 挂起，断言超时后发出兜底文案、`chat_lock` 已释放，且同会话后续消息可正常回复；
- 插话：`participation_decision` 的 `chat_json` 挂起时评估返回 None；`prepare_reply` 内 `embed_text` 挂起时异常上抛且锁已释放；
- 定时回归：`reply_timeout_sec` 设得很小，fake 的生成/评估耗时大于它、小于对应 `scheduled_*_timeout_sec`，断言 `ScheduledService` 的正文准备与发布评估仍成功；
- 配置校验用例。

## P2 召回向量扫描

**问题**：`MemoryService.recall_relevant_cards` 对同一会话调用两次 `score_cards`（人物限定 top 4 + 通用 top k），各自最多扫描 `recall_scan_max = 2000` 行，用纯 Python 逐元素算余弦。本机实测单次扫描 2000 条：768 维约 0.14 秒、3072 维约 0.82 秒；生产 2 vCPU 预计更慢，且在线程中持有 GIL 拖慢事件循环。`cosine` 每行重复计算查询向量模长。

**改动**：

1. `MemoryRepository` 改为一次查询、一次打分：SQL 使用通用过滤（无主体或主体命中），打分后在 Python 中分出两组：
   - 人物限定：`subject_ids` 与请求主体有交集的前 4 条；
   - 通用：全部候选的前 k 条。
   返回结构与现有 `scoped + general` 拼接一致，`build_recall_block` 不变。
2. 查询向量模长只算一次；逐行只算点积和行向量模长。不引入 numpy。
3. 直接替换旧的两次调用方式，不保留双实现；同步更新 `tests/test_repositories.py`、`tests/test_identity_retrieval.py` 中对 `score_cards` 的调用。

**行为差异（需接受）**：原人物限定查询在“仅命中主体”的集合中取最近 2000 条；合并后从“无主体或命中主体”的最近 2000 条中选。只有当某会话有效向量超过 2000 条时，较旧的人物卡片才可能少召回。P0 数据可判断是否触及。

**测试**：

- 现有召回与身份检索测试全绿。
- 等价性（有效向量数 ≤ `recall_scan_max`）：替换前先按当前实现新增固定数据用例，断言 `recall_relevant_cards` 返回的 id 与顺序（含有/无主体、无请求主体、相似度 ≤ 0 被丢弃、同分时按 `memory_id` 降序）；替换后同一用例不改仍须通过。不保留旧实现做对照。
- 已接受的差异：把 `recall_scan_max` 设为很小的值，构造一张旧的命中主体卡与更多较新的无主体卡，断言合并后该人物卡不再出现在人物限定结果中，固定这一行为边界。
- 本地用 2000 × 3072 合成数据对比前后耗时，结果写入提交说明。

## P3 依赖锁定

**问题**：`requirements.txt` 全部为 `>=`。按 AGENTS.md 流程在发布目录重新安装时，上游大版本变化可能直接在生产启动或运行时失败，回退旧目录也无法保证依赖一致。（P0 显示生产实际共用一个 venv，并未按发布重新安装；锁定后若改为按发布安装，可得到与现用 venv 相同的直接依赖。）

**改动**：

1. 以 P0 记录的生产现用版本，将 `requirements.txt` 直接依赖改为 `==`；`requirements-dev.txt` 继续引用它。
2. 本地新建虚拟环境按锁定版本安装，跑完整离线测试与 lint。
3. 不锁传递依赖（保持简单）；如需完整可复现，再另议 `pip freeze` 全量锁文件。

## 实施顺序与提交

1. P0（需授权）→ 记录结论。
2. P1 → 独立提交 `fix: 为回复与插话生成阶段增加超时`。
3. P2 → 独立提交 `perf: 合并召回向量扫描`（若 P0 显示规模很小，可延后）。
4. P3 → 独立提交 `chore: 锁定生产依赖版本`。
5. 每步运行受影响测试；全部完成后运行 `python -m unittest discover -s tests -v`、`ruff check .`、`git diff --check`。
6. 部署另行授权，按 AGENTS.md 流程执行；本计划无 schema 或数据变化，新增配置项需在生产 TOML 随代码发布并核对。

## 待确认

- `reply_timeout_sec` 默认值：建议 60 秒。它覆盖整段生成（身份路由最多 8 秒、两次 embedding、启用 Gemini 联网搜索时可能偏慢的 `chat_json`）；若生产日志显示正常回复耗时更长，需上调。目前模型日志没有耗时字段，无法从现有数据直接确认。插话评估共用该值，作为上限偏宽松。
- P0 是否授权执行。

## 剩余边界

- 超时只取消本地等待；上游可能已完成生成并计费。已进入 `asyncio.to_thread` 的 SQLite 读或打分不会被中断，只是不再阻塞锁。
- 合并扫描后仍是纯 Python 线性扫描，记忆规模持续增长时需重新评估（如降低 `recall_scan_max` 或引入向量库）；本计划不做。
- 只锁直接依赖，传递依赖仍可能变化。
