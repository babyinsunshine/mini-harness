import json

import pytest
from test_skills import write_skill

from mini_harness.context.manager import ContextManager
from mini_harness.context.policy import ContextPolicy
from mini_harness.session import Session
from mini_harness.tools.files import edit_file, read_file, write_file
from mini_harness.tools.permissions import classify
from mini_harness.tools.skills import list_skills, load_skill


@pytest.fixture
def session(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    with Session(tmp_path / "session", project) as session:
        yield session


def enable_sandbox(session, config=None):
    from mini_harness.sandbox import Sandbox

    config_path = session.directory / "sandbox.json"
    config_path.write_text(json.dumps(config or {}))
    session.sandbox = Sandbox.load(session.cwd, config_path)
    return session.sandbox


def test_project_write_and_edit_work_but_outside_write_is_blocked(session, tmp_path):
    enable_sandbox(session)
    write_file(session, "app.txt", "before")
    read_file(session, "app.txt")
    edit_file(session, "app.txt", "before", "after")
    assert (session.cwd / "app.txt").read_text() == "after"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside content")
    assert "outside content" in read_file(session, str(outside))["output"]
    with pytest.raises(PermissionError, match="sandbox|Sandbox"):
        edit_file(session, str(outside), "outside", "changed")
    assert outside.read_text() == "outside content"


def test_cd_does_not_expand_write_root_and_prefix_sibling_is_outside(session, tmp_path):
    enable_sandbox(session)
    sibling = tmp_path / "project-backup"
    sibling.mkdir()
    session.cwd = sibling
    with pytest.raises(PermissionError):
        write_file(session, "new.txt", "blocked")
    assert not (sibling / "new.txt").exists()


def test_symlink_to_outside_cannot_be_used_for_writing(session, tmp_path):
    enable_sandbox(session)
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    (session.cwd / "link").symlink_to(outside)
    read_file(session, "link")
    with pytest.raises(PermissionError):
        write_file(session, "link", "changed")
    assert outside.read_text() == "original"


def test_denied_content_stays_blocked_after_parent_directory_grant(session, tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    secret = private / "secret.txt"
    secret.write_text("private fixture")
    sandbox = enable_sandbox(session, {"deny_paths": [str(secret)]})
    sandbox.grant([str(private)])
    with pytest.raises(PermissionError):
        read_file(session, str(secret))
    with pytest.raises(PermissionError):
        write_file(session, str(secret), "changed")
    write_file(session, str(private / "allowed.txt"), "allowed")
    assert (private / "allowed.txt").read_text() == "allowed"


def test_deny_path_within_project_also_blocks_alias(session):
    private = session.cwd / "private"
    private.mkdir()
    (private / "secret.txt").write_text("private fixture")
    (session.cwd / "alias").symlink_to(private, target_is_directory=True)
    enable_sandbox(session, {"deny_paths": [str(private)]})
    with pytest.raises(PermissionError):
        read_file(session, "alias/secret.txt")
    with pytest.raises(PermissionError):
        write_file(session, "private/new.txt", "blocked")


def test_only_env_file_is_writable_in_session_directory(session):
    sandbox = enable_sandbox(session)
    env_file = session.directory / "env.sh"
    write_file(session, str(env_file), "export PROJECT_MODE=demo\n")
    assert env_file.read_text() == "export PROJECT_MODE=demo\n"
    with pytest.raises(PermissionError):
        write_file(session, str(session.directory / "other.txt"), "blocked")
    sandbox.deny_paths = (env_file,)
    with pytest.raises(PermissionError):
        read_file(session, str(env_file))


def test_configuration_cannot_be_modified_by_granting_its_parent(session):
    sandbox = enable_sandbox(session)
    sandbox.grant([str(session.directory)])
    with pytest.raises(PermissionError):
        write_file(session, str(sandbox.config_path), "{}")


def test_relative_deny_paths_are_relative_to_config_file(session):
    secret = session.directory / "secret.txt"
    secret.write_text("private fixture")
    enable_sandbox(session, {"deny_paths": ["secret.txt"]})
    with pytest.raises(PermissionError):
        read_file(session, str(secret))


@pytest.mark.parametrize("config", [
    {"deny_paths": "secret.txt"}, {"deny_paths": [3]},
    {"allow_write_requests": "false"}, {"allow_bypass": 1}, {"unknown_option": True},
])
def test_invalid_configuration_is_rejected_instead_of_relaxing_rules(session, config):
    with pytest.raises(ValueError):
        enable_sandbox(session, config)


def test_skills_and_automatic_project_instructions_respect_denied_files(session):
    directory = write_skill(session.cwd, "PRIVATE SKILL CONTENT")
    agents = session.cwd / "AGENTS.md"
    agents.write_text("PRIVATE PROJECT INSTRUCTIONS")
    enable_sandbox(session, {"deny_paths": [str(directory), str(agents)]})
    assert list_skills(session) == []
    with pytest.raises(PermissionError):
        load_skill(session, "python-testing")
    context = ContextManager(session, None, ContextPolicy())
    messages = context.with_runtime_context([])
    assert "PRIVATE PROJECT INSTRUCTIONS" not in str(messages)


def test_permission_classification_uses_sandbox_rules_for_outside_reads(session, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("public fixture")
    enable_sandbox(session)
    call = {"name": "read_file", "args": {"path": str(outside)}}
    assert classify(call, session)[0] == "allow"
    assert classify({"name": "write_file", "args": call["args"]}, session)[0] == "deny"
