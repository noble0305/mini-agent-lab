# mini-agent-lab

不依赖重型 Agent 框架，从零手写一系列**最小可运行（Minimal Viable Example）**的 AI Agent 工程实验。

聚焦真正决定 Agent 生产可用性的底层工程问题：**ReAct 控制循环、工具安全围栏、强弱模型路由降本、上下文预算压缩、跨会话持久化记忆与离线回归测试**。每个实验模块彼此组合复用、代码克制可读，且配套零 API 消耗的离线单元测试。

---

## 一、整体运行架构

整个项目采用**分层组合架构**：`S01` 是最底层的执行内核与安全工具注册表，`S02` 直接复用 `S01` 的循环作为子任务执行器，`S03` 在 `S01` 工具体系上叠加预算守卫、滚动压缩与纯文本长期记忆。

```mermaid
flowchart TB
    subgraph Entry["1. CLI 入口层"]
        M1["s01_react_loop.main"]
        M2["s02_planner_executor.main"]
        M3["s03_context_memory.main"]
    end

    subgraph Orchestration["2. 编排与上下文治理层"]
        S2["S02 双模型路由 (router.py): Planner 拆解 AC -> Verifier 验收返工 -> Token 分账"]
        S3["S03 会话记忆与压缩 (memory_agent.py): Token 预算守卫 + 安全切片摘要 + 跨会话记忆注入"]
    end

    subgraph Core["3. 核心执行内核层 (S01)"]
        LOOP["ReAct Agent Loop (agent.py): LLM 推理 -> Tool Calling -> 步数熔断"]
        REG["ToolRegistry: Schema 生成 / JSON 容错 / 异常转文本回传"]
    end

    subgraph Infra["4. 安全工具与外部依赖层"]
        LLM["OpenAI 兼容 LLM 接口 (DeepSeek / OpenAI / Gemini / 本地代理)"]
        TOOLS["安全沙箱工具 (tools.py): AST calc / 沙箱 list_files / 限长 read_text_file"]
        MEM["纯 Markdown 持久化记忆 (.agent_memory.md): memory_read / memory_write"]
    end

    M1 --> LOOP
    M2 --> S2
    M3 --> S3

    S2 -->|"每个子任务下放给便宜模型执行"| LOOP
    S2 -->|"Planner 拆解 & Verifier 验收"| LLM
    S3 -->|"挂载记忆工具 & 分发调用"| REG
    S3 -->|"多轮推理 & 滚动压缩"| LLM
    S3 -->|"启动加载 & 落盘持久化"| MEM

    LOOP --> REG
    LOOP --> LLM
    REG --> TOOLS
    REG --> MEM
```

---

## 二、目录结构与演进路线

```text
mini-agent-lab/
├── s01_react_loop/               # [S01] 最小 ReAct Loop + 工具注册容错 + 沙箱工具
│   ├── agent.py                  # 核心循环 (~140 行)：ToolRegistry + run_agent + max_steps 熔断
│   ├── tools.py                  # AST 白名单计算器 + 防路径穿越沙箱文件工具
│   └── main.py                   # 单次任务 CLI 入口
├── s02_planner_executor/         # [S02] 强弱双模型路由（规划/执行/验收返工）
│   ├── router.py                 # create_plan + verify_subtask + run_pipeline + Token 分账
│   └── main.py                   # 支持同厂商或跨厂商双模型 CLI 入口
├── s03_context_memory/           # [S03] 上下文自动压缩 + 纯 Markdown 跨会话记忆
│   ├── memory_agent.py           # FileMemory + 安全边界切片 + compact_history + MemorySession
│   └── main.py                   # 支持多轮交互与重启验证的 CLI 入口
├── tests/                        # 零 API 费用的离线单元测试（FakeClient 依赖注入）
│   ├── test_react_loop.py
│   ├── test_planner_executor.py
│   └── test_context_memory.py
├── .env.example                  # 模型与路由配置模板
└── pyproject.toml                # uv 依赖管理（仅 openai + python-dotenv）
```

