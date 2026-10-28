"""工具调用级权限：明确危险的拒绝，不确定的询问，普通操作直接允许。"""

import shlex
from pathlib import Path

from langchain_core.messages import ToolMessage

from ..session import Session
from .skills import skill_path

SYSTEM_PATHS = ("/etc", "/boot", "/proc", "/sys", "/dev", "/usr", "/bin", "/sbin", "/lib")
READ_FLAGS = {
    "ls": {"-a", "-l", "-h", "-la", "-al", "-lh", "-lah", "--all"},
    "cat": {"-n", "--number"},
    "head": set(),
    "tail": set(),
    "rg": {"-n", "-i", "-F", "-l", "-S", "--files", "--hidden", "--no-ignore"},
    "git": {
        "--short", "-s", "--porcelain", "--branch", "--oneline", "--stat", "--name-only",
        "--name-status", "--cached", "--staged", "--no-ext-diff", "--no-textconv",
    },
}


class Permissions:
    """先审批再进入执行记录；approve 由 CLI 提供，缺省时不执行需要审批的操作。"""

    def __init__(self, session: Session, approve=None, parent=None, *, full_access=False):
        self.session, self.approve = session, approve
        self.parent = parent
        self.full_access = full_access
        self.session_trusted = False  # 只保存在内存，恢复会话时重新审批。

    async def check(self, tool_call):
        """允许时返回 None；拒绝时返回未执行的 ToolMessage。"""
        authority = self.parent or self
        sandbox_request = (
            self.session.sandbox.permission_request(tool_call)
            if self.session.sandbox is not None else None
        )
        if sandbox_request is not None:
            action, reason = sandbox_request
            if action == "ask" and tool_call["name"] == "bash" and not authority.full_access:
                ordinary_action, ordinary_reason = _bash_rule(
                    tool_call.get("args", {}).get("command"), self.session, apply_sandbox=False,
                )
                if ordinary_action == "deny":
                    action, reason = ordinary_action, ordinary_reason
        elif authority.full_access:
            action, reason = "allow", "Full access enabled"
        else:
            action, reason = classify(tool_call, self.session)
        allowed = action == "allow"
        if action == "ask" and sandbox_request is None and authority.session_trusted:
            allowed = True
        elif action == "ask" and authority.approve is not None:
            request = dict(tool_call)
            if sandbox_request is not None:
                request["sandbox_request"] = True
            if self.parent:
                request["agent_id"] = self.session.id
            allowed = await authority.approve(request, reason)
        self.session.record(
            "permission", call_id=tool_call["id"], action=action, allowed=allowed, reason=reason
        )
        if allowed:
            return None
        result = ToolMessage(
            content=f"Permission denied; tool was not executed. {reason}. "
                    "Do not retry this action through another tool.",
            tool_call_id=tool_call["id"], id=f"result-{tool_call['id']}", status="error",
        )
        self.session.record_message(result)
        return result


def classify(tool_call: dict, session: Session) -> tuple[str, str]:
    """返回 allow / ask / deny 和原因；只检查请求，不执行或修改任何内容。"""
    name, args = tool_call["name"], tool_call.get("args", {})
    if name in {"read_file", "write_file", "edit_file"}:
        return _file_rule(args.get("path"), session, writing=name != "read_file")
    if name == "update_plan":
        return "allow", "Update this session's task plan"
    if name in {"spawn_agent", "wait_agent", "continue_agent", "close_agent"}:
        return "allow", "Manage subagents owned by this session"
    if name == "list_skills":
        return "allow", "Read project skill descriptions"
    if name == "list_mcp_tools":
        return "allow", "Read configured MCP tool descriptions and schemas"
    if name == "load_skill":
        try:
            path = skill_path(session, args.get("name"))
        except (ValueError, RuntimeError) as error:
            return "deny", str(error)
        return _file_rule(str(path), session, writing=False)
    if name == "bash":
        return _bash_rule(args.get("command"), session)
    if name == "stop_task":
        return "allow", "Stop a task owned by this session"
    return "ask", "This tool requires approval"


def _file_rule(path, session: Session, *, writing: bool, apply_sandbox=True) -> tuple[str, str]:
    """先保护系统/会话记录，再检查固定目录范围；cd 不扩大权限。"""
    if not isinstance(path, str) or not path:
        return "ask", "Missing or invalid path"
    target = session.resolve_path(path)
    sandbox = session.sandbox if apply_sandbox else None
    if sandbox is not None:
        try:
            sandbox.check_path(
                target, writing=writing, env_file=session.directory / "env.sh",
            )
        except PermissionError as error:
            return "deny", str(error)
    if writing:
        if any(target.is_relative_to(Path(root).resolve()) for root in SYSTEM_PATHS):
            return "deny", "Writing critical system paths is forbidden"
        if target.parent == session.directory and (
            target.name == "transcript.jsonl"
            or target.name.split("-", 1)[0] in {"state.sqlite", "runtime.sqlite"}
        ):
            return "deny", "Overwriting session records is forbidden"
        if target == (session.directory / "env.sh").resolve() or ".git" in target.parts:
            return "ask", "Changing executable environment or Git configuration requires approval"
    if sandbox is not None:
        return "allow", "File is within sandbox rules"
    roots = (Path(session.get("project_root")), session.directory)
    if any(target.is_relative_to(root) for root in roots):
        return "allow", "File is inside the project or this session"
    return "ask", "File is outside the project and this session"


