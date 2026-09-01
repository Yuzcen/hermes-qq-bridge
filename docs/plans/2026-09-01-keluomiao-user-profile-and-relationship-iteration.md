# 可洛喵用户画像与关系感知系统实施计划

> **For Hermes:** 按 `subagent-driven-development` 或当前主会话逐任务执行；每个任务完成后都要做规格复核、测试和回滚检查。

**Goal:** 为可洛喵建立按 QQ 身份识别、按群隔离、渐进式披露的用户画像与关系感知系统，让她能逐步认识群友、形成有证据的互动温度差异，并通过真实互动反馈持续改进接话判断，同时不把无用资料塞进 `SOUL.md` 或每轮上下文。

**Architecture:** 原始消息和发送结果留在现有 `state.db`；画像候选事件先写入按群隔离的 Markdown/JSON 学习层，经过置信度和重复证据验证后才晋升为摘要。用户画像只影响称呼、熟悉度和语气，不能单独决定是否回复、工具权限或管理员权限；是否接话仍由模型结合当前现场判断。普通聊天只加载核心 SOUL 和当前群现场，只有需要时才按 `memory_index → 当前群摘要 → 相关用户摘要 → 原始历史` 渐进式读取。

**Tech Stack:** Python 3.11；SQLite；现有 Hermes Profile / NapCat OneBot v11；Markdown 学习档案；现有 `state.db`、`judgment_logs/`、`ITERATION_LOG.md`；pytest；不新增核心模型工具，不把用户画像写入全局核心 prompt。

---

## 0. 现状与不可违反的边界

当前已经存在：

- 实际运行 profile：`/home/johntime/.hermes/profiles/qq-group/`
- 核心人设：`SOUL.md`
- 群级提示：`config.yaml` 的 `channel_overrides`
- 原始状态：`state.db`
- 群术语：`glossary/<group_id>.json`
- 接话判断项目：`memory/reply_judgment/`
- 完整审计：`media_archive/ITERATION_LOG.md`
- 每小时 watchdog
- 每 6 小时自主迭代
- 独立开源仓库：`/home/johntime/hermes-qq-bridge/`

不可违反：

1. QQ 号是用户主键，昵称只能是可变显示属性。
2. 不把不同群的原始聊天、群梗和关系状态混在一起。
3. 同一群仍是共享群聊现场，不按用户拆成私聊机器人。
4. 好感/熟悉度不能直接变成“这个人发消息就必回”。
5. 关系温度不能赋予工具权限、群管理权限或 owner 身份。
6. 一次偶发消息不能晋升为稳定画像。
7. 负面反馈优先影响短期互动边界，不立即等同于永久讨厌。
8. `SOUL.md` 只保留跨群、每轮都必须知道的稳定规则；用户档案和详细样本不写进去。
9. 正常消息行为优先软修复；代码只处理身份、协议、安全、隔离、并发和资源保护。
10. 所有自动修改必须有证据、备份、范围和回滚点。

---

# Phase 1：建立可审计的数据模型，不改变回复行为

目标：先让系统能可靠记录“认识了谁、在什么群、发生了什么互动”，不让画像立即影响模型回复。

## Task 1：冻结现有运行态并建立独立工作分支

**Objective:** 让后续画像开发与现网可洛喵运行代码分离，可回滚、可开源审查。

**Files:**
- Modify: `/home/johntime/hermes-qq-bridge/` Git repository only
- Read-only reference: `/home/johntime/.hermes/profiles/qq-group/`

**Steps:**

1. 检查工作区和当前分支：

```bash
cd /home/johntime/hermes-qq-bridge
git status --short --branch
git log --oneline -3
```

2. 创建功能分支：

```bash
git switch -c feat/user-profile-relationship
```

3. 记录现网基线，但不要复制敏感文件：

```bash
python3 - <<'PY'
import sqlite3
p='/home/johntime/.hermes/profiles/qq-group/state.db'
db=sqlite3.connect(p)
for t in ('messages','sessions','gateway_routing'):
    print(t, db.execute(f'SELECT count(*) FROM {t}').fetchone()[0])
PY
```

**Acceptance:**

- 独立仓库分支创建成功；
- 现网 profile 没有被修改；
- 仓库工作区没有 `.env`、日志、数据库或私有导出；
- 若后续任务失败，删除功能分支即可回滚。

**Commit:**

```bash
git commit --allow-empty -m "chore: start user profile relationship work"
```

