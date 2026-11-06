"""Provider-level multimodal message conversion tests without network calls."""

from __future__ import annotations

from codebase_rag.services.agent.models import Message
from codebase_rag.services.agent.providers.anthropic import AnthropicProvider
from codebase_rag.services.agent.providers.gemini import GeminiLLMProvider
from codebase_rag.services.agent.providers.llm_sdk_common import build_chat_messages
from codebase_rag.services.agent.providers.openai_chat import OpenAIChatProvider
from codebase_rag.services.agent.providers.openai_responses import OpenAIResponsesProvider


_DATA_URL = "data:image/png;base64,aGVsbG8="


def _multimodal_message():
    return Message(
        role="user",
        content=[
            {"type": "text", "text": "describe this architecture"},
            {"type": "image_url", "image_url": {"url": _DATA_URL}},
        ],
    )


def test_provider_modules_and_multimodal_chat_messages_are_available():
    assert OpenAIChatProvider is not None
    built = build_chat_messages([_multimodal_message()])
    assert built[0]["content"][1]["type"] == "image_url"


def test_openai_responses_converts_multimodal_parts_and_keeps_instructions():
    system = Message(role="system", content="You create knowledge points")
    assert OpenAIResponsesProvider._build_instructions([system]) == "You create knowledge points"
    items = OpenAIResponsesProvider._build_input([system, _multimodal_message()])
    assert len(items) == 1
    assert items[0]["content"] == [
        {"type": "input_text", "text": "describe this architecture"},
        {"type": "input_image", "image_url": _DATA_URL},
    ]


def test_gemini_converts_data_url_to_inline_data():
    contents = GeminiLLMProvider._build_contents([_multimodal_message()])
    assert contents[0]["parts"][1] == {
        "inlineData": {"mimeType": "image/png", "data": "aGVsbG8="}
    }


def test_anthropic_converts_data_url_to_base64_image_source():
    messages = AnthropicProvider._build_messages([_multimodal_message()])
    assert messages[0]["content"][1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "aGVsbG8=",
        },
    }
