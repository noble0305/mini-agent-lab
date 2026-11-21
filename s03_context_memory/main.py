"""S03 CLI 入口：体验多轮自动上下文压缩 + 跨会话纯 Markdown 持久化记忆。"""

from __future__ import annotations

import os
from pathlib import Path
import sys

from dotenv import load_dotenv
from openai import OpenAI

from s01_react_loop.tools import build_default_registry
from s03_context_memory.memory_agent import FileMemory, MemorySession, register_memory_tools


def main() -> None:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("Missing OPENAI_API_KEY. Copy .env.example to .env and fill in your key.", file=sys.stderr)
        sys.exit(1)

    base_url = os.getenv("OPENAI_BASE_URL") or None
    model = os.getenv("OPENAI_MODEL", "deepseek-chat")
    token_budget = int(os.getenv("S03_TOKEN_BUDGET", "1200"))

    workspace = Path.cwd()
    memory_path = workspace / ".agent_memory.md"
    memory = FileMemory(memory_path)
    registry = register_memory_tools(build_default_registry(workspace), memory)
    client = OpenAI(api_key=api_key, base_url=base_url)

    session = MemorySession(
        client=client,
        model=model,
        registry=registry,
        memory=memory,
        token_budget=token_budget,
    )

    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:]).strip()
        res = session.ask(prompt)
        print(f"Answer (steps={res.steps}, compacted={res.compacted}, est_tokens={res.estimated_tokens_after}):")
        print(res.answer)
        return

    print(f"S03 MemorySession | model={model} | memory={memory_path.name} | budget={token_budget} est tokens")
    print("Type 'exit' to quit (restart later to verify cross-session persistence).\n")
    while True:
        try:
            user_input = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input or user_input.lower() in {"exit", "quit"}:
            break
        res = session.ask(user_input)
        print(f"Agent> {res.answer}")
        print(f"  [steps={res.steps} | compacted={res.compacted} | est_tokens={res.estimated_tokens_after}]\n")


if __name__ == "__main__":
    main()
