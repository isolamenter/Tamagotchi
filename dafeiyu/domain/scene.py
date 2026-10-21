"""Small, conservative gates for known mismatches; no model-based intent router."""

from __future__ import annotations

import re


def scene_for(text: str) -> str:
    if any(
        term in text
        for term in (
            "不想听建议",
            "别给建议",
            "难过",
            "难受",
            "心情很差",
            "压力很大",
            "我生日",
            "祝我",
            "祝福",
        )
    ):
        return "care"
    if any(
        term in text
        for term in (
            "你说错了",
            "刚才说错",
            "你算错",
            "不是你说的",
            "那不是我",
            "你叫错",
            "这个限制我说过",
        )
    ):
        return "correction"
    if any(
        term in text
        for term in (
            "帮我分析",
            "帮我排查",
            "怎么排查",
            "怎么复现",
            "解释一下",
            "比较一下",
            "详细说",
            "怎么修",
            "帮我写",
            "为什么会报错",
        )
    ):
        return "substantive"
    if re.search(r"帮我|怎么|如何|分析|解释|排查|比较", text) and re.search(
        r"报错|超时|bug|接口|配置|代码|依赖|数据库|缓存", text, re.I
    ):
        return "substantive"
    return "casual"


def body_teasing(text: str) -> bool:
    return bool(
        re.search(
            r"(?:你|大肥鱼|鲸鱼娘)(?:(?![我他她]).){0,6}(?:胖|体重|第三碗|肥了|太肥|真肥)"
            r"|你这(?:条)?(?:蓝色)?(?:大)?肥鱼|(?:胖|肥)的是你|说你(?:胖|肥)|谁胖了",
            text,
        )
    )


def example_allowed(example: dict, text: str, *, card: bool = False) -> bool:
    scene = scene_for(text)
    if card:
        mode, name = example.get("mode"), example.get("scene")
        if mode == "reasoning" and scene != "substantive":
            return False
        if mode == "correction" and scene != "correction":
            return False
        if name in {"body_defense", "fat_name", "refuse_weight"} and not body_teasing(text):
            return False
        return not (scene in {"care", "substantive"} and mode == "teasing")
    intent = example.get("intent")
    if intent == "body_defense" and (scene != "casual" or not body_teasing(text)):
        return False
    if intent in {"debug", "reasoning"} and scene != "substantive":
        return False
    if scene == "care" and intent not in {"care", "blessing", "boundary", "farewell"}:
        return False
    return True
