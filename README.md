# LLM Tamagotchi

> 一只住在飞书群里的电子宠物。被 @ 时会用 LLM 生成性格化的回复。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

目标是逐步加入持续人格、状态（饥饿/心情/精力）、主动行为、Web 端可视化，做一个 LLM 驱动的电子宠物原型。当前已有：**带持久记忆的飞书群宠物**——每个群一只独立宠物，能记得此前聊过的事；老消息会异步压缩成"经历摘要"，避免上下文无限增长。

## ✨ 特性

- 模块化 FastAPI 服务，正式 ASGI 入口为 `tamagotchi:app`
- 基于飞书事件订阅（`im.message.receive_v1`）：群里只有真实 @ 才触发回复，p2p 私聊直接回复；非 @ 群消息会被宠物"旁听"进上下文（缓冲后按 tick 批量落库）
- **每个 `chat_id` 一只独立宠物，对话历史用 SQLite 持久化（stdlib，零额外依赖）**
- **长期记忆 = 事件卡片 + RAG**：消息累积后压成结构化卡片（when/who/what/vibe/hooks）+ 向量索引；回复时按相关性 + 时序双路召回，塞回 system message 当"想起的事"
- **状态系统：satiety / mood / energy / curiosity / affection 五维 + 每日 vibe 词；中段不渲染，只在极端档给一句模糊感受。普通对话只读 state，state 只由卡片按钮的确定性规则修改**
- **状态玩法：五维状态会触发 `active_need` 需求事件，卡片给出 2-3 个选择；卡片绑定不可变 ID，需求/自由卡全群只结算一次，定时卡每人一次、最多三次；LLM 只生成反馈台词**
- **主动发言：进程内常驻 asyncio 心跳，宠物会按固定时刻写日记 / 说梦境；休息时暂停自动需求和普通主动卡、冻结已有需求计时，醒来后恢复**
- **交互卡片：主动发言、需求事件、梦境 / 日记都以飞书消息卡片呈现，底部带玩法状态、五维状态进度条和互动按钮。梦境卡还会额外调图像模型生成一张插图嵌进卡里**
- **Web 可视化面板：`/web` 单页面板，展示宠物列表、五维状态、当前需求、记忆卡片与消息时间线，每 15 秒自动刷新**
- LLM 走 OpenAI 兼容 API（适配 OpenAI / NewApi / 各类代理网关）
- AES 加密回调可选支持
- 飞书要求 3s 内响应，长任务自动走 `BackgroundTasks` 异步

## 🏗 架构

当前代码按职责拆成几层：

```text
tamagotchi.py          # FastAPI app / lifespan / dependency wiring
config.py              # env + TOML loading
runtime.py             # process-local locks and buffers
routes/                # HTTP routes: feishu, gm, health
services/              # business orchestration
domain/                # pure state/card/gameplay/memory/pet logic
repositories/          # SQLite persistence
integrations/          # Feishu and OpenAI-compatible clients
tests/                 # local unittest coverage; not needed on the server
```

```
飞书群消息（@bot 或旁听消息）
       │
       ▼
事件订阅 POST ──▶ HTTPS 入口 ──▶ FastAPI /feishu/webhook
                                        │  立即 200
                                        ▼
                                 BackgroundTask
                                        │
                                        ├─ direct（群里被 @ / p2p 私聊）：
                                        │   ├─ 距上次回复 < 节流间隔 → 只记消息、不回复
                                        │   ├─ 读未压缩 history + 当前 state
                                        │   ├─ RAG: embed(user_text) → top-K 卡片 + top-N 最近
                                        │   ├─ OpenAI 兼容 API → JSON {reply, speaker_name}
                                        │   ├─ 发送成功后 append assistant + 更新 last_reply_ts（不改五维 state）
                                        │   └─ POST /im/v1/messages/{id}/reply
                                        ├─ observer：缓冲进内存，autonomous tick 批量落库
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

uvicorn tamagotchi:app --host 0.0.0.0 --port ${PORT:-8000}
```

