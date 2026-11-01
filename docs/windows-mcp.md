# WSL 操作 Windows

harness 在 WSL 中运行，通过 Windows PowerShell 启动
[Windows-MCP](https://github.com/CursorTouch/Windows-MCP)。服务器必须运行在已登录的
Windows 桌面会话中，锁屏、UAC 安全桌面或更高权限的应用可能无法操作。

## 安装一次

已验证提交 `94b52cfb48076cb793e710351517fa18a316cd12` 包含 UIA 超时和控制状态传递修复。
当时 PyPI 版本同样标为 0.8.8，直接运行 `uvx windows-mcp` 不一定得到这些修复。
在 **Windows PowerShell** 中准备固定源码和 Python 3.14 环境：

```powershell
$commit = '94b52cfb48076cb793e710351517fa18a316cd12'
$parent = Join-Path $env:LOCALAPPDATA 'mini-harness\mcp'
$archive = Join-Path $env:TEMP "windows-mcp-$commit.zip"
New-Item -ItemType Directory -Force $parent | Out-Null
Invoke-WebRequest "https://github.com/CursorTouch/Windows-MCP/archive/$commit.zip" -OutFile $archive
Expand-Archive $archive -DestinationPath $parent -Force
$source = Join-Path $parent "Windows-MCP-$commit"
uvx --python 3.14 --from $source windows-mcp --help
Remove-Item $archive
```

前提是 Windows 已安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)。
最后一条 `uvx` 命令应成功退出，之后启动时使用本机缓存，不再访问包索引。
需要更新依赖时，在安装步骤中联网重新准备环境；不要自动追踪 upstream main。

## 启动 harness

在 WSL 项目根目录复制模板到已忽略的本机配置：

```bash
mkdir -p .mini-harness
cp examples/mcp.windows-wsl.json .mini-harness/mcp.windows.json
uv run --extra mcp mini-harness --mcp-config .mini-harness/mcp.windows.json
```

模板从 Windows 环境变量确定源码和 uvx 路径，没有个人用户名。
若 uv 安装位置不同，用 Windows 的 `Get-Command uvx` 找到路径后修改本机 JSON。
模板使用 `relaxed`：截图等只读操作不争抢桌面控制，直接点击、输入等仍保留控制保护。
用户操作鼠标键盘时可能返回 `USER_CONTROL`；按 `ControlStatus` 的等待提示再检查，
不要自动重放结果未确认的点击或提交。没有使用 `--no-control`。
配置只忽略已知弃用警告，其他错误仍会显示。harness 原有审批与 `--full-access` 独立生效。

## 模型怎样操作

先用 `list_mcp_tools(server="windows")` 看名称和简短描述，再指定 `tool_name` 查看
所需工具的完整说明和实际 schema，避免把整份目录塞进上下文。
视觉模型调用截图时可以直接带回图片：

```json
{
  "server": "windows",
  "tool_name": "Screenshot",
  "arguments": {},
  "include_images": true
}
```

这是 `call_mcp_tool` 的参数，`include_images` 属于 harness，不是服务器的参数。
截图仍会保存，工具结果同时带图片，不必再调用 `read_image`。文本模型保持默认 `false`，
使用 `Snapshot` 的 UI 树；图片路径本身不代表模型看到了图片。

原生 `Click`、`Type` 使用服务器返回的坐标，不要套用 PowerShell 自写脚本的 DPI 缩放比例。
裁剪或缩放截图时，按工具返回的原点与尺寸换算。`Screenshot` 的快速路径跳过 UI 树，
返回空窗口列表不代表应用不存在；需要控件或窗口信息时再调用 `Snapshot`。
`region` 用于截图裁剪，不保证同时缩小 UI 树的遍历范围。
遇到 UIA 超时先检查当前窗口，结果未确认时不要重复执行原动作。

本轮实测使用独立测试窗口：切换约 7.7 秒，三轮中文输入各约 1.4 秒、点击约 0.6 秒，
截图约 1.7 秒，UI 树约 3.1 秒。三轮未出现控制等待错误；这不是所有应用的性能保证。
