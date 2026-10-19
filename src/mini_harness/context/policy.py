"""上下文窗口、压缩门槛和保留范围的配置。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextPolicy:
    """容量与余量的单位为 token；rounds 指完整消息组，seconds 的单位为秒。"""

    window: int = 128000
    reserve: int = 8192
    auto_buffer: int = 13000
    block_buffer: int = 3000
    keep_rounds: int = 4
    micro_keep_rounds: int = 2
    micro_keep_tokens: int = 40000
    micro_min_savings: int = 20000
    micro_idle_seconds: float = 300
    auto: bool = True

    def __post_init__(self):
        if not 0 < self.reserve < self.window:
            raise ValueError("output reserve must be positive and smaller than the window")
        if not 0 <= self.block_buffer <= self.auto_buffer < self.window - self.reserve:
            raise ValueError("buffers must satisfy 0 <= block <= auto < window - reserve")
        if min(
            self.keep_rounds, self.micro_keep_rounds, self.micro_keep_tokens, self.micro_min_savings
        ) < 0 or self.micro_idle_seconds <= 0:
            raise ValueError("retention and Micro thresholds are invalid")

    @property
    def auto_limit(self) -> int:
        return self.window - self.reserve - self.auto_buffer

    @property
    def block_limit(self) -> int:
        return self.window - self.reserve - self.block_buffer
