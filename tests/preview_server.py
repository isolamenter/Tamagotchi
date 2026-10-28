"""Isolated local browser fixture. Never connects to Feishu or a model endpoint."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from dafeiyu.app import create_app
from tests.helpers import container


def create_preview():
    temporary = tempfile.TemporaryDirectory(prefix="dafeiyu-ui-")
    c = container(str(Path(temporary.name) / "preview.db"))
    now = time.time()
    with c.db.connect() as conn:
        for cid, title in [
            (1, "摸鱼研究所（演示）"),
            (2, "代码茶水间（演示）"),
            (3, "周末聊天（演示）"),
        ]:
            conn.execute(
                "INSERT INTO conversations(id,chat_id,title,created_at,last_reply_ts,last_proactive_topic) VALUES(?,?,?,?,?,?)",
                (cid, f"chat-demo-{cid}", title, now - 86400 * 7, now - 120, "今天的摸鱼计划"),
            )
            conn.execute(
                "INSERT INTO sys_cache(key,val) VALUES(?,?)",
                (
                    f"daily_diary:{cid}",
                    json.dumps(
                        {
                            "date": c.state_domain.local_date_hour(now)[0],
                            "ready": cid == 1,
                            "text": "今天听到测试终于跑通了，我也跟着松了口气。",
                            "evidence": [],
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            count = 90 if cid == 1 else 5
            for index in range(count):
                bot = index % 3 == 1
                texts = [
                    "今天的测试终于跑通了。",
                    "漂亮，可以喘口气了。",
                    "先把结果记下来，再去吃饭。",
                    "这个问题先别急，看看是哪一步没接上。",
                    "行，回来再接着聊。",
                    "我去倒杯水，马上回来。",
                ]
                text = (
                    texts[index % len(texts)]
                    if cid == 1
                    else f"第 {cid} 个演示会话的虚构消息 {index + 1}"
                )
                conn.execute(
                    "INSERT INTO messages(conversation_id,role,content,ts,sender_name,is_observer,source) VALUES(?,?,?,?,?,?,?)",
                    (
                        cid,
                        "assistant" if bot else "user",
                        text,
                        now - (count - index) * 60 - (cid - 1) * 7200,
                        "小夏" if index % 2 else "阿白",
                        int(not bot and index % 5 == 0),
                        "direct" if bot or index % 5 else "observer",
                    ),
                )
            for i, kind in enumerate(["reality", "hearsay", "imagination"]):
                conn.execute(
                    "INSERT INTO memories(conversation_id,kind,when_text,who,what,vibe,hooks,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        cid,
                        kind,
                        "今天",
                        "群里",
                        [
                            "大家一起定位了测试中的超时问题，最后发现是请求排队。",
                            "小夏提到周末可能会去看展，还没有确定安排。",
                            "梦见一座白饭山，尾巴差点变成饭勺。这是梦里的事。",
                        ][i],
                        ["松了口气", "有点期待", "离谱但香"][i],
                        "演示 虚构",
                        now - i * 3600,
                    ),
                )
    with patch("dafeiyu.app.build_container", return_value=c):
        app = create_app(c.config)
    app.state.preview_temporary = temporary
    return app
