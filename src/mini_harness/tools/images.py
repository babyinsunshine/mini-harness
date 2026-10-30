"""读取 PNG/JPEG，保存原始快照，并返回模型可消费的图片块。"""

import base64
import hashlib

from ..session import Session
from .file_guard import access_file

MAX_IMAGE_BYTES = 5 * 1024 * 1024


def read_image(session: Session, path: str) -> list[dict]:
    """按文件头识别格式；限制读取量，不解码或缩放图片。"""
    target = session.resolve_path(path)
    with access_file(session, target, record_version=False):
        with target.open("rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds the 5 MiB limit")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime_type, suffix = "image/png", ".png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime_type, suffix = "image/jpeg", ".jpg"
    else:
        raise ValueError("Only PNG and JPEG images are supported")
    saved = session.artifacts / (hashlib.sha256(data).hexdigest() + suffix)
    if not saved.exists():
        saved.write_bytes(data)
    return [
        {"type": "text", "text": f"Image saved to: {saved}\nUse read_image to view it again."},
        {"type": "image", "base64": base64.b64encode(data).decode(), "mime_type": mime_type},
    ]
