# 会话记忆

目标：在保留近期原文的同时召回有价值的长期上下文，并区分真实事件、转述与想象。不是全局共享人物数据库。

## 行为与规则

- 消息、卡片和召回按 `conversation_id` 隔离；会话处理位置决定哪些原文进入自动上下文，不代表原文被删除。
- 超过配置阈值后异步压缩旧消息，保留最近窗口；阈值与窗口大小以 [`settings.toml`](../../config/settings.toml) 的 `memory` 段为准。
- 卡片类型为 `reality`、`hearsay`、`imagination`；梦境不是现实承诺或共同经历。提示词给出语义约束，模型是否遵守仍需实际评测。
- 卡片保存和处理位置更新位于同一数据库事务；embedding 在卡片成功存储后单独生成，失败不撤销卡片。
- 召回将同会话的相关卡片和近期卡片去重合并；query embedding 失败时仍可使用近期卡片。
- 向量按 provider、model 与维度匹配；切换 embedding 模型后不可假设旧向量兼容。

## 边界与保证

- 压缩支持显式经校验的空卡片列表 `{"cards": []}` 推进处理位置，保留原文同时避免水聊触发错误退避。
- 压缩卡片在落库前必须通过整批校验（包含合法 `what` 与 `kind`）；若存在任何非法卡片则整批失败并保留历史供重试，绝不静默部分推进。
- 真正失败累计 5 次后，系统退避并采用逐条原文的确定性卡片兜底，绝不丢失历史。
- 原文保留不代表任何一条都能自动召回；没有向量的卡片只可能通过近期卡片路径进入上下文。

## 维护与依据

旧库先离线迁移，再按需要重建向量；重建调用真实模型并修改目标库，不是普通测试。见[数据决策](../decisions/0001-schema2-offline-migration.md)。

- 编排：[`memory_service.py`](../../dafeiyu/services/memory_service.py)
- 事务/筛选：[`memory_repo.py`](../../dafeiyu/repositories/memory_repo.py)
- 格式/相似度：[`domain/memory.py`](../../dafeiyu/domain/memory.py)
- 上下文与触发：[`conversation_repo.py`](../../dafeiyu/repositories/conversation_repo.py)、[`observer_service.py`](../../dafeiyu/services/observer_service.py)、[`reply_service.py`](../../dafeiyu/services/reply_service.py)
- 回归入口：[`test_repositories.py`](../../tests/test_repositories.py)、[`test_services.py`](../../tests/test_services.py)、[`test_memory_compression.py`](../../tests/test_memory_compression.py)、[`test_reembed.py`](../../tests/test_reembed.py)
