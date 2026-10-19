"""提示词按用途存放为 Markdown 文件。"""

from pathlib import Path


def load_prompt(name: str) -> str:
    """name 是不含扩展名的文件名；读取对应 Markdown 的原始文本。"""
    return Path(__file__).with_name(f"{name}.md").read_text(encoding="utf-8")
