"""文件访问策略和本次运行的额外授权；不写入会话数据库。"""

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Sandbox:
    project_root: Path
    deny_paths: tuple[Path, ...] = ()
    allow_write_requests: bool = True
    allow_bypass: bool = False
    config_path: Path | None = None
    extra_write_paths: set[Path] = field(default_factory=set)
    session_directory: Path | None = None
    runtime_path: Path | None = field(default=None, init=False, repr=False)

    @property
    def blocked_paths(self) -> tuple[Path, ...]:
        """WSL 的 Windows 执行桥接不继承 Linux 沙箱，必须在沙箱内隐藏入口。"""
        paths = list(self.deny_paths)
        if sys.platform == "linux" and "microsoft" in os.uname().release.lower():
            paths += [path.resolve() for path in (Path("/init"), Path("/run/WSL"))
                      if path.exists()]
        return tuple(paths)

    @classmethod
    def load(cls, project_root: Path, config_path: Path | None = None):
        """只接受明确路径和布尔开关，拒绝拼错或类型错误的配置。"""
        config_path = Path(config_path).expanduser().resolve() if config_path else None
        config = json.loads(config_path.read_text()) if config_path else {}
        fields = {"deny_paths", "allow_write_requests", "allow_bypass"}
        if not isinstance(config, dict) or config.keys() - fields:
            raise ValueError("Unknown sandbox configuration fields")
        denied = config.get("deny_paths", [])
        if not isinstance(denied, list) or any(
            not isinstance(path, str) or not path.strip() for path in denied
        ):
            raise ValueError("deny_paths must be a list of file or directory paths")
        for name in ("allow_write_requests", "allow_bypass"):
            if name in config and not isinstance(config[name], bool):
                raise ValueError(f"{name} must be a boolean")
        base = config_path.parent if config_path else Path(project_root)
        return cls(
            project_root=Path(project_root).resolve(),
            deny_paths=tuple((base / Path(path).expanduser()).resolve() for path in denied),
            allow_write_requests=config.get("allow_write_requests", True),
            allow_bypass=config.get("allow_bypass", False),
            config_path=config_path,
        )

    def check_path(self, path: Path, *, writing=False, env_file: Path | None = None):
        """禁止路径优先；当前会话的 env.sh 和真实 scratch 是明确可写例外。"""
        path = Path(path).resolve()
        if any(path.is_relative_to(denied) for denied in self.blocked_paths):
            raise PermissionError(f"Sandbox denies access: {path}")
        if not writing:
            return
        if path in (self.config_path, self.runtime_path):
            raise PermissionError(f"Sandbox configuration/runtime is read-only: {path}")
        if env_file is not None and (
            path == env_file.resolve() or path.is_relative_to(env_file.parent / "tmp")
        ):
            return
        state_directory = self.session_directory or (env_file.parent if env_file else None)
        if state_directory is not None and path.is_relative_to(state_directory):
            raise PermissionError(f"Sandbox session management files are read-only: {path}")
        if not any(path.is_relative_to(root) for root in (
            self.project_root, *self.extra_write_paths,
        )):
            raise PermissionError(f"Sandbox blocks writing outside approved directories: {path}")

    def write_paths(self, paths: list[str]) -> list[Path]:
        """在审批前解析实际目录，审批界面与执行使用相同的绝对路径。"""
        if not paths or any(not isinstance(path, str) or not path.strip() for path in paths):
            raise ValueError("Specify at least one existing directory")
        resolved = sorted({Path(path).expanduser().resolve() for path in paths})
        for path in resolved:
            self.check_path(path)
            if not path.is_dir():
                raise ValueError(f"Write access requires an existing directory: {path}")
        return resolved

    def grant(self, paths: list[str]):
        resolved = set(self.write_paths(paths))
        if resolved != {Path(path).expanduser().absolute() for path in paths}:
            raise PermissionError("Approved directory changed; request access again")
        for path in sorted(resolved, key=lambda path: len(path.parts)):
            roots = (self.project_root, *self.extra_write_paths)
            if not any(path.is_relative_to(root) for root in roots):
                self.extra_write_paths.add(path)

    def status(self):
        """放入运行上下文，旧对话中的授权文字不代表当前有效权限。"""
        return {
            "write_paths": sorted(map(str, (self.project_root, *self.extra_write_paths))),
            "deny_paths": list(map(str, self.blocked_paths)),
            "read_only_session": str(self.session_directory) if self.session_directory else None,
            "extra_grants": "Valid only until harness exits; not restored from history.",
        }

    def permission_request(self, tool_call: dict) -> tuple[str, str] | None:
        """识别扩权和临时绕过；它们必须明确审批，不复用普通会话信任。"""
        name, args = tool_call["name"], tool_call.get("args", {})
        if name == "request_sandbox_access":
            if not self.allow_write_requests:
                return "deny", "Sandbox write-access requests are disabled"
            try:
                paths = self.write_paths(args.get("paths", []))
            except (ValueError, TypeError, PermissionError) as error:
                return "deny", str(error)
            # 固定审批展示的实际目录，执行阶段不能再追随原始符号链接。
            args["paths"] = list(map(str, paths))
            scope = "Additional writable directories until exit: " + ", ".join(map(str, paths))
        elif name == "bash" and args.get("bypass_sandbox"):
            if not self.allow_bypass:
                return "deny", "Unsandboxed execution requests are disabled"
            scope = "Run this command and its children UNSANDBOXED once; deny paths also lifted"
        else:
            return None
        reason = args.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return "deny", "Sandbox permission requests require a reason"
        return "ask", f"{scope}. Reason: {reason}"

    def require_runtime(self) -> str:
        """显式开启后，缺少隔离能力就报错，不能退回普通执行。"""
        if self.runtime_path is None:
            executable = shutil.which("bwrap")
            if sys.platform != "linux" or executable is None:
                raise RuntimeError(
                    "Sandbox requires Linux/WSL2 and Bubblewrap (install bubblewrap)"
                )
            # 固定真实入口，后续项目中的同名程序或被替换的 PATH 链接不能接管启动。
            self.runtime_path = Path(executable).resolve()
        return str(self.runtime_path)

    def command(self, argv: list[str], *, env_file: Path, cwd_file: Path) -> list[str]:
        """主机只读、指定路径可写；共享网络，隔离 PID 并随父进程退出。"""
        temporary = env_file.parent / "tmp"
        temporary.mkdir(exist_ok=True)
        if temporary.resolve() != temporary:
            raise PermissionError("Scratch directory must not be a symbolic link")
        command = [
            self.require_runtime(), "--unshare-all", "--share-net", "--unshare-user",
            "--disable-userns", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
            "--ro-bind", "/", "/",
        ]
        writable = {self.project_root, *self.extra_write_paths}
        if any(path.resolve() != path for path in writable):
            raise PermissionError("Writable directory changed; request access again")
        for path in sorted(writable, key=lambda path: (len(path.parts), str(path))):
            command += ["--bind", str(path), str(path)]
        state_directory = self.session_directory or env_file.parent
        protected_files = [
            path for path in (self.config_path, self.runtime_path) if path is not None
        ]
        protected = [*self.blocked_paths, state_directory, *protected_files]
        # 只遮蔽目标还不够：固定可写祖先，阻止移走父目录后在下次调用读取旧内容。
        ancestors, scratch_ancestors = set(), set()
        for path in protected:
            for parent in path.parents:
                if parent != temporary and parent.is_relative_to(temporary):
                    scratch_ancestors.add(parent)
                    continue
                if parent in writable or parent.is_relative_to(state_directory):
                    continue
                if any(parent.is_relative_to(root) for root in writable):
                    ancestors.add(parent)
        for path in sorted(ancestors, key=lambda path: len(path.parts)):
            command += ["--bind", str(path), str(path)]
        command += ["--ro-bind", str(state_directory), str(state_directory)]
        for path in (env_file, cwd_file, temporary):
            command += ["--bind", str(path), str(path)]
        for path in sorted(scratch_ancestors, key=lambda path: len(path.parts)):
            command += ["--bind", str(path), str(path)]
        # 专用挂载放在可写父目录之后，避免父目录覆盖它们。
        command += ["--proc", "/proc", "--dev", "/dev", "--setenv", "TMPDIR", str(temporary)]
        for path in protected_files:
            command += ["--ro-bind", str(path), str(path)]
        # 遮蔽原内容；先处理父目录，已被遮蔽的子路径无需重复挂载。
        masked = []
        for path in sorted(set(self.blocked_paths), key=lambda path: len(path.parts)):
            if any(path.is_relative_to(parent) for parent in masked):
                continue
            if not path.exists():
                raise ValueError(f"Sandbox deny path does not exist: {path}")
            if path.is_dir():
                command += ["--tmpfs", str(path), "--chmod", "000", str(path),
                            "--remount-ro", str(path)]
            else:
                empty = env_file.parent / ".sandbox-denied"
                empty.touch(mode=0, exist_ok=True)
                empty.chmod(0)
                command += ["--ro-bind", str(empty), str(path)]
            masked.append(path)
        return [*command, "--", *map(str, argv)]
