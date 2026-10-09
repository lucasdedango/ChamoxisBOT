import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from manager.ai.gateway import Gateway
from manager.ai.router import Router, AIUnavailable
from manager.ai.ollama_client import final_content
from shared.schemas import Chat, Message


class FinalContentTests(unittest.TestCase):
    def test_removes_tagged_thinking_and_ignores_separate_thinking_field(self):
        self.assertEqual(final_content({"message": {"thinking": "private", "content":
            '<think>analysis</think>\n{"answer":"Bonjour"}'}}), '{"answer":"Bonjour"}')

    def test_rejects_unfinished_thinking_empty_final_and_token_exhaustion(self):
        for body in ({"message": {"content": "<think>unfinished"}},
                     {"message": {"content": "<think>analysis</think>"}},
                     {"message": {"content": "cut off"}, "done_reason": "length"},
                     {"message": {"thinking": "no answer"}}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                final_content(body)


class FinalAnswerTests(unittest.IsolatedAsyncioTestCase):
    def backend(self, content):
        return SimpleNamespace(url="http://local", model="test", available=AsyncMock(return_value=True),
                               chat=AsyncMock(return_value=content))

    async def test_chat_returns_only_validated_final_answer(self):
        backend = self.backend('{"answer":"Le débit est nul et aucun seed n’est connecté."}')
        body = Chat(messages=[Message(role="user", content="Pourquoi ça bloque ?")])
        result = await Gateway(Router([backend])).chat(body)
        self.assertEqual(result["result"], "Le débit est nul et aucun seed n’est connecté.")
        self.assertEqual(len(body.messages), 1)
        self.assertIn("answer", backend.chat.await_args.args[1]["properties"])

    async def test_unmarked_reasoning_is_rejected_and_next_backend_used(self):
        remote = self.backend("Okay, let's see. The user is asking why the download is stalled...")
        local = self.backend('{"answer":"Aucun seed connecté actuellement."}')
        result = await Gateway(Router([remote, local])).chat(Chat(messages=[Message(role="user", content="Bloqué ?")]))
        self.assertEqual(result["result"], "Aucun seed connecté actuellement.")

    async def test_reasoning_without_final_answer_is_unavailable(self):
        backend = self.backend("Okay, let's see. I should respond in French...")
        with self.assertRaises(AIUnavailable):
            await Gateway(Router([backend])).chat(Chat(messages=[Message(role="user", content="Bonjour")]))

    async def test_explicit_structured_api_still_returns_json(self):
        backend = self.backend('{"field":42}')
        result = await Gateway(Router([backend])).chat(Chat(messages=[Message(role="user", content="JSON")], structured=True))
        self.assertEqual(result["result"], {"field": 42})
