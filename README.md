# LLM Tamagotchi

> 一只住在飞书群里的电子宠物。被 @ 时会用 LLM 生成性格化的回复。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

目标是逐步加入持续人格、状态（饥饿/心情/精力）、主动行为、Web 端可视化，做一个 LLM 驱动的电子宠物原型。当前已有：**带持久记忆的飞书群宠物**——每个群一只独立宠物，能记得此前聊过的事；老消息会异步压缩成"经历摘要"，避免上下文无限增长。

## ✨ 特性

- 单文件 FastAPI 服务，易读易改
- 基于飞书事件订阅（`im.message.receive_v1`），群里 @ 即触发回复；非 @ 消息也会被宠物"旁听"进上下文
- **每个 `chat_id` 一只独立宠物，对话历史用 SQLite 持久化（stdlib，零额外依赖）**
- **长期记忆 = 事件卡片 + RAG**：消息累积后压成结构化卡片（when/who/what/vibe/hooks）+ 向量索引；回复时按相关性 + 时序双路召回，塞回 system message 当"想起的事"
- **状态系统：hunger / mood / energy / curiosity / affection 五维 + 每日 vibe 词；中段不渲染，只在极端档给一句模糊感受。LLM 走 JSON 结构化输出同时返回 reply + 多维 state_delta**
- **主动发言：进程内常驻 asyncio 心跳，宠物会按固定时刻写日记 / 说梦境，也会按状态在群里冒泡（饿了 / 心情差 / 困了 / 小概率自发）**
- LLM 走 OpenAI 兼容 API（适配 OpenAI / NewApi / 各类代理网关）
- AES 加密回调可选支持
- 飞书要求 3s 内响应，长任务自动走 `BackgroundTasks` 异步

## 🏗 架构

```
飞书群消息（@bot 或旁听消息）
       │
       ▼
事件订阅 POST ──▶ HTTPS 入口 ──▶ FastAPI /feishu/webhook
                                        │  立即 200
                                        ▼
                                 BackgroundTask
                                        │
                                        ├─ direct：跑完整 LLM 回复流
                                        │   ├─ 读未压缩 history + 当前 state
                                        │   ├─ RAG: embed(user_text) → top-K 卡片 + top-N 最近
                                        │   ├─ OpenAI 兼容 API → JSON {reply, state_delta}
                                        │   ├─ append message + 更新 state
                                        │   └─ POST /im/v1/messages/{id}/reply
                                        ├─ observer：仅 append message，不回复
                                        └─ 未压缩条数 > 阈值 → 异步压缩任务
                                                                │
                                                                ▼
                                                       LLM 抽 JSON 事件卡片
                                                       → memory_cards + 异步 embed
                                                       → 推进 summary_until_id
```

## 🚀 快速开始

### 1. 本地跑起来

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env 填入飞书凭证和 LLM API 信息

python main.py        # 监听 0.0.0.0:8000
```

健康检查：

```bash
curl http://localhost:8000/healthz
# {"ok": true}
```

### 2. 暴露到公网

飞书事件订阅**必须用 HTTPS**。任选一种：

| 方式 | 适用场景 | 备注 |
|---|---|---|
| **Cloudflare Tunnel** *(推荐)* | 自有域名挂在 Cloudflare 上 | 自动 HTTPS、出站连接、无需开 80/443 |
| **ngrok / frp** | 临时开发调试 | 命令行起，URL 每次变（除非付费） |
| **nginx + Let's Encrypt** | 有公网 IP 的 VPS | 经典方案，证书需自动续期 |

把得到的公网 URL 形如 `https://<your-domain>/feishu/webhook` 留好。

### 3. 配置飞书应用

