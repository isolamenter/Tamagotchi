from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
