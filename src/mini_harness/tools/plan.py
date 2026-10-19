"""单会话的一份当前计划：整表更新，复用会话存储与事件记录。"""

from typing import Literal

from pydantic import BaseModel

from ..session import Session


class PlanStep(BaseModel):
    """工具参数结构，同时告诉模型每个步骤需要哪些字段。"""

    step: str
    status: Literal["pending", "in_progress", "completed"]


def update_plan(session: Session, plan: list[PlanStep], explanation: str | None = None) -> dict:
    """校验后替换当前计划；空列表清空计划，错误输入不覆盖旧数据。"""
    if any(not item.step.strip() for item in plan):
        raise ValueError("Plan steps must not be empty")
    if sum(item.status == "in_progress" for item in plan) > 1:
        raise ValueError("At most one plan step can be in_progress")
    result = {
        "plan": [{"step": item.step.strip(), "status": item.status} for item in plan],
        "explanation": explanation,
    }
    session.set("plan", result)
    session.record("plan", **result)
    return result
