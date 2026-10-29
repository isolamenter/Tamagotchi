from __future__ import annotations

import json
from collections import deque
from dataclasses import replace
from unittest.mock import patch

from dafeiyu.app import build_container
from dafeiyu.config import load_config
from dafeiyu.integrations.feishu_client import DeliveryRejected


def evidence(mid, quote, relation="self"):
    return {"message_id": mid, "quote": quote, "relation": relation}


def alias(subject, name, mid, quote, relation="self", status="confirmed"):
    return {
        "subject_id": subject,
        "alias": name,
        "status": status,
        "evidence": [evidence(mid, quote, relation)],
    }


def fact(subject, mid, quote, what, *, key="", supersedes=(), relation="self"):
    return {
        "kind": "reality",
        "subject_ids": [subject],
        "who": "小林",
        "what": what,
        "evidence": [evidence(mid, quote, relation)],
        "profile_key": key,
        "profile_category": "preference" if key else "",
        "confidence": "confirmed",
        "supersedes": list(supersedes),
    }


def make_config(db_path: str = "unused-fixture.db"):
    return load_config(
        {
            "FEISHU_APP_ID": "fixture",
            "FEISHU_APP_SECRET": "fixture",
            "CHAT_MODEL": "fixture-chat",
            "EMBED_MODEL": "fixture-embedding",
            "FEISHU_VERIFICATION_TOKEN": "verify-fixture",
            "GM_TOKEN": "admin-fixture",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "OPENAI_API_KEY": "fixture",
            "STATE_DB": db_path,
        }
    )


class FakeLLM:
    def __init__(self):
        self.responses = deque()
        self.calls = []
        self.closed = False
        self.fail = False

    async def chat_json(self, messages, **kwargs):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("fixture model failure")
        return (
            self.responses.popleft()
            if self.responses
            else json.dumps(
                {
                    "reply_mode": "reaction",
                    "reply": "在呢",
                    "speaker_name": "",
                    "meme_tag": "",
                },
                ensure_ascii=False,
            )
        )

    async def embed_texts(self, texts, **kwargs):
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def embed_text(self, text, **kwargs):
        return [1.0, 0.0, 0.0]

    async def describe_image(self, content):
        return "一个蓝色的玩具"

    async def generate_image(self, prompt):
        return None

    async def close(self):
        self.closed = True


class FakeFeishu:
    def __init__(self):
        self.calls = []
        self.fail = ""
        self.closed = False
        # Display names and reactions are recorded apart from delivery calls.
        self.member_names = {}
        self.member_calls = []
        self.reactions = []
        self.uploaded_images = {}

    async def chat_member_names(self, chat_id):
        self.member_calls.append(chat_id)
        if self.fail == "members":
            raise RuntimeError("fixture member lookup failure")
        return dict(self.member_names)

    async def add_reaction(self, message_id, emoji_type):
        if self.fail == "reaction":
            raise RuntimeError("fixture reaction failure")
        self.reactions.append(("add", message_id, emoji_type))
        return f"reaction-{message_id}"

    async def delete_reaction(self, message_id, reaction_id):
        self.reactions.append(("delete", message_id, reaction_id))

    async def download_uploaded_image(self, image_key):
        return self.uploaded_images.get(image_key)

    def record(self, name, *args):
        if self.fail == name:
            if name in {"card", "text"}:
                raise DeliveryRejected("fixture delivery rejected")
            raise RuntimeError("fixture delivery failure")
        self.calls.append((name, *args))

    async def get_bot_open_id(self):
        self.record("identity")
        return "bot-id"

    async def send_text(self, chat_id, text):
        self.record("text", chat_id, text)

    async def reply_text(self, message_id, text):
        self.record("reply", message_id, text)

    async def send_image(self, chat_id, image_key):
        self.record("image", chat_id, image_key)
        return "image-message"

    async def upload_image(self, image):
        self.record("upload", len(image))
        return "image-key"

    async def download_image(self, message_id, key):
        self.record("download", message_id, key)
        return b"fixture-image"

    async def send_card(self, chat_id, card):
        self.record("card", chat_id, card)
        return "card-message"

    async def close(self):
        self.closed = True


def container(db_path):
    config = replace(
        make_config(db_path),
        reply_debounce_sec=0,
        event_interject_debounce_sec=0,
        event_interject_enabled=False,
    )
    llm, feishu = FakeLLM(), FakeFeishu()
    with (
        patch("dafeiyu.app.LLMClient", return_value=llm),
        patch("dafeiyu.app.FeishuClient", return_value=feishu),
    ):
        return build_container(config)


def event(
    text,
    *,
    sender="alice",
    name="大肥鱼",
    message_id="message-1",
    chat_type="group",
    mention=True,
    message_type="text",
):
    return {
        "sender": {"sender_id": {"open_id": sender}},
        "message": {
            "message_id": message_id,
            "chat_id": "chat-fixture",
            "chat_type": chat_type,
            "message_type": message_type,
            "content": json.dumps(
                {"text": text} if message_type == "text" else text, ensure_ascii=False
            ),
            "mentions": [{"key": "@_bot", "name": name, "id": {"open_id": "bot-id"}}]
            if mention
            else [],
        },
    }
