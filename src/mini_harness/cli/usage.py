"""显示 API 返回的缓存用量，并从现有会话记录汇总统计。"""

import json

from ..session import Session


def format_cache_usage(input_tokens: int, cached_tokens: int | None) -> str:
    """以输入 token 为分母；None 是未报告，0 是已报告零命中。"""
    if cached_tokens is None or input_tokens == 0:
        return "cache n/a"
    return f"cache {cached_tokens / input_tokens:.1%} ({cached_tokens:,}/{input_tokens:,})"


def format_session_usage(session: Session) -> str:
    """汇总已落盘的主请求；独立子会话与摘要调用不在统计范围内。"""
    usages = []
    if session.transcript.exists():
        with session.transcript.open(encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                if record["kind"] == "model":
                    usages.append(record["usage"])

    reported = [usage for usage in usages if usage]
    cache_reported = [
        usage for usage in reported
        if usage.get("input_token_details", {}).get("cache_read") is not None
    ]
    input_tokens = sum(usage["input_tokens"] for usage in reported)
    output_tokens = sum(usage["output_tokens"] for usage in reported)
    # 未报告缓存量的请求不进入分母；累计值按 token 加权，不平均单次百分比。
    cache_input_tokens = sum(usage["input_tokens"] for usage in cache_reported)
    cached_tokens = (
        sum(usage["input_token_details"]["cache_read"] for usage in cache_reported)
        if cache_reported else None
    )
    cache = format_cache_usage(cache_input_tokens, cached_tokens)
    return "\n".join([
        f"[stats] Main session {session.id} · {len(usages)} model requests",
        f"Reported tokens: {input_tokens:,} in / {output_tokens:,} out "
        f"({len(reported)}/{len(usages)} requests)",
        f"{cache} · reported for {len(cache_reported)}/{len(usages)} requests",
    ])
