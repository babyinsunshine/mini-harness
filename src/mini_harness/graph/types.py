"""State 写入检查点；Resources 只在当前进程中使用。"""

from dataclasses import dataclass

from langchain_core.messages import SystemMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.graph import MessagesState

from ..context.manager import ContextManager
from ..session import Session
from ..tools.permissions import Permissions
from ..tools.processes import ProcessManager


class State(MessagesState):
    """从 MessagesState 继承 messages 字段和消息合并规则。

    pending_calls 是待处理的工具调用；next 是回复校验后选择的节点。
    last_user_input_id 在压缩后仍保留，防止恢复时重新提交已接收的子任务指令。
    """
    pending_calls: list[dict]
    approved: bool
    repair_attempts: int
    next: str
    last_user_input_id: str


@dataclass
class Resources:
    """通过 runtime.context 注入的运行资源，不写入检查点。"""

    session: Session
    processes: ProcessManager
    context_manager: ContextManager
    permissions: Permissions
    model: Runnable
    tools: dict[str, BaseTool]
    system_message: SystemMessage