---

## Task 2：定义画像事件和关系事件 schema

**Objective:** 用结构化事件表达“观察到的行为”，避免模型直接写不可审计的好感结论。

**Files:**
- Create: `src/hermes_qq_bridge/profile_schema.py`
- Test: `tests/test_profile_schema.py`

**建议数据结构：**

```python
from dataclasses import dataclass, field
from typing import Literal

ProfileEventKind = Literal[
    "explicit_interest",
    "explicit_disinterest",
    "style_observation",
    "direct_call",
    "continued_dialogue",
    "helpful_feedback",
    "correction",
    "positive_feedback",
    "negative_feedback",
    "boundary_request",
    "ignored_reply",
]

@dataclass(frozen=True)
class ProfileEvent:
    event_id: str
    group_id: str
    user_id: str
    kind: ProfileEventKind
    source_message_id: str
    observed_at: float
    value: str
    confidence: float
    evidence: str
    expires_at: float | None = None

@dataclass
class Dimension:
    value: float = 0.0
    confidence: float = 0.0
    evidence_count: int = 0
    last_updated: float | None = None

@dataclass
class RelationshipState:
    familiarity: Dimension = field(default_factory=Dimension)
    warmth: Dimension = field(default_factory=Dimension)
    trust: Dimension = field(default_factory=Dimension)
    interaction_affinity: Dimension = field(default_factory=Dimension)
    boundary_sensitivity: Dimension = field(default_factory=Dimension)
```

**Required validation:**

- `group_id` 和 `user_id` 不能为空；
- `confidence`、dimension value 必须钳制在 `[0, 1]`；
- 事件必须带原始消息 ID；
- 不能存在“好感=必回”字段；
- `negative_feedback` 和 `boundary_request` 必须支持过期时间；
- 事件只能引用一个明确群号。

**TDD:**

1. 先写非法 QQ/空群号/越界置信度测试；
2. 运行测试，确认失败；
3. 实现 dataclass 和 validation；
4. 运行：

```bash
pytest tests/test_profile_schema.py -q
```

**Acceptance:** 10 个以上 schema 测试通过，且无行为决策字段。

**Commit:**

```bash
git add src/hermes_qq_bridge/profile_schema.py tests/test_profile_schema.py
git commit -m "feat: define auditable profile and relationship events"
```

---

## Task 3：建立按群、按用户的 Markdown 学习目录

**Objective:** 把画像候选和关系状态放到渐进式披露目录，而不是写进 `SOUL.md`。

**Files:**
- Create: `docs/profile-learning-layout.md`
- Create: `runtime/memory_index.md`
- Create: `runtime/users/README.md`
- Create: `runtime/relationships/README.md`
- Modify: `.gitignore`

**目录：**

```text
runtime/
├── memory_index.md
├── users/
│   └── qq_<user_id>/
│       ├── global.md
│       └── groups/
│           └── <group_id>.md
├── relationships/
│   └── <group_id>/
│       └── qq_<user_id>.md
└── events/
    └── <group_id>.jsonl
```

**写入规则：**

- `events/<group_id>.jsonl`：候选事件，保留 source message ID；
- `users/.../global.md`：只有明确、稳定、适合跨群的事实；
- `users/.../groups/<group_id>.md`：该用户在该群的表达习惯和兴趣；
- `relationships/<group_id>/qq_<id>.md`：仅记录该群与该用户的互动温度和边界；
- `memory_index.md`：只有晋升后的短摘要；
- 完整原文仍在 `state.db`/`ITERATION_LOG.md`，不复制进日常读取文件。

**Acceptance:**

- 文档明确普通聊天不读完整目录；
- 当前群判断只允许读取当前群文件；
- 文件包含 `confidence`、`evidence_count`、`last_updated`、`expires_at`；
- `.gitignore` 排除运行时用户数据和事件 JSONL，不影响公开仓库源码。

**Commit:**

```bash
git add docs/profile-learning-layout.md runtime .gitignore
git commit -m "docs: define progressive disclosure profile layout"
```

---

# Phase 2：从真实 state.db 抽取用户画像候选

目标：只做候选抽取和审计，不影响可洛喵发言。

## Task 4：实现基于真实 QQ 号的候选事件抽取器

**Objective:** 从 `state.db` 提取用户公开表达、直接呼叫、正负反馈和互动事件。

