"""S03: 上下文自动压缩（滚动摘要）+ 基于纯 Markdown 文件的跨会话持久化记忆。"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from s01_react_loop.agent import Tool, ToolRegistry


class FileMemory:
    """极简纯 Markdown 持久化记忆存储（零数据库，人类可直接用编辑器/Obsidian 查看）。"""

    def __init__(self, path: Path, max_chars: int = 3000) -> None:
        self.path = path.resolve()
        self.max_chars = max_chars

    def read(self) -> str:
        if not self.path.exists():
            return ""
        text = self.path.read_text(encoding="utf-8").strip()
        if len(text) > self.max_chars:
            return text[: self.max_chars] + f"\n... [memory truncated at {self.max_chars} chars]"
        return text

    def append(self, section: str, fact: str) -> str:
        clean_section = (section or "General").strip()
        clean_fact = (fact or "").strip()
        if not clean_fact:
            return "Error: fact cannot be empty."
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.path.read_text(encoding="utf-8") if self.path.exists() else "# Agent Memory\n"
        entry = f"- [{clean_section}] {clean_fact}\n"
        if entry in existing:
            return f"Already recorded in {self.path.name}: {clean_fact}"
        sep = "" if existing.endswith("\n") else "\n"
        self.path.write_text(existing + sep + entry, encoding="utf-8")
        return f"Saved to {self.path.name}: [{clean_section}] {clean_fact}"


def register_memory_tools(registry: ToolRegistry, memory: FileMemory) -> ToolRegistry:
    """往已有 ToolRegistry 挂载长期记忆读写工具。"""
    registry.register(
        Tool(
            name="memory_read",
            description="Read persistent cross-session facts, user preferences, and notes from memory.md.",
            parameters={"type": "object", "properties": {}},
            fn=lambda: memory.read() or "(memory is empty)",
        )
    )
    registry.register(
        Tool(
            name="memory_write",
            description="Persist a durable fact, decision, or user preference into memory.md for future sessions.",
            parameters={
                "type": "object",
                "properties": {
                    "section": {"type": "string", "description": "Short category like Profile, Decision, Progress"},
                    "fact": {"type": "string", "description": "One concise durable fact to remember"},
                },
                "required": ["section", "fact"],
            },
            fn=memory.append,
        )
    )
    return registry


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """零依赖轻量 Token 估算（中英混排按约 2.5~3 字符 ≈ 1 token 保守估算）。"""
    total_chars = 0
    for m in messages:
        total_chars += len(str(m.get("content") or ""))
        if m.get("tool_calls"):
            total_chars += len(json.dumps(m["tool_calls"], ensure_ascii=False))
    return max(1, total_chars // 3)


def find_safe_split_index(history: list[dict[str, Any]], keep_recent: int) -> int:
    """
    在不含 system 的对话历史中找到安全切分点（保留 history[split_idx:]）。
    关键防坑：绝不能从 role='tool' 开始保留，否则孤立的 tool 消息会触发 API 400 报错。
    """
    if len(history) <= keep_recent:
        return 0
    split_idx = len(history) - keep_recent
    while split_idx > 0 and history[split_idx].get("role") == "tool":
        split_idx -= 1
    return split_idx


def compact_history(
    client: Any,
    model: str,
    history: list[dict[str, Any]],
    keep_recent: int = 4,
) -> tuple[list[dict[str, Any]], int, bool]:
    """将早期历史折叠为一条滚动摘要，保留最近完整回合。返回 (新历史, 压缩消耗tokens, 是否执行了压缩)。"""
    split_idx = find_safe_split_index(history, keep_recent)
    if split_idx <= 0:
        return history, 0, False

    old_chunk = history[:split_idx]
    recent_chunk = history[split_idx:]
    transcript = "\n".join(
        f"{m.get('role')}: {str(m.get('content') or '')[:300]}"
        for m in old_chunk
    )

    tokens = 0
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Compress the following conversation segment into <= 6 concise bullet points. "
                        "Keep key facts, file findings, decisions, and open issues; drop chatter."
                    ),
                },
                {"role": "user", "content": transcript},
            ],
        )
        tokens = getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
        summary = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        summary = f"[Compaction fallback after error: {type(e).__name__}] Dropped {len(old_chunk)} older messages."

    summary_msg = {
        "role": "user",
        "content": f"[Conversation Summary — compacted {len(old_chunk)} earlier messages]\n{summary}",
    }
    return [summary_msg, *recent_chunk], tokens, True


@dataclass
class TurnResult:
    answer: str
    steps: int
    stopped_reason: str
    compacted: bool = False
    estimated_tokens_before: int = 0
    estimated_tokens_after: int = 0
    total_tokens: int = 0
    tool_calls_log: list[dict[str, Any]] = field(default_factory=list)


class MemorySession:
    """支持多轮对话、超预算自动压缩与跨会话文件记忆的轻量会话管理器。"""

    def __init__(
        self,
        client: Any,
        model: str,
        registry: ToolRegistry,
        memory: FileMemory,
        base_system_prompt: str = (
            "You are a concise engineering assistant. "
            "When the user shares durable preferences, decisions, or facts worth remembering across sessions, "
            "call memory_write to save them."
        ),
        token_budget: int = 1200,
        keep_recent: int = 4,
        max_steps: int = 6,
        verbose: bool = True,
    ) -> None:
        self.client = client
        self.model = model
        self.registry = registry
        self.memory = memory
        self.base_system_prompt = base_system_prompt
        self.token_budget = token_budget
        self.keep_recent = keep_recent
        self.max_steps = max_steps
        self.verbose = verbose
        self.history: list[dict[str, Any]] = []
        self.compaction_count: int = 0

    def _build_system_message(self) -> dict[str, str]:
        mem_text = self.memory.read()
        if not mem_text:
            return {"role": "system", "content": self.base_system_prompt}
        return {
            "role": "system",
            "content": f"{self.base_system_prompt}\n\n## Persistent Memory (from {self.memory.path.name})\n{mem_text}",
        }

    def ask(self, user_prompt: str) -> TurnResult:
        self.history.append({"role": "user", "content": user_prompt})
        compacted = False
        total_tokens = 0
        tool_calls_log: list[dict[str, Any]] = []

        est_before = estimate_tokens([self._build_system_message(), *self.history])
        if est_before > self.token_budget:
            new_hist, c_tokens, did_compact = compact_history(
                self.client, self.model, self.history, keep_recent=self.keep_recent
            )
            if did_compact:
                self.history = new_hist
                self.compaction_count += 1
                compacted = True
                total_tokens += c_tokens
                if self.verbose:
                    after_c = estimate_tokens([self._build_system_message(), *self.history])
                    print(f"[compaction #{self.compaction_count}] est tokens: {est_before} -> {after_c}")

        tools = self.registry.schemas() or None
        for step in range(1, self.max_steps + 1):
            messages = [self._build_system_message(), *self.history]
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
            )
            if getattr(resp, "usage", None) and getattr(resp.usage, "total_tokens", None):
                total_tokens += resp.usage.total_tokens

            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None) or []

            if not tool_calls:
                answer = (msg.content or "").strip()
                self.history.append({"role": "assistant", "content": answer})
                est_after = estimate_tokens([self._build_system_message(), *self.history])
                return TurnResult(
                    answer=answer,
                    steps=step,
                    stopped_reason="completed",
                    compacted=compacted,
                    estimated_tokens_before=est_before,
                    estimated_tokens_after=est_after,
                    total_tokens=total_tokens,
                    tool_calls_log=tool_calls_log,
                )

            self.history.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in tool_calls
                    ],
                }
            )
            for tc in tool_calls:
                obs = self.registry.execute(tc.function.name, tc.function.arguments)
                tool_calls_log.append(
                    {"step": step, "tool": tc.function.name, "arguments": tc.function.arguments, "output": obs}
                )
                if self.verbose:
                    print(f"[step {step}] call {tc.function.name}({tc.function.arguments}) -> {obs[:140]}")
                self.history.append({"role": "tool", "tool_call_id": tc.id, "content": obs})

        est_after = estimate_tokens([self._build_system_message(), *self.history])
        return TurnResult(
            answer=f"Stopped: reached max_steps={self.max_steps}.",
            steps=self.max_steps,
            stopped_reason="max_steps_reached",
            compacted=compacted,
            estimated_tokens_before=est_before,
            estimated_tokens_after=est_after,
            total_tokens=total_tokens,
            tool_calls_log=tool_calls_log,
        )
