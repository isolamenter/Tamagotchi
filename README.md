# 大肥鱼 2.0

住在飞书群里的大肥鱼：平级群友，嘴硬心软，认真问题认真答。

2.0 将原 Tamagotchi 项目完整转向群聊角色。当前是本地发布候选，生产尚未切换；具体范围和验收见[重构记录](docs/2.0-refactor.md)与[发布说明](docs/release-2.0.md)。

## 能做什么

- 群 @ 与私聊正常回复，合并连续消息，同时保留说话人和完整的近期对话。
- 非 @ 消息作为旁听；有具体理由才主动插话，受静默时段、冷却和前次是否有人回应约束。
- 每天在配置窗口生成梦境与日记，内容卡片可附梦境插图。
- 长期记忆区分真实互动、群友转述和角色想象；向量记录模型和维度，避免模型切换后错用旧向量。
- 保留六张静态梗图资源，仅在明确投喂／邀饭、邀请摸鱼、直接身材调侃且回复文字与图片一致时开放三类自动配图；需有本轮原文证据，间隔至少 9 条纯文字直接回复及 5 分钟。有歧义不发。
- 新后台提供会话命名、消息筛选和翻页、记忆筛选、文字／图片发送、主动发言和定时内容触发。手动发送进入同一份历史。

## 本地运行

Python 3.11+。运行目录为仓库根目录。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# 在本地填写必要配置；不要提交 .env。
.venv/bin/uvicorn dafeiyu.app:create_app --factory --env-file .env --host 127.0.0.1 --port 8000
```

- 后台：`http://127.0.0.1:8000/web`，输入独立的 `GM_TOKEN`。
- 健康检查：`http://127.0.0.1:8000/healthz`。
- 飞书事件地址仍是 `/feishu/webhook`。
- 服务不会在导入模块时创建数据库；显式创建应用时，空文件初始化为结构 2，旧结构直接拒绝。
- 默认数据库是仓库下的 `dafeiyu.db`。已有数据必须先迁移到新文件，再明确设置 `STATE_DB`，不要把旧库路径直接交给 2.0。

## 配置

| 位置 | 内容 |
| --- | --- |
| `.env` | 飞书与模型凭证、模型名称、管理口令、`STATE_DB` |
| `config/persona.toml` | 大肥鱼身份、外观、关系与语言风格 |
| `config/prompts.toml` | 输出契约、记忆压缩、主动开口、梦境和日记 |
| `config/settings.toml` | 模型预算、温度、历史压缩、检索和调度参数 |
| `config/style_corpus.toml` | 原创场景口头禅和结构卡 |
| `assets/memes/*.png` | 六种静态梗图 |

模型保留 OpenAI 兼容接口和 Gemini 原生接口两种接入。`LLM_PROVIDER=gemini` 使用 `GEMINI_API_KEY`，其他接入使用 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`；`CHAT_MODEL`、`EMBED_MODEL` 必须明确填写，`IMAGE_MODEL` 可选；不默认替你选择模型。

`GM_TOKEN` 未配置时管理接口关闭，不再借用飞书校验口令。后台通过 `X-GM-Token` 请求头鉴权，不接受 URL 中的口令；当前标签页退出时会清除口令和草稿。

## 项目结构

```text
dafeiyu/
  app.py                 应用工厂、依赖装配与生命周期
  config.py              配置读取和约束验证
  runtime.py             会话锁、任务、旁听缓冲与相邻回复状态
  domain/                上下文、场景、风格、记忆和输出契约
  services/
    incoming.py          飞书消息解析与身份识别
    reply_service.py     排队、合并与对话生成
    delivery.py          统一投递、历史记录与梗图控制
    autonomous_service.py  主动开口、梦境与日记
    memory_service.py    压缩和召回
    observer_service.py  旁听缓冲
    style_service.py     风格向量缓存与检索
  repositories/          仅访问 2.0 数据结构；schema.sql 为结构定义
  integrations/          飞书和模型客户端
  routes/                事件、管理、页面与健康检查
  web/                   HTML、CSS、JavaScript 与角色图
  tools/                 一次性回填、向量重建、发布包生成
config/                  四份 TOML
assets/memes/            固定梗图
tests/                   单元、集成、迁移和浏览器测试
docs/                    设计、验收和发布记录
```

## 数据回填

源库只读，目标和报告必须是不存在的新文件。此操作不替换源库或切换服务。

```bash
python3 -m dafeiyu.tools.migrate --source /path/to/old-snapshot.db --destination /path/to/dafeiyu-v2.db --report /path/to/import-report.json
```

保留会话、消息、记忆、用户名称、事件去重记录和已发布的内容卡。旧养成状态与结算卡不进入新结构，数量／字段清单写入报告；临时凭证缓存不导入。旧向量缺少可信模型标识，迁移后须重建：

```bash
python3 -m dafeiyu.tools.reembed --database /path/to/dafeiyu-v2.db --env-file .env
```

重建时先生成全部向量，再在事务中核对记忆快照并替换；失败或期间记忆发生变化时不替换旧向量。后台“运行状态”会显示待构建的长期记忆数。生产操作顺序和切换边界见[发布说明](docs/release-2.0.md)。

## 验证

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q dafeiyu
ruff check dafeiyu tests
ruff format --check dafeiyu tests
node --check dafeiyu/web/workspace.js
git diff --check
```

浏览器验收使用 `requirements-dev.txt` 中的 Playwright。`tests.preview_server` 只创建临时库、虚构对话和模拟客户端；不会发送真实飞书消息。

```bash
python3 -m uvicorn tests.preview_server:create_preview --factory --host 127.0.0.1 --port 8765
# 另一个终端：使用 Playwright 安装的 Chromium。
python3 -m playwright install chromium
python3 tests/browser_smoke.py
# 也可设置 DAFEIYU_BROWSER_EXECUTABLE 指向已有 Chrome 可执行文件。测试使用全新临时会话。
```

[桌面预览](docs/preview/desktop.png) · [记忆页](docs/preview/memories.png) · [移动端](docs/preview/mobile.png)。预览全部为虚构测试数据，不是生产聊天。

## 生成发布包

```bash
python3 -m dafeiyu.tools.release --output dist/dafeiyu-2.0.0-local.tar.gz
```

只打包运行代码、网页、TOML、PNG 和生产依赖声明；排除数据库、凭证、测试、日志和缓存。`manifest.json` 记录逐文件 SHA-256、版本、基线提交及工作区是否有改动。本地未提交包仅供审阅，正式部署前须按仓库 `AGENTS.md` 先提交并重新生成，不能把基线提交 ID 当成当前包的提交 ID。

剩余边界：真实模型的连续聊天自然度、真实飞书投递和生产数据切换需要独立验收；通过本地测试不代表这些项目已经通过。