健康检查：

```bash
curl http://localhost:8000/healthz
# {"ok": true}
```

### 线上部署到 gcp-vps

生产服务运行在 `gcp-vps:~/tamagotchi`，systemd 服务名 `tamagotchi`。默认上线只同步文件、安装依赖、重启服务，**不动 `state.db`**：

```bash
scp -r tamagotchi.py config.py runtime.py domain integrations repositories routes services \
  prompts.toml pet_style.toml pet_config.toml requirements.txt .env gcp-vps:~/tamagotchi/
ssh gcp-vps '~/tamagotchi/.venv/bin/pip install -r ~/tamagotchi/requirements.txt'
ssh gcp-vps 'sudo systemctl restart tamagotchi'
```

如果 IAP 隧道下普通 `scp` 卡住或远端出现 0 字节文件，改用：

```bash
scp -O -r tamagotchi.py config.py runtime.py domain integrations repositories routes services \
  prompts.toml pet_style.toml pet_config.toml requirements.txt .env gcp-vps:~/tamagotchi/
```

验证：

```bash
ssh gcp-vps 'curl -fsS http://127.0.0.1:8000/healthz'
ssh gcp-vps 'systemctl status tamagotchi --no-pager'
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
   - `im:resource:upload`（或 `im:resource`，**仅当配置了 `IMAGE_MODEL` 想要梦境插图时**才需要——卡片嵌图前要先 `POST /im/v1/images` 换 `image_key`。没开通时上传报 `code 99991672`，梦境卡安静退回纯文字。**改完权限要重新发布应用版本才生效**）
   - 群友姓名**不再走通讯录权限**——宠物从对话里学名字（有人报名字时回复 JSON 带回 `speaker_name`，写进 `user_names` 表），还没学到的群友降级显示 `群友-后4位`。无需 `contact:*` 权限。
4. **事件与回调 → 事件配置**：
   - 订阅方式：**事件回调**（HTTP）
   - 请求地址：`https://<your-domain>/feishu/webhook`
   - 加密策略：**不加密**（如需加密，把 `Encrypt Key` 填到 `.env` 的 `FEISHU_ENCRYPT_KEY`）
   - **Verification Token** → 复制到 `.env` 的 `FEISHU_VERIFICATION_TOKEN`
   - 添加事件：`im.message.receive_v1`（接收消息 v2.0）
   - **卡片回传交互**：开启此能力，主动发言卡片上的互动按钮被点击时才会以 `card.action.trigger` 事件推到同一个 `/feishu/webhook`（不需要单独配卡片回调地址）
5. **版本管理与发布** → 创建版本 → 提交审核 → 发布
6. 机器人加入**内部群**（见下），群里 @ 它测试

> 接入"接收群消息"权限后，宠物会同时收到群里**所有**消息：
> - 被 @ 的消息走完整 LLM 回复（direct 模式）。**回复节流**：群里距上次正式回复不足 `[reply].min_interval_sec` 时，新 @ 消息照常记进记忆但不触发 LLM 回复，下次正式回复时宠物能看到这期间被说了什么。
> - 其余群聊以 `observer` 形式进上下文。observer 消息**不逐条落库**，先缓冲在进程内，每个 autonomous tick（或下一条 direct 消息到来前、进程关闭时）批量写入 SQLite，降低写库和压缩频率。
> - 一直没人 @ 它的群不会因为观察消息就被自动孵化出宠物，必须先 @ 一次创建。

#### ⚠️ 内部群 vs 外部群

自建应用的机器人**默认只能加入内部群**（同一租户内）。外部群（跨企业）里搜不到也加不了。要在外部群使用，需要把应用做成商店应用，或申请租户管理员开通跨组织白名单。**测试时建议新开一个内部群最快**。

## ⚙️ 配置参考（`.env`）

