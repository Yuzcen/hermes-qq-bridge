# Hermes QQ Bridge（中文说明）

> 基于 **Hermes Agent 原生 Profile、Gateway、Session、Memory、Skills、MCP 与 Toolset** 能力的 QQ / NapCat OneBot v11 接入参考实现。
>
> 本仓库的目标不是重新实现一个聊天模型、记忆系统或工具调度器，而是让 QQ 群消息以安全、可审计的方式进入 Hermes 原生 Agent 工作流；QQ 桥接层只处理 OneBot 协议与传输边界。

[English summary](#english-summary) · [架构与审查说明](docs/架构与审查说明.md) · [用户画像与关系感知迭代计划](docs/plans/2026-09-01-keluomiao-user-profile-and-relationship-iteration.md)

---

## 1. 项目要解决什么问题

QQ 群机器人不应只是一套“关键词触发 → 固定回复”的脚本。一个自然、可持续迭代的群聊 Agent 需要同时具备：

- 正确接收 OneBot v11 / NapCat 群事件；
- 保留每条消息真实发送者、群号、消息号和引用关系；
- 按群隔离上下文，而不是把不同群聊天混在一起；
- 让模型根据当前群聊现场判断是否参与，而不是用随机丢消息代替语义判断；
- 需要外部事实、群历史、图片或文件时，能通过工具调用获得证据；
- 工具调用、思考、重试和框架状态不泄露到 QQ 群；
- 保留审计证据，以便复盘“该不该回”“有没有查证”“最终是否真正发送”。

本项目负责第一层的 QQ / OneBot 接入与传输安全边界，并将高阶 Agent 能力交给 Hermes 原生框架。

---

## 2. 设计原则：充分复用 Hermes 原生流程

### 不重复造轮子

下列能力**不在本桥接层重写**，而是复用 Hermes Agent 原生实现：

| 能力 | 复用的 Hermes 原生机制 | 桥接层职责 |
|---|---|---|
| Agent 对话循环 | Agent loop：模型调用 → tool call → 工具结果 → 最终回复 | 将 QQ 事件送入正确的群聊会话 |
| 多平台接入 | Gateway 与平台适配器 | 提供/兼容 NapCat OneBot 输入输出 |
| 会话与上下文 | Profile 隔离、SQLite `state.db`、session routing | 提供稳定的群聊 session key |
| 长期记忆 | Hermes Memory / 可选 Mem0 | 不在桥接层复制用户记忆 |
| 历史对话检索 | `session_search` | 不在桥接层自行拼接全量历史 |
| 工具发现与调度 | Toolsets、Tool Registry | 将需要的工具集暴露给 Profile |
| 外部搜索 | MCP server，例如 Tavily MCP | 不在桥接层硬编码搜索 API |
| 可复用流程 | Skills | 将复杂操作放进 Skill，而非不断堆进人格提示词 |
| 定时复盘 | Cron | 由独立任务审计行为和候选改动 |
| 人格与群差异 | `SOUL.md` 与 channel override | 桥接层不替模型制定普通聊天语义 |

因此，项目的核心边界是：

```text
QQ/NapCat 负责消息事件
        ↓
Bridge 负责协议、身份、附件、传输和输出安全
        ↓
Hermes Profile 负责会话、记忆、工具调用、模型推理和人格
        ↓
最终自然语言回复才回到 QQ
```

> 详细职责边界、数据流、工具调用闭环与当前实现状态见：[docs/架构与审查说明.md](docs/架构与审查说明.md)。

---

## 3. 总体架构

```text
┌───────────────────────────────────────────────────────────┐
│                         QQ 群                              │
└──────────────────────────┬────────────────────────────────┘
                           │ OneBot v11 event
┌──────────────────────────▼────────────────────────────────┐
│                     NapCat / OneBot                        │
└──────────────────────────┬────────────────────────────────┘
                           │ Reverse WebSocket / HTTP
┌──────────────────────────▼────────────────────────────────┐
│              hermes-qq-bridge（本仓库）                    │
│  - NoneBot 事件接收                                         │
│  - 群号 / 发送者 / 消息号保真                               │
│  - 短时合并、附件规范化、协议校验                           │
│  - 最终输出过滤与脱敏日志                                   │
│  - 不发送中间思考、工具进度、错误详情                       │
└──────────────────────────┬────────────────────────────────┘
                           │ 请求进入 Hermes Gateway/API
┌──────────────────────────▼────────────────────────────────┐
│              Hermes Agent 原生 Profile                     │
│  - SOUL / 群级 override：人格与接话判断                    │
│  - Session / state.db：按群上下文与持久记录                │
│  - Memory / Mem0：长期记忆                                 │
│  - Tools / MCP：搜索、群历史、文件、图片等                 │
│  - Skills：复杂流程的按需加载                              │
│  - Cron / watchdog：独立复盘、候选规则、人工审查           │
└──────────────────────────┬────────────────────────────────┘
                           │ 仅最终正文或 NO_REPLY
┌──────────────────────────▼────────────────────────────────┐
│                 Outbound Safety Gate                       │
│  - 拦截框架状态、工具痕迹、内部推理、provider 错误         │
│  - quiet 群物理静默                                         │
│  - OneBot 消息发送                                          │
└───────────────────────────────────────────────────────────┘
```

---

## 4. 当前仓库包含什么

```text
.
├── bot.py                         # NoneBot 启动入口
├── plugins/hermes_bridge.py        # OneBot → Hermes 的参考桥接实现
├── tests/test_bridge_v4.py         # 桥接层回归测试
├── .env.example                    # 脱敏配置模板
├── docs/
│   ├── 架构与审查说明.md            # 面向审查人的中文全局架构说明
│   └── plans/
│       └── 2026-09-01-keluomiao-user-profile-and-relationship-iteration.md
├── pyproject.toml
└── LICENSE
```

### 已经实现并测试的桥接能力

- NoneBot2 接收 OneBot v11 / NapCat 消息；
- 群会话标识与短时消息合并；
- 真实发送者与消息标识保留；
- 图片、文件、闪照等附件位置规范化；
- 附件落盘的路径安全处理；
- 通过环境变量调用 Hermes HTTP API；
- 最终回复发送与 OneBot 段构造；
- 对消息正文进行脱敏结构化日志记录；
- 测试中的 fake OneBot 事件和 mocked Hermes 调用。

当前测试结果：

```text
13 passed
```

---

## 5. 当前正在演进、但不应误称为“已由本仓库完成”的能力

以下是完整系统设计的一部分，但其主实现归属 Hermes Profile / 运行环境，或仍处于计划与灰度阶段：

- 模型自主的“是否接话”判断；
- 用户画像、群内关系状态和互动温度；
- 按群隔离的渐进式记忆披露；
- 事实查证 → 工具调用 → 基于结果回答的闭环；
- 群历史按需分页检索；
- MCP 健康检查与工具降级；
- 每小时 watchdog 与每 6 小时候选迭代；
- 固定 holdout、shadow、灰度和回滚机制；
- 资源压力观测与 token bucket dry-run。

这些能力的完整实施顺序、数据结构、测试、验收和回滚点在：

- [用户画像与关系感知迭代计划](docs/plans/2026-09-01-keluomiao-user-profile-and-relationship-iteration.md)
- [架构与审查说明](docs/架构与审查说明.md)

本项目不会把计划当作已完成实现，也不会把运行中的真实群聊数据、Profile、密钥或数据库提交到 GitHub。

---

## 6. 运行方式

### 前置条件

- Python 3.11+
- 已运行的 NapCat / OneBot v11
- 已配置的 Hermes Agent Profile 或 Hermes API Server
- 独立的 QQ 机器人账号（不要使用个人主账号）

### 本地安装

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
cp .env.example .env
```

编辑 `.env`，填写本地环境实际值。不要提交 `.env`。

```bash
python bot.py
```

随后将 NapCat 的 OneBot v11 反向 WebSocket 客户端指向该 bridge。

### Hermes 原生侧的推荐配置流程

该项目假设 Hermes 负责 Agent 能力。建议使用 Hermes 原生命令完成配置：

```bash
# 创建或选择隔离的群聊 Profile
hermes profile create qq-group
hermes --profile qq-group config edit

# 配置/查看可用工具集与 MCP
hermes --profile qq-group tools list
hermes --profile qq-group mcp list
hermes --profile qq-group mcp test tavily

# 安装并运行 Gateway
hermes --profile qq-group gateway install
hermes --profile qq-group gateway status
```

Hermes 文档：

- [Profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles)
- [Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/)
- [MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)
- [Configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration)

> 实际参数、密钥、群号、机器人 QQ、MCP 地址和运行时数据库均属于部署环境，故意不放入本仓库。

---

## 7. 测试

```bash
python -m pytest -q
```

测试使用伪造 OneBot 事件和 mock Hermes 调用：

- 不连接生产 QQ；
- 不连接生产 NapCat；
- 不请求生产 Hermes Gateway；
- 不读取真实群聊记录、真实密钥或运行时数据库。

---

## 8. 安全与隐私边界

以下内容必须保留在 Git 之外：

```text
.env
真实 API Key / Token
QQ 账号、群号、群成员名单
state.db
会话记录、群聊导出、日志
下载的附件、媒体缓存
生产 Profile 及其 memory / skills / cron 输出
```

桥接层的最后一道输出安全边界必须阻止以下内容进入 QQ：

```text
工具调用过程
内部推理或 planning
框架状态文本
JSON / stack trace
provider / MCP 错误详情
认证、限流、网络故障提示
```

对于真实部署，应进一步：

- 使用独立机器人 QQ 账号；
- 配置群白名单和管理权限；
- 将工具权限按 Profile 最小化分配；
- 对读取、搜索与写入类工具分别审查；
- 修改消息路由、发送策略或输出过滤后运行测试并验证真实链路。

---

## 9. 欢迎审查与讨论

非常欢迎围绕以下问题提出 Issue 或 Review：

1. QQ / OneBot 事件是否被完整、正确地映射到 Hermes 群会话；
2. 发送者身份、@、引用、附件、短时合并是否会造成上下文归因错误；
3. 桥接层与 Hermes 原生 Agent 层的职责边界是否足够清晰；
4. 工具调用是否能做到“需要时真实执行、过程不扰群、结果可审计”；
5. 用户画像、群级关系与记忆隔离是否符合渐进式披露和隐私边界；
6. 资源保护是否会错误干预模型的自然接话判断；
7. 哪些测试、holdout 或灰度指标还应该补充。

提出 Issue 时，请不要粘贴真实群聊、密钥、账号、会话数据库或未脱敏截图。

---

## English summary

`hermes-qq-bridge` is a QQ/NapCat OneBot v11 integration reference for Hermes Agent.

It intentionally reuses Hermes-native Profiles, Gateway, sessions/state.db, memory, skills, MCP, toolsets, and cron jobs. The bridge owns protocol adaptation, sender/message fidelity, attachment normalization, transport safety, and final-response-only delivery; it does **not** reimplement an LLM agent, memory system, or tool scheduler.

See the Chinese architecture review document for the complete design, current implementation boundary, planned user-profile/relationship system, verification strategy, and privacy constraints.

## License

MIT. See [LICENSE](LICENSE).
