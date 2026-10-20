import base64
import copy
import json
import struct
import zlib

import pytest

from eidolon.config import VisionModelConfig
from eidolon.engine import ConversationEngine
from eidolon.images import ImageStore, MAX_UPLOAD_BYTES, png_dimensions
from eidolon.providers import ProviderError
from eidolon.storage import Store
from eidolon.tools import ToolRegistry
from eidolon.tools.vision import VisionAnalysis, resolve_vision
from test_engine import Provider, config
from test_service import room


def png(width=2, height=2, *, pixels=None):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels or (b"\0" + b"\xff\0\0" * width) * height))
            + chunk(b"IEND", b""))


def attach(cfg, data=None):
    return ImageStore(cfg.state_dir).upload(base64.b64encode(data or png()).decode("ascii"))


def reply(text="It is red."):
    return [{"type": "token", "text": text}, {"type": "finish", "reason": "stop"}]


def tool(image_id, question="Read the label and explain the layout.", call_id="image1"):
    return [{"type": "tool_call", "call": {
        "id": call_id, "type": "function", "function": {
            "name": "vision_analyze", "arguments": json.dumps({
                "image_id": image_id, "question": question,
            }),
        },
    }}, {"type": "finish", "reason": "tool_calls"}]


@pytest.fixture
def conversation(tmp_path):
    cfg = config(tmp_path)
    cfg.memory.enabled = False
    store = Store(tmp_path / "state.sqlite3")
    store.create_session("today", kind="chat")
    yield cfg, store
    store.close()


def test_private_images_validate_deduplicate_and_preserve_pixels(tmp_path):
    images = ImageStore(tmp_path)
    data = png()
    image = images.upload(base64.b64encode(data).decode())
    assert image["width"] == 2 and image["height"] == 2
    assert images.upload(base64.b64encode(data).decode())["id"] == image["id"]
    assert images.directory.stat().st_mode & 0o777 == 0o700
    saved = images.directory / (image["id"] + ".png")
    assert saved.stat().st_mode & 0o777 == 0o600
    part = images.content_part(image["id"])
    assert base64.b64decode(part["image_url"]["url"].split(",")[1]) == data
    with pytest.raises(ValueError, match="complete PNG"):
        png_dimensions(data[:-12])
    with pytest.raises(ValueError, match="complete PNG"):
        png_dimensions(data[:-8] + b"broken!!")
    with pytest.raises(ValueError, match="50 megapixel"):
        png_dimensions(png(0, 2))
    with pytest.raises(ValueError, match="Invalid clipboard"):
        images.upload("not base64")
    with pytest.raises(ValueError, match="at most four"):
        images.resolve([image["id"]] * 5)
    with pytest.raises(ValueError, match="valid clipboard"):
        images.resolve([{}])
    with pytest.raises(ValueError, match="attached"):
        images.load("../../private")
    saved.unlink()
    saved.symlink_to(tmp_path / "not-an-attachment")
    with pytest.raises(ValueError, match="symbolic"):
        images.load(image["id"])


def test_separate_vision_honors_connection_stream_usage_opt_out(tmp_path):
    from eidolon.providers import OpenAIProvider

    cfg = config(tmp_path)
    cfg.vision = VisionModelConfig(mode="separate", provider="local", model="analyst")
    connection = cfg.selected_provider()
    connection.parameters = {"temperature": .9, "stream_options": {"include_usage": False}}
    profile = resolve_vision(cfg)
    assert OpenAIProvider(profile)._payload([], None, "plain")["stream_options"] == {
        "include_usage": False}
    assert "temperature" not in profile.parameters
    profile.parameters["stream_options"]["include_usage"] = True
    assert connection.parameters["stream_options"] == {"include_usage": False}