| 变量 | 说明 | 必填 |
|---|---|---|
| `FEISHU_APP_ID` | 飞书自建应用 App ID | ✓ |
| `FEISHU_APP_SECRET` | 飞书自建应用 App Secret | ✓ |
| `FEISHU_VERIFICATION_TOKEN` | 事件订阅 Verification Token。**必填**（缺失启动失败）——空值会让公网 webhook 无鉴权 | ✓ |
| `FEISHU_ENCRYPT_KEY` | 启用加密策略时填，否则留空 |  |
| `CHAT_MODEL` | chat 模型名（如 `gpt-4o-mini`、`claude-3-5-sonnet`、`gemini-...`） | ✓ |
| `EMBED_MODEL` | RAG 用的 embedding 模型，默认 `text-embedding-3-small`。**换成不同维度的模型会使旧向量失配、历史召回失效，需重嵌** |  |
| `IMAGE_MODEL` | 梦境插图的图像模型；openai 后端走 `/v1/images/generations`（如 `imagen-4.0-fast-generate-001` / `gpt-image-1` / `dall-e-3`），gemini 后端走原生 SDK（支持 Gemini 系图像模型）。留空 = 关闭插图、梦境退回纯文字卡 |  |
| `LLM_PROVIDER` | LLM 后端，`openai`（OpenAI 兼容，缺省）或 `gemini`（google-genai 原生 SDK）；两个后端复用同一组 `CHAT_MODEL` / `EMBED_MODEL` / `IMAGE_MODEL` |  |
| `OPENAI_BASE_URL` | OpenAI 兼容 API 端点（末尾通常带 `/v1`）；`LLM_PROVIDER=openai` 时使用 | ✓ |
| `OPENAI_API_KEY` | OpenAI 兼容 API Key；`LLM_PROVIDER=openai` 时使用 | ✓ |
| `GEMINI_BASE_URL` | Gemini 端点，留空 = Google 官方端点；仅 `LLM_PROVIDER=gemini` 时使用 |  |
| `GEMINI_API_KEY` | Gemini API Key；`LLM_PROVIDER=gemini` 时**必填** |  |
| `STATE_DB` | SQLite 持久化文件路径，默认 `state.db`（相对启动目录） |  |
| `PORT` | 服务监听端口，默认 8000 |  |
| `GM_TOKEN` | `/gm/*` Web 调试接口 token；留空则复用 `FEISHU_VERIFICATION_TOKEN` |  |

## 🐾 自定义风格与玩法

配置拆成三层，改完重启服务（`systemctl restart tamagotchi`）后生效：

- `pet_style.toml`：电子宠物的风格、人设底色、口吻、默认互动方式。
- `prompts.toml`：玩法流程，包括记忆压缩、状态渲染、主动发言、日记 / 梦境、GM 默认触发文案、兜底回复和 JSON 输出契约。
- `pet_config.toml`：运行参数，包括记忆压缩阈值、`[reply]` 群 @ 回复节流间隔、`[observer]` 旁听缓冲上限、初始状态、状态衰减、`[gameplay]` 需求事件参数、主动发言间隔 / 静默时段 / 触发阈值、`[card]` 交互卡片开关 / 进度条格数 / 结算上限。

常改的是 `pet_style.toml [style].prompt`：默认是通用电子宠物，可换成毒舌猫、哲学家小狗、傲娇龙、机器人团子等。玩法类规则继续放在 `prompts.toml`，不要写进业务代码。

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
| `[proactive_triggers]` | satiety / mood / energy / spontaneous 的触发文案 |
| `[scheduled_event]` | 定时日记 / 梦境的触发说明 |
| `[[scheduled_events]]` | 定时事件定义，如梦境、日记 |
| `[fallback_reply]` | 非文本、空消息、LLM 空回复、LLM 报错的兜底文案 |
| `[card]` | 主动发言交互卡片的展示文案：进度条字符 / label、按钮文字、点击反馈语、按钮点击后的 LLM 反馈 prompt |
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