**Files:**
- Create: `scripts/extract_profile_events.py`
- Test: `tests/test_extract_profile_events.py`
- Read-only: `/home/johntime/.hermes/profiles/qq-group/state.db`

**抽取原则：**

1. 以 `messages` 的真实 sender prefix 和 session/chat_id 为输入；
2. 不用昵称判断用户身份；
3. 先处理显式信号：
   - 明确兴趣/不感兴趣；
   - 明确叫可洛喵；
   - 明确“太吵/别回/没叫你”；
   - 明确表扬或纠错；
4. 行为推断只生成低置信度候选；
5. assistant 回复后的互动需要结合后续 1～3 个 user turn；
6. `主人`、`师傅`、`sub_type`、空 reasoning 不自动生成负面画像；
7. 不把“用户发得多”直接标成恶意；只记录资源压力候选。

**输出格式：**

```json
{
  "event_id": "evt-...",
  "group_id": "185465601",
  "user_id": "2450763725",
  "kind": "boundary_request",
  "source_message_id": "6977",
  "value": "不希望可洛喵重复同一个问句",
  "confidence": 0.92,
  "evidence": "用户直接指出重复问法",
  "observed_at": 1788...
}
```

**TDD：**

- 不同群相同 QQ 号不得合并原始事件；
- 不同 QQ 号相同昵称不得合并；
- “别说了”进入 boundary_request，不直接降低 warmth；
- “你刚才答错了”进入 correction，不直接降低 trust；
- 用户明确表达“我喜欢 X”才生成 high-confidence explicit_interest；
- 群友说“他很烦”只能记录为 opinion，不生成被评价者事实。

**验证：**

```bash
python scripts/extract_profile_events.py \
  --state-db /home/johntime/.hermes/profiles/qq-group/state.db \
  --since "2026-09-01T00:00:00+08:00" \
  --out /tmp/profile-events.jsonl
pytest tests/test_extract_profile_events.py -q
```

**Acceptance:** 输出事件可按群、QQ、原始消息 ID追溯；脚本不会写现网 profile。

**Commit:**

```bash
git add scripts/extract_profile_events.py tests/test_extract_profile_events.py
git commit -m "feat: extract auditable user profile events"
```

---

## Task 5：实现关系维度更新器和时间衰减

**Objective:** 根据候选事件更新关系摘要，但把短期边界和长期关系分开。

**Files:**
- Create: `src/hermes_qq_bridge/relationship_update.py`
- Test: `tests/test_relationship_update.py`

**更新规则：**

```text
direct_call / continued_dialogue / positive_feedback
  familiarity 小幅上升
  interaction_affinity 小幅上升

helpful_feedback / correction
  familiarity 小幅上升
  trust 不自动下降

negative_feedback / boundary_request
  boundary_sensitivity 上升
  短期 participation cooldown 增加
  warmth 不自动大幅下降

ignored_reply
  只影响短期当前话题参与倾向
  不直接降低永久 warmth
```

**初始步长建议：**

```text
显式表达：       ±0.08
高置信互动事件： ±0.04
行为推断：       ±0.01～0.02
单次上限：       不超过 0.10
```

**衰减：**

```text
短期边界/当前话题：24h～7d
群内兴趣：14d～30d
长期明确事实：90d 以上，除非用户明确更正
```

**TDD：**

- 同一事件重复处理不能重复加分；
- 负面反馈不能把 warmth 直接降为 0；
- correction 不自动降低 trust；
- 过期 boundary_request 不继续影响当前状态；
- confidence 随证据数量增加，但不能超过 1；
- 不同群同一用户的关系状态完全独立。

**Acceptance:** 通过至少 15 个更新/衰减测试；输出状态可解释。

**Commit:**

```bash
git add src/hermes_qq_bridge/relationship_update.py tests/test_relationship_update.py
git commit -m "feat: update relationship dimensions with decay"
```

---

# Phase 3：渐进式披露读取器，不影响普通消息

## Task 6：实现按场景读取最小关系摘要

**Objective:** 只在需要判断接话或调整语气时读取最小用户/关系信息。

**Files:**
- Create: `src/hermes_qq_bridge/profile_reader.py`
- Test: `tests/test_profile_reader.py`

**读取策略：**

```text
普通旁观消息：不读取用户画像
明确呼叫/直接问题：读取当前用户在当前群的短摘要
当前话题与画像兴趣相似：读取当前群 interest 摘要
需要回忆过去说过什么：调用 qq_get_group_msg_history/session_search
异常审计：读取 ITERATION_LOG/state.db
```