@pytest.mark.parametrize("post_history", ["system", "user"])
async def test_native_image_turn_and_followup_keep_structured_content(conversation, post_history):
    cfg, store = conversation
    cfg.providers[0].vision = True
    cfg.providers[0].post_history = post_history
    image = attach(cfg)
    provider = Provider([reply(), reply("The same image is red.")])
    engine = ConversationEngine(cfg, store, provider=provider)
    result = [event async for event in engine.generate("today", "", images=[image["id"]])]
    assert result[-1]["type"] == "completed", result
    assert result[-1]["metadata"]["image_route"] == "dialogue"
    current = next(message for message in provider.requests[0] if message["role"] == "user")
    assert any(part["type"] == "image_url" for part in current["content"])
    if post_history == "user":
        assert "Post-history" in current["content"][-1]["text"]
    messages = store.list_messages("today")
    assert messages[0]["content"] == "" and messages[0]["images"] == [image]
    assert "images" not in messages[1]
    followup = [event async for event in engine.generate("today", "What color was it?")]
    assert followup[-1]["type"] == "completed", followup
    assert "data:image/png;base64," in json.dumps(provider.requests[1])
    assert "base64" not in json.dumps(store.pending_outbox())


@pytest.mark.parametrize("support", [False, None])
async def test_unconfirmed_native_vision_rejects_without_sending(conversation, support):
    cfg, store = conversation
    cfg.providers[0].vision = support
    image = attach(cfg)
    provider = Provider([reply()])
    engine = ConversationEngine(cfg, store, provider=provider)
    events = [event async for event in engine.generate("today", "Describe", images=[image["id"]])]
    assert events[-1]["type"] == "error"
    assert "/preferences" in events[-1]["message"]
    assert not provider.requests
    assert not store.list_messages("today")


async def test_separate_vision_receives_dialogue_questions_and_never_leaks_pixels(conversation):
    cfg, store = conversation
    cfg.providers[0].vision = True
    cfg.vision = VisionModelConfig(mode="separate", provider="local", model="image-analyst")
    image = attach(cfg)
    question = "Read only the bottom-left label."
    dialogue = Provider([tool(image["id"], question),
                         tool(image["id"], "What is above that label?", "image2"), reply()])
    visual = Provider([reply("The label reads RED."), reply("A red square is above it.")])
    profiles = []

    def factory(profile, **options):
        profiles.append((copy.deepcopy(profile), options))
        return visual

    registry = ToolRegistry(cfg)
    try:
        engine = ConversationEngine(cfg, store, tools=registry, provider=dialogue,
                                    provider_factory=factory)
        events = [event async for event in engine.generate("today", "What is this?",
                                                          images=[image["id"]])]
        assert events[-1]["type"] == "completed", events
        sent = json.dumps(dialogue.requests)
        assert "data:image" not in sent and "base64" not in sent
        assert image["id"] in sent and "vision_analyze" in sent
        assert "The label reads RED." in sent
        assert visual.requests[0][1]["content"][0]["type"] == "image_url"
        assert visual.requests[0][1]["content"][1]["text"] == question
        assert visual.requests[0][1]["content"][0] == visual.requests[1][1]["content"][0]
        assert profiles[0][1]["stream_usage"] is True
        assert "data:image/png;base64," in json.dumps(visual.requests)
        assert profiles[0][0].model == "image-analyst"
        assert profiles[0][0].vision is None  # dialogue override does not follow another model
        assert profiles[0][0].tools is False
        assert events[-1]["metadata"]["image_route"] == "vision_model"
        assert "base64" not in json.dumps(store.list_messages("today"))
        measurements = store.list_request_metrics("today")
        assert [item["role"] for item in reversed(measurements)] == [
            "dialogue", "vision", "dialogue", "vision", "dialogue"]
        assert all(item["outcome"] == "completed" for item in measurements)
        assert "base64" not in json.dumps(measurements)
    finally:
        await registry.close()


