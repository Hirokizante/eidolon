import pytest

from eidolon.config import ModelMetadata, ProviderConfig
from eidolon.providers.context import ContextBudget, TokenCounter, _encoding
from eidolon.providers.openai import OpenAIProvider


def test_discovered_window_and_manual_override_share_output_reserve():
    profile = ProviderConfig(model="test-model", model_metadata={
        "test-model": ModelMetadata(context_window=1_000_000, source="test", updated_at=0),
    }, parameters={"max_tokens": 8000})
    budget = ContextBudget.for_profile(profile)
    assert budget.window == 1_000_000
    assert budget.reserved_tokens == 28_000
    assert budget.fits(972_000) and not budget.fits(972_001)
    profile.context_window = 16_000
    budget = ContextBudget.for_profile(profile)
    assert budget.window == 16_000 and budget.output_tokens == 8000
    profile.model = "other-model"
    profile.context_window = None
    budget = ContextBudget.for_profile(profile)
    assert budget.window is None and budget.fits(2_000_000)


@pytest.mark.parametrize("key", ["max_tokens", "max_completion_tokens", "max_output_tokens"])
def test_output_reservation_accepts_compatible_parameters_without_clamping_them(key):
    profile = ProviderConfig(context_window=8000, parameters={key: 9000})
    budget = ContextBudget.for_profile(profile)
    assert budget.output_tokens == 9000 and not budget.fits(0)
    profile.parameters = {"max_tokens": 500, "max_completion_tokens": 1000}
    assert ContextBudget.for_profile(profile).output_tokens == 1000
    profile.parameters = {"max_tokens": False, "max_completion_tokens": -1}
    assert ContextBudget.for_profile(profile).output_tokens == 2000


def test_counter_includes_tool_schemas_arguments_and_presentation_format():
    counter = TokenCounter("gpt-4o")
    messages = [{"role": "user", "content": "Hello 世界 👋 <|endoftext|>"}]
    plain = counter.request(messages)
    tools = [{"type": "function", "function": {
        "name": "lookup", "description": "Descriptions also use context. " * 500,
    }}]
    structured = counter.request(messages, tools, OpenAIProvider.response_format("json"))
    assert structured > plain + 500
    messages.append({"role": "assistant", "tool_calls": [{"id": "call1", "function": {
        "name": "lookup", "arguments": '{"query":"More input"}',
    }}]})
    assert counter.request(messages) > plain


def test_unavailable_vocabulary_uses_utf8_fallback(monkeypatch):
    _encoding.cache_clear()
    try:
        def offline(name):
            raise OSError("offline")

        monkeypatch.setattr("tiktoken.get_encoding", offline)
        counter = TokenCounter("offline-test-model")
        assert counter.encoding is None
        assert counter.text("é🌸") == 6
    finally:
        _encoding.cache_clear()


def test_image_budget_ignores_base64_length_and_preserves_text_and_message_parts():
    counter = TokenCounter("test-model")
    # Make the fallback especially sensitive to accidental base64 tokenization.
    counter.encoding = None
    def message(encoded):
        return {"role": "user", "content": [
            {"type": "text", "text": "Read the labels."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
        ]}
    small, large = message("a"), message("a" * 1_000_000)
    original = large["content"][1]["image_url"]["url"]
    assert counter.message(small) == counter.message(large)
    assert 4096 < counter.message(large) < 5000
    assert large["content"][1]["image_url"]["url"] == original
    large["content"][0]["text"] += " Count every red label." * 200
    assert counter.message(large) > counter.message(small) + 4000
    small["content"].append(small["content"][1])
    assert counter.message(small) > 8192