| 维度 | 含义 | 没人互动时漂向 |
|---|---|---|
| `satiety` | 饱腹度，100=吃饱，0=极饿 | 清醒漂向 ~5（变饿）、睡眠漂向 ~20 |
| `mood` | 心情，100=超开心，0=极沮丧 | 清醒漂向 ~25、睡眠回暖到 ~70 |
| `energy` | 精力，100=活蹦乱跳，0=快睡着了 | 清醒漂向 ~15、睡眠恢复到 ~95 |
| `curiosity` | 探索欲，100=想追问 / 扯新话题，0=心不在焉 | 清醒漂向 ~20、睡眠回升到 ~70 |
| `affection` | 对群的感情，100=深度依恋，0=陌生 / 距离感 | 清醒缓慢漂向 ~15、睡眠回暖到 ~50 |
| `recent_vibe` | 每日凌晨从 `pet_style.toml [recent_vibes].pool` 抽一个氛围词 | 跨日翻牌 |

- **存在 `pets.state_json` 一个字段里**，浮点 + `last_update_ts` + 各种 last_* 时间戳
- **lazy compute**：没后台 cron，读时按"距上次更新过了多久"算到当前；每次互动后写回新值
- **衰减 = 向 baseline 指数收敛**：每维不再线性单调走到 0/100，而是 `v = baseline + (v-baseline)·exp(-rate·h)` 漂向各自 baseline（多在中段）。好处：长期没人理的宠物状态回落到中段（→ 不渲染 → LLM 自由发挥），不会五维全贴极端档把回复钉死。清醒 / 睡觉两段各有一组 baseline（如 energy 睡觉段 baseline=100 = 回血）
- **LLM 走 JSON 结构化输出**：普通回复只返回 `{"reply": "...", "speaker_name": "..."}`。state 作为只读上下文影响回复语气、长度和话题倾向，不由 LLM 返回或修改五维数值。`speaker_name` 可选，当前说话人报了名字才填，用来从对话里学群友名字（写进 `user_names` 表）
- LLM 输出不是合法 JSON 时，把整段当 reply 兜底，state 不变（不会让宠物挂掉）
- **状态渲染只在极端档触发**：每维都看 `pet_config.toml [state.bands.<dim>]` 的 extreme_high / high / low / extreme_low；落在中段就完全不渲染该维度，避免把回复钉死成同质化语气
- 全维度都中段、且 vibe 为空时，整个状态感受块都不注入——LLM 自由发挥

初始值在 `pet_config.toml [state.initial]`，衰减的 baseline / rate 在 `[state.decay_active]` / `[state.decay_quiet]`，分档阈值在 `[state.bands.<dim>]`，每档对应的感受句在 `prompts.toml [state_render.lines]`。

## 🎮 状态需求事件

玩法运行状态保存在 `pets.state_json`，卡片交互契约另存 `card_instances` / `card_claims`。当前运行字段：

```json
{
  "active_need": {},
  "need_cooldowns": {},
  "last_social_ts": 0,
  "last_free_card_ts": 0
}
```

- `domain/gameplay.py` 是纯规则层：需求判定、选择规则和卡片动作的五维状态结算都在这里。
- `services/gameplay_service.py` 是编排层：tick 创建需求、GM/卡片结算时复用同一套规则。
- `active_need` 同时只保留一个需求事件，候选类型为 `hungry / sleepy / sad / bored / lonely`，优先级按生理急迫和严重度排序。
- 每个需求事件提供 2-3 个选择，例如饿了可以 `feed / snack_hunt / promise_food`，无聊可以 `play / tell_news / send_explore`。
- 点击选择后，核心五维数值由规则结算；主维达到“触发阈值 + 10”才算解决，弱选择会续出新一轮需求卡；LLM 只负责生成一句人格化反馈，不决定数值变化。

相关配置在 `pet_config.toml [gameplay]`：

```toml
[gameplay]
enabled = true
need_ttl_sec = 1800
need_cooldown_sec = 7200

[gameplay.need_thresholds]
hungry = 20
sleepy = 35
sad = 40
bored = 35
lonely = 30
```

