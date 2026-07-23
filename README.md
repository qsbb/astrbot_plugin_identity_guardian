<h1 align="center">astrbot_plugin_identity_guardian</h1>

<p align="center">
  身份守护者 · Identity Guardian<br />
  让 bot 在 QQ 群里像真实成员一样理解「自己是谁、对方是谁、双方是什么关系、当前能做什么」，
  并把经过代码层授权的行动能力提供给 LLM。
</p>

<p align="center">
  <a href="#安装">安装</a>
  ·
  <a href="#配置项">配置项</a>
  ·
  <a href="#llm-工具">LLM 工具</a>
  ·
  <a href="#指令组">指令组</a>
  ·
  <a href="#安全模型">安全模型</a>
  ·
  <a href="#免责声明">免责声明</a>
</p>

---

## 项目定位

本插件的核心不是自动群治理，而是把**身份、关系、对象与风险**共同映射为可执行行动，使情绪、人设及其他插件产生的对话意图能够安全落地。

- bot 在群里获取当前身份（群主 / 管理员 / 普通成员）并注入提示词
- 分析两个身份能干什么，并把能做到的东西创建工具供 LLM 调用
- 普通成员只能请求影响自己（如禁言自己、改自己的名片）
- 主人与 bot 互动时允许害羞式短禁言（可配置、严格限时）
- 群主 / 管理员时，根据 QQ 入群申请中的问题与答案自动审核
- bot 进入新群聊被欢迎时表现一般人反应
- 有不好的消息自动禁言 / 撤回（可选，默认关闭）
- 与 [astrbot_plugin_active_learner](https://github.com/qsbb/astrbot_plugin_active_learner) 知识库联动辅助入群审核

## 设计哲学

1. **身份不等于授权**：bot 的群角色只决定权限上限，最终授权还必须考虑请求者、目标、关系、动作类型与配置策略。
2. **允许行动而非强制行动**：工具可用只表示「可以做」，不代表「必须做」；是否执行由主对话 LLM 结合人设、情绪与上下文自主判断。
3. **本人请求与他人请求分离**：普通成员可以请求 bot 对请求者本人执行允许的动作，但不能借此影响第三人。
4. **互动保护与强保护分离**：主人、群主、管理员默认免疫踢出、长时禁言等高风险动作；可单独允许严格限时的玩笑式短禁言，不采用绝对免疫。
5. **只自动放行，不自动拒绝**：入群答案只有在高置信度正确时自动通过；错误、不确定、知识不足或联动失败均保持待审，交由其他管理员处理。
6. **决策可追溯**：每次有副作用的行动记录请求者、目标、关系、授权依据、参数、结果与简短决策摘要。
7. **平台隔离**：仅 aiocqhttp 适配器启用 OneBot 行动；其他平台仅注入可获取的身份与关系信息。

## 功能概览

| 模块 | 能力 |
| --- | --- |
| 身份识别 | bot / 发送者 / 目标在群中的 role（owner / admin / member）查询与缓存 |
| 关系识别 | 主人 / 友好用户 / 普通群员关系解析，注入到 LLM 上下文 |
| 行动边界注入 | `on_llm_request` 钩子注入当前身份、关系、允许行动集与安全规则 |
| LLM 工具 | 13 个工具：禁言、踢出、撤回、改名片、改头衔、改群名、设管理员、全员禁言、入群审核等 |
| 三层安全防护 | 规则预筛 + 独立 LLM 审核 + API 层硬拦截（默认开启第三层） |
| 入群审核 | 读取 QQ 申请问题与答案，结合配置答案与可选知识库，仅高置信度时自动通过 |
| 内容审核 | 独立于主对话的 LLM 内容审核，违规分级处罚（默认关闭） |
| 防刷屏 | 同一用户 10 秒内消息数超阈值直接禁言（可配置） |
| 二次确认 | 高破坏性操作（踢人、全员禁言、改群名、任命管理员）走人工确认队列 |
| 熔断器 | 1 小时内管理操作总数超阈值自动停用所有高风险工具 |
| 审计日志 | 所有有副作用的行动写入 `audit.jsonl`，便于复盘与申诉 |
| 紧急停止 | `/idg stop` 一键停用所有管理工具，仅保留身份注入 |
| 知识库联动 | 入群审核可查询 active_learner 知识库作为判定证据 |

## 安装

1. 将本仓库放入 AstrBot 插件目录。

```bash
git clone https://github.com/qsbb/astrbot_plugin_identity_guardian.git
```

2. 安装依赖（首版零第三方依赖，全部使用 AstrBot 内置能力）。

```bash
pip install -r requirements.txt
```

3. 完全重启 AstrBot 以加载新插件（热重载无法生效新版本代码）。

4. 在 AstrBot 插件管理中启用本插件。

5. 在插件配置中填写 `owner_users`（bot 主人 QQ 号列表）。

## 配置项

### 基础

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 插件总开关 |
| `owner_users` | list<string> | `[]` | bot 主人 QQ 号列表（纯数字字符串），用于建立主人关系，不依赖聊天文本自称。例如 `["123456", "789012"]` |
| `friendly_users` | list<string> | `[]` | 额外友好用户 QQ 号列表（纯数字字符串）。群主和管理员可按平台身份自动识别，无需重复填写 |
| `protected_users` | list<string> | `[]` | 强保护用户 QQ 号列表（纯数字字符串），禁止被踢出、长时禁言及批量处罚 |
| `log_level` | string | `INFO` | 日志级别：DEBUG / INFO / WARNING / ERROR |

> 注：`owner_users` / `friendly_users` / `protected_users` / `blacklist_users` 填写的是 **QQ 号**（OneBot 平台 ID，纯数字字符串），非 AstrBot 内部 UID。代码中通过 `event.get_sender_id()` 比对，aiocqhttp 平台返回的 sender_id 即 QQ 号。

### 互动与处罚

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `allow_playful_mute_protected` | bool | `false` | 允许对主人 / 强保护用户进行玩笑式短禁言 |
| `playful_mute_max_seconds` | int | `60` | 玩笑式禁言最大时长（秒），必须小于 `max_mute_seconds` |
| `max_mute_seconds` | int | `1800` | LLM 无法超过此值 |
| `confirm_mute_threshold` | int | `3600` | 需人工确认的禁言时长阈值（秒） |
| `auto_confirm_threshold` | string | `mute_short` | 自动执行的最高处罚档位：warn / mute_short / mute_long / delete / kick |
| `blacklist_users` | list<string> | `[]` | 永久黑名单 QQ 号列表（纯数字字符串），触发即踢 |
| `action_cooldown_seconds` | int | `60` | 同一用户同一操作冷却（秒） |
| `circuit_breaker_threshold` | int | `10` | 全局熔断阈值（1 小时内管理操作总数） |

### 内容审核

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `auto_moderate` | bool | `false` | 启用独立 LLM 内容审核（第二层）。若已安装其他防注入 / 内容过滤插件，可关闭此项避免重复审核 |
| `moderation_rules` | list<string> | `[]` | 违规关键词正则列表（第一层预筛），命中即固定处罚不经 LLM。例如 `["加我微信.*", "免费领.*"]` |
| `spam_threshold` | int | `5` | 刷屏阈值（条 / 10 秒，0 = 关闭） |
| `manual_threshold` | float | `0.6` | 内容审核置信度低于此值时不自动处罚 |
| `cross_group_violation` | bool | `false` | 违规历史是否跨群共享 |

### 安全护栏

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enable_api_guard` | bool | `true` | 启用 API 层硬拦截（第三层护栏），强烈建议始终开启 |
| `enable_set_admin_revoke` | bool | `false` | 是否允许撤销管理员操作（默认只允许任命） |

### 入群审核

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `join_audit_mode` | string | `off` | 入群审核模式：off / approve_only / notify_only |
| `join_questions` | list<string> | `[]` | 入群问答配置，每项一行，格式 `问题\|答案1,答案2`。例如 `["1+1=?\|2,二", "本群做什么的\|技术交流,编程讨论"]`。不含 `\|` 时整体视为答案。留空则仅依赖 LLM 语义判断 |
| `join_approve_threshold` | float | `0.9` | 自动通过的最低置信度 |
| `audit_notify_targets` | list<string> | `[]` | 审核人工通知目标列表（AstrBot 会话 ID，unified_msg_origin 格式）。例如 `["aiocqhttp:GroupMessage:123456"]` |
| `pending_ttl_hours` | int | `24` | 待审请求保留时长（小时） |

### 知识库联动

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enable_active_learner_recall` | bool | `false` | 入群审核查询 active_learner 知识库 |
| `active_learner_scope` | string | `group` | 知识检索范围：group / global |

### LLM 与欢迎

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `audit_llm_provider` | string | `""` | 审核用 LLM Provider（留空则回退主对话 LLM），可填便宜模型降低成本 |
| `confirm_notify_targets` | list<string> | `[]` | 二次确认通知目标列表（AstrBot 会话 ID，unified_msg_origin 格式） |
| `welcome_bot_speak` | bool | `false` | bot 进群是否主动发言 |
| `welcome_template` | string | `""` | bot 进群发言模板 |
| `identity_refresh_interval` | int | `1800` | 身份刷新间隔（秒） |

## LLM 工具

工具按身份分级暴露，运行时由 `PolicyEngine` 硬校验：

### 反应与自助（普通成员可用）

| 工具 | 说明 |
| --- | --- |
| `mute_current_sender` | 短时禁言当前消息发送者。目标由事件绑定，不能指定第三人。适用于对方辱骂、骚扰后的反应，或与主人互动的玩笑式短禁言 |
| `request_self_mute` | 响应当前发送者对其本人的禁言请求。目标由系统绑定为请求者本人，不接受 user_id |
| `set_self_card` | 修改 bot 自己的群名片 |

### 管理员工具

| 工具 | 说明 |
| --- | --- |
| `mute_member` | 禁言指定群成员。仅友好用户（主人 / 管理员）请求时可用 |
| `unmute_member` | 解除指定群成员的禁言 |
| `kick_member` | 踢出指定群成员。高风险操作，需人工确认 |
| `delete_message` | 撤回一条群消息 |
| `set_member_card` | 设置群成员名片。普通成员只能修改自己的名片 |
| `set_group_name` | 修改群名称。需人工确认 |
| `set_whole_ban` | 开启或关闭全员禁言。高风险操作，需人工确认 |
| `approve_join_request` | 批准入群申请 |

### 仅群主

| 工具 | 说明 |
| --- | --- |
| `set_member_title` | 设置群成员专属头衔 |
| `set_group_admin` | 设置或撤销群管理员。需人工确认，撤销需额外配置 `enable_set_admin_revoke=true` |

## 指令组

`/idg` 指令组提供人工兜底入口：

| 指令 | 说明 |
| --- | --- |
| `/idg status` | 查看插件状态（版本、运行状态、熔断、冷却、待确认等） |
| `/idg stop` | 紧急停止所有管理工具（仅注入身份） |
| `/idg resume` | 恢复管理操作 |
| `/idg reset_breaker` | 重置熔断器 |
| `/idg refresh` | 刷新身份缓存（下次请求时重新获取） |
| `/idg approve <confirm_id>` | 批准待确认操作 |
| `/idg reject <confirm_id>` | 拒绝待确认操作 |
| `/idg help` | 查看帮助 |

## 安全模型

### 三层防护

```
┌─────────────────────────────────────────────────────────────┐
│  第一层：规则预筛（moderation_rules + spam_threshold）        │
│  命中即固定处罚，不调 LLM                                    │
└─────────────────────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  第二层：独立 LLM 审核（auto_moderate）                       │
│  独立于主对话消息链路，不注入身份、不注册工具、只输出 JSON     │
│  默认关闭，若已安装其他防注入插件可关闭此项                   │
└─────────────────────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  第三层：API 层硬拦截（enable_api_guard，默认开启）           │
│  在管理 API 真正执行前校验白名单、频率、批量、熔断           │
│  命中即阻断，即使 LLM 被完全注入也无法绕过                   │
└─────────────────────────────────────────────────────────────┘
```

### 授权判定模型

每个有副作用的动作必须由统一策略引擎 `PolicyEngine.evaluate` 计算：

```
ActionDecision = PolicyEngine.evaluate(
    bot_role,           # bot 在当前群的 role
    requester_role,     # 请求者 role
    requester_relation, # 请求者关系（owner/friendly/normal）
    target_role,        # 目标 role
    target_relation,    # 目标关系
    action,             # 动作名
    target_is_requester,# 目标是否为请求者本人
    trigger_source,     # 触发来源：llm_autonomous/self_service/explicit_request
    requested_params,   # 请求参数
)
```

### 操作危险等级

| 等级 | 操作 | 默认流程 |
| --- | --- | --- |
| L0 只读 | get_*, list_* | 直接执行 |
| L1 轻微 | mute ≤ 阈值、delete_msg（≤120s）、set_self_card | 直接执行 |
| L2 中等 | mute > 阈值、set_card、set_group_name | 直接执行 + 日志 |
| L3 高 | kick、set_whole_ban、set_title、set_admin(任命) | 人工二次确认 |
| L4 极高 | set_group_admin 撤销他人管理员 | 人工确认 + 仅群主 + 显式配置开启 |

### 防滥用机制

- **关系与保护分离**：`owner_users` / `friendly_users` 用于关系理解，`protected_users` 用于强保护
- **参数上限**：禁言时长受 `max_mute_seconds` 约束，LLM 无法突破
- **冷却**：同一目标同一操作冷却内不重复执行
- **分级频率限制**：L0 30 次 / 分钟，L1-L2 10 次 / 分钟，L3 3 次 / 分钟，L4 1 次 / 小时
- **批量操作检测**：60 秒内对 5+ 不同 user_id 调用 kick/mute → 立即熔断 + 通知管理员
- **全局熔断**：1 小时内管理操作总数超阈值 → 自动停用所有高风险工具
- **黑名单兜底**：`blacklist_users` 触发即踢，不经过 LLM
- **紧急停止**：`/idg stop` 立即停用所有管理工具

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│  LLM (Function Calling 决策层)                              │
└─────────────────────────────────────────────────────────────┘
                          ▲ 工具调用 / 提示词
┌─────────────────────────────────────────────────────────────┐
│  插件主类 main.py (Star)                                     │
│  ├─ on_llm_request:        身份注入                          │
│  ├─ event_message_type(ALL): notice/request 分发            │
│  ├─ llm_tool(*):           工具实现                          │
│  └─ command_group("idg"):  人工兜底指令                      │
└─────────────────────────────────────────────────────────────┘
                          │ 调用
┌─────────────────────────────────────────────────────────────┐
│  core/ 业务层                                                │
│  ├─ identity.py    bot、发送者与目标身份查询和缓存           │
│  ├─ relationship.py 主人/友好用户/普通成员关系解析           │
│  ├─ policy.py      身份×关系×目标×动作统一授权策略           │
│  ├─ capability.py  能力描述与工具注册                         │
│  ├─ audit.py       入群申请判断与仅通过执行                  │
│  ├─ knowledge.py   active_learner 只读知识检索桥接           │
│  ├─ moderation.py  内容审核（违规判断、分级处罚）            │
│  ├─ welcome.py     bot 进群欢迎                             │
│  ├─ confirm.py     二次确认流程（待审队列）                  │
│  ├─ cooldown.py    操作冷却与防刷屏                          │
│  ├─ audit_log.py   审计日志                                  │
│  ├─ prompts.py     提示词模板                                │
│  ├─ onebot.py      OneBot API 封装（错误处理、超时）         │
│  └─ config.py      配置读取与校验                            │
└─────────────────────────────────────────────────────────────┘
                          │
┌─────────────────────────────────────────────────────────────┐
│  AstrBot 框架 (Context / StarTools / ProviderRequest)       │
└─────────────────────────────────────────────────────────────┘
```

## 与其他插件联动

### astrbot_plugin_active_learner

入群审核可通过稳定的只读桥接接口查询 active_learner 知识库。知识仅作为判定证据，查询失败或证据不足时保持待审，不自动拒绝。

开启方式：

1. 安装并启用 [astrbot_plugin_active_learner](https://github.com/qsbb/astrbot_plugin_active_learner)
2. 在本插件配置中开启 `enable_active_learner_recall`
3. 选择检索范围 `active_learner_scope`：`group`（仅当前群）或 `global`（全局）

### 与 conversation_flow / 情绪 / 人设插件

本插件**不接管或重写**其他插件的判断。只补充身份上下文和行动执行层：

- 其他插件产生情绪、人设、对话意图 → 主对话 LLM 综合判断
- LLM 决定执行行动 → 调用本插件工具
- 本插件 `PolicyEngine` 硬校验授权 → 执行或拒绝

## 插件信息

| 项目 | 内容 |
| --- | --- |
| 插件名 | `astrbot_plugin_identity_guardian` |
| 展示名 | 身份守护者 |
| 当前版本 | `v0.1.0` |
| 作者 | Justice-ocr |
| AstrBot 版本 | `>=4.17.0,<5.0.0` |
| 支持平台 | `aiocqhttp`（NapCat / Lagrange / LLOneBot 等 OneBot V11 实现） |
| 许可证 | `MIT` |

## 开发与验证

```bash
# 代码风格检查
python -m ruff check .
python -m ruff format --check .

# 单元测试（59 个用例）
python -m pytest tests/ -v
```

测试覆盖：策略引擎（policy）、能力映射（capability）、配置解析（config）、冷却与熔断（cooldown）、内容审核（moderation）、入群审核（audit）。

## 免责声明

- 本插件提供群管理行动能力，使用者需自行确认使用场景符合平台规则与法律法规。
- 所有有副作用的行动均记录审计日志，便于复盘与申诉。
- 入群审核仅自动放行高置信度正确答案，不自动拒绝任何申请；未通过自动审核的申请保留 QQ 待审状态，交由其他管理员处理。
- 内容审核功能依赖 LLM 文本判断，存在误判可能；如已安装其他防注入 / 内容过滤插件，可关闭本插件的 `auto_moderate` 避免重复审核。
- 紧急情况下可使用 `/idg stop` 立即停用所有管理工具。
- 插件作者不对 LLM 误判、第三方服务变更、账号封禁或任何滥用后果承担责任。

## 致谢

- 感谢 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 提供插件系统、LLM Tool、事件钩子与 Pages 能力。
- 感谢 [astrbot_plugin_active_learner](https://github.com/qsbb/astrbot_plugin_active_learner) 提供知识库联动能力。
- 设计参考了传统群管插件（艾雨综合群管、OPQBot-GroupManager）的关键词规则思路，但在身份感知、关系识别与三层防护上有本质区别。