| 编号 | 模块 | 解决的核心工程问题 | 状态 |
| :--- | :--- | :--- | :--- |
| **S01** | `s01_react_loop/` | 脱离重框架手写最小 ReAct 闭环、工具异常自修复、沙箱越权防护、步数熔断 | ✅ 已完成 |
| **S02** | `s02_planner_executor/` | 强模型只做拆解与验收、便宜模型承担高频工具调用，量化 Token 卸载降本比例 | ✅ 已完成 |
| **S03** | `s03_context_memory/` | 长对话超预算自动滚动摘要（不拆散 `tool_calls/tool` 对）+ 跨会话 Markdown 长期记忆 | ✅ 已完成 |
| **S04** | `s04_evals/` | 离线 Golden Dataset 回归评测（工具准确率 / 任务完成率 / 成本波动对比） | 🚧 计划中 |

---

## 三、核心运行过程流程图

### 1. S01 · 最小 ReAct Loop 运行过程

展示一次完整请求如何在 `run_agent` 中循环推进，重点是**工具异常不崩进程（转为 Observation 回传给模型自修复）**以及 **`max_steps` 强制熔断**。

```mermaid
flowchart TD
    A["用户提问 (user_prompt)"] --> B["初始化 messages: system + user"]
    B --> C{"step <= max_steps ?"}
    C -- "否 (触发熔断)" --> STOP["返回 stopped_reason=max_steps_reached"]
    C -- "是" --> D["调用 LLM (携带 messages + tools schema)"]
    D --> E{"响应包含 tool_calls ?"}
    E -- "否 (模型直接作答)" --> DONE["返回 AgentResult (stopped_reason=completed)"]
    E -- "是" --> F["追加 assistant(tool_calls) 到 messages"]
    F --> G["ToolRegistry.execute(name, raw_args)"]
    G --> H{"校验与执行结果"}
    H -- "正常返回" --> OBS["产出结果文本"]
    H -- "未知工具 / 非法 JSON / 沙箱越权 / 运行异常" --> ERR["捕获异常并转为 Error 文本 (不中断循环)"]
    OBS --> I["追加 role=tool (observation) 到 messages"]
    ERR --> I
    I --> J["step + 1"]
    J --> C
```

---

### 2. S02 · 双模型路由（Planner + Cheap Executor + Verifier）运行过程

核心思路是**权责分离 + 上下文隔离**：强模型只负责最值钱的「定计划」和「把关验收」，繁琐的多步工具调用全部交给便宜模型；且每个子任务只看前序精简摘要，避免 Token 滚雪球。

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant R as Router (router.py)
    participant P as Planner / Verifier (强模型)
    participant E as Executor (便宜模型 + S01 Loop)
    participant T as 沙箱工具集

    U->>R: 提交复杂大任务
    R->>P: create_plan (附可用工具列表)
    P-->>R: 返回 JSON 子任务列表 (含目标 goal 与验收标准 AC)
    Note over R: 若 JSON 解析失败，自动降级为单子任务兜底

    loop 逐个执行 SubTask
        loop 最多尝试 1 + max_retries 次
            R->>E: 注入总体目标 + 前序精简摘要 + 当前 AC (返工时附 Feedback)
            E->>T: 运行 S01 ReAct Loop 调用本地工具
            T-->>E: 返回工具结果
            E-->>R: 返回子任务输出 + executor_tokens
            R->>P: verify_subtask (对照 AC 审查输出)
            P-->>R: 返回 JSON (passed + feedback)
            alt passed == true
                Note over R: 记录精简摘要，跳出重试进入下一个 SubTask
            else passed == false 且仍有重试额度
                Note over R: 将 feedback 注入下一次 Prompt 触发定向返工
            else 达到 max_retries
                Note over R: 优雅止损，标记 UNVERIFIED 避免死循环烧钱
            end
        end
    end

    R-->>U: 输出汇总结果 + Token 分账 (executor_token_ratio)
