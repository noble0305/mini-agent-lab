"""S05 最小 HTTP 服务：仅标准库 http.server，无 FastAPI/Flask 等重框架。

路由：
  GET  /health -> 200 服务状态与运行模式
  POST /chat   -> 200 {"message": "..."} 交给 S01 ReAct 内核执行

请求日志以 JSONL 追加写入 LOG_DIR/requests.jsonl，仅记录元数据与截断后的 prompt，
不记录任何 API key 或完整密钥信息。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from s05_service.config import Settings
from s05_service.runtime import AgentRuntime

MAX_PROMPT_CHARS = 8000
LOG_PROMPT_CHARS = 200


class RequestLogger:
    """线程安全的 JSONL 追加写入器；日志失败不影响请求本身。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._lock:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            pass


class App:
    def __init__(self, settings: Settings, runtime: AgentRuntime | None = None) -> None:
        self.settings = settings
        self.runtime = runtime or AgentRuntime(settings)
        self.logger = RequestLogger(Path(settings.log_dir) / "requests.jsonl")

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "service": self.settings.service_name,
            "version": self.settings.version,
            "mode": self.settings.mode,
        }

    def chat(self, payload: Any, request_id: str) -> tuple[int, dict[str, Any]]:
        if not isinstance(payload, dict):
            return 422, {"error": "request body must be a JSON object", "request_id": request_id}
        message = payload.get("message", payload.get("prompt"))
        if not isinstance(message, str) or not message.strip():
            return 422, {"error": "'message' is required and must be a non-empty string", "request_id": request_id}
        if len(message) > MAX_PROMPT_CHARS:
            return 422, {"error": f"'message' exceeds {MAX_PROMPT_CHARS} characters", "request_id": request_id}
        try:
            return 200, self.runtime.chat(message.strip(), request_id)
        except Exception as exc:  # 上游模型异常统一转 502，避免堆栈与密钥外泄
            return 502, {
                "error": "agent_runtime_error",
                "detail": type(exc).__name__,
                "request_id": request_id,
            }


class AgentHTTPRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mini-agent-lab-s05/0.1.0"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 覆盖基类签名
        # 默认 stderr 访问日志会污染容器日志，统一走 JSONL。
        return

    def do_GET(self) -> None:
        started = time.monotonic()
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        request_id = uuid.uuid4().hex
        if path == "/health":
            status, body = 200, self.app.health()
        else:
            status, body = 404, {"error": "not_found", "request_id": request_id}
        self._send_json(status, body)
        self._log_request(request_id, "GET", path, status, started)

    def do_POST(self) -> None:
        started = time.monotonic()
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        request_id = uuid.uuid4().hex
        if path != "/chat":
            # 未读取请求体，复用连接会错位，显式关闭。
            self.close_connection = True
            self._send_json(404, {"error": "not_found", "request_id": request_id})
            self._log_request(request_id, "POST", path, 404, started)
            return

        status, payload_or_error = self._read_json_body(request_id)
        if status != 200:
            self._send_json(status, payload_or_error)
            self._log_request(request_id, "POST", path, status, started, error=payload_or_error.get("error"))
            return

        status, body = self.app.chat(payload_or_error, request_id)
        self._send_json(status, body)
        self._log_request(
            request_id,
            "POST",
            path,
            status,
            started,
            prompt=str(payload_or_error.get("message") or payload_or_error.get("prompt") or "")
            if isinstance(payload_or_error, dict)
            else "",
            body=body,
        )

    def _read_json_body(self, request_id: str) -> tuple[int, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return 411, {"error": "Content-Length header required", "request_id": request_id}
        try:
            length = int(raw_length)
        except ValueError:
            return 400, {"error": "invalid Content-Length", "request_id": request_id}
        if length < 0 or length > self.app.settings.max_body_bytes:
            self.close_connection = True
            return 413, {
                "error": f"request body exceeds {self.app.settings.max_body_bytes} bytes",
                "request_id": request_id,
            }
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, {"error": "invalid JSON body", "request_id": request_id}
        return 200, payload

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def _log_request(
        self,
        request_id: str,
        method: str,
        path: str,
        status: int,
        started: float,
        prompt: str = "",
        body: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
            "method": method,
            "path": path,
            "status": status,
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "mode": self.app.settings.mode,
        }
        if prompt:
            record["prompt"] = prompt[:LOG_PROMPT_CHARS]
        if body and status == 200:
            record["steps"] = body.get("steps")
            record["stopped_reason"] = body.get("stopped_reason")
            record["answer_chars"] = len(str(body.get("answer") or ""))
        if error:
            record["error"] = error
        self.app.logger.write(record)


def make_server(
    settings: Settings,
    runtime: AgentRuntime | None = None,
    host: str | None = None,
    port: int | None = None,
) -> ThreadingHTTPServer:
    app = App(settings, runtime)
    server = ThreadingHTTPServer((host or settings.host, settings.port if port is None else port), AgentHTTPRequestHandler)
    server.app = app  # type: ignore[attr-defined]
    server.daemon_threads = True
    return server


def run(settings: Settings | None = None) -> None:
    settings = settings or Settings.from_env()
    server = make_server(settings)
    host, port = server.server_address
    print(
        f"{settings.service_name} listening on http://{host}:{port} "
        f"(mode={settings.mode}, log_dir={settings.log_dir})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()