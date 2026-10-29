"""真实图与文件，只有外部模型用固定回复替代。"""


import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from test_agent_cli import ScriptedModel, call, consume

from mini_harness.agent import Harness
from mini_harness.cli.display import display_events
from mini_harness.context.compact import estimate_tokens
from mini_harness.session import Session


async def test_default_budget_completes_forty_tool_rounds(tmp_path):
    (tmp_path / "sample.txt").write_text("small fixture\n")
    model = ScriptedModel(responses=[
        *[call("read_file", {"path": "sample.txt"}, f"read-{i}") for i in range(40)],
        AIMessage(content="Finished forty rounds"),
    ])
    async with Harness.open(tmp_path, tmp_path / "s", model) as harness:
        assert await consume(harness, "Read and verify repeatedly") == "Finished forty rounds"
        assert len(model.seen) == 41


async def test_model_can_wait_for_own_background_task_without_approval(tmp_path):
    model = ScriptedModel()
    async with Harness.open(tmp_path, tmp_path / "s", model) as harness:
        started = await harness.processes.run("cat", run_in_background=True)
        task_id = started["task_id"]
        await harness.processes.send_input(task_id, "task result", eof=True)
        model.responses.extend([
            call("wait_task", {"task_id": task_id, "timeout": 2}, "wait"),
            AIMessage(content="Checked task result"),
        ])
        assert await consume(harness, "Wait for the task") == "Checked task result"
        result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
        assert result.status == "success" and "task result" in result.text
        assert harness.session.get_execution("wait")[0] == "done"


async def test_model_usage_calibrates_input_and_is_visible_in_events(tmp_path):
    usage = {"input_tokens": 9000, "output_tokens": 3, "total_tokens": 9003}
    reply = AIMessage(
        content="Done", usage_metadata=usage, response_metadata={"finish_reason": "stop"}
    )
    model = ScriptedModel(responses=[reply])
    async with Harness.open(tmp_path, tmp_path / "s", model) as harness:
        events = [event async for event in harness.events("Hi")]
        completed = [event for event in events if event["type"] == "model_end"]
        assert len(completed) == 1
        assert completed[0]["usage"] == usage
        assert completed[0]["finish_reason"] == "stop" and completed[0]["elapsed"] >= 0
        history = model.seen[0][1:]
        assert harness.context.estimate_input_tokens(history) == 9000
        extended = [*history, reply, HumanMessage(content="Continue with tests")]
        assert harness.context.estimate_input_tokens(extended) == (
            9000 + estimate_tokens(extended) - estimate_tokens(history)
        )
        # 相同 ID 的内容被改写时也不能复用旧输入量。
        history[0].content = "Replaced history"
        assert harness.context.estimate_input_tokens(history) == (
            estimate_tokens(history) + harness.context.overhead_tokens
        )


async def test_nonzero_bash_exit_is_an_error_with_a_correlated_event(tmp_path):
    async def approve(call, reason):
        return True

    model = ScriptedModel(responses=[
        call("bash", {"command": "exit 7"}, "failed-command"), AIMessage(content="Command failed")
    ])
    async with Harness.open(tmp_path, tmp_path / "s", model, approve=approve) as harness:
        events = [event async for event in harness.events("Run the harmless failing fixture")]
        end = next(event for event in events if event["type"] == "tool_end")
        start = next(event for event in events if event["type"] == "tool_start")
        assert end["status"] == "error" and end["exit_code"] == 7
        assert start["call_id"] == end["call_id"] == "failed-command"
        assert harness.session.get_execution("failed-command")[0] == "error"


async def test_truncated_call_is_not_executed_and_model_gets_feedback(tmp_path):
    reply = call("write_file", {"path": "partial.py", "content": "def incomplete():"}, "cut")
    reply.response_metadata = {"finish_reason": "length"}
    model = ScriptedModel(responses=[reply, AIMessage(content="I will use a smaller edit")])
    async with Harness.open(tmp_path, tmp_path / "s", model) as harness:
        await consume(harness, "Write complete code")
        assert not (tmp_path / "partial.py").exists()
        assert harness.session.get_execution("cut") is None
        result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
        assert result.tool_call_id == "cut"
        assert result.status == "error"
        assert "not executed" in result.content.lower()


async def test_malformed_call_is_reported_instead_of_ending_blank(tmp_path):
    reply = AIMessage(
        content="",
        tool_calls=call(
            "write_file", {"path": "sibling.txt", "content": "valid"}, "valid"
        ).tool_calls,
        invalid_tool_calls=[
            {"name": "write_file", "args": '{"path": }', "id": "bad", "error": "Invalid JSON"}
        ],
    )
    model = ScriptedModel(responses=[reply, AIMessage(content="Corrected response")])
    async with Harness.open(tmp_path, tmp_path / "s", model) as harness:
        assert await consume(harness, "Create a file") == "Corrected response"
        results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
        assert {m.tool_call_id for m in results} == {"valid", "bad"}
        assert all(m.status == "error" for m in results)
        assert not (tmp_path / "sibling.txt").exists()
        assert harness.session.get_execution("valid") is None
        assert harness.session.get_execution("bad") is None


