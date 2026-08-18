# 凝心溯溪-序

> 凝心溯溪系列身份模块：让 bot 在 QQ 群里像真实成员一样理解「自己是谁、对方是谁、双方是什么关系、当前能做什么」，并把经过代码层授权的行动能力提供给 LLM。

> **凝心溯溪系列** 当前完整插件清单为知、言、序、情、境、声、核：各插件职责独立、互不冲突，可按需组合使用，覆盖知识学习、对话调节、身份管理、关系状态、环境感知、语音与更新管理。

| 字 | 模块 | 说明 |
|----|------|------|
| [知](https://github.com/qsbb/astrbot_plugin_active_learner) | 知识学习 | 自动检索注入、多源学习、交叉验证 |
| [言](https://github.com/qsbb/astrbot_plugin_conversation_flow) | 对话调节 | 沉默判断、智能分段、插话衔接 |
| [序](https://github.com/qsbb/astrbot_plugin_identity_guardian) | 身份管理 | 关系感知、权限边界、群组行动（本插件） |
| [情](https://github.com/qsbb/astrbot_plugin_relationship) | 关系状态 | 情绪、好感、信任、熟悉度状态记录与只读建议 |
| [境](https://github.com/qsbb/astrbot_plugin_environment_awareness) | 环境感知 | 时间、天气、空气质量、预警与环境关心候选 |
| [声](https://github.com/qsbb/astrbot_plugin_voice_hub) | 语音合成 | 双 TTS 后端、多音色管理、AI 导演 |
| [核](https://github.com/qsbb/astrbot_plugin_update_manager) | 更新管理 | 安全检查、计划、串行更新与回滚 |

---

## 当前实现信息

- 版本号以 `metadata.yaml` 为唯一事实源；AstrBot 兼容范围：`>=4.17,<5`；主要支持 `aiocqhttp`。
- 命令入口：`/idg` 命令组，支持状态、停止/恢复、熔断重置、身份刷新和待确认操作处理。
- 页面入口：身份控制面由受信任消费者（例如临的管理页）提供结构化入口；序自身仍可通过 AstrBot 插件配置独立使用。
- 权限身份始终取当前平台事件的原始 `sender_id` 与 bot/group role；情中配置的跨平台自然人归属只用于关系和记忆连续性，不能继承主人、友好、保护、黑名单或群管理权限。

### 系列诊断日志

- 诊断会捕获本插件自有 logger 的 `DEBUG` 到 `CRITICAL` 事件；内存缓冲最多保留 1000 条，日志页单次最多读取 1000 条、浏览器最多暂存 10000 条。每条记录由“核”先显示插件中文名，再显示时间、级别和事件。
- 插件把必要的生命周期事件、警告和错误写入内存环形缓冲，并通过 `series.diagnostics@1.0` 只读契约供“核”的日志页汇总查看。
- 契约完整声明系列 ID、插件 ID、中文简称、内存存储及读取/清空能力；仓库元数据匹配后由“核”自动发现，不需要修改“核”。
- 这条诊断通道与 AstrBot 主日志隔离，不会转发诊断记录，也不会读取 AstrBot 全局日志；它只收集本插件自身已经产生的日志和明确诊断事件。
- 自动捕获事件会保留模块、函数、行号、异常类型，以及最长 2000 字符的脱敏日志正文；在“核”的日志页点击事件即可展开。插件不会额外读取聊天消息，但若本插件原有日志本身含有用户文本片段，该片段会在脱敏、截断后进入内存详情。
- 写入前会隐藏令牌、账号标识等敏感字段，并截断过长内容。缓冲仅存在于当前进程，清空、重启或热重载后自动消失并更换流标识。
- “核”不是运行依赖：没有安装或没有启用“核”时，序仍照常执行身份判断和安全授权，只是缺少统一日志查看入口。

## 项目定位

本插件的核心不是自动群治理，而是把**身份、关系、对象与风险**共同映射为可执行行动，使情绪、人设及其他插件产生的对话意图能够安全落地。

- bot 在群里获取当前身份（群主 / 管理员 / 普通成员）并注入提示词
- 分析两个身份能干什么，并把能做到的东西创建工具供 LLM 调用
- 普通成员只能请求影响自己（如禁言自己、改自己的名片）
- 主人与 bot 互动时允许害羞式短禁言（可配置、严格限时）
- 群主 / 管理员时，根据 QQ 入群申请中的问题与答案自动审核
- bot 进入新群聊被欢迎时表现一般人反应
- 有不好的消息自动禁言 / 撤回（内容审核层为预留能力，运行时未启用）
- 与 [astrbot_plugin_active_learner](https://github.com/qsbb/astrbot_plugin_active_learner) 知识库联动辅助入群审核

## 设计哲学

1. **身份不等于授权**：bot 的群角色只决定权限上限，最终授权还必须考虑请求者、目标、关系、动作类型与配置策略。
2. **允许行动而非强制行动**：工具可用只表示「可以做」，不代表「必须做」；是否执行由主对话 LLM 结合人设、情绪与上下文自主判断。
3. **本人请求与他人请求分离**：普通成员可以请求 bot 对请求者本人执行允许的动作，但不能借此影响第三人。
4. **互动保护与强保护分离**：主人、群主、管理员默认免疫踢出、长时禁言等高风险动作；可单独允许严格限时的玩笑式短禁言，不采用绝对免疫。
5. **只自动放行，不自动拒绝**：入群答案只有在高置信度正确时自动通过；错误、不确定、知识不足或联动失败均保持待审，交由其他管理员处理。
6. **决策可追溯**：每次有副作用的行动记录请求者、目标、关系、授权依据、参数、结果与简短决策摘要。
7. **平台隔离**：仅 aiocqhttp 适配器启用 OneBot 行动；其他平台仅注入可获取的身份与关系信息。
8. **身份双轨**：权限轨只认原始平台账号，连续性轨可由情映射到同一自然人；两条轨道不互相提权。

## 功能概览

| 模块 | 能力 |
| --- | --- |
| 身份识别 | bot / 发送者 / 目标在群中的 role（owner / admin / member）查询与缓存 |
| 关系识别 | 主人 / 友好用户 / 普通群员关系解析，注入到 LLM 上下文 |
| 行动边界注入 | `on_llm_request` 钩子注入当前身份、关系、允许行动集与安全规则 |
| LLM 工具 | 14 个工具：禁言、踢出、撤回、改名片、改头衔、改群名、设管理员、全员禁言、成员查询等 |
| 三层安全防护 | 规则预筛（预留，运行时未启用）+ 独立 LLM 审核（预留，运行时未启用）+ API 层硬拦截（默认开启，校验紧急停止与全局熔断） |
| 主动消息授权 | 用完整私聊 UMO 白名单确认主人收件权限；不接受群聊目标或跨平台关系继承 |
| 入群审核 | 读取 QQ 申请问题与答案，按「按群/全局问答预设 → 知识库证据 → 转忽略/人工」顺序判定，仅高置信度时自动通过，无依据时不再 LLM 自由判断 |
| 申请推送 | 有人申请进群且进入人工待审时，自动推送到该群配置的推送群（留空回退推送到申请所属群本身）；推送与审批结果回复都由 AI 按人设自由措辞（只给事实不写死模板；natural 整段生成，formatted 附 LLM 一句话看法、失败回退自动审核结论行，结果回复 LLM 失败回退固定文案），并引导管理员引用推送消息回复「同意/不同意」直接审批，或到入群审核管理页处理 |
| 内容审核 | 独立于主对话的 LLM 内容审核，违规分级处罚（默认关闭；预留，运行时未启用） |
| 防刷屏 | 同一用户 10 秒内消息数超阈值直接禁言（可配置；预留，运行时未启用） |
| 二次确认 | 高破坏性操作（踢人、全员禁言、改群名、任命管理员）走人工确认队列 |
| 熔断器 | 1 小时内管理操作总数超阈值自动停用所有高风险工具 |
| 审计日志 | 所有有副作用的行动写入 `audit.jsonl`，便于复盘与申诉 |
| 紧急停止 | `/idg stop` 一键停用所有管理工具，仅保留身份注入 |
| 知识库联动 | 入群审核可查询 active_learner 知识库作为判定证据 |

序会把“身份边界”和“安全规则”登记为结构化提示片段。安装言时，由言先于知识与关系片段统一
编排并去重；未安装言时仍沿用原直接注入。无论哪条路径，最终 API 执行前都会再次经过
`PolicyEngine` 和 API 护栏，提示词本身不构成授权。

### 主动消息授权契约

序提供 `identity.proactive_authorization@1.0` 只读契约。消费者把完整 `recipient_umo` 交给 `authorize_proactive_delivery()`；只有插件启用、未紧急停止，UMO 的平台、消息类型和会话三段均非空，消息类型精确为 `FriendMessage`、`PrivateMessage` 或 `DirectMessage`，且目标与 `proactive_delivery_targets` 中某一项完全相同时才返回授权。结果不返回 UID，也不执行发送。

这个白名单与 `owner_users` 分开维护：前者授权一个具体平台会话接收主动消息，后者用于当前平台事件中的主人关系判断。情的自然人绑定、好感或其它平台账号不能自动进入白名单。凝心溯溪-境的主动关心还会由言再次调用本契约，群聊、频道、近似字符串和未列出的私聊目标全部失败关闭。

### 跨会话读取授权契约

序提供 `identity.context_bridge_authorization@1.0`，只回答“言这一轮能不能读取另一个作用域的近期弱上下文”，不读取缓存、不返回身份或正文，也不执行发送：

- 其他已绑定私聊 → 当前私聊：只读允许，仍由言判断当前问题是否相关；
- 本人此前群聊 → 当前私聊：只读允许，只能包含这个自然人自己的消息和由其触发的 bot 回复；
- 群聊 → 群聊：拒绝，避免把一个群的内容搬到另一个群；
- 私聊 → 群聊：默认拒绝。当前群消息明确说“继续私聊的话题”时只允许 `topic_only`，群里只能知道近期存在相关对话以及当前群消息已经公开写出的主题；明确说“把私聊具体内容发到群里”时才允许 `details`，且只取必要短片段。

私聊进入群聊的同意只对当前这一条群消息有效，不保存也不继承。出现“不要、不同意、取消”、假设、举例、询问规则或含糊表达时一律拒绝；引用消息里的授权文字不算当前用户本轮同意。这个契约属于上下文隐私边界，不改变主人、友好、保护、黑名单或群管理权限，跨平台自然人身份也不能据此提权。

### Quest 私聊只读授权契约

序提供 `identity.quest_session_authorization@1.0`，供 [astrbot_plugin_embodiment_bridge](https://github.com/qsbb/astrbot_plugin_embodiment_bridge) 判断一个 HTTP 会话能否复用主人范围的只读上下文。调用 `authorize_quest_session(request)`，建议消费者使用契约声明的 `1000ms` 超时。请求必须是普通对象，且只包含下列字段：

```json
{
  "api_principal": "<已认证 API 主体>",
  "client_id": "<Quest 客户端 ID>",
  "platform_id": "<平台实例 ID>",
  "bot_id": "<Bot 原始平台 ID>",
  "user_id": "<主人原始平台 ID>",
  "group_id": null
}
```

- `api_principal` 是 AstrBot API 已认证的稳定主体名称，不是 token 或密码；`client_id` 是稳定的 Quest 客户端标识。
- 权限身份三元组固定为原始 `platform_id + bot_id + user_id`；`group_id` 只表示会话上下文，必须显式传入，私聊使用 `null` 或空字符串，任何非空群号均拒绝。
- 只有 `user_id` 已在 `owner_users` 中，且完整精确绑定命中时才授权。新配置由控制面按 `sha256(api_principal)|client_id|platform_id|bot_id|user_id` 保存；旧版明文首段仅作读取兼容。任何一段近似匹配、跨平台或跨 bot 都不会继承。
- 情中的自然人绑定、好感、关系档位和其他平台账号均不参与权限判断；结果不返回身份数据，也不授予发送、群管、主动消息或任何平台动作。

响应固定包含 `contract_version`、`status`、`authorized`、`reason`、`access`、`owner_confirmed` 和 `grants_platform_action`。`status=authorized` 时仅表示 `access=read_only_context`；业务拒绝返回 `denied`，插件关闭或紧急停止返回 `unavailable`，提供方内部异常返回 `error`。消费者遇到契约缺失、major 版本不兼容、超时、异常、非对象响应、字段缺失或任何非 `authorized` 状态都必须失败关闭：不要读取受保护上下文，但可以继续完全隔离的基础 Quest 对话。

### 统一身份控制面契约

序提供可选的 `identity.control_plane@1.0`，让临等受信任插件把结构化管理页面作为代理入口，而不复制身份授权规则：

- `get_identity_control_plane()` 只返回启用状态、可写状态以及主人和 Quest 绑定数量，不返回账号、绑定串、显示名或密钥。
- `upsert_quest_owner_binding()` 一次接收 API 主体摘要、客户端、平台、Bot 和用户五个字段，原子同步 `owner_users` 与 `quest_session_owner_bindings`；保存失败时运行态不变。
- API 主体由消费者在服务端派生为 `sha256:<64位十六进制>`，用户无需填写，页面不得显示；新绑定只保存不可逆摘要。旧明文绑定仅为读取兼容，下一次同客户端保存会迁移为摘要。
- 提供方存在但关闭、停止、拒绝或报错时，消费者不得再把本地白名单与它合并放行。只有完全未安装序时，消费者才可启用自身独立配置回退。
- 自然人绑定继续由情负责连续关系；自然人 ID、显示名和关系状态永远不能替代原始平台身份或授予权限。

序另提供 `identity.quest_binding_control@1.0`，用于自然人映射后的 Quest 只读会话身份：

- `upsert_quest_binding()` 把 principal 摘要与 client/platform/bot/user 的精确组合再次收敛为 `qrb1` 不可逆整组摘要后写入 `quest_session_read_only_bindings`，配置中不保留可读账号，且绝不修改 `owner_users`。
- 命中该绑定只允许 Quest 使用这组原始身份进入只读上下文，返回 `owner_confirmed=false`、`grants_platform_action=false`；群管理、处罚、主动投递与管理员工具仍按原权限规则判断。
- “情”只提供账号归属事实，“临”仍须提交已认证 principal；自然人 ID、显示名或关系状态不能直接创建绑定。

这里的“统一”只统一身份事实和授权裁决，不把所有名为“白名单”的业务开关混为一种权限。言的内容拦截例外、声的自动语音范围、知的知识领域范围和情的高好感门槛仍是各自业务策略；后续可通过控制面按命名空间读取身份角色，但必须保留插件本地配置作为未安装序时的回退，且集中结果与本地结果不得合并扩大权限。

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
| `quest_session_owner_bindings` | list<string> | `[]` | Quest 私聊只读上下文的精确绑定，格式为 `api_principal|client_id|platform_id|bot_id|user_id`；还需 `user_id` 已列入 `owner_users`，默认不授权 |
| `quest_session_read_only_bindings` | list<string> | `[]` | Quest 非主人只读上下文的 `qrb1` 不可逆整组摘要，由临通过正式契约管理；不包含可读账号、不修改 `owner_users`、不授予平台操作权限 |
| `friendly_users` | list<string> | `[]` | 额外友好用户 QQ 号列表（纯数字字符串）。群主和管理员可按平台身份自动识别，无需重复填写 |
| `protected_users` | list<string> | `[]` | 强保护用户 QQ 号列表（纯数字字符串），禁止被踢出、长时禁言及批量处罚 |
| `log_level` | string | `INFO` | 日志级别：DEBUG / INFO / WARNING / ERROR |

> 注：`owner_users` / `friendly_users` / `protected_users` / `blacklist_users` 填写的是 **QQ 号**（OneBot 平台 ID，纯数字字符串），非 AstrBot 内部 UID。代码中通过 `event.get_sender_id()` 比对，aiocqhttp 平台返回的 sender_id 即 QQ 号。

### 互动与处罚

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `allow_playful_mute_protected` | bool | `false` | 允许对主人 / 强保护用户进行玩笑式短禁言 |
| `playful_mute_max_seconds` | int | `60` | 玩笑式禁言最大时长（秒），必须小于 `max_mute_seconds` |
| `max_mute_seconds` | int | `1800` | 单次禁言时长硬上限（秒），所有禁言工具与 LLM 都无法超过 |
| `confirm_mute_threshold` | int | `600` | 超过此秒数的禁言需人工确认才执行；默认值低于 `max_mute_seconds`，确保长禁言可触发确认，若大于 `max_mute_seconds` 则禁言永远不会触发人工确认 |
| `auto_confirm_threshold` | string | `mute_short` | 内容审核自动执行的最高处罚档位：warn / mute_short / mute_long / delete / kick，超过的降级为警告。设为 delete / kick 后命中审核即可自动撤消息甚至踢人，风险高 |
| `blacklist_users` | list<string> | `[]` | 永久黑名单 QQ 号列表（纯数字字符串），触发即踢；填错会直接误踢群友，保存前请逐位核对 |
| `action_cooldown_seconds` | int | `60` | 同一用户同一操作冷却（秒） |
| `circuit_breaker_threshold` | int | `10` | 全局熔断阈值（1 小时内管理操作总数） |

### 内容审核

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `auto_moderate` | bool | `false` | 启用独立 LLM 内容审核（第二层）。若已安装其他防注入 / 内容过滤插件，可关闭此项避免重复审核 |
| `moderation_rules` | list<string> | `[]` | 违规关键词正则列表（第一层预筛），命中即固定处罚不经 LLM。例如 `["加我微信.*", "免费领.*"]` |
| `spam_threshold` | int | `5` | 刷屏阈值（条 / 10 秒，0 = 关闭） |
| `manual_threshold` | float | `0.6` | 审核置信度低于此值时不自动处罚，取值 0-1，调高更保守 |
| `cross_group_violation` | bool | `false` | 违规历史是否跨群共享；涉及跨群隐私，请确认各群管理知情后再开启 |

### 安全护栏

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enable_api_guard` | bool | `true` | 启用 API 层硬拦截（第三层护栏），在管理 API 执行前校验紧急停止与全局熔断状态，命中即阻断；强烈建议始终开启 |
| `enable_set_admin_revoke` | bool | `false` | 是否允许撤销管理员操作（默认只允许任命） |
| `proactive_delivery_targets` | list<string> | `[]` | 主人主动消息的完整私聊 UMO 白名单；精确匹配，默认不授权任何目标 |

### 入群审核

AstrBot 插件详情中的 `pages/join_review` 是当前生效的入群审核控制面。配置按
`platform_id + group_id` 隔离；刷新已加入群只读 OneBot `get_group_list` / 群信息和权限，
不会自动写配置。新群与未配置群的两个开关均默认关闭。

| 自动审核 | 发送审核 | 行为 |
| --- | --- | --- |
| 关 | 关 | 忽略申请，保持 QQ 原始状态 |
| 开 | 关 | 执行自动审核；未实际批准时保持 QQ 原始待审，不发通知 |
| 关 | 开 | 不调用 LLM，直接写入人工待审并按群配置通知 |
| 开 | 开 | 先自动审核；只有平台批准成功才结束，其余结果全部转人工 |

自动审核的入群问题直接取自申请事件解析结果，不要求与预设问题精确一致。判定严格按
三段顺序：**预设 → 知识联动 → 兜底**。预设取该群在 Page 配置的「入群问答预设」
（按群 `join_questions`，问题留空表示匹配任意入群问题），先精确/模糊匹配申请人答案，
不中再由 LLM 对预设答案做语义比对；该群未配置预设时回退旧全局 `join_questions`，
两者都无预设则直接进入知识联动。知识联动（`enable_active_learner_recall`）只在检索到
证据时才调 LLM 带证据判断。两段都无高置信结果时返回不确定并转忽略/人工——
不再让 LLM 在无参考答案、无知识证据的情况下自由判断。

插件永不自动拒绝申请。只有 Dashboard 管理员在入群审核 Page 明确点击“驳回”，且 Bot
当前仍具备该群审核权限、OneBot API 返回成功后，记录才会变为 `rejected`。通知可发送到
申请所属群、指定审核群或两边；指定审核群只能来自该群保存的配置白名单。

每个群配置还有三个辅助字段：`置顶`（`pinned`）仅影响 Page 群配置表的展示顺序，置顶的群
排在最前并有视觉区分；`推送群`（`push_group_ids`）是该群的入群申请推送目标群列表，
推送群必须是当前 Bot 已加入的群；`推送样式`（`push_style`）决定推送文案：自然语言
（默认；LLM 按人设自由措辞，可用全局 `push_llm_provider` 指定模型）或格式化模板。
有申请进入
人工待审时会自动推送到这些群；推送群留空时回退推送到申请所属群本身。同一申请对同一
推送群只推一次；紧急停止或熔断期间不推送。Page 群配置表只保留摘要列，以上细分设置
均在点击群名打开的悬浮窗中编辑保存。

推送与审批结果回复全链路按人设生成，不写死模板：natural 样式（默认）只把申请事实
（昵称/QQ/等级/问答/来源群）告诉 LLM，措辞、结构、语气全部由 AI 按人设自由发挥，
唯一硬性要求是引导管理员「引用本条消息回复同意或拒绝」（引用审批机制依赖它定位申请）；
formatted 样式用固定模板罗列事实，看法行先调 LLM 生成一句话（`push_llm_provider`，
纯文本、不超过 80 字），LLM 不可用或失败时回退自动审核结论行（未经过自动审核时如实标注）。
推送走 OneBot 发送以拿到消息 ID，随后可在群内**引用该条推送消息**回复
「同意」或「不同意」直接审批：回复者必须是该推送群的群主/管理员或 bot 主人；同意/拒绝
由审核 LLM 做语义判断，语义含糊时不处理、不打扰群；审批仍复用 process_request 的护栏、
bot 角色复查与一次性语义——同一申请只有第一个明确决策生效；审批结果回复（已同意/已拒绝/
已被处理/失败原因）也由 LLM 按人设措辞（同样走 `push_llm_provider` 与目标群人格），
LLM 不可用或失败时回退固定文案（如「该申请已被其他管理员处理或已过期」）。

旧配置仍保留，但不会自动启用任何群：

| 配置项 | 用途 |
| --- | --- |
| `join_audit_mode` | 仅作为 Page 中“应用旧配置”的显式迁移来源 |
| `join_questions` | 旧全局入群问答模板；按群未配置「入群问答预设」时回退使用 |
| `join_approve_threshold` | 自动批准最低置信度 |
| `audit_llm_provider` | 自动审核 LLM Provider |
| `audit_notify_targets` | 只读兼容旧版本，不再用于新通知投递 |
| `pending_ttl_hours` | 人工待审记录 TTL，默认 24 小时 |

### 模拟申请诊断

入群审核 Page 的「模拟申请」卡片可选群、输入问题（留空 = 仅按答案判断）与答案，
模拟一次入群申请：走真实的三段自动审核链路（按群/全局预设 → 知知识库联动 → 兜底），
真实调用审核 LLM（`audit_llm_provider`）与知识检索，逐步展示每段的通过/未通过/跳过
及细节（含 LLM 原始返回摘要与证据条数），底部给出最终结论与「实际发生时会发生什么」
（批准 / 保持平台待审 / 转人工待审并推送，取决于该群两个开关）。整个模拟**零副作用**：
不调用平台批准/拒绝接口、不写待审记录、不发通知/推送、不写审计日志。

模拟结果为「转人工待审并推送」时，结果区还会给出**推送文案预览**：与生产推送
共用同一渲染路径，申请人昵称/QQ 用占位值；按该群 `push_style` 展示格式化模板或
natural 自然语言文案（都会真实调用 `push_llm_provider` 指定的 LLM——natural 整段
生成、formatted 生成一句话看法，带入目标群会话人格与最近 ≤10 条群消息作为上下文，
失败按生产同样的规则回退并如实标注）；预览小字标注看法来源
（LLM 生成 / 自动审核结论 / 无）。预览只生成不发送；注意这是独立生成调用，
主对话链路上的记忆类插件钩子不会在此触发。

### 知识库联动

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enable_active_learner_recall` | bool | `false` | 入群审核在预设判定未命中后查询 active_learner 知识库；有证据才调 LLM 带证据判断，无证据则转忽略/人工 |
| `active_learner_scope` | string | `group` | 知识检索范围：group 只检索当前群 / global 检索全局共享知识（可能把其他群内容用于本群判断） |

### LLM 与欢迎

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `audit_llm_provider` | string | `""` | 审核用 LLM Provider（留空则回退主对话 LLM），可填便宜模型降低成本 |
| `push_llm_provider` | string | `""` | 入群申请推送文案的 LLM Provider（留空回退主对话 LLM）：natural 整段文案与 formatted 的一句话看法都用它；natural 生成失败自动回退格式化模板，formatted 看法失败回退自动审核结论行 |
| `confirm_notify_targets` | list<string> | `[]` | 二次确认通知目标列表（AstrBot 会话 ID，unified_msg_origin 格式） |
| `welcome_bot_speak` | bool | `false` | bot 进群是否主动发言 |
| `welcome_template` | string | `""` | bot 进群发言模板，支持 `{group_name}` / `{group_id}` 占位符；留空则不主动发言 |
| `identity_refresh_interval` | int | `1800` | 身份刷新间隔（秒） |

## LLM 工具

工具会在每次 LLM 请求前按 bot 在当前群的真实身份过滤：普通成员看不到管理员和群主工具，管理员看不到群主专属工具，群主可见全部工具。该过滤只影响本次请求，不会改动其他群或其他插件的工具；运行时仍由 `PolicyEngine` 再次硬校验，防止身份变化或伪造调用绕过。

### 反应与自助（普通成员可用）

| 工具 | 说明 |
| --- | --- |
| `mute_current_sender` | 短时禁言当前消息发送者。目标由事件绑定，不能指定第三人。适用于对方辱骂、骚扰后的反应，或与主人互动的玩笑式短禁言 |
| `request_self_mute` | 响应当前发送者对其本人的禁言请求。目标由系统绑定为请求者本人，不接受 user_id |
| `set_self_card` | 修改 bot 自己的群名片 |
| `get_group_member_info` | 查询群成员的昵称、群名片、角色与头衔（只读，无副作用） |
| `list_group_members` | 列出当前群成员总数与管理层名单（只读，无副作用） |

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

### 仅群主

| 工具 | 说明 |
| --- | --- |
| `set_member_title` | 设置群成员专属头衔 |
| `set_group_admin` | 设置或撤销群管理员。需人工确认，撤销需额外配置 `enable_set_admin_revoke=true` |

## 指令组

`/idg` 指令组提供人工兜底入口：

除 `/idg help` 外，状态、停止、恢复、刷新、熔断重置和确认审批均要求 AstrBot 管理员权限。批准操作只能在创建确认的原群执行，并会按审批时的实时身份重新运行策略；校验失败时确认记录保持待审。

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
│  命中即固定处罚，不调 LLM（预留，运行时未启用）               │
└─────────────────────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  第二层：独立 LLM 审核（auto_moderate）                       │
│  独立于主对话消息链路，不注入身份、不注册工具、只输出 JSON     │
│  默认关闭（预留，运行时未启用）                               │
└─────────────────────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  第三层：API 层硬拦截（enable_api_guard，默认开启）           │
│  在管理 API 真正执行前校验紧急停止与全局熔断状态              │
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

> 注：L0-L4 分级限频未实现，当前限流手段为全局熔断器（`circuit_breaker_threshold`）。

### 防滥用机制

- **关系与保护分离**：`owner_users` / `friendly_users` 用于关系理解，`protected_users` 用于强保护
- **参数上限**：禁言时长受 `max_mute_seconds` 约束，LLM 无法突破
- **冷却**：同一目标同一操作冷却内不重复执行（`action_cooldown_seconds` 已预留，运行时未启用）
- **分级频率限制**：L0 30 次 / 分钟，L1-L2 10 次 / 分钟，L3 3 次 / 分钟，L4 1 次 / 小时（未实现）
- **批量操作检测**：60 秒内对 5+ 不同 user_id 调用 kick/mute → 立即熔断 + 通知管理员（未实现）
- **全局熔断**：1 小时内管理操作总数超阈值 → 自动停用所有高风险工具
- **黑名单兜底**：`blacklist_users` 在工具调用授权路径生效，触发即踢且拒绝再将其加入；与保护名单冲突时保护优先
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
│  ├─ join_review*.py 按群配置、人工队列、通知和 Page API      │
│  ├─ group_discovery.py 只读发现 aiocqhttp Bot 与群权限       │
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
| 展示名 | 凝心溯溪-序 |
| 当前版本 | 见 `metadata.yaml`（唯一事实源） |
| 作者 | 凌溪 |
| AstrBot 版本 | `>=4.17,<5` |
| 支持平台 | `aiocqhttp`（NapCat / Lagrange / LLOneBot 等 OneBot V11 实现） |
| 许可证 | `MIT` |

## 开发与验证

```bash
# 代码风格检查
python -m ruff check .
python -m ruff format --check .

# 单元测试
python -m pytest tests/ -v
```

注：`core/request_context.py` 是系列共享的字节一致副本，由系列级测试锁定内容，已通过 `ruff.toml` 排除在本仓库 lint/format 之外。

测试覆盖：策略引擎（policy）、能力映射（capability）、配置解析（config）、冷却与熔断（cooldown）、内容审核（moderation）、入群审核（audit）。

## 维护约定

任何可观察功能、配置项或安全边界的增删改，必须在同一批变更中同步 README、CHANGELOG 的
`Unreleased`、配置 schema 与回归测试。版本号在实现、文档和验证完成后由发布者确认。

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