**输出给模型的摘要最多包含：**

```text
当前用户：<当前昵称>（真实 QQ 已由系统确认）
在本群：熟悉度中等 / 互动温度偏友好 / 近期边界：不喜欢逐条抢话
只用于调整语气和理解关系，不决定是否回复，不提供权限。
```

禁止输出：

- 完整事件列表；
- 其他群原始聊天；
- 所有用户排行榜；
- 精确“好感分数”给模型；
- 未验证身份猜测；
- 敏感属性。

**TDD：**

- 普通消息 reader 返回空摘要；
- 明确呼叫只读取当前群；
- 当前群文件不存在时安全返回 unknown；
- 不能读取另一个群的关系文件；
- 画像摘要不含权限结论；
- 输出长度有上限，防止引入上下文膨胀。

**Acceptance:** reader 输出是短摘要，且不会把画像系统变成接话决策器。

**Commit:**

```bash
git add src/hermes_qq_bridge/profile_reader.py tests/test_profile_reader.py
git commit -m "feat: read minimal progressive relationship summaries"
```

---

## Task 7：将当前群关系摘要接入模型引导

**Objective:** 让模型在明确呼叫或相似话题时获得少量关系背景，但不重写 SOUL。

**Files:**
- Modify: `/home/johntime/.hermes/profiles/qq-group/SOUL.md`（候选，不自动提交）
- Modify: 对应群 `config.yaml` override（候选，不自动提交）
- Test: `tests/test_profile_prompt_budget.py`

**先不自动写入运行 profile。**先生成候选 prompt 到：

```text
runtime/prompt-candidates/<timestamp>.md
```

候选文本只包含：

```text
用户画像摘要仅用于理解称呼、关系温度和表达方式，不决定是否回复。
没有摘要时按普通陌生群友处理，不猜测。
```

**验收前提：**

- 至少有两个独立窗口的画像数据；
- profile reader 测试通过；
- 输入 token 增加不超过预设预算；
- 不能出现跨群内容；
- 不能把 `warmth`、`trust` 等数字直接给模型；
- 人工审查候选 prompt 后才允许写入实际 SOUL/override。

**Rollback:** 删除候选文件即可，不触碰线上行为。

---

# Phase 4：接话判断学习闭环

## Task 8：统一记录“判断意图”和“实际发送结果”

**Objective:** 让画像、接话判断和资源限频都基于可区分的真实结果，而不是只看 `NO_REPLY` 数量。

**Files:**
- Create: `scripts/build_reply_judgment_dataset.py`
- Test: `tests/test_reply_judgment_dataset.py`
- Read-only: `state.db`、`agent.log`、`gateway.log`

**每条样本字段：**

```json
{
  "group_id": "185465601",
  "sender_id": "2450763725",
  "trigger_message_id": "6977",
  "assistant_message_id": "6978",
  "model_output_class": "no_reply|natural_text|control_token|framework_text|empty",
  "transport_result": "send_ok|quiet_suppressed|safety_suppressed|provider_failed|unknown",
  "reply_class": "positive_reply|correct_silence|false_positive|false_negative|post_reply_boundary|unknown",
  "tool_calls": [],
  "evidence": [],
  "confidence": 0.0
}
```

**关键要求：**

- `NO_REPLY` 不等于正确沉默；
- `quiet_suppressed` 不等于模型没有想说；
- `SEND OK` 才代表真正出站；
- `_REPLY` 等控制词单独归类；
- `主人`、`sub_type` 不能单独成为问题；
- reasoning 为空只能降低审计置信度，不能自动判错。

**Acceptance:** 可以输出按群、按用户、按窗口的可复核样本，不改变线上行为。

---

## Task 9：建立群友反馈标注和人工复核队列

**Objective:** 将“好”“蠢”“话多”“没叫你”等自然反馈转成候选标签，但不自动当作绝对真值。

**Files:**
- Create: `runtime/review_queue/README.md`
- Create: `scripts/build_feedback_review_queue.py`
- Test: `tests/test_feedback_review_queue.py`

**标签：**

```text
accepted_reply
useful_correction
ignored_reply
too_verbose
too_short
wrong_target
unwanted_interruption
missed_call
control_token_error
```

**标注等级：**

