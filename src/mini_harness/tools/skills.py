"""按需读取项目 .agents/skills/ 下的技能目录与指令。"""

import re
from pathlib import Path

import yaml

from ..session import Session


def _skills_directory(session: Session) -> Path:
    """固定使用初始项目目录；cd 不改变 skill 的发现范围。"""
    project = Path(session.get("project_root"))
    directory = (project / ".agents" / "skills").resolve()
    if not directory.is_relative_to(project):
        raise ValueError("Skill directory must stay inside the project")
    return directory


def skill_path(session: Session, name: str) -> Path:
    """按技能名定位 SKILL.md；拒绝路径穿越和指向目录外的符号链接。"""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise ValueError("Skill name must contain lowercase letters, numbers and hyphens")
    directory = _skills_directory(session)
    path = (directory / name / "SKILL.md").resolve()
    if not path.is_relative_to(directory):
        raise ValueError("Skill file must stay inside the project skill directory")
    return path


def _read_skill(path: Path, name: str, session: Session) -> tuple[dict, str]:
    """解析 YAML 元数据与 Markdown 正文；格式错误交给现有工具错误处理。"""
    if session.sandbox is not None:
        session.sandbox.check_path(path)
    text = path.read_text(encoding="utf-8")
    match = re.fullmatch(r"---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)(.*)", text, re.S)
    if not match:
        raise ValueError(f"Missing YAML frontmatter in {path}")
    try:
        metadata = yaml.safe_load(match[1])
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid YAML in {path}: {error}") from error
    if not isinstance(metadata, dict) or metadata.get("name") != name:
        raise ValueError(f"Skill name must match its directory name in {path}")
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"Missing skill description in {path}")
    instructions = match[2].strip()
    if not instructions:
        raise ValueError(f"Missing skill instructions in {path}")
    return {"name": name, "description": description.strip()}, instructions


def list_skills(session: Session) -> list[dict]:
    """读取技能名称与描述；正文和配套资料不加入目录结果。"""
    catalog = []
    for path in sorted(_skills_directory(session).glob("*/SKILL.md")):
        name = path.parent.name
        try:
            metadata, _ = _read_skill(skill_path(session, name), name, session)
        except PermissionError:
            continue
        catalog.append(metadata)
    return catalog


def load_skill(session: Session, name: str) -> dict:
    """返回完整正文及资源基准目录；每次调用读取当前文件，不执行脚本。"""
    path = skill_path(session, name)
    _, instructions = _read_skill(path, name, session)
    return {"name": name, "directory": str(path.parent), "instructions": instructions}
