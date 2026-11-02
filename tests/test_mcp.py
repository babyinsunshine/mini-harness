"""真实 MCP 子进程与图；只替代外部模型。"""

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from test_agent_cli import ScriptedModel, call, consume
from test_images import PNG
from test_subagents import AgentModel

from mini_harness.agent import Harness

pytest.importorskip("fastmcp.client")


@pytest.fixture
def mcp_project(tmp_path):
    server = tmp_path / "server.py"
    server.write_text('''
import json
import os
import base64
import sys
from pathlib import Path
from typing import Annotated
from pydantic import Field
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent

root = Path(os.environ["TEST_MCP_ROOT"])
(root / "server.pid").write_text(str(os.getpid()))
with (root / "starts.txt").open("a") as stream:
    stream.write("x")
server = MCPServer("fixture")
count = 0

async def record_requests(context, call_next):
    if context.method in {"tools/list", "tools/call"}:
        with (root / "requests.jsonl").open("a") as stream:
            print(json.dumps({"method": context.method, "params": context.params}), file=stream)
    return await call_next(context)

server.middleware.append(record_requests)

@server.tool()
def increment(label: str) -> dict:
    """Increment a counter, retaining state between calls."""
    global count
    count += 1
    (root / "effect.json").write_text(json.dumps({"count": count}))
    return {"label": label, "count": count, "pid": os.getpid(),
            "has_model_key": "OPENAI_API_KEY" in os.environ}

@server.tool()
def fail() -> str:
    """Return a controlled error."""
    raise ToolError("MCP fixture failure")

@server.tool()
def mixed() -> CallToolResult:
    """Return text with additional structured data."""
    return CallToolResult(content=[TextContent(type="text", text="Saved successfully")],
                          structured_content={"record_id": 42})

@server.tool(structured_output=False)
def payload(kind: str) -> list:
    """Return a large result or image for artifact handling."""
    if kind == "long":
        return [TextContent(type="text", text="x" * 20000)]
    return [ImageContent(type="image", data=base64.b64encode(b"image-fixture").decode(),
                         mime_type="image/png")]

@server.tool()
def visual(long_text: bool = False) -> CallToolResult:
    """Return a screenshot, text and extra structured fields together."""
    return CallToolResult(content=[
        TextContent(type="text", text="Screenshot ready" + ("x" * 20000 if long_text else "")),
        ImageContent(type="image", mime_type="image/png", data=
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"),
    ], structured_content={"width": 1})

def verbose(label: Annotated[str, Field(description="detail " * 3000 + "END OF PARAMETER")]) -> str:
    return label
verbose.__doc__ = "A tool with detailed instructions. " + "detail " * 3000 + "END OF INSTRUCTIONS"
server.tool()(verbose)

@server.tool()
async def slow() -> str:
    """Wait until canceled."""
    import asyncio
    (root / "slow.started").touch()
    with (root / "slow.effects").open("a") as stream:
        stream.write("x")
    await asyncio.sleep(60)
    return "finished"

if len(sys.argv) > 1:
    server.run("streamable-http", host="127.0.0.1", port=int(sys.argv[1]))
else:
    server.run()
''')
    config = {"mcpServers": {"local": {
        "command": sys.executable,
        "args": ["server.py"],
        "env": {"TEST_MCP_ROOT": str(tmp_path)},
    }}}
    (tmp_path / ".mcp.json").write_text(json.dumps(config))
    return tmp_path


def mcp_call(call_id="increment"):
    return call("call_mcp_tool", {
        "server": "local", "tool_name": "increment", "arguments": {"label": "test"},
    }, call_id)


def received_requests(project, method):
    records = [json.loads(line) for line in (project / "requests.jsonl").read_text().splitlines()]
    return [record["params"] for record in records if record["method"] == method]


