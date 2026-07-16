from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
import asyncio
from pathlib import Path

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:  # pragma: no cover - exercised only without deps installed.
    TestClient = None


class AppSmokeTests(unittest.TestCase):
    def test_healthz(self):
        if TestClient is None:
            self.skipTest("fastapi is not installed in this Python environment")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.update(
                {
                    "FEISHU_APP_ID": "app",
                    "FEISHU_APP_SECRET": "secret",
                    "FEISHU_VERIFICATION_TOKEN": "verify-token",
                    "OPENAI_BASE_URL": "https://example.invalid/v1",
                    "OPENAI_API_KEY": "key",
                    "STATE_DB": str(Path(tmp) / "state.db"),
                }
            )
            sys.modules.pop("tamagotchi", None)
            app_module = importlib.import_module("tamagotchi")
            with TestClient(app_module.app) as client:
                resp = client.get("/healthz")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json(), {"ok": True})

    def test_gm_read_does_not_write_state(self):
        if TestClient is None:
            self.skipTest("fastapi is not installed in this Python environment")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.update(
                {
                    "FEISHU_APP_ID": "app",
                    "FEISHU_APP_SECRET": "secret",
                    "FEISHU_VERIFICATION_TOKEN": "verify-token",
                    "OPENAI_BASE_URL": "https://example.invalid/v1",
                    "OPENAI_API_KEY": "key",
                    "STATE_DB": str(Path(tmp) / "state.db"),
                }
            )
            sys.modules.pop("tamagotchi", None)
            app_module = importlib.import_module("tamagotchi")
            with TestClient(app_module.app) as client:
                container = app_module.app.state.container
                pet_id = asyncio.run(container.pet_repo.get_or_create_pet("oc_gm"))
                with container.db.connect() as conn:
                    before = conn.execute(
                        "SELECT state_json FROM pets WHERE id = ?", (pet_id,)
                    ).fetchone()["state_json"]
                resp = client.get(f"/gm/state?token=verify-token&pet_id={pet_id}")
                self.assertEqual(resp.status_code, 200)
                with container.db.connect() as conn:
                    after = conn.execute(
                        "SELECT state_json FROM pets WHERE id = ?", (pet_id,)
                    ).fetchone()["state_json"]
                self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
