# mini-agent-lab

不依赖重型 Agent 框架，从零手写一系列**最小可运行（Minimal Viable Example）**的 AI Agent 工程实验，重点打通底层循环、工具边界、上下文治理与评测闭环。

## 为什么做这个仓库

市面上 Agent 框架层出不穷，但真正决定生产可用性的往往是几个朴素的底层工程问题：
- Tool Calling 循环、异常回传与步数熔断怎么写才不失控？
- 工具的权限围栏与沙箱边界怎么划？
- 多模型路由、上下文压缩与离线 Eval 怎么落地？

每个实验严格控制在最小代码量，开箱可跑，且自带离线单元测试（无需消耗真实 API 即可验证核心控制流）。

---

## 实验路线图

| 编号 | 目录 | 核心主题 | 状态 |
| :--- | :--- | :--- | :--- |
| **S01** | `s01_react_loop/` | 最小 ReAct Loop（Tool Registry + 报错回传自修复 + 沙箱工具 + 最大步数熔断） | ✅ 已完成 |
| **S02** | `s02_planner_executor/` | 双模型路由（强模型 Planner 拆任务验收 + 小模型 Executor 执行降本） | 🚧 计划中 |
| **S03** | `s03_context_memory/` | 上下文压缩与基于文件系统的持久化记忆 | 🚧 计划中 |
| **S04** | `s04_evals/` | 离线 Golden Dataset 回归评测（工具准确率 / 完成率 / 成本追踪） | 🚧 计划中 |

---

## 快速开始（S01）

### 1. 安装依赖（推荐 `uv`）

```bash
uv sync
```

### 2. 先跑离线单元测试（零 API 消耗）

```bash
uv run pytest -v
```

覆盖多步 Tool Calling、未知工具/非法 JSON 容错回传、目录穿越拦截、最大步数熔断。

### 3. 接入真实模型运行

复制环境变量模板并填入任意 OpenAI 兼容 API Key（DeepSeek / OpenAI / Gemini 兼容接口均可）：

```bash
cp .env.example .env
uv run python -m s01_react_loop.main "先列出当前目录的文件，读一下 README.md，再算一下 365 * 24"
```

---

## S01 核心设计要点

- **核心循环约 140 行**（`s01_react_loop/agent.py`）：清晰展示 `messages -> LLM -> tool_calls -> observation -> messages` 的完整闭环，无隐藏黑盒。
- **工具错误不崩循环**：未知工具名、JSON 解析失败、工具执行抛异常均转为结构化错误文本回传给模型，让模型有机会自我修正。
- **最小安全围栏**（`s01_react_loop/tools.py`）：计算器用 `ast` 白名单解析代替 `eval()`；文件工具强制校验路径不可逃逸工作区沙箱，并限制单次读取长度防上下文撑爆。
- **可测试设计**：`client` 通过参数注入，离线测试可直接替换桩对象验证控制流，不依赖真实网络。

## License

[MIT](./LICENSE)
