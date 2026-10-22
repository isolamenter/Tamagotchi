# 架构

大肥鱼是一个单进程异步 Python 服务。HTTP、后台调度和管理 UI 共用应用容器；SQLite 存储会话与记忆，外部副作用集中在飞书和模型集成中。

## 模块与依赖

| 模块 | 职责与边界 |
| --- | --- |
| [`app.py`](dafeiyu/app.py)、[`config.py`](dafeiyu/config.py) | 组合根：读取环境/TOML、创建数据库与服务、注册路由、管理生命周期。导入模块本身不开库，创建应用时才初始化。 |
| [`routes/`](dafeiyu/routes/) | 飞书回调、健康检查、管理鉴权/API、静态工作台入口；调用容器，不承担模型业务编排。 |
| [`services/`](dafeiyu/services/) | `incoming` 归一化输入；`reply` 直接回复；`observer` 批量旁听；`autonomous` 调度；`memory` 压缩/召回；`style` 选例；`delivery` 对外发送与发送后记录。 |
| [`domain/`](dafeiyu/domain/) | 上下文包装、回复解析、状态规则、记忆格式/向量运算、风格与梗图选择规则；不直接访问飞书或数据库。 |
| [`repositories/`](dafeiyu/repositories/) | 会话、消息、记忆/向量、风格索引、系统缓存的 SQL 与事务；表结构见 `schema.sql`。 |
| [`integrations/`](dafeiyu/integrations/) | `FeishuClient` 封装飞书 API；`LLMClient` 封装聊天、embedding、识图和生图的 provider 差异。 |
| [`runtime.py`](dafeiyu/runtime.py) | 进程内会话锁、压缩锁、缓冲、防抖任务和后台任务跟踪，不是持久任务队列。 |
| [`web/`](dafeiyu/web/)、[`tools/`](dafeiyu/tools/) | 无构建前端调用 `/gm/*`；离线迁移、重建向量、源码打包工具不由请求路由自动执行。 |

主要调用方向为 routes → services → repositories / integrations；domain 提供规则，app 负责组装。不是严格的“纯六边形架构”：repository 也使用 domain/config/runtime，飞书 client 使用 system repository 做缓存；以实际构造参数为准，不宣称层间完全隔离。

## 关键数据流

1. **接收与回复**：`/feishu/webhook` 验 token、可选解密、登记事件去重 → 后台交给 `ReplyService` → `IncomingService` 识别私聊/@、归一化文本与图片描述 → 分会话防抖/串行处理 → 保存用户消息 → 拼装近期上下文、记忆与风格示例 → 模型生成 → `DeliveryService` 发飞书并记录成功结果。
2. **旁听与主动发言**：未直接寻址的消息只进入已建立会话的旁听缓冲 → 批量落库 → 事件插话或周期循环评估。定时梦境/日记与普通主动插话使用不同触发门槛，详见[聊天规格](docs/specs/conversation.md)。
3. **记忆**：较旧消息经压缩形成分会话卡片 → 后台生成向量 → 召回时结合相关卡片与近期卡片。原文、处理位置、卡片和向量是不同数据层，详见[记忆规格](docs/specs/memory.md)。
4. **管理**：浏览器携带独立 `X-GM-Token` → GM 路由校验明确的 `conversation_id` → 查询 repository 或调用相同发送/主动服务。界面是管理工具，不是独立聊天后端。

## 架构边界

- **单 worker**：锁、待回复队列和旁听缓冲都在内存；每个进程还会启动自己的调度循环。不能仅靠 SQLite 宣称多进程无重复发言。
- **外部发送不是数据库事务**：飞书投递与本地记录不能原子提交；Webhook 去重也不是持久任务队列。超时或崩溃时不得假设 exactly-once，发送状态不明应先核对群聊记录。
- **持久化**：只接受 schema 2；迁移写新库，向量重建独立执行。依据与后果见[决策记录](docs/decisions/0001-schema2-offline-migration.md)。
- **配置与语义**：`.env` 提供密钥/模型/路径；TOML 提供人格、prompt、运行参数和清洗后语料。prompt 意图不等于程序已保证的行为。
- **启动与健康**：生命周期启动风格索引和自主循环，退出时刷旁听缓冲并取消任务。风格初始化失败会记录降级状态；`/healthz` 不检查外部依赖。
- **隐私与管理**：`/web` 可加载静态页面，`/gm/*` 才校验独立管理口令；应用没有用户账号/角色权限系统。真实代理、HTTPS 与网络访问策略不在仓库中定义。