```text
automatic_candidate
model_suggested
human_confirmed
human_rejected
```

**规则：**

- 群友的一句“蠢”只能进入 review queue；
- 明确“没叫你/别说了”可以作为高置信边界反馈，但仍只影响当前群短期状态；
- 不能把某个群友的评价写成另一个人的客观画像；
- 高风险、跨群、身份相关样本必须人工复核。

---

## Task 10：修改自主迭代任务为“候选—验证—晋升”流程

**Objective:** 让可洛喵每 6 小时学习画像和接话边界，但不能每轮直接改 SOUL。

**Files:**
- Modify: Hermes cron job `6b4f41e6190e`
- Modify: `memory/reply_judgment/index.md`
- Modify: `memory/reply_judgment/<group_id>.md`
- Modify: `memory/users/...` and `memory/relationships/...`
- Append: `media_archive/ITERATION_LOG.md`

**每轮流程：**

```text
读取当前群样本
→ 生成候选画像事件
→ 更新待复核区
→ 对重复模式计数
→ 生成候选引导
→ 不自动改 SOUL
→ 写 ITERATION_LOG
```

**晋升阈值：**

```text
明确用户自述事实：1 次进入候选，2 次可稳定
行为推断：至少 3 次独立事件、跨 2 个时间窗口
群内关系倾向：至少 5 次有方向互动
跨群稳定规则：至少 2 个群、无相反证据
SOUL 规则：人工确认 + 离线 holdout + 小范围灰度通过
```

**每轮限制：**

- 最多更新一个群的一个小节；
- 最多 3 行软引导；
- 默认只写群级学习档案；
- 不因一次反馈修改全局人格；
- 不自动删除 session；
- 不自动执行群管理；
- 不把完整日志注入模型。

---

# Phase 5：工程资源防御与拟真分离

目标：将限频放在外围，避免模型用 prompt 猜恶意用户，也避免资源保护破坏真人感。

## Task 11：设计并实现群级/用户级资源压力记录

**Objective:** 记录资源压力，不改变模型的语义接话规则。

**Files:**
- Create: `src/hermes_qq_bridge/resource_pressure.py`
- Test: `tests/test_resource_pressure.py`
- Config docs: `docs/resource-protection.md`

**建议记录：**

```text
user_id
chat_id
inbound_count
model_turn_count
tool_call_count
input_tokens
output_tokens
retry_count
provider_error_count
last_seen
```

**建议限制：**

```text
全局并发模型请求：2～4
同群并发请求：1
工具并发：独立 bucket，2～4
```

第一版不直接启用激进限频，只做观测和 dry-run：

```text
正常负载：透传
高压负载：记录告警
极端负载：只在明确资源保护条件下合并/延迟
```

不要根据单一用户的消息频率判断恶意。

---

## Task 12：实现 token bucket / cooldown 的 dry-run 模式

**Objective:** 先模拟限频决策，不立即丢消息，验证不会伤害正常聊天。

**Files:**
- Modify: `src/hermes_qq_bridge/resource_pressure.py`
- Test: `tests/test_token_bucket.py`
- Create: `runtime/resource-policy.example.yaml`

**建议初始策略：**

```yaml
group:
  refill_per_second: 0.05
  burst: 3
user:
  refill_per_second: 0.08
  burst: 3
tools:
  refill_per_second: 0.03
  burst: 2
mode: dry_run
```

解释：

- 群级允许短暂真人式突发；
- 长期持续高频才进入压力状态；
- 工具调用独立计算，不因聊天频率直接禁用工具；
- 明确 @ 进入高优先级队列，但仍受全局 provider 资源上限；
- dry-run 只记录“如果启用会怎么处理”。

**Acceptance:** 经过至少 30 个时间序列测试；边界处不出现固定窗口双倍突发；429/5xx 使用指数退避和 jitter。

---

## Task 13：把拟真表达规则从资源规则中分离

**Objective:** 允许自然短答、被烦时收口和轻度参与，同时不让模型看到资源管理术语。

**Files:**
- Candidate only: `runtime/prompt-candidates/natural-participation.md`
- Test: `tests/test_natural_participation_prompt.py`

**候选模型引导：**

