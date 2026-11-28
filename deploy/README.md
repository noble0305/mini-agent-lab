# S05 远程部署

把 S01 ReAct 内核封装成最小 HTTP 服务，部署到 `oracle-1` 的 Docker 中运行。

## 拓扑与边界

- 单容器 `mini-agent-lab`，镜像基于 `python:3.12-slim`，仅安装 `openai`（mock 模式下不导入）。
- 服务监听容器 `0.0.0.0:8080`，宿主机只绑定 `127.0.0.1:8080`：**不修改安全组/防火墙**，外部访问请用 `ssh -L 8080:127.0.0.1:8080 oracle-1`。
- 容器硬限制 `mem_limit: 300m`（`memswap_limit` 同为 300m），CPU 1 核，进程数 128。
- 请求日志以 JSONL 追加到命名卷 `mini-agent-lab-logs`（容器内 `/app/logs/requests.jsonl`）。
- 密钥只通过环境变量 / 远程 `deploy/.env` 注入，仓库内不保存任何真实密钥；没有 key 时自动进入 mock 模式。

## 首次前置条件

- 本地：`ssh oracle-1` 免密可用，`rsync` 可用。
- 远程：Docker 29 + Compose v2，家目录可写。

## 常用命令

```bash
make test        # 本地完整离线测试（含 S05 真实 HTTP 服务用例）
make deploy      # rsync -> 远程 docker compose build/up -> 自动 smoke
make smoke       # 仅执行远程 curl 冒烟：/health 200 + /chat 返回 8760
make logs        # 查看最近 20 条 JSONL 请求日志
make down        # 停止并移除容器
```

部署后可手工验证：

```bash
ssh oracle-1 "curl -s http://127.0.0.1:8080/health"
ssh oracle-1 "curl -s -X POST http://127.0.0.1:8080/chat \
  -H 'Content-Type: application/json' -d '{\"message\":\"计算 365 * 24\"}'"
```

## 接入真实模型（可选）

在 `deploy/.env` 中写入（该文件已被 `.gitignore` 与 rsync 规则排除，不会提交）：

```dotenv
MOCK_LLM=0
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-chat
```

随后再次 `make deploy`。`MOCK_LLM` 未显式设为 `0` 时，即使配置了 key 也会保持 mock。

## 接口

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/health` | `200 {"status":"ok","mode":"mock"|"llm",...}` |
| POST | `/chat` | 请求 `{"message":"..."}`，返回 `answer / steps / stopped_reason / tool_calls` |

`/chat` 复用 `s01_react_loop.agent.run_agent` 与 `build_default_registry`：mock 客户端只模拟 LLM 响应，工具注册、参数校验、步数熔断均走真实内核。

## 与其它主机的边界

- `oracle-2` 本轮不使用。
- `huawei-1` 上的 `rag-pg` 容器属于其它项目，本部署不涉及、不修改。