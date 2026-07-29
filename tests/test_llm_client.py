from __future__ import annotations

import unittest
from types import SimpleNamespace

from integrations.llm_client import LLMClient, _to_gemini_contents


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


class EmbeddingBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_batch_preserves_order_and_single_reuses_it(self):
        calls = []

        class Embeddings:
            async def create(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(
                    data=[
                        SimpleNamespace(embedding=[1.0, 0.0]),
                        SimpleNamespace(embedding=[0.0, 1.0]),
                    ]
                )

        llm = LLMClient.__new__(LLMClient)
        llm.provider = "openai"
        llm.config = SimpleNamespace(embed_model="embed-test")
        llm.client = SimpleNamespace(embeddings=Embeddings())
        vectors = await llm.embed_texts(["first", "second"])
        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(calls[0]["input"], ["first", "second"])

        async def one(texts, *, purpose=""):
            self.assertEqual(texts, ["single"])
            return [[3.0, 4.0]]

        llm.embed_texts = one
        self.assertEqual(await llm.embed_text("single"), [3.0, 4.0])

    async def test_gemini_sets_retrieval_task_type(self):
        calls = []

        class EmbedContentConfig:
            def __init__(self, **kwargs):
                self.task_type = kwargs.get("task_type")

        class Models:
            async def embed_content(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(
                    embeddings=[
                        SimpleNamespace(values=[1.0]),
                        SimpleNamespace(values=[2.0]),
                    ]
                )

        llm = LLMClient.__new__(LLMClient)
        llm.provider = "gemini"
        llm.config = SimpleNamespace(embed_model="gemini-embedding-001")
        llm._types = SimpleNamespace(EmbedContentConfig=EmbedContentConfig)
        llm._genai = SimpleNamespace(aio=SimpleNamespace(models=Models()))
        vectors = await llm.embed_texts(
            ["doc one", "doc two"], purpose="retrieval_document"
        )
        self.assertEqual(vectors, [[1.0], [2.0]])
        self.assertEqual(calls[0]["contents"], ["doc one", "doc two"])
        self.assertEqual(calls[0]["config"].task_type, "RETRIEVAL_DOCUMENT")

    async def test_empty_and_count_mismatch_fail_closed(self):
        llm = LLMClient.__new__(LLMClient)
        llm.provider = "openai"
        llm.config = SimpleNamespace(embed_model="embed-test")

        class Embeddings:
            async def create(self, **_kwargs):
                return SimpleNamespace(data=[])

        llm.client = SimpleNamespace(embeddings=Embeddings())
        self.assertEqual(await llm.embed_texts([]), [])
        self.assertIsNone(await llm.embed_texts(["valid", ""]))
        self.assertIsNone(await llm.embed_texts(["valid"]))


if __name__ == "__main__":
    unittest.main()
