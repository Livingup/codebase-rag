from pathlib import Path
from types import SimpleNamespace
import unittest

import yaml

from codebase_rag.services.agent.models import Message, ToolSchema
from codebase_rag.services.agent.providers.deepseek import DeepSeekProvider


class _EmptyAsyncStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class DeepSeekToolChoiceTest(unittest.IsolatedAsyncioTestCase):
    def test_config_example_sets_deepseek_tool_choice_auto(self):
        config_path = Path(__file__).resolve().parents[2] / "config.example.yaml"
        with config_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        deepseek = raw["agent"]["llm"]["providers"]["deepseek"]
        tool_choices = {model["id"]: model["tool_choice"] for model in deepseek["models"]}

        self.assertEqual(tool_choices["deepseek-v4-flash"], "auto")
        self.assertEqual(tool_choices["deepseek-v4-pro"], "auto")

    async def test_deepseek_provider_sends_auto_tool_choice(self):
        captured = {}

        def stream(request, options=None):
            captured["request"] = request
            return _EmptyAsyncStream()

        provider = DeepSeekProvider("https://api.deepseek.com")
        provider._make_client = lambda api_key: SimpleNamespace(stream=stream)

        await provider.chat_completion(
            api_key="test-key",
            messages=[Message(role="user", content="finish the task")],
            model="deepseek-v4-flash",
            tools=[
                ToolSchema(
                    name="end_task",
                    description="Finish the task.",
                    parameters={"type": "object", "properties": {}},
                ),
            ],
            tool_choice="auto",
            thinking_config={"thinking": {"type": "disabled"}},
        )

        request = captured["request"]
        self.assertEqual(request.tool_choice, "auto")
        self.assertEqual(request.tools[0]["function"]["name"], "end_task")
        self.assertEqual(request.messages[0]["role"], "user")


if __name__ == "__main__":
    unittest.main()
