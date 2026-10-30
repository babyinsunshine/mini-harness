import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from test_agent_cli import ScriptedModel, call, consume
from test_context import SummaryModel, policy

from mini_harness.agent import Harness
from mini_harness.context.compact import micro_compact
from mini_harness.context.manager import ContextManager
from mini_harness.session import Session
from mini_harness.tools.factory import create_tools


def write_skill(project, instructions="Keep the existing public API."):
    directory = project / ".agents" / "skills" / "python-testing"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        "---\nname: python-testing\ndescription: >\n"
        "  Follow the project's\n  testing conventions.\n---\n" + instructions + "\n"
    )
    return directory


async def test_model_discovers_loads_and_reads_skill_resources_through_the_real_graph(tmp_path):
    project = tmp_path / "project"
    directory = write_skill(project, "Read references/testing.md before writing tests.")
    reference = directory / "references" / "testing.md"
    reference.parent.mkdir()
    reference.write_text("Use local fixtures instead of the live API.")
    script = directory / "scripts" / "check.py"
    script.parent.mkdir()
    marker = project / "script-ran"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    model = ScriptedModel(responses=[
        call("list_skills", {}, "list"),
        call("load_skill", {"name": "python-testing"}, "load"),
        call("read_file", {"path": str(reference)}, "reference"),
        AIMessage(content="Ready to follow the testing conventions"),
    ])
    session_dir = tmp_path / "session"
    async with Harness.open(project, session_dir, model) as harness:
        await consume(harness, "Find the project's testing conventions")
        results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
        assert all(result.status == "success" for result in results)
        assert json.loads(results[0].text) == [{
            "name": "python-testing", "description": "Follow the project's testing conventions."
        }]
        loaded = json.loads(results[1].text)
        assert loaded == {
            "name": "python-testing", "directory": str(directory),
            "instructions": "Read references/testing.md before writing tests.",
        }
        assert "Use local fixtures" in results[2].text
        assert "Read references" not in results[0].text
        assert "python-testing" not in str(model.seen[0])
        assert not marker.exists()
        await harness.compact()
        snapshot = await harness.graph.aget_state(harness.config)
        assert results[1] in snapshot.values["messages"]

    resumed = ScriptedModel(responses=[AIMessage(content="Continuing with the same conventions")])
    async with Harness.open(project, session_dir, resumed) as harness:
        await consume(harness, "Continue")
        assert results[1] in resumed.seen[0]


async def test_skill_catalog_is_read_on_demand_and_uses_the_initial_project_directory(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    with Session(tmp_path / "session", project) as session:
        tools = {tool.name: tool for tool in create_tools(session, None)}
        assert await tools["list_skills"].ainvoke({}) == []
        directory = write_skill(project)
        session.cwd = tmp_path
        catalog = await tools["list_skills"].ainvoke({})
        assert [item["name"] for item in catalog] == ["python-testing"]
        loaded = await tools["load_skill"].ainvoke({"name": "python-testing"})
        assert loaded["instructions"] == "Keep the existing public API."
        write_skill(project, "Run focused tests before the full suite.")
        loaded = await tools["load_skill"].ainvoke({"name": "python-testing"})
        assert loaded["instructions"] == "Run focused tests before the full suite."
        assert loaded["directory"] == str(directory)


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside", "missing"])
async def test_invalid_or_unknown_skills_do_not_read_outside_the_catalog(tmp_path, name):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "outside").write_text("PRIVATE DATA")
    model = ScriptedModel(responses=[
        call("load_skill", {"name": name}, "invalid"), AIMessage(content="Load failed")
    ])
    async with Harness.open(project, tmp_path / "session", model) as harness:
        await consume(harness, "Load the specified skill")
        result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
        assert result.status == "error"
        assert "PRIVATE DATA" not in result.text


