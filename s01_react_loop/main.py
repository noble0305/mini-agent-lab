"""S01 CLI 入口：读取 .env 配置并运行最小 ReAct Agent。"""

from __future__ import annotations

import os
from pathlib import Path
import sys

from dotenv import load_dotenv
from openai import OpenAI

from s01_react_loop.agent import run_agent
from s01_react_loop.tools import build_default_registry


def main() -> None:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("Missing OPENAI_API_KEY. Copy .env.example to .env and fill in your key.", file=sys.stderr)
        sys.exit(1)

    base_url = os.getenv("OPENAI_BASE_URL") or None
    model = os.getenv("OPENAI_MODEL", "deepseek-chat")
    client = OpenAI(api_key=api_key, base_url=base_url)
    registry = build_default_registry(Path.cwd())

    prompt = " ".join(sys.argv[1:]).strip()
    if not prompt:
        prompt = "先看看当前目录有哪些文件，再算一下 365 * 24 等于多少。"

    print(f"Model: {model}\nPrompt: {prompt}\n---")
    result = run_agent(client=client, model=model, user_prompt=prompt, registry=registry)
    print(f"---\nAnswer ({result.stopped_reason}, steps={result.steps}, tokens={result.total_tokens}):")
    print(result.answer)


if __name__ == "__main__":
    main()
