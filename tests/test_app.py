from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dafeiyu.app import create_app
from tests.helpers import container, event


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = container(str(Path(self.tmp.name) / "new.db"))
        with patch("dafeiyu.app.build_container", return_value=self.c):
            self.app = create_app(self.c.config)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.headers = {"X-GM-Token": "admin-fixture"}
        self.cid = asyncio.run(self.c.conversation_repo.get_or_create_conversation("chat-fixture"))

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_health_and_public_shell(self):
        self.assertEqual(self.client.get("/healthz").json(), {"ok": True, "version": "2.0.0"})
        response = self.client.get("/web")
        self.assertEqual(response.status_code, 200)
        self.assertIn("群聊工作台", response.text)
        self.assertNotIn("admin-fixture", response.text)
        self.assertEqual(self.client.get("/static/workspace.js").status_code, 200)

    def test_diary_without_prepared_content_does_not_generate_or_send(self):
        response = self.client.post(
            "/gm/diary", headers=self.headers, json={"conversation_id": self.cid}
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("尚未产生日记", response.json()["detail"])
        self.assertEqual(self.c.llm.calls, [])
        self.assertEqual(self.c.feishu.calls, [])

    def test_corrupt_diary_storage_is_not_reported_as_normal_not_ready(self):
        asyncio.run(self.c.system_repo.set_sys_cache(f"daily_diary:{self.cid}", "invalid-json"))
        response = self.client.post(
            "/gm/diary", headers=self.headers, json={"conversation_id": self.cid}
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.c.llm.calls, [])
        self.assertEqual(self.c.feishu.calls, [])

    def test_diary_api_sends_saved_content_and_respects_manual_mark(self):
        now = 1793012400.0
        date, _ = self.c.state_domain.local_date_hour(now)
        asyncio.run(
            self.c.system_repo.set_sys_cache(
                f"daily_diary:{self.cid}",
                json.dumps(
                    {
                        "date": date,
                        "ready": True,
                        "text": "今天听到好消息，我也开心了。",
                        "evidence": [],
                    }
                ),
            )
        )
        with patch("dafeiyu.routes.gm.time.time", return_value=now):
            response = self.client.post(
                "/gm/diary", headers=self.headers, json={"conversation_id": self.cid, "mark": True}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], "今天听到好消息，我也开心了。")
        self.assertEqual(response.json()["marked_date"], date)
        self.assertEqual(self.c.llm.calls, [])

    def test_auth_uses_header_not_url_and_reads_do_not_create_conversations(self):
        self.assertEqual(self.client.get("/gm/conversations").status_code, 401)
        self.assertEqual(self.client.get("/gm/conversations?token=admin-fixture").status_code, 401)
        self.assertEqual(
            self.client.get("/gm/conversations", headers=self.headers).status_code, 200
        )
        self.assertEqual(
            self.client.get("/gm/state?conversation_id=999", headers=self.headers).status_code,
            404,
        )
        response = self.client.get("/gm/conversations", headers=self.headers)
        self.assertEqual(len(response.json()["conversations"]), 1)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_read_state_does_not_write(self):
        with self.c.db.connect() as conn:
            before = dict(conn.execute("SELECT * FROM conversations").fetchone())
        self.assertEqual(
            self.client.get(
                f"/gm/state?conversation_id={self.cid}", headers=self.headers
            ).status_code,
            200,
        )
        with self.c.db.connect() as conn:
            self.assertEqual(dict(conn.execute("SELECT * FROM conversations").fetchone()), before)

    def test_validation_rejects_old_target_and_empty_or_boolean_target(self):
        for body in [
            {"pet_id": self.cid, "text": "hello"},
            {"conversation_id": True, "text": "hello"},
            {"conversation_id": self.cid, "text": "   "},
        ]:
            self.assertEqual(
                self.client.post("/gm/text", headers=self.headers, json=body).status_code,
                422,
            )
        self.assertFalse(self.c.feishu.calls)

    def test_manual_text_is_recorded_and_target_is_explicit(self):
        response = self.client.post(
            "/gm/text",
            headers=self.headers,
            json={"conversation_id": self.cid, "text": "虚构测试消息"},
        )
        self.assertEqual(response.status_code, 200)
        rows = self.client.get(
            f"/gm/messages?conversation_id={self.cid}", headers=self.headers
        ).json()["messages"]
        self.assertEqual(rows[0]["source"], "manual")
        self.assertEqual(rows[0]["content"], "虚构测试消息")
        self.assertEqual(self.c.feishu.calls[0], ("text", "chat-fixture", "虚构测试消息"))

    def test_failed_send_is_not_recorded_or_disclosed_as_success(self):
        self.c.feishu.fail = "text"
        response = self.client.post(
            "/gm/text",
            headers=self.headers,
            json={"conversation_id": self.cid, "text": "测试"},
        )
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("fixture delivery", response.text)
        self.assertEqual(asyncio.run(self.c.message_repo.recent_messages(self.cid, 20)), [])

    def test_rename_is_local_and_pagination_is_bounded(self):
        response = self.client.patch(
            f"/gm/conversations/{self.cid}",
            headers=self.headers,
            json={"title": "测试群聊"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get("/gm/conversations", headers=self.headers).json()["conversations"][0][
                "title"
            ],
            "测试群聊",
        )
        self.assertEqual(
            self.client.get(
                f"/gm/messages?conversation_id={self.cid}&limit=500",
                headers=self.headers,
            ).status_code,
            422,
        )
        self.assertFalse(self.c.feishu.calls)

    def test_upload_validates_size_and_type_and_records_delivery(self):
        response = self.client.post(
            "/gm/image",
            headers=self.headers,
            data={"conversation_id": self.cid},
            files={"file": ("fixture.txt", b"test", "text/plain")},
        )
        self.assertEqual(response.status_code, 422)
        response = self.client.post(
            "/gm/image",
            headers=self.headers,
            data={"conversation_id": self.cid},
            files={"file": ("fixture.png", b"fixture-image", "image/png")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            asyncio.run(self.c.message_repo.recent_messages(self.cid, 20))[0]["source"],
            "manual",
        )

    def test_webhook_verifies_challenge_and_deduplicates_messages(self):
        self.assertEqual(
            self.client.post(
                "/feishu/webhook",
                json={"type": "url_verification", "challenge": "test"},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/feishu/webhook",
                json={
                    "type": "url_verification",
                    "challenge": "test",
                    "token": "verify-fixture",
                },
            ).json(),
            {"challenge": "test"},
        )
        body = {
            "header": {
                "token": "verify-fixture",
                "event_id": "event-fixture",
                "event_type": "im.message.receive_v1",
            },
            "event": event("@_bot 你好"),
        }
        self.client.post("/feishu/webhook", json=body)
        self.client.post("/feishu/webhook", json=body)
        self.assertEqual(len(asyncio.run(self.c.message_repo.recent_messages(self.cid, 20))), 2)

    def test_lifespan_closes_clients_and_background_tasks(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.get("/healthz").status_code, 200)
        self.assertTrue(self.c.feishu.closed)
        self.assertTrue(self.c.llm.closed)
        self.assertFalse(self.c.runtime.background_tasks)