## 🌙 主动发言

宠物**不是只被动响应 @**——服务进程里有一个常驻 asyncio 心跳，宠物自己决定什么时候开口。

- **心跳间隔**：当前值 10 min 扫一次所有宠物
- **固定时刻**：默认本地 `10:00` 发一次"梦境"、`19:00` 发一次"日记"，独立于 state 和普通主动发言冷却；成功发出后用 `state_json` 里的日期字段去重，同一天不重复。错过时只在目标时点后一小时补发，周末休息日不会自动发送
- **需求事件优先**：梦境 / 日记补发完成后，tick 会先用 `[gameplay.need_thresholds]` 检查状态阈值；命中则生成需求卡片并写入 `active_need`，本轮不再发普通主动闲聊。
- **代码层廉价过滤**（不调 LLM）：没有需求事件时，先看本地时间（默认 +8 时区）是否在静默时段（19:00-次日10:00）或周末休息日、再看冷却（当前 2h 内不重发），都过了才进普通 state 触发
- **触发条件**（按优先级，生理急迫优先于软需求）：
  - `satiety <= 10` → 抱怨饿了
  - `mood <= 25` → 撒娇 / 抱怨没人理
  - `energy <= 20` → 宣告要睡了
  - `curiosity <= 20` → 无聊了想找人玩
  - `affection <= 15` → 孤单了想求关注
  - 都没触发时 20% 概率自发冒一句"hi"
- **LLM 层生成**：过滤通过才走完整 prompt + RAG 召回卡片 + state + JSON 输出，得到 reply 文本后通过飞书 `/im/v1/messages?receive_id_type=chat_id` 推到群里
- **失败安全**：发飞书失败不写 DB；tick 中任一宠物异常不影响其它宠物；loop 自身崩了也不会退出（log 后继续）

主动发言的时间、冷却、静默时段、普通状态阈值、自发概率在 `pet_config.toml [autonomous]` / `[autonomous.trigger_thresholds]`；需求事件阈值在 `[gameplay.need_thresholds]`；触发文案和 `[[scheduled_events]]` 定时事件定义在 `prompts.toml`。

## 🎴 交互卡片

**主动发言**（宠物自己冒泡）、**梦境**、**日记**都会以飞书消息卡片呈现；@ / p2p 回复本身是纯文本，但发现未通知的需求时会附一张首次 CTA。

卡片结构：宠物的话（或梦/日记文本）+ 当前需求（若有）+ 五维状态进度条 + 一排互动按钮。梦境卡额外在文字下方插一张图像模型生成的插图。

- **进度条**：`satiety` 直接表示「饱腹度」，和其它四维一样都是满 = 好，不做反转
- **需求卡片按钮由 `active_need` 决定**：如果状态触发了需求事件，按钮来自 `domain/gameplay.py` 的 choice 规则，同一个需求会给 2-3 个带权衡的选择。
- **卡片按钮统一结算**：有 `active_need` 时显示该需求的 choice；没有 `active_need` 时显示自由互动按钮；两者都经过 `GameplayDomain` 的统一卡片结算入口。梦境 / 日记卡按钮也是固定的卡片动作（☀️ 早上好 / 🌙 晚安）
- **梦境插图**：梦境这条 `[[scheduled_events]]` 配了 `gen_image = true`，LLM 输出短梦话同时返回 `image_prompt`；服务用它调 `IMAGE_MODEL`（`/v1/images/generations`）拿 base64 → 上传飞书拿 `image_key` → 嵌入卡片。`IMAGE_MODEL` 留空或生成 / 上传任一步失败都安静回退成纯文字梦境卡；超时在 `pet_config.toml [card].image_timeout_sec`。日记不生成图
- **点击 = 确定性结算**：所有卡片按钮都走统一的卡片结算入口；Need choice 和自由互动按钮都使用确定性的五维 delta。每个需求都有一条可靠的直接解法，另两条会把代价明显转移到其它维度；LLM 只生成反馈台词，不决定核心数值。
- **协作一次**：需求/自由卡全群首次点击才结算数值，后续点击只留下社交反馈；梦境 / 日记卡每位群友一次、全群最多三次贡献。
- **卡片时效**：卡片发出 `[card].button_ttl_sec`（默认 30min）后按钮失效，过期点击只弹 toast 不改状态；需求过期会升级并在下一活跃窗口重发，而非静默进入冷却。
- **有人格的反馈逐条累积**：每次点击后台单独用 LLM 生成一句符合宠物风格的反馈台词，**向下追加**进卡片文本日志而不是整段刷新——多人点击时各自的台词逐条堆叠在卡片里（最多保留 `[card].card_log_max_lines` 条）
- **防重放**：每张卡都有不可变 `card_id` 和持久化 claim；旧 payload 安全失效，重复点击不会再改状态。反馈 PATCH 按消息 ID 串行，避免多人点击覆盖。

