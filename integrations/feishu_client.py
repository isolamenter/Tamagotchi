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
    # 飞书 tenant_access_token 失效/过期的业务码，命中则刷新缓存重试一次。
    _TOKEN_EXPIRED_CODES = {99991661, 99991663, 99991668}

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
            if resp.status_code != 200:
                raise RuntimeError(
                    f"get tenant_access_token http {resp.status_code}: {resp.text[:200]}"
                )
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"get tenant_access_token failed: {data}")
            token = data["tenant_access_token"]
            try:
                expires_in = float(data.get("expire", 7000))
            except (TypeError, ValueError):
                expires_in = 7000.0
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

    async def _authed_request(
        self, method: str, url: str, *, label: str, headers: dict | None = None, **kwargs
    ) -> dict | None:
        """带 tenant token 的请求；命中 token 失效码时清缓存刷新并重试一次。
        非 JSON 响应返回 None（调用方按失败处理）。"""
        data: dict | None = None
        for attempt in range(2):
            token = await self.get_tenant_access_token()
            req_headers = {"Authorization": f"Bearer {token}", **(headers or {})}
            resp = await self.http.request(method, url, headers=req_headers, **kwargs)
            try:
                data = resp.json()
            except Exception:
                log.error("%s: non-JSON response (status=%s)", label, resp.status_code)
                return None
            if isinstance(data, dict) and data.get("code") in self._TOKEN_EXPIRED_CODES and attempt == 0:
                log.warning(
                    "%s: tenant token rejected (code=%s), refreshing and retrying",
                    label,
                    data.get("code") if isinstance(data, dict) else None,
                )
                await self.system_repo.delete_sys_cache("tenant_access_token")
                continue
            return data
        return data

    async def reply_text(self, message_id: str, text: str) -> None:
        data = await self._authed_request(
            "POST",
            f"{self.config.feishu_base}/im/v1/messages/{message_id}/reply",
            label="reply",
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )
        if not data or data.get("code") != 0:
            log.error("reply failed: %s", data)
            raise RuntimeError(f"feishu reply failed: {data}")

    async def send_text(self, chat_id: str, text: str) -> None:
        data = await self._authed_request(
            "POST",
            f"{self.config.feishu_base}/im/v1/messages",
            label="send",
            params={"receive_id_type": "chat_id"},
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )
        if not data or data.get("code") != 0:
            log.error("send failed: %s", data)
            raise RuntimeError(f"feishu send failed: {data}")

    async def upload_image(self, image_bytes: bytes) -> str | None:
        data = await self._authed_request(
            "POST",
            f"{self.config.feishu_base}/im/v1/images",
            label="upload image",
            data={"image_type": "message"},
            files={"image": ("pet.png", image_bytes, "image/png")},
        )
        if not data or data.get("code") != 0:
            log.error("upload image failed: %s", data)
            return None
        return (data.get("data") or {}).get("image_key")

    async def send_image(self, chat_id: str, image_key: str) -> str:
        """发送独立图片消息（msg_type=image），内存直传不落盘。"""
        data = await self._authed_request(
            "POST",
            f"{self.config.feishu_base}/im/v1/messages",
            label="send image",
            params={"receive_id_type": "chat_id"},
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={
                "receive_id": chat_id,
                "msg_type": "image",
                "content": json.dumps({"image_key": image_key}, ensure_ascii=False),
            },
        )
        if not data or data.get("code") != 0:
            log.error("send image failed: %s", data)
            raise RuntimeError(f"feishu send image failed: {data}")
        message_id = str((data.get("data") or {}).get("message_id") or "")
        if not message_id:
            raise RuntimeError(f"feishu send image returned no message_id: {data}")
        return message_id

    async def send_card(self, chat_id: str, card: dict) -> str:
        data = await self._authed_request(
            "POST",
            f"{self.config.feishu_base}/im/v1/messages",
            label="send card",
            params={"receive_id_type": "chat_id"},
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
        )
        if not data or data.get("code") != 0:
            log.error("send card failed: %s", data)
            raise RuntimeError(f"feishu send card failed: {data}")
        message_id = str((data.get("data") or {}).get("message_id") or "")
        if not message_id:
            raise RuntimeError(f"feishu send card returned no message_id: {data}")
        return message_id

    async def update_card_message(self, message_id: str, card: dict) -> None:
        data = await self._authed_request(
            "PATCH",
            f"{self.config.feishu_base}/im/v1/messages/{message_id}",
            label="update card",
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={"content": json.dumps(card, ensure_ascii=False)},
        )
        if not data or data.get("code") != 0:
            log.error("update card failed: %s", data)
            raise RuntimeError(f"feishu update card failed: {data}")
