"""文件访问策略和本次运行的额外授权；不写入会话数据库。"""

import json
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
        """禁止路径优先；env.sh 是会话目录内唯一默认可写的工具文件。"""
        path = Path(path).resolve()
        if any(path.is_relative_to(denied) for denied in self.deny_paths):
            raise PermissionError(f"Sandbox denies access: {path}")
        if not writing:
            return
        if path == self.config_path:
            raise PermissionError(f"Sandbox configuration is read-only: {path}")
        if env_file is not None and path == env_file.resolve():
            return
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
        self.extra_write_paths.update(self.write_paths(paths))

    def status(self):
        """放入运行上下文，旧对话中的授权文字不代表当前有效权限。"""
        return {
            "write_paths": sorted(map(str, (self.project_root, *self.extra_write_paths))),
            "deny_paths": list(map(str, self.deny_paths)),
            "extra_grants": "Valid only until harness exits; not restored from history.",
        }

    def require_runtime(self) -> str:
        """显式开启后，缺少隔离能力就报错，不能退回普通执行。"""
        executable = shutil.which("bwrap")
        if sys.platform != "linux" or executable is None:
            raise RuntimeError("Sandbox requires Linux/WSL2 and Bubblewrap (install bubblewrap)")
        return executable

    def command(self, argv: list[str], *, env_file: Path, cwd_file: Path) -> list[str]:
        """主机只读、指定路径可写；共享网络，隔离 PID 并随父进程退出。"""
        temporary = env_file.parent / "tmp"
        temporary.mkdir(exist_ok=True)
        command = [
            self.require_runtime(), "--unshare-all", "--share-net", "--unshare-user",
            "--disable-userns", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
            "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
            "--tmpfs", str(temporary), "--setenv", "TMPDIR", str(temporary),
        ]
        writable = {self.project_root, *self.extra_write_paths, env_file, cwd_file}
        for path in sorted(writable, key=lambda path: (len(path.parts), str(path))):
            command += ["--bind", str(path), str(path)]
        if self.config_path is not None:
            command += ["--ro-bind", str(self.config_path), str(self.config_path)]
        # 遮蔽原内容；先处理父目录，已被遮蔽的子路径无需重复挂载。
        masked = []
        for path in sorted(set(self.deny_paths), key=lambda path: len(path.parts)):
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
