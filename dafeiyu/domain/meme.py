"""Conservative matches for the actual captions: 开饭！ / 摸了 / 是算力！."""

from __future__ import annotations

import re

from dafeiyu.domain.scene import scene_for

_ADDRESS = r"(?:(?:大肥鱼|鲸鱼娘)[，,：: ]*)?"
_END = r"[！!。.?？~～]*"
_AGREE = r"(?:好(?:呀|耶)?[，,！! ]*)?"
_RULES = {
    "eat_rice": (
        re.compile(
            _ADDRESS + r"(?:来(?:一起)?(?:吃饭|干饭)|一起(?:吃饭|干饭)|请你吃饭|"
            r"(?:给你|投喂你)(?:一碗|两碗|三碗|一大碗)?(?:白饭|米饭))(?:吧|啦|咯)?" + _END
        ),
        re.compile(
            _AGREE
            + r"(?:来(?:啦|了|咯)|开饭(?:啦|了)?|干饭(?:去|啦)?|碗(?:已经)?端好了|吃饭去)"
            + _END
        ),
    ),
    "slack_off": (
        re.compile(
            _ADDRESS + r"(?:来(?:一起)?摸鱼|一起摸鱼|你(?:先)?(?:摸鱼|休息)(?:一下|一会儿)?|"
            r"一起休息(?:一下|一会儿)?)(?:吧|啦)?" + _END
        ),
        re.compile(
            _AGREE + r"(?:摸了|一起摸鱼|那我(?:先)?摸(?:一会儿)?鱼|休息一下|趴一会儿)" + _END
        ),
    ),
    "fat_defense": (
        re.compile(
            _ADDRESS + r"(?:(?:你|大肥鱼|鲸鱼娘)(?:又|也|太|真|有点)?(?:胖|肥)(?:了|啦)?|"
            r"你的尾巴(?:太|真|好)肥(?:了)?)" + _END
        ),
        re.compile(r"(?:(?:这|那)?是算力|不是胖[，,]是算力|这是算力储备)" + _END),
    ),
}
AUTO_MEME_TAGS = frozenset(_RULES)


def meme_rejection_reason(tag: str, evidence: str, user_text: str, reply_text: str) -> str:
    """Only whole, unambiguous utterances qualify; a keyword or quoted span cannot."""
    if tag not in _RULES:
        return "disabled_tag"
    if not evidence or evidence not in user_text:
        return "missing_evidence"
    user_rule, reply_rule = _RULES[tag]
    if (
        scene_for(user_text) != "casual"
        or not user_rule.fullmatch(user_text.strip())
        or not user_rule.fullmatch(evidence.strip())
        or not reply_rule.fullmatch(reply_text.strip())
    ):
        return "scene_mismatch"
    return ""
