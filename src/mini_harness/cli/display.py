"""把执行事件变成简短终端文本；正文与状态的输出位置由 CLI 控制。"""

import json
import sys


def format_event(event) -> str:
    event_type = event["type"]
    if event_type == "model_start":
        return "[model] Thinking…"
    if event_type == "model_end":
        details = [f"{event['elapsed']:.2f}s"]
        if usage := event["usage"]:
            details.append(f"{usage['input_tokens']:,} in / {usage['output_tokens']:,} out")
        if event["finish_reason"]:
            details.append(event["finish_reason"])
        return "[model] " + " · ".join(details)
    if event_type == "tool_start":
        tool_args = event["args"]
        target = tool_args.get("command", tool_args.get("path", tool_args.get("task_id", "")))
        return f"[tool] {event['name']}: {str(target)[:160]}"
    if event_type == "tool_denied":
        return f"[denied] {event['name']}: {event['reason']}"
    if event_type == "repair":
        return f"[repair {event['attempt']}/2] {event['reason']}"
    if event_type == "compact_start":
        return "[context] Compacting…"
    if event_type == "compact_end":
        return f"[context] {event['before']:,} → {event['after']:,} estimated tokens"
    if event_type == "background":
        task = event["task"]
        return f"[task] {task['task_id']}: {task['status']} (exit {task['exit_code']})"
    if event_type != "tool_end":
        return ""

    tool_output = event["output"]
    try:
        parsed_output = json.loads(tool_output)
    except (ValueError, TypeError):
        parsed_output = {}
    output_details = parsed_output if isinstance(parsed_output, dict) else {}
    if event["name"] == "update_plan" and event["status"] == "success":
        plan = output_details["plan"]
        completed = sum(item["status"] == "completed" for item in plan)
        parts = [f"[plan] {completed}/{len(plan)} completed"]
        if explanation := output_details.get("explanation"):
            parts.append(explanation)
        markers = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        parts.extend(f"{markers[item['status']]} {item['step']}" for item in plan)
        return "\n".join(parts)
    preview = str(output_details.get("output", output_details.get("preview", tool_output)))
    # Bash 的最后几行通常最有价值；文件内容从选区开头展示。
    lines = preview.splitlines()
    selected = lines[-6:] if event["name"] == "bash" else lines[:6]
    excerpt = "\n".join(line[-200:] if event["name"] == "bash" else line[:200] for line in selected)
    status = event["status"]
    if output_details.get("exit_code") not in (None, 0):
        status = f"exit {output_details['exit_code']}"
    parts = [f"[{status}] {event['name']} ({event['elapsed']:.2f}s)"]
    if excerpt:
        parts.append(excerpt)
    if len(lines) > 6:
        parts.append("[… preview shortened …]")
    if output_details.get("output_path"):
        parts.append(f"Full output: {output_details['output_path']}")
    if output_details.get("has_more"):
        parts.append(f"Continue with offset={output_details['next_offset']}")
    return "\n".join(parts)


async def display_events(harness, user_input):
    """正文保留在 stdout；执行状态和短预览显示在 stderr。"""
    text_line_open = False
    async for event in harness.events(user_input):
        if event["type"] == "text":
            print(event["text"], end="", flush=True)
            text_line_open = True
        elif status := format_event(event):
            if text_line_open:
                print(flush=True)
                text_line_open = False
            print(status, file=sys.stderr, flush=True)
    if text_line_open:
        print()
