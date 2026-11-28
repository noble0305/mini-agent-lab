"""S05 Agent 运行时：复用 S01 的 ToolRegistry 与 run_agent，不重写推理循环。"""

from __future__ import annotations

from typing import Any

from s01_react_loop.agent import ToolRegistry, run_agent
from s01_react_loop.tools import build_default_registry
from s05_service.config import Settings


class AgentRuntime:
    def __init__(
        self,
        settings: Settings,
        registry: ToolRegistry | None = None,
        client: Any | None = None,
    ) -> None:
        self.settings = settings
        self._registry = registry or build_default_registry(settings.workspace)
        self._client = client if client is not None else self._build_client()

    @property
    def mode(self) -> str:
        return self.settings.mode

    def _build_client(self) -> Any:
        if self.settings.mock_llm:
            from s05_service.mock_llm import MockChatClient

            return MockChatClient()
        # 真实模式才导入 openai，mock 模式零第三方依赖即可运行。
        from openai import OpenAI

        return OpenAI(
            api_key=self.settings.openai_api_key,
            base_url=self.settings.openai_base_url or None,
        )

    def chat(self, message: str, request_id: str) -> dict[str, Any]:
        result = run_agent(
            client=self._client,
            model=self.settings.model,
            user_prompt=message,
            registry=self._registry,
            max_steps=self.settings.max_steps,
            verbose=False,
        )
        return {
            "request_id": request_id,
            "mode": self.mode,
            "model": self.settings.model if not self.settings.mock_llm else "mock-deterministic",
            "answer": result.answer,
            "steps": result.steps,
            "stopped_reason": result.stopped_reason,
            "total_tokens": result.total_tokens,
            "tool_calls": result.tool_calls_log,
        }