"""真实 SQLite 检查点保留与恢复；仅外部模型使用离线替身。"""

import base64
import sqlite3

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, message_to_dict
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from test_agent_cli import ScriptedModel, call, consume

from mini_harness.agent import Harness
from mini_harness.graph.builder import build_graph
from mini_harness.session import Session

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)


async def test_latest_checkpoint_preserves_conversation_and_images(tmp_path):
    (tmp_path / "screen.png").write_bytes(PNG)
    directory = tmp_path / "session"
    replies = []
    for index in range(3):
        replies.extend([
            call("read_image", {"path": "screen.png"}, f"image-{index}"),
            AIMessage(content=f"Observed {index}"),
        ])
    async with Harness.open(tmp_path, directory, ScriptedModel(responses=replies)) as harness:
        for index in range(3):
            assert await consume(harness, f"Inspect {index}") == f"Observed {index}"
            history = [item async for item in harness.graph.checkpointer.alist(harness.config)]
            assert len(history) == 1
        snapshot = await harness.graph.aget_state(harness.config)
        contents = [m.content for m in snapshot.values["messages"]]
        assert all(contents.count(text) == 1 for text in ("Inspect 0", "Inspect 1", "Inspect 2"))
    model = ScriptedModel(responses=[AIMessage(content="Continued")])
    async with Harness.open(tmp_path, directory, model) as harness:
        assert await consume(harness, "Continue") == "Continued"
        assert all(any(m.content == text for m in model.seen[0]) for text in (
            "Inspect 0", "Inspect 1", "Inspect 2",
        ))
        images = [block for m in model.seen[0] if isinstance(m.content, list)
                  for block in m.content if block.get("type") == "image"]
        assert images and base64.b64decode(images[-1]["base64"]) == PNG


async def test_existing_history_keeps_latest_pending_result_on_reopen(tmp_path):
    directory = tmp_path / "session"
    result = ToolMessage(content="Already completed", tool_call_id="finished", id="result-finished")
    with Session(directory, tmp_path) as session:
        async with AsyncSqliteSaver.from_conn_string(str(session.database)) as saver:
            graph = build_graph(saver)
            config = {"configurable": {"thread_id": session.id}}
            await graph.aupdate_state(config, {
                "messages": [HumanMessage(content="Run once", id="original")],
            }, as_node="collect")
            await graph.aupdate_state(config, {
                "messages": [call("bash", {"command": "touch replayed"}, "finished")],
                "pending_calls": [{"name": "bash", "args": {}, "id": "finished"}],
                "approved": True,
            }, as_node="permission")
            snapshot = await graph.aget_state(config)
            await saver.aput_writes(snapshot.config, [
                ("messages", [result]), ("pending_calls", []), ("approved", False),
            ], snapshot.tasks[0].id)
            session.start_execution("finished")
            session.finish_execution("finished", message_to_dict(result))
    model = ScriptedModel(responses=[AIMessage(content="Recovered")])
    async with Harness.open(tmp_path, directory, model) as harness:
        history = [item async for item in harness.graph.checkpointer.alist(harness.config)]
        assert len(history) == 1
        assert history[0].pending_writes
        async with harness.graph.checkpointer.conn.execute(
            "SELECT DISTINCT checkpoint_id FROM writes"
        ) as cursor:
            assert await cursor.fetchall() == [(snapshot.config["configurable"]["checkpoint_id"],)]
        assert await consume(harness, "Continue without replay") == "Recovered"
        messages = model.seen[0]
        index = next(i for i, message in enumerate(messages) if getattr(message, "tool_calls", []))
        assert messages[index + 1] == result
        assert messages[index + 2].content == "Continue without replay"
    assert not (tmp_path / "replayed").exists()


async def test_retention_is_per_thread_and_namespace(tmp_path):
    async with Harness.open(tmp_path, tmp_path / "session", ScriptedModel()) as harness:
        saver = harness.graph.checkpointer
        for thread_id, namespace in (("alpha", ""), ("alpha", "branch"), ("beta", "")):
            config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": namespace}}
            for value in ("old", "latest"):
                checkpoint = empty_checkpoint()
                checkpoint["channel_values"] = {"value": value}
                config = await saver.aput(config, checkpoint, {}, {})
        async with saver.conn.execute(
            "SELECT thread_id, checkpoint_ns, count(*) FROM checkpoints "
            "GROUP BY thread_id, checkpoint_ns"
        ) as cursor:
            assert sorted(await cursor.fetchall()) == [
                ("alpha", "", 1), ("alpha", "branch", 1), ("beta", "", 1),
            ]
        for thread_id, namespace in (("alpha", ""), ("alpha", "branch"), ("beta", "")):
            saved = await saver.aget_tuple({
                "configurable": {"thread_id": thread_id, "checkpoint_ns": namespace},
            })
            assert saved.checkpoint["channel_values"] == {"value": "latest"}


async def test_failed_checkpoint_cleanup_preserves_previous_state(tmp_path):
    directory = tmp_path / "session"
    async with Harness.open(tmp_path, directory, ScriptedModel()) as harness:
        saver = harness.graph.checkpointer
        config = {"configurable": {"thread_id": harness.session.id, "checkpoint_ns": ""}}
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"value": "original"}
        config = await saver.aput(config, checkpoint, {}, {})
        # 数据库拒绝删旧快照，验证新旧状态不会只提交一半。
        await saver.conn.executescript("""
            CREATE TRIGGER reject_cleanup BEFORE DELETE ON checkpoints
            BEGIN SELECT RAISE(ABORT, 'fixture cleanup failed'); END;
        """)
        replacement = empty_checkpoint()
        replacement["channel_values"] = {"value": "replacement"}
        with pytest.raises(sqlite3.IntegrityError, match="fixture cleanup failed"):
            await saver.aput(config, replacement, {}, {})
        await saver.conn.rollback()
    async with AsyncSqliteSaver.from_conn_string(str(directory / "state.sqlite")) as saver:
        latest = await saver.aget_tuple({"configurable": {"thread_id": directory.name}})
        assert latest.checkpoint["channel_values"] == {"value": "original"}
