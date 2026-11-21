"""S03 离线单元测试：验证跨会话 Markdown 记忆、超预算自动压缩、tool_call 对完整边界与压缩降级。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from s01_react_loop.agent import ToolRegistry
from s03_context_memory.memory_agent import (
    FileMemory,
    MemorySession,
    find_safe_split_index,
    register_memory_tools,
)


def _make_response(content: str = "", tool_calls: list[dict] | None = None, tokens: int = 30):
    parsed_calls = [
        SimpleNamespace(
            id=tc["id"],
            function=SimpleNamespace(name=tc["name"], arguments=tc["arguments"]),
        )
        for tc in (tool_calls or [])
    ]
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=parsed_calls))],
        usage=SimpleNamespace(total_tokens=tokens),
    )


class FakeClient:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.recorded_messages: list[list[dict]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, tools=None):
        self.recorded_messages.append([dict(m) for m in messages])
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def test_cross_session_file_memory_persistence(tmp_path: Path):
    mem_file = tmp_path / "memory.md"
    memory = FileMemory(mem_file)
    registry = register_memory_tools(ToolRegistry(), memory)

    # Session 1 writes a durable fact via tool call
    client1 = FakeClient(
        [
            _make_response(
                tool_calls=[
                    {
                        "id": "m1",
                        "name": "memory_write",
                        "arguments": '{"section": "Preference", "fact": "Always prefer minimal runnable examples"}',
                    }
                ]
            ),
            _make_response(content="已记住你的偏好。"),
        ]
    )
    s1 = MemorySession(client1, "mock-model", registry, memory, verbose=False)
    r1 = s1.ask("请记住我偏好最小可运行示例")
    assert r1.stopped_reason == "completed"
    assert "minimal runnable examples" in mem_file.read_text(encoding="utf-8")

    # Session 2 starts completely fresh with the same memory file
    client2 = FakeClient([_make_response(content="你偏好最小可运行示例。")])
    s2 = MemorySession(client2, "mock-model", registry, FileMemory(mem_file), verbose=False)
    r2 = s2.ask("我有什么学习偏好？")
    assert r2.stopped_reason == "completed"
    # Verify system prompt injected the persisted memory automatically
    sys_prompt = client2.recorded_messages[0][0]["content"]
    assert "Always prefer minimal runnable examples" in sys_prompt


def test_compaction_triggers_when_over_budget(tmp_path: Path):
    memory = FileMemory(tmp_path / "memory.md")
    registry = register_memory_tools(ToolRegistry(), memory)
    client = FakeClient(
        [
            _make_response(content="A" * 240),  # turn 1 reply
            _make_response(content="B" * 240),  # turn 2 reply
            _make_response(content="- Summary: covered topic A and B"),  # compaction summarizer call
            _make_response(content="第三轮正常回答。"),  # turn 3 reply
        ]
    )
    session = MemorySession(
        client, "mock-model", registry, memory, token_budget=300, keep_recent=2, verbose=False
    )

    r1 = session.ask("第一轮长问题：" + "X" * 180)
    r2 = session.ask("第二轮长问题：" + "Y" * 180)
    r3 = session.ask("第三轮问题")

    assert r1.compacted is False
    assert r2.compacted is False
    assert r3.compacted is True
    assert session.compaction_count == 1
    assert any("[Conversation Summary" in m["content"] for m in session.history)
    assert r3.answer == "第三轮正常回答。"


def test_safe_split_never_breaks_tool_call_and_tool_response_pair():
    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "obs1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    # keep_recent=3 would naively slice at index 2 (role="tool"), which breaks OpenAI API protocol.
    split_idx = find_safe_split_index(history, keep_recent=3)
    assert split_idx == 1
    assert history[split_idx]["role"] == "assistant"


def test_compaction_graceful_fallback_on_summarizer_error(tmp_path: Path):
    memory = FileMemory(tmp_path / "memory.md")
    registry = register_memory_tools(ToolRegistry(), memory)
    client = FakeClient(
        [
            _make_response(content="R" * 300),
            RuntimeError("upstream timeout"),  # summarizer fails
            _make_response(content="降级后依然正常回答。"),
        ]
    )
    session = MemorySession(
        client, "mock-model", registry, memory, token_budget=100, keep_recent=1, verbose=False
    )
    session.ask("Q1 " + "Z" * 300)
    r2 = session.ask("Q2")
    assert r2.compacted is True
    assert r2.answer == "降级后依然正常回答。"
    assert "Compaction fallback" in session.history[0]["content"]