展示文案在 `prompts.toml [card]`，自由互动按钮的可见条件和确定性 delta 在 `pet_config.toml [card]`，需求 choice 和统一结算在 `domain/gameplay.py`。`[card].enabled = false` 时主动发言、需求事件、梦境、日记都退回纯文本 / 不发需求卡。需要在飞书开发者后台开启「卡片回传交互」能力（见上文飞书应用配置）。

## 🧪 GM Web 调试接口

GM 只走 HTTP，不会通过飞书群消息触发。所有接口都需要 `?token=...` 或请求头 `X-GM-Token`；token 优先用 `GM_TOKEN`，没配置时复用 `FEISHU_VERIFICATION_TOKEN`。

常用命令：

```bash
# 查看命令
curl 'https://<your-domain>/gm/help?token=TOKEN'

# 列出当前宠物，拿 chat_id / pet_id
curl 'https://<your-domain>/gm/pets?token=TOKEN'

# 设置状态
curl -X POST 'https://<your-domain>/gm/state?token=TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"chat_id":"oc_xxx","set":{"satiety":10,"mood":20,"energy":15}}'

# 对状态做增量
curl -X POST 'https://<your-domain>/gm/state?token=TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"chat_id":"oc_xxx","delta":{"satiety":-10,"mood":-5}}'

# 查看玩法状态 / 手动生成需求 / 直接结算需求
curl 'https://<your-domain>/gm/gameplay?token=TOKEN&chat_id=oc_xxx'
curl -X POST 'https://<your-domain>/gm/need?token=TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"chat_id":"oc_xxx","kind":"hungry"}'
curl -X POST 'https://<your-domain>/gm/resolve_need?token=TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"chat_id":"oc_xxx","action":"feed","actor":"GM"}'

# 手动主动说话 / 梦境 / 日记
curl -X POST 'https://<your-domain>/gm/speak?token=TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"chat_id":"oc_xxx","trigger":"GM 手动测试：现在主动冒泡"}'
curl -X POST 'https://<your-domain>/gm/dream?token=TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"chat_id":"oc_xxx"}'
curl -X POST 'https://<your-domain>/gm/diary?token=TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"chat_id":"oc_xxx"}'
```

`/gm/dream` 和 `/gm/diary` 默认不写每日去重字段，适合反复测试；传 `{"mark": true}` 才会标记当天已发。

## 📊 Web 可视化面板

浏览器打开 `https://<your-domain>/web?token=TOKEN`,得到一个单页面板:宠物列表下拉、五维状态进度条（`satiety` 即「饱腹度」,满 = 好）、今日 vibe、当前需求、记忆卡片列表、最近消息时间线,每 15 秒自动刷新;鉴权同 GM（`GM_TOKEN`,留空复用 `FEISHU_VERIFICATION_TOKEN`）。面板还能直接做 GM 操作:改五维数值并保存、重抽 vibe、手动触发主动发言 / 梦境 / 日记 / tick——全部走现有 GM 接口,无额外后端。

## 📄 License

[MIT](LICENSE) © isolameto