async def test_empty_response_has_a_bounded_repair(tmp_path):
    model = ScriptedModel(responses=[AIMessage(content=""), AIMessage(content="Recovered")])
    async with Harness.open(tmp_path, tmp_path / "s", model) as harness:
        assert await consume(harness, "Hello") == "Recovered"
    model = ScriptedModel(responses=[AIMessage(content="") for _ in range(3)])
    async with Harness.open(tmp_path, tmp_path / "other", model) as harness:
        with pytest.raises(ValueError, match="repair limit"):
            await consume(harness, "Hello")
        assert len(model.seen) == 3


async def test_core_events_include_tool_result_and_preserve_text_stream(tmp_path):
    model = ScriptedModel(
        responses=[
            call("write_file", {"path": "new.py", "content": "ok\n"}, "write"),
            AIMessage(content="Done"),
        ]
    )
    async with Harness.open(tmp_path, tmp_path / "s", model) as harness:
        events = [event async for event in harness.events("Write it")]
        kinds = [event["type"] for event in events]
        assert kinds.index("tool_start") < kinds.index("tool_end") < kinds.index("text")
        assert (tmp_path / "new.py").read_text() == "ok\n"
        assert "".join(event["text"] for event in events if event["type"] == "text") == "Done"
        assert kinds[-1] == "done"


async def test_each_tool_is_approved_against_cwd_at_its_execution(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    calls = [
        call("bash", {"command": f"cd {outside}"}, "cd").tool_calls[0],
        call("write_file", {"path": "blocked.py", "content": "outside"}, "write").tool_calls[0],
    ]
    asked = []

    async def approve(tool_call, reason):
        asked.append(tool_call["id"])
        return tool_call["id"] == "cd"

    model = ScriptedModel(
        responses=[AIMessage(content="", tool_calls=calls), AIMessage(content="Done")]
    )
    async with Harness.open(project, tmp_path / "s", model, approve=approve) as harness:
        await consume(harness, "Inspect the other directory")
        assert asked == ["cd", "write"]
        assert not (outside / "blocked.py").exists()
        assert harness.session.get_execution("write") is None


async def test_cli_status_and_read_continuation_do_not_pollute_stdout(tmp_path, capsys):
    (tmp_path / "long.py").write_text("line\n" * 201)
    model = ScriptedModel(
        responses=[
            call("read_file", {"path": "long.py"}, "read"),
            AIMessage(content="Reviewed"),
        ]
    )
    async with Harness.open(tmp_path, tmp_path / "s", model) as harness:
        await display_events(harness, "Read the file")
    terminal = capsys.readouterr()
    assert terminal.out.strip() == "Reviewed"
    assert "read_file" in terminal.err and "long.py" in terminal.err
    assert "offset=201" in terminal.err
    assert "200: line" not in terminal.err  # CLI keeps previews short.


async def test_cli_keeps_the_error_at_end_of_a_long_log_line(tmp_path, capsys):
    async def approve(tool_call, reason):
        return True

    model = ScriptedModel(
        responses=[
            call("bash", {"command": "python3 -c \"print('x' * 4000 + 'FINAL ERROR')\""}, "log"),
            AIMessage(content="Checked"),
        ]
    )
    async with Harness.open(tmp_path, tmp_path / "s", model, approve=approve) as harness:
        await display_events(harness, "Run the harmless output fixture")
    result_display = capsys.readouterr().err.split("[success] bash", 1)[1]
    assert "FINAL ERROR" in result_display


async def test_resume_a_checkpoint_created_by_create_agent(tmp_path):
    from langchain.agents import create_agent
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    directory = tmp_path / "legacy"
    with Session(directory, tmp_path) as session:
        async with AsyncSqliteSaver.from_conn_string(str(session.database)) as saver:
            legacy = create_agent(
                ScriptedModel(responses=[AIMessage(content="Old answer")]), checkpointer=saver
            )
            await legacy.ainvoke(
                {"messages": [HumanMessage(content="Old request")]},
                {"configurable": {"thread_id": session.id}},
            )
    model = ScriptedModel(responses=[AIMessage(content="Continued")])
    async with Harness.open(tmp_path, directory, model) as harness:
        assert await consume(harness, "New request") == "Continued"
    contents = [m.content for m in model.seen[0]]
    assert contents[1:4] == ["Old request", "Old answer", "New request"]