async def test_mcp_batches_full_schemas_and_reuses_catalog(mcp_project):
    model = ScriptedModel(responses=[
        call("list_mcp_tools", {"server": "local"}, "summary"),
        call("list_mcp_tools", {
            "server": "local", "tool_names": ["verbose", "increment", "verbose"],
        }, "batch"),
        call("list_mcp_tools", {"server": "local", "tool_names": []}, "empty"),
        AIMessage(content="Discovered"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json") as harness:
        await consume(harness, "Inspect these tools together")
    results = {m.tool_call_id: json.loads(m.text)
               for m in model.seen[-1] if isinstance(m, ToolMessage)}
    batch = json.loads(Path(results["batch"]["output_path"]).read_text())
    assert [item["name"] for item in batch] == ["verbose", "increment"]
    assert batch[0]["description"].endswith("END OF INSTRUCTIONS")
    assert batch[0]["input_schema"]["properties"]["label"]["description"].endswith(
        "END OF PARAMETER"
    )
    assert batch[1]["input_schema"]["required"] == ["label"]
    assert json.loads(results["empty"]["output"]) == []
    assert len(received_requests(mcp_project, "tools/list")) == 1


@pytest.mark.parametrize("arguments", [{}, {"label": 42}])
async def test_mcp_invalid_arguments_stay_local_and_model_can_correct(mcp_project, arguments):
    model = ScriptedModel(responses=[
        call("call_mcp_tool", {
            "server": "local", "tool_name": "increment", "arguments": arguments,
        }, "invalid"),
        call("call_mcp_tool", {
            "server": "local", "tool_name": "increment", "arguments": {"label": "fixed"},
        }, "corrected"),
        AIMessage(content="Corrected"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json", full_access=True) as harness:
        await consume(harness, "Call and correct the arguments if needed")
    results = {m.tool_call_id: m for m in model.seen[-1] if isinstance(m, ToolMessage)}
    error = results["invalid"]
    assert error.status == "error" and "not executed" in error.text
    feedback = json.loads(error.text.split("\n", 1)[1])
    details = json.loads(feedback["output"])
    assert details["input_schema"]["properties"]["label"]["type"] == "string"
    assert "label" in details["error"] or details["path"] == ["label"]
    assert results["corrected"].status == "success"
    requests = received_requests(mcp_project, "tools/call")
    assert [(r["name"], r["arguments"]) for r in requests] == [
        ("increment", {"label": "fixed"}),
    ]
    assert json.loads((mcp_project / "effect.json").read_text())["count"] == 1
    assert len(received_requests(mcp_project, "tools/list")) == 1


async def test_mcp_large_validation_schema_is_saved_without_remote_call(mcp_project):
    model = ScriptedModel(responses=[
        call("call_mcp_tool", {
            "server": "local", "tool_name": "verbose", "arguments": {},
        }, "invalid"),
        AIMessage(content="Read the error"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json", full_access=True) as harness:
        await consume(harness, "Call with missing parameters")
    error = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
    assert error.status == "error" and "not executed" in error.text
    prepared = json.loads(error.text.split("\n", 1)[1])
    assert prepared["truncated"] and len(prepared["preview"]) <= 2000
    details = json.loads(Path(prepared["output_path"]).read_text())
    assert details["input_schema"]["properties"]["label"]["description"].endswith(
        "END OF PARAMETER"
    )
    assert received_requests(mcp_project, "tools/call") == []


async def test_mcp_unknown_batch_name_returns_error_instead_of_partial_catalog(mcp_project):
    model = ScriptedModel(responses=[
        call("list_mcp_tools", {
            "server": "local", "tool_names": ["increment", "missing"],
        }, "unknown"),
        AIMessage(content="Checked"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json") as harness:
        await consume(harness, "Find the requested tools")
    result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
    assert result.status == "error" and "missing" in result.text


async def test_mcp_catalog_cache_is_separate_for_each_server(mcp_project):
    other = mcp_project / "other"
    other.mkdir()
    source = (mcp_project / "server.py").read_text()
    (other / "server.py").write_text(
        source.replace("increment(label: str)", "increment(label: int)")
    )
    config_path = mcp_project / ".mcp.json"
    config = json.loads(config_path.read_text())
    config["mcpServers"]["other"] = {
        "command": sys.executable, "args": ["server.py"], "cwd": "other",
        "env": {"TEST_MCP_ROOT": str(other)},
    }
    config_path.write_text(json.dumps(config))
    model = ScriptedModel(responses=[
        *[call("list_mcp_tools", {"server": server, "tool_names": ["increment"]}, server)
          for server in ("local", "other")],
        mcp_call("local-effect"),
        call("call_mcp_tool", {
            "server": "other", "tool_name": "increment", "arguments": {"label": 7},
        }, "other-effect"),
        AIMessage(content="Both called"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=config_path, full_access=True) as harness:
        await consume(harness, "Discover and call both servers")
    results = {m.tool_call_id: m for m in model.seen[-1] if isinstance(m, ToolMessage)}
    for server, parameter_type in (("local", "string"), ("other", "integer")):
        schema = json.loads(json.loads(results[server].text)["output"])[0]["input_schema"]
        assert schema["properties"]["label"]["type"] == parameter_type
        assert results[f"{server}-effect"].status == "success"
    assert len(received_requests(mcp_project, "tools/list")) == 1
    assert len(received_requests(other, "tools/list")) == 1


async def test_mcp_reopened_session_fetches_new_schema(mcp_project):
    directory = mcp_project / "session"
    config_path = mcp_project / ".mcp.json"
    model = ScriptedModel(responses=[
        call("list_mcp_tools", {"server": "local", "tool_names": ["increment"]}, "before"),
        AIMessage(content="Saved"),
    ])
    async with Harness.open(mcp_project, directory, model, mcp_config=config_path) as harness:
        await consume(harness, "Inspect the current schema")
    source = mcp_project / "server.py"
    source.write_text(source.read_text().replace("increment(label: str)", "increment(label: int)"))
    model = ScriptedModel(responses=[
        call("list_mcp_tools", {"server": "local", "tool_names": ["increment"]}, "after"),
        call("call_mcp_tool", {
            "server": "local", "tool_name": "increment", "arguments": {"label": 7},
        }, "effect"),
        AIMessage(content="Resumed"),
    ])
    async with Harness.open(mcp_project, directory, model, mcp_config=config_path,
                            full_access=True) as harness:
        await consume(harness, "Continue with the updated server")
    results = {m.tool_call_id: m for m in model.seen[-1] if isinstance(m, ToolMessage)}
    schema = json.loads(json.loads(results["after"].text)["output"])[0]["input_schema"]
    assert schema["properties"]["label"]["type"] == "integer"
    assert results["effect"].status == "success"
    assert len(received_requests(mcp_project, "tools/list")) == 2


@pytest.mark.parametrize("sandbox", [False, True])
async def test_mcp_discovers_schemas_keeps_session_and_cleans_up(mcp_project, monkeypatch, sandbox):
    if sandbox:
        monkeypatch.setattr("mini_harness.sandbox.Sandbox.require_runtime", lambda self: "bwrap")
    monkeypatch.setenv("OPENAI_API_KEY", "test-value-must-not-reach-mcp")
    model = ScriptedModel(responses=[
        call("list_mcp_tools", {}, "servers"),
        call("list_mcp_tools", {"server": "local", "tool_names": ["increment"]}, "catalog"),
        mcp_call("first"), mcp_call("second"), AIMessage(content="Done"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json",
                            full_access=True, sandbox=sandbox) as harness:
        assert await consume(harness, "Discover and call MCP") == "Done"
        messages = model.seen[-1]
        results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
        assert all(result.status == "success" for result in results.values())
        assert "local" in results["servers"].text
        catalog = json.loads(json.loads(results["catalog"].text)["output"])
        assert [tool["name"] for tool in catalog] == ["increment"]
        increment = next(tool for tool in catalog if tool["name"] == "increment")
        assert increment["input_schema"]["required"] == ["label"]
        first = json.loads(json.loads(results["first"].text)["output"])
        second = json.loads(json.loads(results["second"].text)["output"])
        assert first["count"] == 1 and second["count"] == 2
        assert first["pid"] == second["pid"]
        assert not first["has_model_key"]
        pid = first["pid"]
        assert os.kill(pid, 0) is None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("allowed", [False, True])
async def test_mcp_call_respects_existing_approval(mcp_project, allowed):
    async def approve(tool_call, reason):
        return allowed

    model = ScriptedModel(responses=[mcp_call(), AIMessage(content="Checked")])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json",
                            approve=approve) as harness:
        await consume(harness, "Call MCP")
    assert (mcp_project / "effect.json").exists() == allowed
    result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
    assert result.status == ("success" if allowed else "error")


async def test_mcp_error_returns_to_model(mcp_project):
    model = ScriptedModel(responses=[
        call("call_mcp_tool", {
            "server": "local", "tool_name": "fail", "arguments": {},
        }, "fail"),
        AIMessage(content="Handled"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json",
                            full_access=True) as harness:
        assert await consume(harness, "Handle the failing tool") == "Handled"
    result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
    assert result.status == "error" and "MCP fixture failure" in result.text
    assert "input_schema" not in result.text and "not executed" not in result.text
    assert len(received_requests(mcp_project, "tools/call")) == 1


async def test_incomplete_mcp_call_is_reported_without_crashing_cli(mcp_project, capfd):
    from mini_harness.cli.display import display_events

    model = ScriptedModel(responses=[
        call("call_mcp_tool", {"server": "local", "tool_name": "increment"}, "incomplete"),
        AIMessage(content="Handled"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json",
                            full_access=True) as harness:
        await display_events(harness, "Call with missing arguments")
    result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
    assert result.status == "error" and "arguments" in result.text
    assert "Handled" in capfd.readouterr().out
    assert not (mcp_project / "effect.json").exists()


async def test_mcp_preserves_text_and_structured_content(mcp_project):
    model = ScriptedModel(responses=[
        call("call_mcp_tool", {
            "server": "local", "tool_name": "mixed", "arguments": {},
        }, "mixed"),
        AIMessage(content="Read result"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json",
                            full_access=True) as harness:
        await consume(harness, "Read text and structured data")
    result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
    assert result.status == "success"
    output = json.loads(result.text)["output"]
    assert "Saved successfully" in output
    assert json.loads(output)["structured_content"] == {"record_id": 42}


async def test_mcp_catalog_does_not_require_approval(mcp_project):
    model = ScriptedModel(responses=[
        call("list_mcp_tools", {"server": "local"}, "catalog"), AIMessage(content="Found tools"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json") as harness:
        await consume(harness, "Inspect MCP")
    result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
    assert result.status == "success" and "increment" in result.text


async def test_mcp_catalog_stays_small_and_full_schema_is_available_on_demand(mcp_project):
    model = ScriptedModel(responses=[
        call("list_mcp_tools", {"server": "local"}, "catalog"),
        call("list_mcp_tools", {"server": "local", "tool_names": ["verbose"]}, "schema"),
        AIMessage(content="Discovered"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json") as harness:
        await consume(harness, "Discover the server's tools")
    results = {m.tool_call_id: json.loads(m.text)
               for m in model.seen[-1] if isinstance(m, ToolMessage)}
    catalog = json.loads(results["catalog"]["output"])
    assert {item["name"] for item in catalog} >= {"increment", "visual", "verbose"}
    assert all("input_schema" not in item for item in catalog)
    schema = json.loads(Path(results["schema"]["output_path"]).read_text())[0]
    assert schema["name"] == "verbose"
    assert schema["description"].endswith("END OF INSTRUCTIONS")
    assert schema["input_schema"]["required"] == ["label"]


async def test_child_reuses_parent_mcp_and_leaves_connection_open(mcp_project):
    def reply(child, messages):
        label = "child" if child else (
            "again" if any(m.content == "Again" for m in messages) else "parent"
        )
        if not any(isinstance(m, ToolMessage) and m.tool_call_id == label for m in messages):
            return mcp_call(label)
        return AIMessage(content="Done")

    model = AgentModel(handler=reply)
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json",
                            full_access=True) as harness:
        await consume(harness, "Start")
        child = harness.subagents.spawn("Use MCP", timeout=10)
        result = await harness.subagents.wait(child["agent_id"])
        assert result["status"] == "waiting", result
        assert json.loads((mcp_project / "effect.json").read_text())["count"] == 2
        await consume(harness, "Again")
        assert json.loads((mcp_project / "effect.json").read_text())["count"] == 3
        assert (mcp_project / "starts.txt").read_text() == "x"


async def test_mcp_large_text_and_images_are_saved_as_artifacts(mcp_project):
    model = ScriptedModel(responses=[
        *[call("call_mcp_tool", {
            "server": "local", "tool_name": "payload", "arguments": {"kind": kind},
        }, kind) for kind in ("long", "image")],
        AIMessage(content="Saved"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json",
                            full_access=True) as harness:
        await consume(harness, "Read payloads")
        results = {m.tool_call_id: json.loads(m.text)
                   for m in model.seen[-1] if isinstance(m, ToolMessage)}
        assert Path(results["long"]["output_path"]).read_text() == "x" * 20000
        assert len(results["long"]["preview"]) < 20000
        image = next(harness.session.artifacts.glob("*.png"))
        assert image.read_bytes() == b"image-fixture"
        assert str(image) in results["image"]["output"]
        assert not any(isinstance(m, HumanMessage) and isinstance(m.content, list)
                       for m in model.seen[-1])


@pytest.mark.parametrize("long_text", [False, True])
async def test_mcp_inline_images_reach_model_without_losing_text_or_data(mcp_project, long_text):
    from mini_harness.tools.images import read_image

    model = ScriptedModel(responses=[
        call("call_mcp_tool", {
            "server": "local", "tool_name": "visual", "arguments": {"long_text": long_text},
            "include_images": True,
        }, "screenshot"),
        AIMessage(content="Observed"),
    ])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=mcp_project / ".mcp.json",
                            full_access=True) as harness:
        await consume(harness, "Inspect the screenshot")
        messages = model.seen[-1]
        images = [block for message in messages
                  if isinstance(message, HumanMessage) and isinstance(message.content, list)
                  for block in message.content if block.get("type") == "image"]
        assert len(images) == 1
        assert images[0]["mime_type"] == "image/png"
        assert base64.b64decode(images[0]["base64"]) == PNG
        result = next(m for m in messages if isinstance(m, ToolMessage))
        assert result.status == "success"
        prepared = json.loads(result.text)
        output = Path(prepared["output_path"]).read_text() if long_text else prepared["output"]
        assert "Screenshot ready" + ("x" * 20000 if long_text else "") in output
        assert json.loads(output)["structured_content"] == {"width": 1}
        saved = next(harness.session.artifacts.glob("*.png"))
        assert saved.read_bytes() == PNG and str(saved) in output
        read_image(harness.session, str(saved))
        assert list(harness.session.artifacts.glob("*.png")) == [saved]


async def test_streamable_http_uses_the_same_tool_interface(mcp_project):
    environment = dict(os.environ, TEST_MCP_ROOT=str(mcp_project))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (mcp_project / "http.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(mcp_project / "server.py"), str(port)],
            env=environment, stdout=log, stderr=log,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    assert process.poll() is None and time.monotonic() < deadline
                    time.sleep(0.05)
            (mcp_project / ".mcp.json").write_text(json.dumps({"mcpServers": {
                "local": {"url": f"http://127.0.0.1:{port}/mcp"},
            }}))
            model = ScriptedModel(responses=[mcp_call(), AIMessage(content="HTTP works")])
            async with Harness.open(mcp_project, mcp_project / "session", model,
                                    mcp_config=mcp_project / ".mcp.json",
                                    full_access=True) as harness:
                assert await consume(harness, "Call HTTP MCP") == "HTTP works"
            assert json.loads((mcp_project / "effect.json").read_text())["count"] == 1
            assert process.poll() is None  # HTTP 服务由外部启动，关闭客户端不关闭服务。
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


async def test_explicit_config_resolves_cwd_relative_to_its_directory(mcp_project):
    config_path = mcp_project / ".mcp.json"
    config = json.loads(config_path.read_text())
    config["mcpServers"]["local"]["cwd"] = "."
    config_path.write_text(json.dumps(config))
    model = ScriptedModel(responses=[mcp_call(), AIMessage(content="Done")])
    async with Harness.open(mcp_project, mcp_project / "session", model,
                            mcp_config=config_path, full_access=True) as harness:
        await consume(harness, "Use the explicit config")
    assert json.loads((mcp_project / "effect.json").read_text())["count"] == 1


async def test_canceling_mcp_call_records_interruption_and_cleans_server(mcp_project):
    model = ScriptedModel(responses=[call("call_mcp_tool", {
        "server": "local", "tool_name": "slow", "arguments": {},
    }, "slow"), AIMessage(content="Inspect remote state before retrying")])
    async with asyncio.timeout(10):
        async with Harness.open(mcp_project, mcp_project / "session", model,
                                mcp_config=mcp_project / ".mcp.json",
                                full_access=True) as harness:
            running = asyncio.create_task(consume(harness, "Start slow MCP"))
            try:
                while not (mcp_project / "slow.started").exists():
                    await asyncio.sleep(0.01)
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            saved = harness.session.get_execution("slow")
            assert saved[0] == "error" and "interrupted" in saved[1]["data"]["content"].lower()
            snapshot = await harness.graph.aget_state(harness.config)
            result = next(m for m in snapshot.values["messages"] if isinstance(m, ToolMessage))
            assert result.tool_call_id == "slow" and "not confirmed" in result.content
            assert snapshot.next == ()
            assert (mcp_project / "slow.effects").read_text() == "x"
            await consume(harness, "Inspect the operation, do not repeat it")
            assert (mcp_project / "slow.effects").read_text() == "x"
            pid = int((mcp_project / "server.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_cli_mcp_config_is_restored_on_resume(mcp_project):
    from mini_harness.session import Session

    sessions = mcp_project / "sessions"
    config = mcp_project / ".mcp.json"
    environment = dict(os.environ, OPENAI_API_KEY="local-startup-test-not-a-secret",
                       LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
    command = [sys.executable, "-m", "mini_harness", "--session-dir", str(sessions)]
    first = subprocess.run(
        [*command, "--cwd", str(mcp_project), "--mcp-config", str(config)],
        input="/stats\n/exit\n", env=environment, text=True, capture_output=True, timeout=15,
    )
    assert first.returncode == 0, first.stderr
    directory = next(sessions.iterdir())
    with Session(directory) as session:
        assert session.get("config")["mcp_config"] == str(config)
    pid = int((mcp_project / "server.pid").read_text())
    resumed = subprocess.run(
        [*command, "--resume", directory.name],
        input="/stats\n/exit\n", env=environment, text=True, capture_output=True, timeout=15,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert (mcp_project / "starts.txt").read_text() == "xx"
    assert int((mcp_project / "server.pid").read_text()) != pid


async def test_mcp_server_only_starts_when_config_is_selected(mcp_project):
    model = ScriptedModel(responses=[AIMessage(content="Ready")])
    async with Harness.open(mcp_project, mcp_project / "session", model) as harness:
        assert await consume(harness, "Hello") == "Ready"
        assert not (mcp_project / "starts.txt").exists()
