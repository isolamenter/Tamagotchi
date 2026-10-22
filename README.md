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

`STATE_DB` 指向新文件或已完成迁移的 schema-2 数据库，父目录须存在。不要直接指向旧版数据库。

```sh
python -m uvicorn dafeiyu.app:create_app --factory --env-file .env --host 127.0.0.1 --port 8000
```

- 健康检查：`http://127.0.0.1:8000/healthz`（仅进程存活，不代表模型/飞书可用）。
- 管理工作台：`http://127.0.0.1:8000/web`，使用 `GM_TOKEN`；读写 API 均校验 `X-GM-Token`。
- 飞书订阅 `im.message.receive_v1`，HTTP 回调路径为 `/feishu/webhook`。真实接入还需可达的 HTTPS 地址和飞书侧消息/资源权限；仓库未提供可导入的权限清单，须按实际应用核对。

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

无编译步骤；源码发布工具打包运行文件和 SHA-256 manifest，不包含密钥、数据库、测试或本套文档：

```sh
python -m dafeiyu.tools.release --output var/dafeiyu-release.tar.gz
```

输出文件必须不存在。打包不会部署或切换服务。旧库迁移与 embedding 重建是独立维护操作，先查看工具帮助与[数据决策](docs/decisions/0001-schema2-offline-migration.md)：

```sh
python -m dafeiyu.tools.migrate --help
python -m dafeiyu.tools.reembed --help
```

重建向量会调用真实模型并修改目标库；不要把它当作普通测试。

## 项目结构与文档

- `dafeiyu/`：应用组装、路由、领域规则、服务、存储、外部集成、管理 UI 和维护工具。
- `config/`、`assets/memes/`：可配置行为、清洗后的风格/梗图语料与素材。
- `tests/`：离线回归、迁移验证和管理 UI 冒烟。
- [ARCHITECTURE.md](ARCHITECTURE.md)：模块职责、依赖与数据流。
- [AGENTS.md](AGENTS.md)：跨 Agent 的唯一规范入口。
- [聊天行为](docs/specs/conversation.md)、[记忆行为](docs/specs/memory.md)：需要长期维护的功能约束。
- [架构决策](docs/decisions/)：仅记录已确认的决策；[任务计划](docs/plans/) 仅供未来复杂、多阶段任务使用。

旧 README 引用的本地 `.docs/` 指南和部署记录未随仓库提供；本套文档不声称恢复了其中内容，也不删除本地文件。
