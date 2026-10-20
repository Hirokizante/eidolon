import asyncio
import base64
import copy
import io
from pathlib import Path

import pytest

from eidolon.config import ModelMetadata, VisionModelConfig
from eidolon.engine import ConversationEngine
from eidolon.images import ImageStore
from eidolon.storage import Store
from eidolon.tools.vision import VisionAnalysis, resolve_vision
from test_engine import Provider, config
from test_images import attach, png, reply


async def test_cancelled_vision_analysis_closes_provider_stream(tmp_path):
    cfg = config(tmp_path)
    cfg.vision = VisionModelConfig(mode="separate", provider="local", model="analyst")
    image = attach(cfg)
    entered, closed = asyncio.Event(), asyncio.Event()

    class WaitingProvider:
        async def stream(self, messages, **options):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield {"type": "finish", "reason": "stop"}
            finally:
                closed.set()

    analysis = VisionAnalysis(cfg, {image["id"]},
                              provider_factory=lambda *args, **kwargs: WaitingProvider())
    task = asyncio.create_task(analysis.analyze(image["id"], "Describe the image."))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


def test_vision_route_does_not_inherit_dialogue_request_options_or_other_model_limits(tmp_path):
    cfg = config(tmp_path)
    dialogue = cfg.providers[0]
    dialogue.vision = False
    dialogue.context_window = 3000
    dialogue.parameters = {"messages": [{"role": "user", "content": "wrong"}],
                           "temperature": 1.3, "max_completion_tokens": 200,
                           "reasoning_effort": "high", "thinking": {"type": "enabled"}}
    dialogue.model_metadata["analyst"] = ModelMetadata(
        context_window=16384, vision=True, source="provider", updated_at=0,
    )
    cfg.vision = VisionModelConfig(mode="separate", provider="local", model="analyst",
                                  max_output_tokens=1500, reasoning_effort="low")
    original = copy.deepcopy(dialogue)
    profile = resolve_vision(cfg)
    assert profile.parameters == {"max_tokens": 1500, "reasoning_effort": "low"}
    assert profile.context_window is None and profile.context_window_for() == 16384
    assert profile.vision is None and profile.model_metadata["analyst"].vision is True
    assert not profile.tools and profile.presentation == "plain"
    assert dialogue == original


async def test_context_budget_trims_old_images_and_keeps_current_multimodal_message(tmp_path):
    cfg = config(tmp_path)
    cfg.memory.enabled = False
    cfg.providers[0].vision = True
    cfg.providers[0].context_window = 8000
    cfg.providers[0].parameters = {"max_tokens": 1000}
    old, current = attach(cfg), attach(cfg, png(3, 3))
    store = Store(tmp_path / "state.sqlite3")
    store.create_session("today", kind="chat")
    try:
        store.complete_turn("today", "The first image", "It is red.", metadata={"images": [old]})
        provider = Provider([reply()])
        engine = ConversationEngine(cfg, store, provider=provider)
        events = [event async for event in engine.generate("today", "The new image",
                                                           images=[current["id"]])]
        assert events[-1]["type"] == "completed", events
        parts = [part for message in provider.requests[0]
                 if isinstance(message.get("content"), list) for part in message["content"]]
        image_parts = [part for part in parts if part["type"] == "image_url"]
        assert image_parts == [ImageStore(cfg.state_dir).content_part(current["id"])]
        assert [message["content"] for message in store.list_messages("today")
                if message["role"] == "user"] == ["The first image", "The new image"]
    finally:
        store.close()


def test_image_encoding_uses_the_exact_bounded_bytes_that_were_validated(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    data = png()
    image = attach(cfg, data)
    images = ImageStore(cfg.state_dir)
    target = images.directory / (image["id"] + ".png")
    original_open = Path.open
    reads = []

    def changing_file(path, *args, **kwargs):
        if path == target:
            reads.append(path)
            # A replacement between validation and encoding must never supply
            # unchecked bytes, regardless of their size, format, or hash.
            return io.BytesIO(data if len(reads) == 1 else b"unchecked replacement")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", changing_file)
    part = images.content_part(image["id"])
    assert base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) == data
    assert len(reads) == 1


async def test_current_image_disappearing_before_dispatch_fails_without_a_saved_turn(tmp_path):
    cfg = config(tmp_path)
    cfg.memory.enabled = False
    image = attach(cfg)
    path = ImageStore(cfg.state_dir).directory / (image["id"] + ".png")

    class LosingAttachmentProvider(Provider):
        async def supports_vision(self):
            path.unlink()
            return True

    store = Store(tmp_path / "state.sqlite3")
    store.create_session("today", kind="chat")
    try:
        provider = LosingAttachmentProvider([reply()])
        engine = ConversationEngine(cfg, store, provider=provider)
        events = [event async for event in engine.generate("today", "Describe this",
                                                           images=[image["id"]])]
        assert events[-1]["type"] == "error"
        assert "image" in events[-1]["message"].lower()
        assert not provider.requests
        assert not store.list_messages("today")
    finally:
        store.close()


def test_image_wire_budget_reserves_current_images_then_prefers_recent_history(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    older = attach(cfg, png(2, 2))
    recent = attach(cfg, png(3, 3))
    current = attach(cfg, png(4, 4))
    images = ImageStore(cfg.state_dir)

    def message(image):
        return {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "eidolon-image:" + image["id"]}},
        ]}

    # Two historical records repeat the current image ID, so reserving by ID
    # alone would incorrectly keep its oldest occurrence ahead of recent data.
    messages = [message(current), message(older), message(recent), message(current)]
    original = copy.deepcopy(messages)
    monkeypatch.setattr("eidolon.images.MAX_REQUEST_IMAGE_BYTES",
                        current["size"] + recent["size"])
    expanded = images.expand_messages(messages, required_ids={current["id"]})
    assert messages == original
    assert expanded[-1]["content"][0] == images.content_part(current["id"])
    assert expanded[-2]["content"][0] == images.content_part(recent["id"])
    for historical in expanded[:2]:
        assert historical["content"][0]["type"] == "text"
        assert "payload budget" in historical["content"][0]["text"]


def test_missing_historical_image_remains_a_nonfatal_explicit_placeholder(tmp_path):
    cfg = config(tmp_path)
    old, current = attach(cfg), attach(cfg, png(3, 3))
    images = ImageStore(cfg.state_dir)
    (images.directory / (old["id"] + ".png")).unlink()
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "eidolon-image:" + image["id"]}},
    ]} for image in (old, current)]
    expanded = images.expand_messages(messages, required_ids={current["id"]})
    assert expanded[0]["content"][0] == {
        "type": "text", "text": "[Previously attached image is unavailable.]",
    }
    assert expanded[1]["content"][0] == images.content_part(current["id"])
