"""离线单元测试：不依赖真实 LLM API，验证 ReAct Loop 与安全边界的核心行为。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from s01_react_loop.agent import run_agent
from s01_react_loop.tools import build_default_registry


def _make_response(content: str = "", tool_calls: list[dict] | None = None, tokens: int = 50):
    parsed_calls = []
    for tc in tool_calls or []:
        parsed_calls.append(
            SimpleNamespace(
                id=tc["id"],
                function=SimpleNamespace(name=tc["name"], arguments=tc["arguments"]),
            )
        )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=parsed_calls))],
        usage=SimpleNamespace(total_tokens=tokens),
    )


class FakeOpenAIClient:
    """按预设序列返回模型响应的极简桩客户端，同时记录每轮收到的 messages。"""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.recorded_messages: list[list[dict]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, tools):
        self.recorded_messages.append([dict(m) for m in messages])
        return self._responses.pop(0)


def test_multi_step_tool_calling(tmp_path: Path):
    (tmp_path / "note.txt").write_text("hello agent", encoding="utf-8")
    registry = build_default_registry(tmp_path)

    client = FakeOpenAIClient(
        [
            _make_response(
                tool_calls=[
                    {"id": "c1", "name": "read_text_file", "arguments": '{"path": "note.txt"}'},
                    {"id": "c2", "name": "calc", "arguments": '{"expression": "6 * 7"}'},
                ]
            ),
            _make_response(content="文件内容是 hello agent，计算结果是 42。"),
        ]
    )

    result = run_agent(client, "mock-model", "读文件并计算", registry, verbose=False)
    assert result.stopped_reason == "completed"
    assert result.steps == 2
    assert result.total_tokens == 100
    assert "hello agent" in result.answer and "42" in result.answer
    assert len(result.tool_calls_log) == 2


def test_tool_errors_and_sandbox_escape_are_returned_not_crashed(tmp_path: Path):
    registry = build_default_registry(tmp_path)
    client = FakeOpenAIClient(
        [
            _make_response(
                tool_calls=[
                    {"id": "c1", "name": "nonexistent", "arguments": "{}"},
                    {"id": "c2", "name": "calc", "arguments": "{bad json"},
                    {"id": "c3", "name": "read_text_file", "arguments": '{"path": "../../etc/passwd"}'},
                ]
            ),
            _make_response(content="已捕获异常并安全结束。"),
        ]
    )

    result = run_agent(client, "mock-model", "测试容错", registry, verbose=False)
    assert result.stopped_reason == "completed"
    outputs = [entry["output"] for entry in result.tool_calls_log]
    assert "unknown tool" in outputs[0]
    assert "invalid JSON" in outputs[1]
    assert "PermissionError" in outputs[2]


def test_max_steps_circuit_breaker(tmp_path: Path):
    registry = build_default_registry(tmp_path)
    loop_resp = _make_response(
        tool_calls=[{"id": "loop", "name": "calc", "arguments": '{"expression": "1 + 1"}'}]
    )
    client = FakeOpenAIClient([loop_resp, loop_resp, loop_resp])

    result = run_agent(client, "mock-model", "无限循环测试", registry, max_steps=3, verbose=False)
    assert result.stopped_reason == "max_steps_reached"
    assert result.steps == 3
    assert "max_steps=3" in result.answer
