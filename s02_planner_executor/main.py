"""S02 CLI 入口：支持同厂商或跨厂商配置 Planner（强模型）+ Executor（便宜模型）。"""

from __future__ import annotations

import os
from pathlib import Path
import sys

from dotenv import load_dotenv
from openai import OpenAI

from s01_react_loop.tools import build_default_registry
from s02_planner_executor.router import run_pipeline


def main() -> None:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("Missing OPENAI_API_KEY. Copy .env.example to .env and fill in your key.", file=sys.stderr)
        sys.exit(1)

    base_url = os.getenv("OPENAI_BASE_URL") or None
    default_model = os.getenv("OPENAI_MODEL", "deepseek-chat")
    planner_model = os.getenv("PLANNER_MODEL", default_model)
    executor_model = os.getenv("EXECUTOR_MODEL", default_model)

    planner_client = OpenAI(api_key=api_key, base_url=base_url)
    exec_key = os.getenv("EXECUTOR_API_KEY") or api_key
    exec_url = os.getenv("EXECUTOR_BASE_URL") or base_url
    executor_client = OpenAI(api_key=exec_key, base_url=exec_url) if (exec_key != api_key or exec_url != base_url) else planner_client

    prompt = " ".join(sys.argv[1:]).strip()
    if not prompt:
        prompt = "先检查当前项目有哪些文件和目录，再读 README.md 总结本仓库的路线图，最后计算 141 + 110 + 39。"

    print(f"Planner:  {planner_model}\nExecutor: {executor_model}\nTask:     {prompt}\n---")
    result = run_pipeline(
        planner_client=planner_client,
        executor_client=executor_client,
        planner_model=planner_model,
        executor_model=executor_model,
        task=prompt,
        registry=build_default_registry(Path.cwd()),
    )
    print(
        f"---\nStatus: {'ALL PASSED' if result.all_passed else 'PARTIAL'} | "
        f"Planner tokens={result.planner_tokens} | Executor tokens={result.executor_tokens} "
        f"({result.executor_token_ratio:.0%} offloaded to executor)"
    )
    print(result.final_answer)


if __name__ == "__main__":
    main()
