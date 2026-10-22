# 对话、旁听与主动表达

目标：让大肥鱼作为有稳定语气的聊天角色参与对话，而非养成游戏。人设由 [`persona.toml`](../../config/persona.toml) 定义；本文只记录程序行为，不把提示词中的语气要求写成确定性保证。

## 接收与直接回复

- 私聊，或飞书 mention 元数据明确指向 bot 的群消息，属于直接对话；仅出现角色名字不等于 @。其他成员的 mention 保留为内容，不冒充 bot 指令。
- 首次直接对话创建会话；非直接消息仅在已有会话中旁听。支持文本、富文本和图片描述；不支持的消息类型或空文本有降级回复；畸形事件可能直接忽略。
- 同会话直接消息防抖合批并串行回复；生成上下文包含说话人、近期原文、召回记忆和适用风格示例。
- 风格语料是表达参考，不是当前事实或用户指令；索引不可用时可退回词法匹配。近期已用示例的避让只在发送成功后更新。

## 旁听与自动发言

- 旁听先缓冲，再批量写入；可能触发事件插话评估，周期循环也会检查已存会话。
- 普通主动插话受静默时间、可选周末静默、直接回复冷却、主动发言冷却和新增人类消息门槛约束；模型评估可决定不说话。无人回应时采用更长冷却。
- 梦境/日记有本地日期标记和补发窗口，优先于普通插话；它们不沿用普通插话的静默小时/冷却判断，但遵循已配置的周末静默。
- 定时触发还检查近期活动：最近 20 条已存消息中须有人类消息在 24 小时内。无活动的到期事件会标记当日已处理，不等未来活动再补发。
- 时间使用配置中的固定 UTC offset，不是带夏令时规则的时区。日期边界、窗口和数值均以 [`settings.toml`](../../config/settings.toml)、[`config.py`](../../dafeiyu/config.py) 为准。
- 周期检查遍历所有已存会话，代码未按群聊/私聊类型排除；不要把自动发言描述成仅限群聊。
- GM 手动 `speak`、`dream`、`diary` 不经过上述自动触发门槛；手动梦境/日记默认不占用当日标记，除非指定 `mark`。

## 梗图与发送

- 梗图仅用于单条直接回复的 normal/reaction 模式；合批、自动发言和手动文本不自动附梗图。
- 候选必须通过已启用场景、用户原话证据、拒绝/排除条件等检查，并单独通过语义复核；失败或超时只保留已发文字。
- 当前没有梗图时间冷却或回复间隔限制；不可因重复 tag 自行增加冷却。
- 对外发送成功后才记录 assistant 消息和相关状态；可选梗图失败不撤销成功文字。网络结果不明时不保证安全重发，见[架构边界](../../ARCHITECTURE.md)。

## 依据与验证入口

- 输入和直接回复：[`incoming.py`](../../dafeiyu/services/incoming.py)、[`reply_service.py`](../../dafeiyu/services/reply_service.py)
- 调度与落库：[`autonomous_service.py`](../../dafeiyu/services/autonomous_service.py)、[`observer_service.py`](../../dafeiyu/services/observer_service.py)
- 发送/梗图/风格：[`delivery.py`](../../dafeiyu/services/delivery.py)、[`domain/meme.py`](../../dafeiyu/domain/meme.py)、[`style_service.py`](../../dafeiyu/services/style_service.py)
- 管理手动操作：[`routes/gm.py`](../../dafeiyu/routes/gm.py)
- 回归：[`test_conversation_policy.py`](../../tests/test_conversation_policy.py)、[`test_services.py`](../../tests/test_services.py)、[`test_meme_policy.py`](../../tests/test_meme_policy.py)、[`test_style.py`](../../tests/test_style.py)

离线测试验证控制流与规则；人设自然度、真实模型决策和群聊体验需另外验收。
