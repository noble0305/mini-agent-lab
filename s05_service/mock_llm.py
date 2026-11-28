"""无密钥 mock 模式：用确定性桩客户端驱动真实的 S01 ReAct 循环。

它不是绕过 Agent，而是模拟 `client.chat.completions.create` 的返回结构，
因此 /chat 请求依然完整经过 ToolRegistry 参数校验、工具执行与 max_steps 熔断。
"""

from __future__ import annotations

import json
import re
import uuid
from types import SimpleNamespace
from typing import Any

from s01_react_loop.tools import safe_calc

_ALLOWED_CHARS = re.compile(r"[0-9.+\-*/()\s]{3,}")
_DATE_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LIST_HINTS = ("列出", "list file", "list files", "ls ")


def _trimmed_variants(candidate: str) -> list[str]:
    variants = [candidate]
    current = candidate
    while current and current[-1] in "+-*/(":
        current = current[:-1].rstrip()
        variants.append(current)
    return variants


def extract_expression(text: str) -> str | None:
    """从自然语言中抽取一条可被安全计算器执行的算术表达式。"""
    normalized = (
        text.replace("×", "*")
        .replace("÷", "/")
        .replace("（", "(")
        .replace("）", ")")
        .replace("　", " ")
    )
    for match in _ALLOWED_CHARS.finditer(normalized):
        candidate = match.group().strip()
        if _DATE_LIKE.match(candidate) or not re.search(r"\d", candidate):
            continue
        for variant in _trimmed_variants(candidate):
            if not re.search(r"\d", variant):
                continue
            if not any(op in variant for op in "+-*/"):
                continue
            try:
                safe_calc(variant)
            except Exception:
                continue
            return variant
    return None


def _response(content: str = "", tool_calls: list[dict[str, Any]] | None = None, tokens: int = 0):
    parsed = [
        SimpleNamespace(
            id=tc["id"],
            type="function",
            function=SimpleNamespace(name=tc["name"], arguments=tc["arguments"]),
        )
        for tc in (tool_calls or [])
    ]
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=parsed or None))],
        usage=SimpleNamespace(total_tokens=max(tokens, 0)),
    )


class _Completions:
    def __init__(self, owner: "MockChatClient") -> None:
        self._owner = owner

    def create(self, *, model: str, messages: list[dict[str, Any]], tools: Any = None, **_: Any):
        return self._owner._create(messages)


class MockChatClient:
    """按 OpenAI SDK 形状返回响应的确定性客户端。"""

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=_Completions(self))

    def _create(self, messages: list[dict[str, Any]]):
        user_text = next(
            (str(m.get("content") or "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        tool_messages = [m for m in messages if m.get("role") == "tool"]

        if tool_messages:
            observation = str(tool_messages[-1].get("content") or "")
            answer = f"[mock] 已通过 S01 ReAct 内核调用工具，结果：{observation}"
            return _response(content=answer, tokens=max(1, len(answer) // 4))

        expression = extract_expression(user_text)
        if expression:
            call = {
                "id": f"mock-{uuid.uuid4().hex[:8]}",
                "name": "calc",
                "arguments": json.dumps({"expression": expression}, ensure_ascii=False),
            }
            return _response(tool_calls=[call], tokens=max(1, len(user_text) // 4))

        lowered = user_text.lower()
        if any(hint in lowered for hint in _LIST_HINTS):
            call = {
                "id": f"mock-{uuid.uuid4().hex[:8]}",
                "name": "list_files",
                "arguments": json.dumps({"path": "."}),
            }
            return _response(tool_calls=[call], tokens=max(1, len(user_text) // 4))

        answer = (
            "[mock] 未配置 LLM key，当前为确定性 mock 模式。"
            f"收到：{user_text}"
        )
        return _response(content=answer, tokens=max(1, (len(user_text) + len(answer)) // 4))