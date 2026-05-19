# LLM Tamagotchi

> 一只住在飞书群里的电子宠物。被 @ 时会用 LLM 生成性格化的回复。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

目标是逐步加入持续人格、状态（饥饿/心情/精力）、主动行为、Web 端可视化，做一个 LLM 驱动的电子宠物原型。当前已有：**带持久记忆的飞书群宠物**——每个群一只独立宠物，能记得此前聊过的事；老消息会异步压缩成"经历摘要"，避免上下文无限增长。

## ✨ 特性

- 单文件 FastAPI 服务（~400 行），易读易改
- 基于飞书事件订阅（`im.message.receive_v1`），群里 @ 即触发
- **每个 `chat_id` 一只独立宠物，对话历史用 SQLite 持久化（stdlib，零额外依赖）**
- **滚动摘要：消息累积到阈值后异步压缩成"过去的经历"塞回 system prompt**
- LLM 走 OpenAI 兼容 API（适配 OpenAI / NewApi / 各类代理网关）
- AES 加密回调可选支持
- 飞书要求 3s 内响应，长任务自动走 `BackgroundTasks` 异步

## 🏗 架构

```
飞书群 @bot
       │
       ▼
事件订阅 POST ──▶ HTTPS 入口 ──▶ FastAPI /feishu/webhook
                                        │  立即 200
                                        ▼
                                 BackgroundTask
                                        │
                                        ├─ SQLite: get_or_create pet by chat_id
                                        ├─ 读 summary + 未压缩历史
                                        ├─ OpenAI 兼容 API 调用 LLM
                                        ├─ append user / assistant 消息到 SQLite
                                        ├─ POST /im/v1/messages/{id}/reply
                                        └─ 未压缩条数 > 阈值 → 异步压缩任务
                                                                │
                                                                ▼
                                                       LLM 把老消息 + 旧 summary
                                                       压成新 summary，写回 pets
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
3. **权限管理** → 至少开通：
   - `im:message`（接收消息）
   - `im:message:send_as_bot`（以机器人身份发消息）
4. **事件与回调 → 事件配置**：
   - 订阅方式：**事件回调**（HTTP）
   - 请求地址：`https://<your-domain>/feishu/webhook`
   - 加密策略：**不加密**（如需加密，把 `Encrypt Key` 填到 `.env` 的 `FEISHU_ENCRYPT_KEY`）
   - **Verification Token** → 复制到 `.env` 的 `FEISHU_VERIFICATION_TOKEN`
   - 添加事件：`im.message.receive_v1`（接收消息 v2.0）
5. **版本管理与发布** → 创建版本 → 提交审核 → 发布
6. 机器人加入**内部群**（见下），群里 @ 它测试

> 群聊里飞书默认只把"被 @ 的消息"投递给机器人，代码不再做二次过滤。

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
| `STATE_DB` | SQLite 持久化文件路径，默认 `state.db`（相对启动目录） |  |
| `PORT` | 服务监听端口，默认 8000 |  |

## 🐾 自定义人格

宠物的人格在 `main.py` 顶部的 `SYSTEM_PROMPT` 常量里。默认是"刚孵化的撒娇电子宠物"。可以改成任何风格：毒舌猫、哲学家小狗、傲娇龙……

```python
SYSTEM_PROMPT = """你是一只 ..."""
```

旁边还有一个 `COMPRESS_PROMPT`，控制老消息被压缩成"经历摘要"时的风格（默认用第一人称、保留用户偏好和情绪起伏）。要让宠物的长期记忆变更精准 / 更像生物，主要靠调这段。

## 🧠 记忆是怎么工作的

- 每个飞书 `chat_id` 一只独立宠物，存在 `pets` 表
- 每条消息（用户的 + 宠物回复的）都写进 `messages` 表，按宠物分组
- 每次回复时，发给 LLM 的是：`SYSTEM_PROMPT + pets.summary + 所有 id > summary_until_id 的消息 + 新 user 消息`
- 当未压缩消息数超过阈值（默认 30），后台异步把最老的一批 + 旧 summary 喂给 LLM 压成新 summary，留最近 10 条不压
- 失败可恢复：压缩 LLM 调用挂了就 log 完算了，下一轮还会再触发

两个常量在 `main.py` 顶部：`BUFFER_KEEP`（最近保留多少条不压）、`COMPRESS_THRESHOLD`（触发阈值）。

## 🗺 Roadmap

- [x] 对话历史持久化（按 chat_id 维度，sqlite + 滚动摘要）
- [ ] 宠物状态（饥饿 / 心情 / 精力）+ LLM 结构化输出（JSON+对话双层）—— schema 里 `state_json` 已留
- [ ] 主动行为：定时投递"梦境/日记"到群（可以读 `pets.summary` 当素材）
- [ ] Web 端 sprite 渲染（情绪驱动表情）
- [ ] 宠物死亡 / `/reborn` 重置、`/dump-memory` 调试命令

## 📄 License

[MIT](LICENSE) © isolameto
