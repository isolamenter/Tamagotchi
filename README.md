# 大肥鱼 2.0

陪大家聊天的鲸娘小女仆大肥鱼，贪吃爱摸鱼，亲近、心软，带一点害羞的小脾气。

飞书聊天机器人：支持私聊与群聊 @ 回复、群聊旁听与主动插话、分会话记忆、梦境/日记，以及 Web 管理工作台。当前版本是对话应用，不再包含养成数值或游戏动作。

## 技术栈与环境

- Python 3.11+（使用标准库 `tomllib`）、FastAPI / Uvicorn、SQLite。
- 飞书 HTTP 事件回调；模型支持 OpenAI-compatible API 或 Gemini 原生 SDK。
- 管理界面是原生 HTML/CSS/JavaScript，无 Node 构建步骤。
- 真实运行需要飞书应用凭据、聊天与 embedding 模型；生成梦境配图时还需要图像模型。离线测试不需要真实凭据。

## 安装与启动

在仓库根目录执行（以下为 macOS/Linux shell）：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
```

按 [`.env.example`](.env.example) 填写 `.env`：飞书的 App ID、Secret、Verification Token，选择 `LLM_PROVIDER` 并填写对应 API Key，以及 `CHAT_MODEL`、`EMBED_MODEL`。`IMAGE_MODEL` 可空；独立的 `GM_TOKEN` 为空时管理 API 禁用。飞书启用加密回调时还须填写 `FEISHU_ENCRYPT_KEY`。生产仅需安装 `requirements.txt`。

`STATE_DB` 指向新文件或已完成迁移的 schema-3 数据库，父目录须存在。不要直接指向 schema 1/2 旧库；运行时不会自动升级。

```sh
python -m uvicorn dafeiyu.app:create_app --factory --env-file .env --host 127.0.0.1 --port 8000
```

- 健康检查：`http://127.0.0.1:8000/healthz`（仅进程存活，不代表模型/飞书可用）。
- 管理工作台：`http://127.0.0.1:8000/web`，使用 `GM_TOKEN`；读写 API 均校验 `X-GM-Token`。
- 飞书订阅 `im.message.receive_v1`，HTTP 回调路径为 `/feishu/webhook`。真实接入还需可达的 HTTPS 地址和飞书侧消息/资源权限；仓库未提供可导入的权限清单，须按实际应用核对。群聊称呼需 `im:chat.members:read`（或已有的 `im:chat:readonly`/`im:chat`），处理中表情需 `im:message.reactions:write_only`（或已有的 `im:message`）；缺少时分别退回“名字未知”和无表情，不影响回复。工作台读取梦境配图使用 `GET /im/v1/images/:image_key`，只能读本应用上传的图片，无需额外权限。

配置由 [config.py](dafeiyu/config.py) 读取；Uvicorn 的 `--env-file` 负责加载 `.env`。人设、提示词、运行参数及语料分别在 [`config/`](config/) 的 TOML 文件中，密钥不要写入这些文件。启动会初始化风格索引并开启后台循环，使用真实配置可能产生模型调用和飞书发言。运行单 worker；多进程边界见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 开发与验证

```sh
python -m unittest discover -s tests -v
ruff check .
git diff --check
```

测试使用临时数据库与 fake 集成。需要预览管理 UI 时，在一个终端启动无外部调用的合成数据服务：

```sh
python -m uvicorn tests.preview_server:create_preview --factory --host 127.0.0.1 --port 8765
```

打开 `http://127.0.0.1:8765/web`，演示口令为 `admin-fixture`。可在另一个终端执行浏览器冒烟：

```sh
python -m playwright install chromium
python -m tests.browser_smoke
```

截图仍由现有测试写入本地 `.docs/preview/`（不入 Git）；这不是项目文档入口。也可通过 `DAFEIYU_BROWSER_EXECUTABLE` 指定已有 Chromium 可执行文件。浏览器测试不等于真实飞书或模型验收。

## 构建与数据工具

无编译步骤；源码发布工具打包运行文件、角色来源/素材记录和 SHA-256 manifest，不包含密钥、数据库、测试或本套开发文档：

```sh
python -m dafeiyu.tools.release --output var/dafeiyu-release.tar.gz
```

输出文件必须不存在。打包不会部署或切换服务。旧库迁移与 embedding 重建是独立维护操作，先查看工具帮助与[数据决策](docs/decisions/0001-schema2-offline-migration.md)：

```sh
python -m dafeiyu.tools.migrate --help
python -m dafeiyu.tools.reembed --help
```

