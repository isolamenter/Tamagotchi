# 会话记忆

目标：在保留近期原文的同时召回有价值的长期上下文，并区分真实事件、转述与想象。不是全局共享人物数据库。

## 行为与规则

- 消息、卡片和召回按 `conversation_id` 隔离；会话处理位置决定哪些原文进入自动上下文，不代表原文被删除。
- 超过配置阈值后异步压缩旧消息，保留最近窗口；阈值与窗口大小以 [`settings.toml`](../../config/settings.toml) 的 `memory` 段为准。
- 卡片类型为 `reality`、`hearsay`、`imagination`；梦境不是现实承诺或共同经历。提示词给出语义约束，模型是否遵守仍需实际评测。
- 卡片保存和处理位置更新位于同一数据库事务；embedding 在卡片成功存储后单独生成，失败不撤销卡片。
- 召回将同会话的相关卡片和近期卡片去重合并；query embedding 失败时仍可使用近期卡片。
- 向量按 provider、model 与维度匹配；切换 embedding 模型后不可假设旧向量兼容。
- `gemini-embedding-2` 的批量文本逐条包装为独立 Content，查询与文档分别使用检索任务前缀；从 `gemini-embedding-001` 切换时必须重建记忆向量，风格向量按新模型重新生成。

## 边界与保证

- 单条原文超过压缩输入预算时分段请求，所有分段校验成功后才原子保存卡片并推进该消息水位；中途失败保留原水位，原文不截断或删除。
- 压缩支持显式经校验的空卡片列表 `{"cards": []}` 推进处理位置，保留原文同时避免水聊触发错误退避。
- 压缩卡片在落库前必须通过整批校验（包含合法 `what` 与 `kind`）；若存在任何非法卡片则整批失败并保留历史供重试，绝不静默部分推进。
- 真正失败累计 5 次后，系统退避并采用逐条原文的确定性卡片兜底，绝不丢失历史。
- 原文保留不代表任何一条都能自动召回；没有向量的卡片只可能通过近期卡片路径进入上下文。

## 维护与依据

旧库先离线迁移，再按需要重建向量；重建调用真实模型并修改目标库，不是普通测试。见[当前数据决策](../decisions/0002-grounded-identity-schema3.md)。

- 编排：[`memory_service.py`](../../dafeiyu/services/memory_service.py)
- 事务/筛选：[`memory_repo.py`](../../dafeiyu/repositories/memory_repo.py)
- 格式/相似度：[`domain/memory.py`](../../dafeiyu/domain/memory.py)
- 上下文与触发：[`conversation_repo.py`](../../dafeiyu/repositories/conversation_repo.py)、[`observer_service.py`](../../dafeiyu/services/observer_service.py)、[`reply_service.py`](../../dafeiyu/services/reply_service.py)
- 回归入口：[`test_repositories.py`](../../tests/test_repositories.py)、[`test_services.py`](../../tests/test_services.py)、[`test_memory_compression.py`](../../tests/test_memory_compression.py)、[`test_reembed.py`](../../tests/test_reembed.py)

## 每日身份与个人信息整理（schema 3）

- **入口独立**：新增人类原文由每日整理读取，不依赖直接回复成功、单条消息、字面自我介绍、压缩阈值或日记/梦境实际发布。旧消息压缩从不删除原文；两个检查点互不吞掉对方输入。
- **真实 ID**：sender_open_id、mention open_id、平台 message_id、人类事件时间一起保存；历史 sender_name 不被全局 user_names 改写。旧全局名字缓存只留审计，不参与新身份判断。不同 ID 不能按同名合并。
- **证据**：别名与每条个人卡片必须引用本会话固定输入快照中的原文和 ID。别名必须在引用原文或该主体的平台 mention 名称中出现，但不要求任何“我叫”句式；多轮互称/回应可共同支持。主体与报告人分开。引用/扮演/否定的语义识别仍依赖模型，离线规则不能证明模型永不误读。
- **事实边界**：assistant 日记/梦境永不进入每日身份证据；纯图片排除，富文本只用单独保留的人类文字。迁移旧原文缺乏图像派生边界，仅可作为 `legacy_unverified` 待确认线索，不能单独确认身份；旧卡片不按名字盲绑主体。
- **单一事实源**：个人信息是 `memories` 中带 `profile_key` 的卡片，类别限偏好、兴趣、近期进行事件、沟通偏好、关系。禁止无根据的心理/健康等敏感推断。模型输出置信度；转述/旧来源强制降为未确认。旧卡片和个人事实不会另复制到易冲突的“整个人物简介”。
- **变更保留历史**：有本人确认的新证据才能通过给定旧 fact ID、同主体/同键的 `supersedes` 替换；第三方/未确认说法不能退休本人已确认事实。未明确解决的冲突或不在本轮有界上下文中的旧键，保存为待确认并存说法，不覆盖旧事实、不阻塞整批；完全相同事实去重。每人最多 64 个活跃个人条目，超限保留为未确认普通证据卡。近期事件默认从证据发生时间起 30 天过期，不从回填当天重新计时。同一近期事件得到更新的人类证据再次确认时，保留事实 ID、补充证据并从新证据时间延长有效期；较旧证据不缩短或延长当前期限。
- **选择性检索**：当前发送者与明确 mention 优先；有其他候选时，用一次有界语义路由读取同会话别名/近期上下文以消歧普通称呼和代词，不传全群个人事实。未知/同名难分则提示澄清，失败安全退回明确 ID。人物过滤在向量 top-k 前执行，补少量近期卡；完整卡片和归属附注总预算 16,000 字符，绝不从另一会话检索个人事实。
- **批量与预算**：本地当日首次实际整理固定新增消息上界；之后到达的消息留到下一日。批量条数/输入字符/输出 tokens/超时/每日调用数与重试间隔均有界且可配置。超长单条按字符偏移续读，只有读完才推进 message ID；未读内容不被跳过。失败消耗当日调用预算但不移动处理水位，重启仍保留预算与进度；合法空结果可推进。
- **原子性**：非法 JSON、未知主体、越界证据、非法替换、模型超时或数据库事务失败，都不部分保存身份/个人信息或推进水位。成功后 embedding 独立补建。单进程约束不变，不宣称外部发送 exactly-once。

实现：`domain/consolidation.py`、`repositories/identity_repo.py`、`services/consolidation_service.py`。回归：`test_daily_identity.py`、`test_identity_retrieval.py`、`test_daily_scheduling.py`、`test_identity_migration.py`。模型实际语义质量与群聊体验需要另行获授权的真实评测，fake 测试只验证合同与控制流。
