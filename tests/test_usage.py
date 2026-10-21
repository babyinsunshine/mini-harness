"""缓存统计使用真实会话记录；只替代外部模型。"""

from langchain_core.messages import AIMessage
from test_agent_cli import ScriptedModel

from mini_harness.agent import Harness
from mini_harness.cli.display import display_events
from mini_harness.cli.interactive import handle_input


async def test_cache_stats_use_token_weighting_and_survive_resume(tmp_path, capsys):
    usages = [
        {
            "input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
            "input_token_details": {"cache_read": 0},
        },
        {
            "input_tokens": 900, "output_tokens": 20, "total_tokens": 920,
            "input_token_details": {"cache_read": 900},
        },
        {"input_tokens": 9000, "output_tokens": 30, "total_tokens": 9030},
        None,
    ]
    model = ScriptedModel(responses=[
        *[AIMessage(content=f"Reply {index}", usage_metadata=usage)
          for index, usage in enumerate(usages)],
        AIMessage(content="Unexpected stats model call"),
    ])
    directory = tmp_path / "session"
    async with Harness.open(tmp_path, directory, model) as harness:
        for index in range(4):
            await display_events(harness, f"Request {index}")
        terminal = capsys.readouterr()
        assert "cache 0.0% (0/100)" in terminal.err
        assert "cache 100.0% (900/900)" in terminal.err
        assert terminal.err.count("cache n/a") == 2

        transcript_before = harness.session.transcript.read_bytes()
        await handle_input(harness, "/stats", tmp_path)
        stats = capsys.readouterr().out
        assert "4 model requests" in stats
        assert "10,000 in / 60 out" in stats
        # 900/1000 = 90%，不能平均两次百分比，也不能把未知缓存量当作 0。
        assert "cache 90.0% (900/1,000)" in stats
        assert "reported for 2/4 requests" in stats
        assert len(model.seen) == 4
        assert harness.session.transcript.read_bytes() == transcript_before

    resumed = ScriptedModel(responses=[AIMessage(content="Unexpected resumed model call")])
    async with Harness.open(tmp_path, directory, resumed) as harness:
        await handle_input(harness, "/stats", tmp_path)
        assert capsys.readouterr().out == stats
        assert not resumed.seen


async def test_stats_without_requests_reports_unavailable_without_calling_model(tmp_path, capsys):
    model = ScriptedModel(responses=[AIMessage(content="Unexpected model call")])
    async with Harness.open(tmp_path, tmp_path / "session", model) as harness:
        await handle_input(harness, "/stats", tmp_path)
        terminal = capsys.readouterr()
        assert not model.seen
        assert "0 model requests" in terminal.out
        assert "cache n/a" in terminal.out
        assert not harness.session.transcript.exists()