```text
你不是来抢着回答每条消息的，也不是为了显得安静而拒绝所有话题。

当你确认当前话题和你有关、你确实感兴趣，并且能补充具体内容、个人反应或自然接梗时，可以参与一轮。参与后观察群友是否接住、纠正或表现出厌烦；没有继续互动时不要追加消息，后续相关时再重新判断。

简单反应可以短，但不要让“嗯/好/哈哈/咋了”变成没有内容的默认填充。被烦到时可以像普通人一样简短收口，不写规则说明、资源限制、内部判断或道歉小作文。
```

绝不能写入模型 prompt：

```text
用户压力分数
恶意概率
剩余 token
限频 bucket
API 成本
```

这些留在外围状态和日志。

---

# Phase 6：离线回放、灰度和上线

## Task 14：建立固定 holdout 数据集

**Objective:** 防止每次修改只对最近几条消息有效，或者因为自我学习形成错误闭环。

**Files:**
- Create: `tests/fixtures/reply_judgment_holdout.jsonl`
- Create: `scripts/replay_reply_judgment.py`
- Test: `tests/test_reply_judgment_holdout.py`

**Holdout 类型：**

```text
明确 @ 可洛喵
直接叫“可洛还在吗”
@其他用户
引用别人消息
普通群友互聊
兴趣话题开放提问
单独图片
工具事实问题
接话后无关后续消息
负面反馈后消息
```

每类至少 10 条脱敏样本，不能包含真实私密内容。

**通过指标：**

```text
明确呼叫召回率不下降
误接率不增加
post_reply_boundary 不恶化
低信息回复率不增加
工具有效率不下降
prompt token 增长在预算内
```

---

## Task 15：先在开高群做 dry-run / shadow 观察

**Objective:** 只让开高群使用候选关系摘要和资源策略记录，不立即扩大到所有群。

**步骤：**

1. 运行候选 reader，不注入模型，只记录本来会读取什么；
2. 运行资源限频 dry-run，不丢消息；
3. 观察至少 1～2 个完整窗口；
4. 对比实际模型输出和候选策略；
5. 人工检查 false positive/false negative；
6. 只有通过才允许小范围注入摘要。

**禁止：**

- 同时改全局 SOUL、群 override、adapter 和模型；
- 同时轮换多个群 session；
- 以“回复数上升”作为唯一成功标准；
- 用 quiet 拦截结果冒充模型判断改善。

---

## Task 16：小范围启用关系摘要

**Objective:** 让关系状态第一次真正影响可洛喵的语气和互动方式。

**启用范围：**

- 只允许一个 normal 群；
- 只允许当前 sender 的当前群摘要；
- 只影响称呼、熟悉语气、回应细腻度；
- 不改变是否回复；
- 不改变工具权限；
- 不改变资源配额。

**上线检查：**

```bash
python -m pytest -q
python scripts/replay_reply_judgment.py --fixture tests/fixtures/reply_judgment_holdout.jsonl
python scripts/extract_profile_events.py --dry-run ...
```

确认后再写入实际 profile prompt，并保留带时间戳备份。

---

## Task 17：扩展到猫窝，并保留开高独立关系状态

**Objective:** 验证同一用户在不同群中的关系和语气确实可以不同。

**要求：**

- 开高与猫窝的原始关系事件不互相注入；
- 全局稳定兴趣可以共享，但群内互动温度不能共享；
- 猫窝群主语气与开高普通群员语气仍由各自群 override 决定；
- 不因猫窝里熟悉而在开高群提高回复频率；
- 不因开高的负面反馈而永久影响猫窝关系。

---

# Phase 7：自主迭代和 watchdog 正式闭环

## Task 18：更新每 6 小时自主迭代任务

**Objective:** 让可洛喵自己积累画像和接话经验，但遵守候选—验证—晋升规则。

**周期：** 每 6 小时。

**必须报告：**

```text
本轮每个群的样本数量
profile event 新增数
关系维度变化
接话五类样本数
工具调用有效率
是否产生候选变更
是否达到晋升条件
是否写入群档案/index/SOUL
```

默认行为：

```text
只写群档案和候选区
不自动改 SOUL
不自动写 mem0
```

## Task 19：更新每小时 watchdog

**Objective:** 独立审查可洛喵是否出现更大结构性问题。

**重点：**

- 持续误接旁人；
- 明确呼叫漏接；
- 接完后持续插话；
- 画像跨群污染；
- 好感影响权限；
- 负反馈被过度泛化；
- 工具调用不必要或失败；
- 资源保护造成正常呼叫漏接；
- `_REPLY`/内部状态出站；
- 低信息回复和模板化行为。

