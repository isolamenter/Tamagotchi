from __future__ import annotations

import json
from dataclasses import dataclass

MEME_TAGS = frozenset({"eat_rice", "slack_off", "fat_defense", "overload", "smug", "shocked"})
REPLY_MODES = frozenset({"reaction", "normal", "substantive"})


@dataclass(frozen=True)
class GeneratedReply:
    text: str
    mode: str
    speaker_name: str = ""
    meme_tag: str = ""
    image_prompt: str = ""
    example_ids: frozenset[str] = frozenset()
    meme_evidence: str = ""

    @classmethod
    def parse(cls, content: str) -> GeneratedReply:
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("Expected a reply object")
        text, mode = data.get("reply"), data.get("reply_mode")
        if not isinstance(text, str) or not text.strip() or mode not in REPLY_MODES:
            raise ValueError("Invalid reply contract")
        tag = data.get("meme_tag", "")
        evidence = data.get("meme_evidence", "")
        return cls(
            text.strip(),
            mode,
            str(data.get("speaker_name") or "").strip(),
            tag if isinstance(tag, str) and tag in MEME_TAGS else "",
            str(data.get("image_prompt") or "").strip(),
            meme_evidence=evidence.strip() if isinstance(evidence, str) else "",
        )
