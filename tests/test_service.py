"""S05 离线单元测试：真实启动标准库 HTTP 服务，验证路由、mock ReAct 执行与 JSONL 日志。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterator
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from s05_service.app import make_server
from s05_service.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=0,
        mock_llm=True,
        log_dir=tmp_path / "logs",
        workspace=tmp_path,
    )


@pytest.fixture()
def service(tmp_path: Path) -> Iterator[tuple[str, Settings]]:
    settings = _settings(tmp_path)
    server = make_server(settings, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", settings
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(base_url: str, method: str, path: str, payload: bytes | None = None):
    request = Request(base_url + path, data=payload, method=method)
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_health_endpoint_reports_mock_mode(service):
    base_url, _ = service
    status, body = _request(base_url, "GET", "/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["mode"] == "mock"
    assert body["service"] == "mini-agent-lab"


def test_chat_uses_real_s01_react_tool_loop(service):
    base_url, _ = service
    status, body = _request(base_url, "POST", "/chat", json.dumps({"message": "计算 365 * 24"}).encode())
    assert status == 200
    assert body["mode"] == "mock"
    assert body["stopped_reason"] == "completed"
    assert body["steps"] == 2
    assert "8760" in body["answer"]
    assert len(body["tool_calls"]) == 1
    assert body["tool_calls"][0]["tool"] == "calc"
    assert body["tool_calls"][0]["output"] == "8760"
    assert body["request_id"]


def test_chat_without_arithmetic_returns_mock_answer(service):
    base_url, _ = service
    status, body = _request(base_url, "POST", "/chat", json.dumps({"message": "你好"}).encode())
    assert status == 200
    assert body["stopped_reason"] == "completed"
    assert "mock" in body["answer"]


def test_request_validation_and_routing(service):
    base_url, _ = service
    status, body = _request(base_url, "POST", "/chat", b"{not json")
    assert status == 400 and body["error"] == "invalid JSON body"

    status, body = _request(base_url, "POST", "/chat", json.dumps({"message": "  "}).encode())
    assert status == 422 and "message" in body["error"]

    status, body = _request(base_url, "GET", "/nope")
    assert status == 404 and body["error"] == "not_found"

    status, body = _request(base_url, "POST", "/nope", b"{}")
    assert status == 404 and body["error"] == "not_found"


def test_requests_are_appended_to_jsonl_without_secrets(service):
    base_url, settings = service
    _request(base_url, "GET", "/health")
    _request(base_url, "POST", "/chat", json.dumps({"message": "计算 12 + 8"}).encode())

    log_path = Path(settings.log_dir) / "requests.jsonl"
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(records) == 2
    assert records[0]["path"] == "/health"
    assert records[1]["path"] == "/chat"
    assert records[1]["status"] == 200
    assert "request_id" in records[1]
    assert "latency_ms" in records[1]
    assert records[1]["prompt"] == "计算 12 + 8"
    assert "api_key" not in json.dumps(records).lower()


def test_settings_auto_fallback_to_mock_without_key():
    assert Settings.from_env({}).mock_llm is True
    assert Settings.from_env({"MOCK_LLM": "1", "OPENAI_API_KEY": "sk-test"}).mock_llm is True
    real = Settings.from_env({"OPENAI_API_KEY": "sk-test", "OPENAI_MODEL": "demo-model"})
    assert real.mock_llm is False and real.mode == "llm" and real.model == "demo-model"