watchdog 只报告问题和证据；除协议、安全和明确可复现代码 bug 外，不自动修改运行行为。

## Task 20：建立人工批准的 SOUL 晋升流程

**Objective:** 防止自主学习把偶发结论写进核心人设。

**晋升材料必须包含：**

1. 至少两次独立观察；
2. 至少一个相反样本检查；
3. 当前群和跨群范围；
4. prompt token 影响；
5. holdout 结果；
6. 灰度窗口结果；
7. 回滚文本和备份路径；
8. 明确说明是软行为规则，不是硬门禁。

只有主人确认后才允许：

```text
候选群档案 → index.md
index.md → 群级 system_prompt
群级 system_prompt → SOUL.md
```

---

# 数据结构示例

## 用户全局画像

```json
{
  "user_key": "qq:2450763725",
  "stable_facts": [
    {
      "topic": "Minecraft",
      "value": "经常讨论",
      "confidence": 0.82,
      "evidence_count": 6,
      "last_updated": "2026-09-01T00:00:00+08:00"
    }
  ]
}
```

## 群内用户画像

```json
{
  "user_key": "qq:2450763725",
  "group_id": "185465601",
  "communication_style": [
    {
      "value": "短句、梗、会直接反馈 bot",
      "confidence": 0.74,
      "evidence_count": 8
    }
  ]
}
```

## 群内关系状态

```json
{
  "group_id": "185465601",
  "user_key": "qq:2450763725",
  "relationship": {
    "familiarity": {"value": 0.68, "confidence": 0.75},
    "warmth": {"value": 0.61, "confidence": 0.65},
    "trust": {"value": 0.54, "confidence": 0.58},
    "interaction_affinity": {"value": 0.77, "confidence": 0.72},
    "boundary_sensitivity": {"value": 0.72, "confidence": 0.80}
  },
  "short_term": {
    "last_feedback": "希望不要逐条抢话",
    "expires_at": "2026-09-02T00:00:00+08:00"
  }
}
```

模型默认不直接看到这些数字，只看到经过筛选的极短自然语言摘要。

---

# 完成标准

整个项目只有同时满足以下条件，才算完成：

## 数据正确性

- QQ 号作为不可伪造主键；
- 同群与跨群数据隔离；
- 用户评价、模型判断、实际发送结果分开；
- 画像事件可追溯到真实消息 ID；
- 不把一次消息写成稳定事实。

## 行为质量

- 明确呼叫召回率不下降；
- 误接旁人率不增加；
- 接话后持续插话率下降或不恶化；
- 低信息模板回复率下降；
- 关系状态只改善语气，不接管回复决策；
- 负反馈能产生短期边界效果，但不过度永久化。

## 上下文质量

- 普通消息不注入完整用户画像；
- 当前 turn 只注入最小摘要；
- 输入 token 在预算内；
- 不再把完整历史和完整迭代日志常驻上下文；
- `message-alternation repair` 不因新功能增加。

## 资源保护

- 工具调用、模型请求、出站发送分别限频；
- 正常用户的短暂高频不会被误封；
- 持续资源压力有 dry-run、告警和可逆冷却；
- provider 失败使用指数退避和 jitter；
- 资源保护不会改变可洛喵的自然说话风格。

## 可回滚性

- 每个阶段独立提交；
- 每次线上写入都有备份；
- 可以只关闭关系摘要，不影响接话判断；
- 可以只关闭资源策略，不删除画像数据；
- 可以只归档候选档案，不动 SOUL；
- 开源仓库不含任何真实 profile、密钥、群聊和数据库。

---

# 推荐执行顺序

不要一次执行全部任务。推荐按以下批次推进：

```text
批次 A：Task 1–3
  只建仓库边界、schema、渐进式披露目录

批次 B：Task 4–6
  只抽取画像候选、更新关系状态、实现 reader

批次 C：Task 8–10
  接话判断数据集、反馈复核、自主迭代闭环

批次 D：Task 11–13
  资源压力 dry-run、token bucket、拟真表达分离

批次 E：Task 14–17
  holdout、开高灰度、猫窝验证

批次 F：Task 18–20
  watchdog、自主迭代、人工晋升 SOUL
```

每个批次结束后暂停，检查真实数据和群友反馈，再决定是否进入下一批。第一批不改变线上行为，第二批不影响回复，第三批才开始把学习结果作为候选摘要使用。
