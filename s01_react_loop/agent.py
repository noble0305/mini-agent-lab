"""S01: 最小可运行 ReAct Agent Loop（约 140 行，零重框架）。"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Callable


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., str]

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """工具注册与安全分发器：参数校验与执行异常统一转字符串回传给 LLM。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [t.to_openai_schema() for t in self._tools.values()]

    def execute(self, name: str, raw_arguments: str) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: unknown tool '{name}'. Available: {list(self._tools)}"
        try:
            args = json.loads(raw_arguments or "{}")
            if not isinstance(args, dict):
                return "Error: tool arguments must be a JSON object."
        except json.JSONDecodeError as e:
            return f"Error: invalid JSON arguments for '{name}': {e}"
        try:
            return str(tool.fn(**args))
        except Exception as e:
            return f"Error executing '{name}': {type(e).__name__}: {e}"


@dataclass
class AgentResult:
    answer: str
    steps: int
    stopped_reason: str  # "completed" | "max_steps_reached"
    tool_calls_log: list[dict[str, Any]] = field(default_factory=list)
    total_tokens: int = 0


def run_agent(
    client: Any,
    model: str,
    user_prompt: str,
    registry: ToolRegistry,
    system_prompt: str = "You are a concise, accurate engineering assistant. Use tools when needed.",
    max_steps: int = 6,
    verbose: bool = True,
) -> AgentResult:
    """执行标准 ReAct 循环，直到模型直接回答或触发最大步数熔断。"""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    tools = registry.schemas() or None
    tool_calls_log: list[dict[str, Any]] = []
    total_tokens = 0

    for step in range(1, max_steps + 1):
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
        )
        if getattr(resp, "usage", None) and getattr(resp.usage, "total_tokens", None):
            total_tokens += resp.usage.total_tokens

        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if not tool_calls:
            return AgentResult(
                answer=(msg.content or "").strip(),
                steps=step,
                stopped_reason="completed",
                tool_calls_log=tool_calls_log,
                total_tokens=total_tokens,
            )

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        for tc in tool_calls:
            name = tc.function.name
            raw_args = tc.function.arguments
            observation = registry.execute(name, raw_args)
            tool_calls_log.append(
                {"step": step, "tool": name, "arguments": raw_args, "output": observation}
            )
            if verbose:
                print(f"[step {step}] call {name}({raw_args}) -> {observation[:160]}")
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": observation}
            )

    return AgentResult(
        answer=f"Stopped: reached max_steps={max_steps} without final answer.",
        steps=max_steps,
        stopped_reason="max_steps_reached",
        tool_calls_log=tool_calls_log,
        total_tokens=total_tokens,
    )