```

---

### 3. S03 · 上下文自动压缩 + 跨会话文件记忆运行过程

解决长对话越跑越慢、上下文溢出和「关闭窗口就失忆」三个痛点。

```mermaid
flowchart TD
    START["启动 MemorySession (绑定 .agent_memory.md)"] --> LOAD["读取 .agent_memory.md 动态拼入 System Prompt"]
    LOAD --> ASK["接收新一轮 user_prompt，追加到 history"]
    ASK --> EST["estimate_tokens(system + history)"]
    EST --> OVER{"超过 token_budget ?"}

    OVER -- "否" --> LOOP["进入 ReAct 工具调用循环"]
    OVER -- "是" --> SPLIT["find_safe_split_index: 定位安全切分点"]
    SPLIT --> GUARD["向前对齐避开 role=tool (绝不拆散 tool_calls/tool 对)"]
    GUARD --> SUM["调用 LLM 将旧消息浓缩为 <= 6 条滚动摘要"]
    SUM --> FB{"摘要调用是否成功?"}
    FB -- "成功" --> REPLACE["用摘要消息替换旧历史，保留最近完整回合"]
    FB -- "失败" --> FALLBACK["降级插入 Compaction fallback 占位，不崩主流程"]
    REPLACE --> LOOP
    FALLBACK --> LOOP

    LOOP --> TOOL{"模型调用 memory_write ?"}
    TOOL -- "是" --> WRITE["去重追加写入 .agent_memory.md (持久落盘)"]
    WRITE --> LOOP
    TOOL -- "否，输出最终回答" --> REPLY["返回 TurnResult (含压缩状态与估算 Token)"]
    REPLY -. "下次重启新 Session 自动继承" .-> START
```

---

## 四、关键工程取舍与避坑笔记

1. **为什么工具异常一律转成字符串回传，而不是直接 `raise`？**
   真实调用中，模型经常传错参数名、拼坏 JSON 或尝试访问不存在的路径。直接抛异常会让整次任务前功尽弃；把明确的 `Error: ...` 当作 `tool` 角色消息喂回上下文，大多数模型在下一步就能自行修正。
2. **为什么 S03 压缩历史时专门写了 `find_safe_split_index`？**
   OpenAI 协议要求每条 `role="tool"` 消息前面必须紧跟包含对应 `tool_call_id` 的 `assistant(tool_calls)` 消息。如果按固定条数硬切（如直接 `history[-4:]`），一旦切口落在 `tool` 消息上，下一次请求必然触发 **HTTP 400 Invalid messages**。向前扫描对齐到非 `tool` 边界是上线前必须补上的防线。
3. **为什么跨会话记忆先用纯 Markdown（`.agent_memory.md`），而不是一上来上向量库？**
   对于个人助手和中小规模项目状态，纯文本文件零基础设施依赖、人类可以直接打开审校纠错、Git 可追踪变更，且无 RAG 切块丢上下文的问题；只有当文档量级上千时才值得叠加检索引擎。
4. **为什么所有核心函数都通过参数注入 `client`？**
   把 `OpenAI` 客户端作为依赖传入，不写死在函数内部，离线单元测试只需传入极简 `FakeClient` 就能在 **0.02 秒内**确定性回归所有异常分支（非法 JSON、路径穿越、返工重试、压缩降级），不依赖网络也不烧 API 费用。

---

## 五、5 分钟快速开始

### 1. 安装依赖（推荐 `uv`）

```bash
uv sync
```

### 2. 运行全部离线单元测试（零 API 消耗）

```bash
uv run pytest -v
```

当前覆盖 10 个核心用例：
- `tests/test_react_loop.py`：多步 Tool Calling、未知工具/非法 JSON/目录穿越拦截回传、最大步数熔断
- `tests/test_planner_executor.py`：双模型拆分与 Token 分账统计、验收不通过触发定向返工、重试止损与非法 Plan 降级
- `tests/test_context_memory.py`：跨会话 Markdown 持久化记忆继承、超预算自动滚动压缩、`tool_calls/tool` 安全切分边界、摘要失败降级兜底

### 3. 接入真实模型运行

复制配置模板并填入任意 OpenAI 兼容接口（DeepSeek / OpenAI / Gemini 兼容端点 / 本地代理均可）：

```bash
cp .env.example .env
```

按需运行对应实验：

```bash
# S01：单模型最小 ReAct Loop
uv run python -m s01_react_loop.main "先列出当前目录的文件，读一下 README.md，再算一下 365 * 24"

# S02：双模型路由（可在 .env 分别设置 PLANNER_MODEL 和 EXECUTOR_MODEL）
uv run python -m s02_planner_executor.main "列出目录文件，读 README.md 总结路线图，再计算 141 + 110 + 39"

# S03：多轮交互 + 自动压缩 + 跨会话持久化记忆（试着告诉它你的偏好，exit 后重启再问它）
uv run python -m s03_context_memory.main
```

---

## License

[MIT](./LICENSE)