重建向量会调用真实模型并修改目标库；不要把它当作普通测试。

## 每日整理、日记与个人信息

已建立的会话会在本地 19:00 后整理新增人类聊天（包括旁听），同一次模型调用同时产出记忆与日记草稿，不需要群友说“记住我”或成功收到回复。平台 sender/mention ID 是人物键，别名不是；身份与个人信息按会话隔离。偏好、兴趣、沟通习惯和近期事件使用带原文证据、报告人、置信度和版本历史的记忆卡，不从日记或梦境生成事实。

默认每会话每天最多 4 次整理调用，每次最多 300 条、64,000 字符上下文、6,000 输出 tokens、90 秒超时；积压逐批处理，长消息分段续读。无新增不调用。对话中需要理解不带 @ 的称呼/代词时，最多增加一次 300-token、8 秒的人物路由调用；不向路由模型提供全群个人事实。两者明确复用现有 `CHAT_MODEL`（不另选 provider/付费模型），可分别用 `consolidation.enabled` 和 `memory.identity_resolve_enabled` 关闭；没有其他人物候选时不做路由；有其他候选时仍做语义判断，因为 @ 对象可能只是收话人而不是问题主体。64,000 是输入字符预算，不是已验证的供应商 token 窗口保证。具体参数在 `settings.toml`；这些是运行时额外模型成本，不包含在离线测试中。

整理本身不会向群聊发送消息。日记只从本轮当天人类原文挑一件事表达角色感受，素材不足可留空；分批全部完成后才可发布，发送时不再调用写作模型。草稿按会话/日期持久保存，失败与记忆、处理进度一起回滚。成功批次在下一轮心跳继续；失败或中断才按重试间隔退避，每日调用预算不变。关闭每日整理也会停止生成新日记；GM 日记操作仅发送已保存的当日日记，没有日记时提示不可发送。

普通主动插话不再限制每天的钟点，仍需新消息、冷却和语义参与批准。日记/梦境自动发布仍使用原配置窗口，但必须有人刚在说话（默认 180 秒内）、通过语义参与判断与冷却；没人说话不会仅因到点而发言。事件消息与周期检查都先考虑待发日记/梦境。两者各自保存评估水位与当日任务，普通插话的否决不会消耗它们的机会。梦境正文和配图在聊天锁外准备，正文持久保存供重启或失败后复用；关闭卡片时不生图。发送前重新检查日期、窗口、活跃、冷却和待回复消息。尚未完成或为空的日记不占用当天已发送标记，也不阻塞普通主动参与。

明确的飞书发送拒绝可以在窗口内重新评估后重试；超时、响应不完整或发送后本地记录失败均按发送状态不明处理，停止该任务的自动及 GM 重发，须先核对群聊与任务记录。任务元数据可从已鉴权的 `/gm/state` 的 `scheduled` 字段查看；当前没有解除不明状态的管理操作。详见[记忆规格](docs/specs/memory.md)与[聊天规格](docs/specs/conversation.md)。

schema 1/2 升级使用离线迁移写新文件，旧库和旧代码必须保留作回退；不能让旧代码读取 schema 3，也不能把新库直接降版本。见[迁移决策](docs/decisions/0002-grounded-identity-schema3.md)。

## 项目结构与文档

- `dafeiyu/`：应用组装、路由、领域规则、服务、存储、外部集成、管理 UI 和维护工具。
- `config/`、`assets/memes/`：可配置行为、清洗后的风格/梗图语料与素材。
- `tests/`：离线回归、迁移验证和管理 UI 冒烟。
- [ARCHITECTURE.md](ARCHITECTURE.md)：模块职责、依赖与数据流。
- [docs/AGENTS.md](docs/AGENTS.md)：跨 Agent 的规范入口。
- [聊天行为](docs/specs/conversation.md)、[记忆行为](docs/specs/memory.md)：需要长期维护的功能约束。
- [角色视觉规范](docs/specs/character.md)：鲸鳍耳、固定外观、角色参考图和 meme/梦境的验收规则。梦境模型需要支持参考图输入（Gemini 图文生成或 OpenAI-compatible `images.edit`）。
- [架构决策](docs/decisions/)：仅记录已确认的决策；[任务计划](docs/plans/) 仅供未来复杂、多阶段任务使用。

旧 README 引用的本地 `.docs/` 指南和部署记录未随仓库提供；本套文档不声称恢复了其中内容，也不删除本地文件。
