from __future__ import annotations

import base64
import hashlib
import json
import logging

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from config import AppConfig
from repositories.system_repo import SystemRepository
from runtime import RuntimeState

log = logging.getLogger("tamagotchi")


class FeishuClient:
    def __init__(
        self,
        config: AppConfig,
        system_repo: SystemRepository,
        runtime: RuntimeState,
    ):
        self.config = config
        self.system_repo = system_repo
        self.runtime = runtime
        self.http = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self.http.aclose()

    def decrypt(self, encrypt_str: str) -> str:
        key = hashlib.sha256(self.config.feishu_encrypt_key.encode("utf-8")).digest()
        data = base64.b64decode(encrypt_str)
        iv, ct = data[:16], data[16:]
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ct) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")

    async def get_tenant_access_token(self) -> str:
        async with self.runtime.token_lock:
            cached = await self.system_repo.get_sys_cache("tenant_access_token")
            if cached:
                return cached
            resp = await self.http.post(
                f"{self.config.feishu_base}/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": self.config.feishu_app_id,
                    "app_secret": self.config.feishu_app_secret,
                },
            )
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"get tenant_access_token failed: {data}")
            token = data["tenant_access_token"]
            expires_in = float(data.get("expire", 7000))
            await self.system_repo.set_sys_cache(
                "tenant_access_token", token, expires_in - 60
            )
            return token

    async def get_bot_open_id(self) -> str:
        cached = await self.system_repo.get_sys_cache("bot_open_id")
        if cached:
            return cached
        token = await self.get_tenant_access_token()
        resp = await self.http.get(
            f"{self.config.feishu_base}/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"get bot info failed: {data}")
        open_id = ((data.get("bot") or {}).get("open_id") or "").strip()
        if not open_id:
            raise RuntimeError(f"bot info missing open_id: {data}")
        await self.system_repo.set_sys_cache("bot_open_id", open_id)
        log.info("bot open_id resolved: %s", open_id)
        return open_id

    async def reply_text(self, message_id: str, text: str) -> None:
        token = await self.get_tenant_access_token()
        resp = await self.http.post(
            f"{self.config.feishu_base}/im/v1/messages/{message_id}/reply",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            log.error("reply failed: %s", data)

    async def send_text(self, chat_id: str, text: str) -> None:
        token = await self.get_tenant_access_token()
        resp = await self.http.post(
            f"{self.config.feishu_base}/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            log.error("send failed: %s", data)
            raise RuntimeError(f"feishu send failed: {data}")

    async def upload_image(self, image_bytes: bytes) -> str | None:
        token = await self.get_tenant_access_token()
        resp = await self.http.post(
            f"{self.config.feishu_base}/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            data={"image_type": "message"},
            files={"image": ("pet.png", image_bytes, "image/png")},
        )
        data = resp.json()
        if data.get("code") != 0:
            log.error("upload image failed: %s", data)
            return None
        return (data.get("data") or {}).get("image_key")

    async def send_card(self, chat_id: str, card: dict) -> None:
        token = await self.get_tenant_access_token()
        resp = await self.http.post(
            f"{self.config.feishu_base}/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            log.error("send card failed: %s", data)
            raise RuntimeError(f"feishu send card failed: {data}")

    async def update_card_message(self, message_id: str, card: dict) -> None:
        token = await self.get_tenant_access_token()
        resp = await self.http.patch(
            f"{self.config.feishu_base}/im/v1/messages/{message_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={"content": json.dumps(card, ensure_ascii=False)},
        )
        data = resp.json()
        if data.get("code") != 0:
            log.error("update card failed: %s", data)

