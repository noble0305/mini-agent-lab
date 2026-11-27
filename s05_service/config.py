"""S05 运行时配置：全部来自环境变量，不含任何默认密钥。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_TRUTHY = {"1", "true", "yes", "on"}


def _parse_bool(raw: str | None) -> bool | None:
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() in _TRUTHY


def _parse_int(raw: str | None, default: int) -> int:
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8080
    model: str = "deepseek-chat"
    openai_api_key: str = ""
    openai_base_url: str = ""
    log_dir: Path = Path("logs")
    workspace: Path = Path(".")
    max_steps: int = 6
    max_body_bytes: int = 64 * 1024
    mock_llm: bool = True
    service_name: str = "mini-agent-lab"
    version: str = "0.1.0"

    @property
    def mode(self) -> str:
        return "mock" if self.mock_llm else "llm"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        api_key = (env.get("OPENAI_API_KEY") or "").strip()
        # 显式 MOCK_LLM=1 强制 mock；未配置 key 时自动降级为 mock，永不因缺 key 启动失败。
        explicit_mock = _parse_bool(env.get("MOCK_LLM"))
        # 有 key 且未显式要求 mock 时走真实模型；其余组合一律安全降级为 mock。
        want_real = bool(api_key) and explicit_mock is not True
        return cls(
            host=(env.get("HOST") or "127.0.0.1").strip(),
            port=_parse_int(env.get("PORT"), 8080),
            model=(env.get("OPENAI_MODEL") or "deepseek-chat").strip(),
            openai_api_key=api_key,
            openai_base_url=(env.get("OPENAI_BASE_URL") or "").strip(),
            log_dir=Path(env.get("LOG_DIR") or "logs"),
            workspace=Path(env.get("WORKSPACE") or "."),
            max_steps=_parse_int(env.get("MAX_STEPS"), 6),
            max_body_bytes=_parse_int(env.get("MAX_BODY_BYTES"), 64 * 1024),
            mock_llm=not want_real,
        )