@pytest.mark.parametrize("link", ["root", "skill", "file"])
async def test_skill_symlinks_cannot_bypass_project_file_permissions(tmp_path, link):
    project = tmp_path / "project"
    project.mkdir()
    outside = write_skill(tmp_path / "outside", "PRIVATE SKILL INSTRUCTIONS")
    root = project / ".agents" / "skills"
    root.parent.mkdir()
    if link == "root":
        root.symlink_to(outside.parent, target_is_directory=True)
    elif link == "skill":
        root.mkdir()
        (root / "python-testing").symlink_to(outside, target_is_directory=True)
    else:
        (root / "python-testing").mkdir(parents=True)
        (root / "python-testing" / "SKILL.md").symlink_to(outside / "SKILL.md")
    model = ScriptedModel(responses=[
        call("list_skills", {}, "list"),
        call("load_skill", {"name": "python-testing"}, "load"),
        AIMessage(content="Access was blocked"),
    ])
    async with Harness.open(project, tmp_path / "session", model) as harness:
        await consume(harness, "Inspect the available skills")
        results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
        assert all(result.status == "error" for result in results)
        assert all("PRIVATE SKILL INSTRUCTIONS" not in result.text for result in results)


async def test_circular_skill_link_returns_a_tool_error_and_model_can_continue(tmp_path):
    project = tmp_path / "project"
    directory = project / ".agents" / "skills" / "loop"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").symlink_to("SKILL.md")
    model = ScriptedModel(responses=[
        call("load_skill", {"name": "loop"}, "load"), AIMessage(content="Still running")
    ])
    async with Harness.open(project, tmp_path / "session", model) as harness:
        assert await consume(harness, "Load the specified skill") == "Still running"
        result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
        assert result.status == "error"
        assert len(model.seen) == 2


async def test_compacting_only_skill_instructions_explains_why_they_are_retained(tmp_path):
    messages = [
        call("load_skill", {"name": "python-testing"}, "skill"),
        ToolMessage(
            content="Keep the testing conventions.", name="load_skill",
            tool_call_id="skill", id="skill-result",
        ),
    ]
    with Session(tmp_path / "session", tmp_path) as session:
        model = SummaryModel()
        context = ContextManager(session, model, policy())
        with pytest.raises(ValueError, match="protected skill"):
            await context.compact(messages, start_group=0, end_group=1)
        assert not model.inputs
        assert messages[1].content == "Keep the testing conventions."


@pytest.mark.parametrize("mode", ["micro", "summary", "partial"])
async def test_regular_compaction_preserves_skill_instructions_and_the_whole_tool_group(
    tmp_path, mode,
):
    skill_calls = [
        {"name": "load_skill", "args": {"name": "python-testing"}, "id": "skill"},
        {"name": "read_file", "args": {"path": "guide.md"}, "id": "parallel"},
    ]
    protected = [
        AIMessage(content="", id="skill-calls", tool_calls=skill_calls),
        ToolMessage(
            content="Important testing conventions. " * 60, name="load_skill",
            tool_call_id="skill", id="skill-result",
        ),
        ToolMessage(content="A result in the same tool group.", tool_call_id="parallel", id="r2"),
    ]
    messages = [
        HumanMessage(content="Preserve the public API", id="first-user"),
        *protected,
        call("bash", {"command": "ls"}, "old-command").model_copy(update={"id": "old-call"}),
        ToolMessage(content="old log " * 2000, tool_call_id="old-command", id="old-result"),
        HumanMessage(content="Now add tests", id="latest-user"),
        AIMessage(content="Ready", id="latest-reply"),
    ]
    with Session(tmp_path / "session", tmp_path) as session:
        settings = policy(
            keep_rounds=1, micro_keep_rounds=0, micro_keep_tokens=0, micro_min_savings=0
        )
        context = ContextManager(session, SummaryModel(), settings)
        if mode == "micro":
            result = micro_compact(messages, session, settings)
            assert "cleared" in next(m.content for m in result if m.id == "old-result")
        else:
            bounds = {"start_group": 1, "end_group": 3} if mode == "partial" else {}
            result = await context.compact(messages, **bounds)
        assert all(message in result for message in protected)


async def test_malformed_skill_metadata_returns_a_clear_tool_error(tmp_path):
    project = tmp_path / "project"
    directory = write_skill(project)
    (directory / "SKILL.md").write_text("---\nname: [broken\n---\nInstructions\n")
    model = ScriptedModel(responses=[
        call("list_skills", {}, "list"), AIMessage(content="Fix the skill metadata first")
    ])
    async with Harness.open(project, tmp_path / "session", model) as harness:
        await consume(harness, "List the skills")
        result = next(m for m in model.seen[-1] if isinstance(m, ToolMessage))
        assert result.status == "error"
        assert "SKILL.md" in result.text