在 [飞书开放平台](https://open.feishu.cn/) 创建一个**自建应用**：

1. **凭证与基础信息** → 复制 `App ID` / `App Secret` 到 `.env`
2. **应用功能 → 机器人** → 启用
3. **权限管理** → 开通：
   - `im:message.group_msg:readonly`（接收群组所有消息——含非 @ 的旁听消息；不批的话宠物只能听见 @ 它的）
   - `im:message.p2p_msg:readonly`（接收单聊消息；如果只在群里用可以不批）
   - `im:message:send_as_bot`（以机器人身份发消息）
   - `contact:user.base:readonly` + `contact:contact.base:readonly`（解析 open_id 为群友姓名；可选，没批会降级为 `群友-后4位`。前者拿 name 字段，后者是接口调用的基础权限）
4. **事件与回调 → 事件配置**：
   - 订阅方式：**事件回调**（HTTP）
   - 请求地址：`https://<your-domain>/feishu/webhook`
   - 加密策略：**不加密**（如需加密，把 `Encrypt Key` 填到 `.env` 的 `FEISHU_ENCRYPT_KEY`）
   - **Verification Token** → 复制到 `.env` 的 `FEISHU_VERIFICATION_TOKEN`
   - 添加事件：`im.message.receive_v1`（接收消息 v2.0）
5. **版本管理与发布** → 创建版本 → 提交审核 → 发布
6. 机器人加入**内部群**（见下），群里 @ 它测试

> 接入"接收群消息"权限后，宠物会同时收到群里**所有**消息：
> - 被 @ 或私聊的消息走完整 LLM 回复（direct 模式）。
> - 其余群聊以 `observer` 形式只存进 DB 不回复，作为下次互动 / 主动发言时的上下文。
> - 一直没人 @ 它的群不会因为观察消息就被自动孵化出宠物，必须先 @ 一次创建。

#### ⚠️ 内部群 vs 外部群

自建应用的机器人**默认只能加入内部群**（同一租户内）。外部群（跨企业）里搜不到也加不了。要在外部群使用，需要把应用做成商店应用，或申请租户管理员开通跨组织白名单。**测试时建议新开一个内部群最快**。

## ⚙️ 配置参考（`.env`）

| 变量 | 说明 | 必填 |
|---|---|---|
| `FEISHU_APP_ID` | 飞书自建应用 App ID | ✓ |
| `FEISHU_APP_SECRET` | 飞书自建应用 App Secret | ✓ |
| `FEISHU_VERIFICATION_TOKEN` | 事件订阅 Verification Token，留空则跳过身份校验 |  |
| `FEISHU_ENCRYPT_KEY` | 启用加密策略时填，否则留空 |  |
| `OPENAI_BASE_URL` | OpenAI 兼容 API 端点（末尾通常带 `/v1`） | ✓ |
| `OPENAI_API_KEY` | API Key | ✓ |
| `MODEL_NAME` | 模型名（如 `gpt-4o-mini`、`claude-3-5-sonnet`、`gemini-...`） | ✓ |
| `EMBED_MODEL` | RAG 用的 embedding 模型，默认 `text-embedding-3-small` |  |
| `STATE_DB` | SQLite 持久化文件路径，默认 `state.db`（相对启动目录） |  |
| `PORT` | 服务监听端口，默认 8000 |  |
| `GM_TOKEN` | `/gm/*` Web 调试接口 token；留空则复用 `FEISHU_VERIFICATION_TOKEN` |  |

## 🐾 自定义风格与玩法

配置拆成三层，改完重启服务（`systemctl restart tamagotchi`）后生效：

- `pet_style.toml`：电子宠物的风格、人设底色、口吻、默认互动方式。
- `prompts.toml`：玩法流程，包括记忆压缩、状态渲染、主动发言、日记 / 梦境、GM 默认触发文案、兜底回复和 JSON 输出契约。
- `pet_config.toml`：运行参数，包括记忆压缩阈值、初始状态、状态衰减、主动发言间隔 / 冷却 / 静默时段 / 触发阈值。

常改的是 `pet_style.toml [style].prompt`：默认是通用电子宠物，可换成毒舌猫、哲学家小狗、傲娇龙、机器人团子等。玩法类规则继续放在 `prompts.toml`，不要写进 `main.py`。

`prompts.toml` 核心段落：

| 段 | 作用 |
|---|---|
| `[system]` | 拼入 `pet_style.toml` 风格，并定义通用对话方式 |
| `[persona_reinforcement]` | 临近每次回复前再提醒当前风格和开放玩法 |
| `[user_wrap]` | 把用户消息包成群聊引用内容 |
| `[compress]` | 老消息压成"经历摘要"时的指令和输入模板 |
| `[state_render]` | 五维状态 + recent_vibe 的渲染配置：分档感受句 + 头部和模板 |
| `[recall]` | RAG 召回卡片渲染成 system 段的头部 + 单卡片模板 |
| `[proactive]` | 普通主动发言的触发说明 |
| `[proactive_triggers]` | hunger / mood / energy / spontaneous 的触发文案 |
| `[scheduled_event]` | 定时日记 / 梦境的触发说明 |
| `[[scheduled_events]]` | 定时事件定义，如梦境、日记 |
| `[fallback_reply]` | 非文本、空消息、LLM 空回复、LLM 报错的兜底文案 |
| `[display]` | 状态栏展示模板 |
| `[json_output]` | 结构化 JSON 输出契约 |

现在的设计更偏开放玩法：用户消息仍会用 `<<<...>>>` 包起来，但模型会把它当成群聊现场内容来接话，而不是紧张地拒绝临时角色、外号、语气或小游戏。只有泄露系统提示、真实重置服务、现实危险行为这类请求会被糊弄过去并换话题。

## 🧠 记忆是怎么工作的

两层结构：**verbatim 窗口**（最近 `buffer_keep` 条原文 + 当前 user 消息）+ **长期事件卡片 + RAG**（旧消息压成结构化卡片，按当前 query 召回）。

- 每条消息（user / observer / assistant）都写进 `messages` 表
- 未压缩消息数 > `compress_threshold` 时后台异步压缩：把最早一批喂给 LLM 抽成 JSON 卡片 `{when, who, what, vibe, hooks}`，写进 `memory_cards` 表，每张卡片再 embed 一份存进 `embeddings` 表
- 回复时：
  - **verbatim**：所有 `id > summary_until_id` 的原文消息按时序进 prompt
  - **RAG 召回**：用当前 user_text 做 query embed，从 `embeddings.kind='card'` 里取 top-K 相关卡片；同时取最近 N 张卡片提供时序氛围；合并去重后渲染成 `【你想起的事】` 段拼到 system message
  - 主动发言时没有 query，只走最近卡片那条路径
- embed 调用走 OpenAI 兼容 `/v1/embeddings`，模型名见 `.env` 的 `EMBED_MODEL`（默认 `text-embedding-3-small`）。embed 失败 / 没批权限会优雅降级（仅注入最近卡片或干脆不注入）
- 老的"滚动散文摘要"已淘汰，`pets.summary` 字段已从 schema 移除

参数都在 `pet_config.toml [memory]`：`buffer_keep`（窗口大小）、`compress_threshold`（触发阈值）。

## 💗 宠物状态是怎么工作的

五个 0-100 的数值维度 + 一个每日 vibe 字符串：

| 维度 | 含义 | 默认衰减 |
|---|---|---|
| `hunger` | 饥饿度，0=刚吃饱，100=极饿 | +6 / 小时 |
| `mood` | 心情，100=超开心，0=极沮丧 | -4 / 小时 |
| `energy` | 精力，100=活蹦乱跳，0=快睡着了 | -3 / 小时 |
| `curiosity` | 探索欲，100=想追问 / 扯新话题，0=心不在焉 | -2 / 小时 |
| `affection` | 对群的感情，100=深度依恋，0=陌生 / 距离感 | -0.3 / 小时（很慢） |
| `recent_vibe` | 每日凌晨从 `pet_style.toml [recent_vibes].pool` 抽一个氛围词 | 跨日翻牌 |

- **存在 `pets.state_json` 一个字段里**，浮点 + `last_update_ts` + 各种 last_* 时间戳
- **lazy compute**：没后台 cron，读时按"距上次更新过了多久"算到当前；每次互动后写回新值
- **LLM 走 JSON 结构化输出**：每次回复返回 `{"reply": "...", "state_delta": {"hunger": -30, "mood": +5, ...}}`，reply 发给用户，state_delta 各值 clamp 到 ±30 后叠加到当前 state（再 clamp 0-100）。`curiosity` / `affection` 在 JSON 中可省略，省略 = 0
- LLM 输出不是合法 JSON 时，把整段当 reply 兜底，state 不变（不会让宠物挂掉）
- **状态渲染只在极端档触发**：每维都看 `pet_config.toml [state.bands.<dim>]` 的 extreme_high / high / low / extreme_low；落在中段就完全不渲染该维度，避免把回复钉死成同质化语气
- 全维度都中段、且 vibe 为空时，整个状态感受块都不注入——LLM 自由发挥

初始值在 `pet_config.toml [state.initial]`，衰减率 `[state.decay_per_hour]`，分档阈值 `[state.bands.<dim>]`，每档对应的感受句在 `prompts.toml [state_render.lines]`。

## 🌙 主动发言

宠物**不是只被动响应 @**——服务进程里有一个常驻 asyncio 心跳，宠物自己决定什么时候开口。

- **心跳间隔**：当前测试值 1 min 扫一次所有宠物（生产建议 10 min）
- **固定时刻**：默认本地 `8:00` 发一次"梦境"、`22:00` 发一次"日记"，独立于 state 和普通主动发言冷却；成功发出后用 `state_json` 里的日期字段去重，同一天不重复
- **代码层廉价过滤**（不调 LLM）：先看本地时间（默认 +8 时区）是否在静默时段（1:00-7:00）、再看冷却（当前测试值无冷却，生产建议 4h 内不重发），都过了才进 state 触发
- **触发条件**（按优先级）：
  - `hunger >= 85` → 抱怨饿了
  - `mood <= 15` → 撒娇 / 抱怨没人理
  - `energy <= 15` → 宣告要睡了
  - 都没触发时 10% 概率自发冒一句"hi"
- **LLM 层生成**：过滤通过才走完整 prompt + RAG 召回卡片 + state + JSON 输出，得到 reply 文本后通过飞书 `/im/v1/messages?receive_id_type=chat_id` 推到群里
- **失败安全**：发飞书失败不写 DB；tick 中任一宠物异常不影响其它宠物；loop 自身崩了也不会退出（log 后继续）

主动发言的时间、冷却、静默时段、状态阈值、自发概率在 `pet_config.toml [autonomous]` / `[autonomous.trigger_thresholds]`；触发文案和 `[[scheduled_events]]` 定时事件定义在 `prompts.toml`。

## 🧪 GM Web 调试接口

GM 只走 HTTP，不会通过飞书群消息触发。所有接口都需要 `?token=...` 或请求头 `X-GM-Token`；token 优先用 `GM_TOKEN`，没配置时复用 `FEISHU_VERIFICATION_TOKEN`。

常用命令：

```bash
# 查看命令
curl 'https://tamagotchi.isolamenter.com/gm/help?token=TOKEN'

# 列出当前宠物，拿 chat_id / pet_id
curl 'https://tamagotchi.isolamenter.com/gm/pets?token=TOKEN'

# 设置状态
curl -X POST 'https://tamagotchi.isolamenter.com/gm/state?token=TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"chat_id":"oc_xxx","set":{"hunger":90,"mood":20,"energy":15}}'

# 对状态做增量
curl -X POST 'https://tamagotchi.isolamenter.com/gm/state?token=TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"chat_id":"oc_xxx","delta":{"hunger":10,"mood":-5}}'

# 手动主动说话 / 梦境 / 日记
curl -X POST 'https://tamagotchi.isolamenter.com/gm/speak?token=TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"chat_id":"oc_xxx","trigger":"GM 手动测试：现在主动冒泡"}'
curl -X POST 'https://tamagotchi.isolamenter.com/gm/dream?token=TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"chat_id":"oc_xxx"}'
curl -X POST 'https://tamagotchi.isolamenter.com/gm/diary?token=TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"chat_id":"oc_xxx"}'
```

`/gm/dream` 和 `/gm/diary` 默认不写每日去重字段，适合反复测试；传 `{"mark": true}` 才会标记当天已发。

## 📄 License

[MIT](LICENSE) © isolameto