async def test_vision_analysis_is_scoped_and_empty_responses_retry_once(conversation):
    cfg, _ = conversation
    cfg.vision = VisionModelConfig(mode="separate", provider="local", model="analyst")
    image = attach(cfg)
    provider = Provider([[{"type": "finish", "reason": "stop"}], reply()])
    analysis = VisionAnalysis(cfg, {image["id"]}, provider_factory=lambda *a, **kw: provider)
    with pytest.raises(ValueError, match="attached to this conversation"):
        await analysis.analyze("a" * 64, "Describe")
    with pytest.raises(ValueError, match="1–8000"):
        await analysis.analyze(image["id"], " ")
    assert not provider.requests
    cfg.vision.model = "changed-after-snapshot"
    assert (await analysis.analyze(image["id"], "Describe"))["model"] == "analyst"
    assert len(provider.requests) == 2
    empty = Provider([[{"type": "finish", "reason": "stop"}]] * 2)
    analysis = VisionAnalysis(cfg, {image["id"]}, provider_factory=lambda *a, **kw: empty)
    with pytest.raises(ProviderError, match="no visible analysis"):
        await analysis.analyze(image["id"], "Describe")


async def test_separate_route_requires_tool_permissions(conversation):
    cfg, store = conversation
    cfg.vision = VisionModelConfig(mode="separate", provider="local", model="analyst")
    image = attach(cfg)
    provider = Provider([reply()])
    registry = ToolRegistry(cfg)
    try:
        engine = ConversationEngine(cfg, store, tools=registry, provider=provider)
        result = [event async for event in engine.generate(
            "today", "Describe", images=[image["id"]], permitted_tools=["read_file"],
        )]
        assert result[-1]["type"] == "error"
        assert "vision_analyze tool" in result[-1]["message"]
        cfg.providers[0].tools = False
        result = [event async for event in engine.generate("today", "Describe", images=[image["id"]])]
        assert "Enable tool calling" in result[-1]["message"]
        assert not provider.requests
    finally:
        await registry.close()


async def test_socket_image_upload_and_image_only_generation(tmp_path, monkeypatch):
    monkeypatch.setattr("eidolon.service.runtime.RoomService._schedule_metadata_refresh", lambda *a: None)
    async with room(tmp_path, monkeypatch) as (service, client, provider, engines):
        from eidolon.config import save_config

        service.config.selected_provider().vision = True
        save_config(service.config, service.configuration)
        image = await client.request("upload_image", data_base64=base64.b64encode(png()).decode())
        events = [event async for event in client.stream("generate", text="", images=[image["id"]])]
        assert events[-1]["type"] == "completed", events
        assert "data:image/png;base64," in json.dumps(provider.requests)
        history = await client.request("history", session_id=service.current_session["id"])
        assert history["messages"][0]["images"] == [image]
        assert not history["messages"][0]["content"]
        with pytest.raises(RuntimeError, match="complete PNG"):
            await client.request("upload_image", data_base64=base64.b64encode(png()[:-12]).decode())


async def test_large_upload_uses_bounded_socket_and_atomic_vision_settings(tmp_path, monkeypatch):
    import asyncio
    import random

    from eidolon.service.client import RoomClient

    monkeypatch.setattr("eidolon.service.runtime.RoomService._schedule_metadata_refresh", lambda *a: None)
    async with room(tmp_path, monkeypatch) as (service, client, provider, engines):
        socket = client.socket_path.parent / "images.sock"
        server = await asyncio.start_unix_server(service.connection, path=str(socket),
                                                limit=MAX_UPLOAD_BYTES + 1)
        async with server:
            large = RoomClient(socket)
            data = png(256, 256, pixels=b"".join(
                b"\0" + random.Random(row).randbytes(256 * 3) for row in range(256)
            ))
            assert len(data) > 100_000
            image = await large.request("upload_image", data_base64=base64.b64encode(data).decode())
            assert image["width"] == image["height"] == 256
        initial = await client.request("overview")
        with pytest.raises(RuntimeError, match="Vision connection"):
            await client.request("save_preferences_with_vision", preferences={"follow_replies": False},
                                 vision={"mode": "separate", "provider": "missing", "model": "x"})
        assert (await client.request("overview"))["config"] == initial["config"]
        saved = await client.request("save_preferences_with_vision", preferences={},
                                     vision={"mode": "separate", "provider": "second", "model": "analyst"})
        assert saved["provider"] == "first" and saved["vision"]["model"] == "analyst"
        assert resolve_vision(service.config).name == "second"
