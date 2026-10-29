"""真实文件、权限、图与适配器；只替代外部模型响应。"""

import base64
import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from test_agent_cli import ScriptedModel, call, consume

from mini_harness.agent import Harness
from mini_harness.responses import request_model_reply
from mini_harness.session import Session
from mini_harness.tools.permissions import classify

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


@pytest.mark.parametrize("data,mime,suffix", [
    (PNG, "image/png", ".png"),
    (b"\xff\xd8\xff\xe0JPEG signature fixture", "image/jpeg", ".jpg"),
])
def test_read_image_identifies_content_and_saves_original_bytes(tmp_path, data, mime, suffix):
    from mini_harness.tools.images import read_image

    source = tmp_path / "wrong-extension.bin"
    source.write_bytes(data)
    with Session(tmp_path / "s", tmp_path) as session:
        content = read_image(session, str(source))
        image = next(block for block in content if block["type"] == "image")
        assert image["mime_type"] == mime
        assert base64.b64decode(image["base64"]) == data
        saved = next(session.artifacts.glob(f"*{suffix}"))
        assert saved.read_bytes() == data and str(saved) in content[0]["text"]
        source.unlink()
        assert read_image(session, str(saved)) == content


@pytest.mark.parametrize("data", [b"", b"this is not a PNG"])
def test_read_image_rejects_unsupported_content_without_artifacts(tmp_path, data):
    from mini_harness.tools.images import read_image

    (tmp_path / "fake.png").write_bytes(data)
    with Session(tmp_path / "s", tmp_path) as session:
        with pytest.raises(ValueError, match="PNG|JPEG"):
            read_image(session, "fake.png")
        assert not list(session.artifacts.iterdir())


def test_read_image_rejects_over_five_mib_without_artifacts(tmp_path):
    from mini_harness.tools.images import read_image

    with (tmp_path / "large.png").open("wb") as stream:
        stream.write(PNG)
        stream.seek(5 * 1024 * 1024)
        stream.write(b"x")
    with Session(tmp_path / "s", tmp_path) as session:
        with pytest.raises(ValueError, match="5 MiB"):
            read_image(session, "large.png")
        assert not list(session.artifacts.iterdir())


def test_image_permissions_follow_file_reads_and_sandbox_denials(tmp_path):
    from mini_harness.sandbox import Sandbox
    from mini_harness.tools.images import read_image

    project = tmp_path / "project"
    project.mkdir()
    secret = project / "secret.png"
    secret.write_bytes(PNG)
    with Session(tmp_path / "s", project) as session:
        request = {"name": "read_image", "args": {"path": str(secret)}}
        assert classify(request, session)[0] == "allow"
        request["args"]["path"] = str(tmp_path / "outside.png")
        assert classify(request, session)[0] == "ask"
        session.sandbox = Sandbox.load(project)
        session.sandbox.deny_paths = (secret,)
        request["args"]["path"] = str(secret)
        assert classify(request, session)[0] == "deny"
        with pytest.raises(PermissionError):
            read_image(session, str(secret))
        assert not list(session.artifacts.iterdir())


async def test_image_tool_reaches_model_and_survives_session_restore(tmp_path):
    source = tmp_path / "screen.png"
    source.write_bytes(PNG)
    directory = tmp_path / "s"
    model = ScriptedModel(responses=[
        call("read_image", {"path": "screen.png"}, "image"), AIMessage(content="Observed"),
    ])
    async with Harness.open(tmp_path, directory, model) as harness:
        assert await consume(harness, "Inspect screen.png") == "Observed"
        snapshot = await harness.graph.aget_state(harness.config)
        result = next(m for m in snapshot.values["messages"] if isinstance(m, ToolMessage))
        assert result.status == "success" and result.tool_call_id == "image"
        assert isinstance(result.content, list)
        assert base64.b64decode(result.content[1]["base64"]) == PNG
        saved = next(harness.session.artifacts.glob("*.png"))
        assert saved.read_bytes() == PNG
    source.write_bytes(b"source changed after inspection")
    model = ScriptedModel(responses=[AIMessage(content="Continued")])
    async with Harness.open(tmp_path, directory, model) as harness:
        await consume(harness, "Continue from the same image")
    images = [block for message in model.seen[-1]
              if isinstance(message, HumanMessage) and isinstance(message.content, list)
              for block in message.content if block.get("type") == "image"]
    assert len(images) == 1 and base64.b64decode(images[0]["base64"]) == PNG


async def test_denied_image_is_not_loaded_even_with_full_access(tmp_path):
    from mini_harness.sandbox import Sandbox

    (tmp_path / "secret.png").write_bytes(PNG)
    model = ScriptedModel(responses=[
        call("read_image", {"path": "secret.png"}, "denied"), AIMessage(content="Denied"),
    ])
    async with Harness.open(tmp_path, tmp_path / "s", model, full_access=True) as harness:
        harness.session.sandbox = Sandbox.load(tmp_path)
        harness.session.sandbox.deny_paths = (tmp_path / "secret.png",)
        await consume(harness, "Read the blocked image")
        result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
        assert result.status == "error" and "denies access" in result.text
        assert not list(harness.session.artifacts.glob("*.png"))


async def test_request_sends_user_image_after_all_tool_results_with_real_adapter():
    image = {"type": "image", "mime_type": "image/png",
             "base64": base64.b64encode(PNG).decode()}
    messages = [
        HumanMessage(content="Inspect image and logs"),
        AIMessage(content="", tool_calls=[
            {"name": "read_image", "args": {"path": "screen.png"}, "id": "image"},
            {"name": "read_file", "args": {"path": "log.txt"}, "id": "log"},
        ]),
        ToolMessage(content=[{"type": "text", "text": "Saved: screen.png"}, image],
                    name="read_image", tool_call_id="image"),
        ToolMessage(content="Log text", name="read_file", tool_call_id="log"),
    ]
    requests = []

    def endpoint(request):
        requests.append(json.loads(request.content))
        chunk = {"id": "local", "object": "chat.completion.chunk", "created": 1,
                 "model": "local-vision", "choices": [
                     {"index": 0, "delta": {"content": "Observed"}, "finish_reason": "stop"},
                 ]}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        model = ChatOpenAI(model="local-vision", api_key="local-test-not-a-secret",
                           base_url="https://local.invalid/v1", http_async_client=client,
                           max_retries=0, stream_usage=False, http_socket_options=())
        reply = await request_model_reply(model, messages)
    assert reply.text == "Observed"
    sent = requests[0]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "tool", "user"]
    assert sent[2]["tool_call_id"] == "image" and sent[3]["tool_call_id"] == "log"
    assert "Saved: screen.png" in str(sent[2]["content"])
    image_url = next(block["image_url"]["url"] for block in sent[-1]["content"]
                     if block["type"] == "image_url")
    assert image_url == "data:image/png;base64," + image["base64"]
    assert messages[2].content[1] == image
