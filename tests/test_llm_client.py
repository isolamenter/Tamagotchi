from __future__ import annotations

import unittest

from integrations.llm_client import _to_gemini_contents


class GeminiContentsTests(unittest.TestCase):
    def test_role_mapping_and_system_merge(self):
        messages = [
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "system", "content": "state block"},
            {"role": "user", "content": "how are you"},
        ]
        system, contents = _to_gemini_contents(messages)
        self.assertEqual(system, "persona\nstate block")
        self.assertEqual(
            contents,
            [
                {"role": "user", "parts": [{"text": "hello"}]},
                {"role": "model", "parts": [{"text": "hi there"}]},
                {"role": "user", "parts": [{"text": "how are you"}]},
            ],
        )

    def test_no_system_messages(self):
        system, contents = _to_gemini_contents([{"role": "user", "content": "yo"}])
        self.assertEqual(system, "")
        self.assertEqual(contents, [{"role": "user", "parts": [{"text": "yo"}]}])

    def test_missing_content_defaults_empty(self):
        system, contents = _to_gemini_contents([{"role": "user"}])
        self.assertEqual(system, "")
        self.assertEqual(contents, [{"role": "user", "parts": [{"text": ""}]}])


if __name__ == "__main__":
    unittest.main()
