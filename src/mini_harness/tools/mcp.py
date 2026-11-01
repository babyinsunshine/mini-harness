"""连接配置好的 MCP 服务；只向模型提供发现和调用两个入口。"""

import base64
import hashlib
import json
import mimetypes
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from langchain_core.tools import tool


@asynccontextmanager
async def connect_mcp(config_path: Path | None, shared=None):
    """主会话持有连接；子会话复用 shared，不重复启动或关闭服务器。"""
    if shared is not None:
        yield shared
        return
    clients = {}
    async with AsyncExitStack() as stack:
        if config_path is not None:
            try:
                from fastmcp.client import Client
                from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
            except ImportError as error:
                raise RuntimeError("Install MCP support with: uv sync --extra mcp") from error
            servers = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]
            for name, settings in servers.items():
                if "command" in settings:
                    cwd = (config_path.parent / settings.get("cwd", ".")).resolve()
                    transport = StdioTransport(
                        command=settings["command"], args=settings.get("args", []),
                        env=settings.get("env"), cwd=str(cwd),
                        keep_alive=False,  # 连接保持到会话退出；退出时也结束服务器进程。
                    )
                else:
                    transport = StreamableHttpTransport(
                        url=settings["url"], headers=settings.get("headers"),
                    )
                clients[name] = await stack.enter_async_context(
                    Client(transport, timeout=60, init_timeout=60)
                )
        yield clients


def create_mcp_tools(session, clients):
    @tool
    async def list_mcp_tools(server: str | None = None, tool_name: str | None = None) -> dict:
        """Discover MCP servers, then tool summaries, then a selected tool's full schema.

        Omit server to list servers. Supply server for tool names and short descriptions.
        Set tool_name to read that tool's complete description and JSON input schema
        before calling it. Long results are saved to output_path; inspect with read_file.
        """
        if server is None:
            return {"servers": list(clients)}
        catalog = []
        for item in await clients[server].list_tools():
            if tool_name is not None and item.name != tool_name:
                continue
            entry = {"name": item.name, "description": (item.description or "")[:200]}
            if tool_name is not None:
                entry.update(description=item.description, input_schema=item.input_schema)
            catalog.append(entry)
        return session.prepare_tool_output(json.dumps(catalog, ensure_ascii=False, indent=2))

    @tool
    async def call_mcp_tool(
        server: str, tool_name: str, arguments: dict, include_images: bool = False,
    ) -> dict | list[dict]:
        """Call a discovered MCP tool with arguments matching its input_schema.

        Only configured servers are available. External actions follow permission checks.
        Long text is saved to output_path. Images/audio are always saved as files.
        For vision models, set include_images=True to see screenshots in this result
        without another read_image call. Text-only models should leave it False.
        """
        result = await clients[server].call_tool(tool_name, arguments)
        parts, images = [], []
        for block in result.content:
            if block.type == "text":
                parts.append(block.text)
            elif block.type in {"image", "audio"}:
                suffix = mimetypes.guess_extension(block.mime_type) or ".bin"
                data = base64.b64decode(block.data)
                # 与 read_image 共用原始字节哈希，重新查看同一截图不再复制文件。
                path = session.artifacts / (hashlib.sha256(data).hexdigest() + suffix)
                if not path.exists():
                    path.write_bytes(data)
                parts.append(f"MCP {block.type} saved to: {path}")
                if block.type == "image" and include_images:
                    images.append({"type": "image", "base64": block.data,
                                   "mime_type": block.mime_type})
            else:
                parts.append(block.model_dump_json(by_alias=True, exclude_none=True))
        output = "\n".join(parts)
        if result.structured_content is not None:
            # 结构化字段可能包含正文里没有的数据；统一经过长度限制和落盘。
            output = json.dumps({"text": output, "structured_content": result.structured_content},
                                ensure_ascii=False)
        prepared = session.prepare_tool_output(output)
        if images:
            return [{"type": "text", "text": json.dumps(prepared, ensure_ascii=False)}, *images]
        return prepared

    return [list_mcp_tools, call_mcp_tool]