def _dangerous(words: list[str], session: Session) -> bool:
    """只识别命令位置和实际选项；搜索字符串里的危险词不是执行。"""
    if not words:
        return False
    name, args = Path(words[0]).name, words[1:]
    options = args[:args.index("--")] if "--" in args else args
    flags = {arg for arg in options if arg.startswith("-")}
    short = "".join(arg[1:] for arg in flags if not arg.startswith("--"))
    if name == "rm":
        return ("r" in short or "R" in short or "--recursive" in flags) and (
            "f" in short or "--force" in flags
        )
    if name in {"shutdown", "reboot", "poweroff", "halt", "mkfs"} or name.startswith("mkfs."):
        return True
    if name == "dd":
        return any(
            arg.startswith("of=") and session.resolve_path(arg[3:]).is_relative_to(Path("/dev"))
            for arg in args
        )
    if name == "git" and args:
        if args[0] == "reset" and "--hard" in flags:
            return True
        if args[0] in {"clean", "push"}:
            return "f" in short or any(flag.startswith("--force") for flag in flags)
    return False


def _bash_rule(command, session: Session, *, apply_sandbox=True) -> tuple[str, str]:
    """检查简单 Bash；复杂语法只做明显危险检查，然后交给用户审批。"""
    if not isinstance(command, str) or not command.strip():
        return "ask", "Missing or invalid command"
    try:
        lexer = shlex.shlex(command.replace("\n", ";"), posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        # shlex 会把词中间的 # 也当注释；禁用它，避免漏掉后面的真实命令。
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return "ask", "Cannot parse this shell command"
    group = []
    complex_shell = False
    for index, token in enumerate([*tokens, ";"]):
        if token and all(char in ";&|<>()" for char in token):
            if _dangerous(group, session):
                return "deny", "Destructive command is forbidden"
            if ">" in token and index + 1 < len(tokens):
                action, reason = _file_rule(
                    tokens[index + 1], session, writing=True, apply_sandbox=apply_sandbox,
                )
                if action == "deny":
                    return action, reason
            group = []
            complex_shell = complex_shell or index < len(tokens)
        else:
            group.append(token)
    if complex_shell or any(char in command for char in "$`*?[]{}\\"):
        return "ask", "Complex shell syntax requires approval"
    if not tokens:
        return "ask", "Empty command"
    if tokens == ["pwd"]:
        return "allow", "Read the working directory"
    name, args = tokens[0], tokens[1:]
    if name not in READ_FLAGS:
        return "ask", "Command is not in the simple read-only allowlist"
    if name == "git":
        if not args or args[0] not in {"status", "diff", "log", "show"}:
            return "ask", "This Git operation requires approval"
        operation, args = args[0], args[1:]
        # diff/show 可以调用配置中的外部程序，自动允许时必须禁用这些扩展。
        options = args[:args.index("--")] if "--" in args else args
        if operation in {"diff", "show"} and not {"--no-ext-diff", "--no-textconv"} <= set(options):
            return "ask", "Git diff/show requires --no-ext-diff --no-textconv"
    return _read_arguments(name, args, session, apply_sandbox=apply_sandbox)


def _read_arguments(name, args, session: Session, *, apply_sandbox=True) -> tuple[str, str]:
    """只接受已知只读选项；文件参数也必须位于允许范围内。"""
    pattern_seen = name != "rg" or "--files" in args
    index = 0
    positional = False
    while index < len(args):
        arg = args[index]
        index += 1
        if arg == "--" and not positional:
            positional = True
            continue
        if not positional and arg.startswith("-"):
            if arg in READ_FLAGS[name]:
                continue
            if (name in {"head", "tail", "git"} and arg == "-n") or (
                name == "rg" and arg in {"-g", "--glob", "-e", "--regexp"}
            ):
                if index >= len(args):
                    return "ask", "Missing option value"
                value = args[index]
                index += 1
                if name != "rg" and not value.isdecimal():
                    return "ask", "Unknown option value"
                if arg in {"-e", "--regexp"}:
                    pattern_seen = True
                continue
            return "ask", "Unknown command option"
        if not pattern_seen:
            pattern_seen = True
            continue
        action, reason = _file_rule(arg, session, writing=False, apply_sandbox=apply_sandbox)
        if action != "allow":
            return action, reason
    if _file_rule(
        str(session.cwd), session, writing=False, apply_sandbox=apply_sandbox,
    )[0] != "allow":
        return "ask", "Working directory is outside the allowed scope"
    return "allow", "Simple read-only command inside the allowed scope"
