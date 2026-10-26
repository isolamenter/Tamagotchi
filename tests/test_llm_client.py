from __future__ import annotations

import typing
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from dafeiyu.integrations.llm_client import LLMClient, _to_gemini_contents


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


class GeminiSearchTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, response):
        generate = AsyncMock(return_value=response)
        llm = LLMClient.__new__(LLMClient)
        llm.provider = "gemini"
        llm.config = typing.cast(typing.Any, SimpleNamespace(
            model_name="gemini-3.1-flash-lite", gemini_api_key="fixture-secret"
        ))
        llm._types = SimpleNamespace(
            GenerateContentConfig=SimpleNamespace,
            Tool=SimpleNamespace,
            GoogleSearch=SimpleNamespace,
        )
        llm._genai = SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        )
        return llm, generate

    async def test_search_is_opt_in_and_preserves_json_output(self):
        response = '{"reply_mode":"normal","reply":"查到了"}'
        llm, generate = self.make_client(SimpleNamespace(
            text=response, candidates=[SimpleNamespace(finish_reason="STOP")],
            usage_metadata=None, prompt_feedback=None, response_id="fixture-response",
        ))
        messages = [
            {"role": "system", "content": "输出 JSON"},
            {"role": "user", "content": "今天有什么新闻？"},
        ]

        result = await llm.chat_json(messages, max_tokens=350, temperature=0.5, web_search=True)
        self.assertEqual(result, response)
        request = generate.call_args.kwargs
        self.assertEqual(request["model"], "gemini-3.1-flash-lite")
        config = request["config"]
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertEqual(config.system_instruction, "输出 JSON")
        self.assertEqual(config.max_output_tokens, 350)
        self.assertEqual(len(config.tools), 1)
        self.assertIsNotNone(config.tools[0].google_search)

        await llm.chat_json(messages, max_tokens=128, temperature=0)
        self.assertFalse(hasattr(generate.call_args.kwargs["config"], "tools"))

    async def test_truncation_logs_finish_usage_and_json_error_without_content(self):
        response = '{"reply":"private response'
        llm, _ = self.make_client(SimpleNamespace(
            text=response, candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")],
            usage_metadata=SimpleNamespace(
                prompt_token_count=900, candidates_token_count=350,
                thoughts_token_count=0, total_token_count=1250,
            ),
            prompt_feedback=None, response_id="fixture-truncated",
        ))
        with self.assertLogs("dafeiyu", level="WARNING") as captured:
            result = await llm.chat_json(
                [{"role": "user", "content": "private request"}],
                max_tokens=350, temperature=0.5,
            )
        self.assertEqual(result, response)
        logs = "\n".join(captured.output)
        for value in ["MAX_TOKENS", "output_tokens=350", "max_output_tokens=350",
                      "prompt_tokens=900", "thought_tokens=0", "total_tokens=1250",
                      "Unterminated string", "position=", "fixture-truncated"]:
            self.assertIn(value, logs)
        self.assertNotIn("private response", logs)
        self.assertNotIn("private request", logs)

    async def test_blocked_empty_response_logs_block_reason(self):
        llm, _ = self.make_client(SimpleNamespace(
            text=None, candidates=None, usage_metadata=None,
            prompt_feedback=SimpleNamespace(block_reason="SAFETY"), response_id=None,
        ))
        with self.assertLogs("dafeiyu", level="WARNING") as captured:
            self.assertEqual(await llm.chat_json([], max_tokens=1200, temperature=0.5), "")
        logs = "\n".join(captured.output)
        self.assertIn("block_reason=SAFETY", logs)
        self.assertIn("chars=0", logs)
        self.assertIn("Expecting value", logs)

    async def test_api_error_logs_status_and_redacts_key_then_propagates(self):
        llm, generate = self.make_client(None)
        error = RuntimeError("fixture error")
        error.code = 429
        error.status = "RESOURCE_EXHAUSTED"
        error.message = "Quota exceeded for fixture-secret"
        generate.side_effect = error
        with self.assertLogs("dafeiyu", level="WARNING") as captured:
            with self.assertRaises(RuntimeError) as raised:
                await llm.chat_json([], max_tokens=1200, temperature=0.5)
        self.assertIs(raised.exception, error)
        logs = "\n".join(captured.output)
        self.assertIn("code=429", logs)
        self.assertIn("RESOURCE_EXHAUSTED", logs)
        self.assertIn("Quota exceeded", logs)
        self.assertIn("[REDACTED]", logs)
        self.assertNotIn("fixture-secret", logs)

    async def test_search_on_unsupported_provider_is_rejected(self):
        llm = LLMClient.__new__(LLMClient)
        llm.provider = "openai"
        with self.assertRaisesRegex(ValueError, "Gemini"):
            await llm.chat_json([], max_tokens=350, temperature=0.5, web_search=True)


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
        llm.config = typing.cast(typing.Any, SimpleNamespace(embed_model="embed-test"))
        llm.client = typing.cast(typing.Any, SimpleNamespace(embeddings=Embeddings()))
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
        llm.config = typing.cast(typing.Any, SimpleNamespace(embed_model="gemini-embedding-001"))
        llm._types = SimpleNamespace(EmbedContentConfig=EmbedContentConfig)
        llm._genai = SimpleNamespace(aio=SimpleNamespace(models=Models()))
        vectors = await llm.embed_texts(["doc one", "doc two"], purpose="retrieval_document")
        self.assertEqual(vectors, [[1.0], [2.0]])
        self.assertEqual(calls[0]["contents"], ["doc one", "doc two"])
        self.assertEqual(calls[0]["config"].task_type, "RETRIEVAL_DOCUMENT")

    async def test_embedding2_keeps_documents_separate_and_formats_retrieval_inputs(self):
        embed = AsyncMock(return_value=SimpleNamespace(embeddings=[
            SimpleNamespace(values=[1.0, 0.0]), SimpleNamespace(values=[0.0, 1.0])
        ]))
        llm = LLMClient.__new__(LLMClient)
        llm.provider = "gemini"
        llm.config = typing.cast(typing.Any, SimpleNamespace(embed_model="gemini-embedding-2"))
        llm._types = SimpleNamespace(Content=SimpleNamespace, Part=SimpleNamespace)
        llm._genai = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(embed_content=embed)))
        vectors = await llm.embed_texts(["doc one", "doc two"], purpose="retrieval_document")
        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])
        request = embed.call_args.kwargs
        self.assertIsNone(request["config"])
        self.assertEqual(
            [content.parts[0].text for content in request["contents"]],
            ["title: none | text: doc one", "title: none | text: doc two"],
        )
        embed.return_value = SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, 0.0])])
        self.assertEqual(
            await llm.embed_text("find this", purpose="retrieval_query"), [1.0, 0.0]
        )
        self.assertEqual(
            embed.call_args.kwargs["contents"][0].parts[0].text,
            "task: search result | query: find this",
        )

    async def test_empty_and_count_mismatch_fail_closed(self):
        llm = LLMClient.__new__(LLMClient)
        llm.provider = "openai"
        llm.config = typing.cast(typing.Any, SimpleNamespace(embed_model="embed-test"))

        class Embeddings:
            async def create(self, **_kwargs):
                return SimpleNamespace(data=[])

        llm.client = typing.cast(typing.Any, SimpleNamespace(embeddings=Embeddings()))
        self.assertEqual(await llm.embed_texts([]), [])
        self.assertIsNone(await llm.embed_texts(["valid", ""]))
        self.assertIsNone(await llm.embed_texts(["valid"]))


class DescribeImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_sends_data_url_and_returns_caption(self):
        seen = {}

        class Completions:
            async def create(self, **kwargs):
                seen.update(kwargs)
                from types import SimpleNamespace

                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=" 一只猫 "))]
                )

        class Chat:
            completions = Completions()

        llm = LLMClient.__new__(LLMClient)
        llm.provider = "openai"
        llm.config = typing.cast(typing.Any, SimpleNamespace(model_name="m"))
        llm.client = typing.cast(typing.Any, SimpleNamespace(chat=Chat()))
        caption = await llm.describe_image(b"\x89PNG\r\n\x1a\nxxx")
        self.assertEqual(caption, "一只猫")
        parts = seen["messages"][0]["content"]
        img = next(p for p in parts if p["type"] == "image_url")
        self.assertTrue(img["image_url"]["url"].startswith("data:image/png;base64,"))

    async def test_openai_failure_returns_none(self):
        class Completions:
            async def create(self, **kwargs):
                raise RuntimeError("boom")

        class Chat:
            completions = Completions()

        llm = LLMClient.__new__(LLMClient)
        llm.provider = "openai"
        llm.config = typing.cast(typing.Any, SimpleNamespace(model_name="m"))
        llm.client = typing.cast(typing.Any, SimpleNamespace(chat=Chat()))
        self.assertIsNone(await llm.describe_image(b"\xff\xd8fake"))
        self.assertIsNone(await llm.describe_image(b""))


if __name__ == "__main__":
    unittest.main()
