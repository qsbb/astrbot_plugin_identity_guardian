# Changelog

遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> 当前系列归属：知、言、序、情、声、核；下方版本号与日期均为真实历史记录，不因当前文档整改而改写。

## [0.1.6] - 2026-07-27

### Changed

- 版本号格式迁移：由带 `v` 前缀的 `v0.1.5` 迁移为三段式无前缀格式 `0.1.6`，与系列其他插件统一。同步更新 `metadata.yaml`、`__init__.py`（`main.py` 注册版本由 `__init__.__version__` 导出，随之生效）与 README 中的当前版本标注。历史版本条目保持原样，不做改写。
- `metadata.yaml` 的 `astrbot_version` 写法与系列统一为 `">=4.17,<5"`（语义与原 `">=4.17.0,<5.0.0"` 等价，下界仍覆盖代码使用的 API）。

## [v0.1.5] - 2026-07-26

### Fixed

- 修复所有写操作实际生效却被报告为失败的问题。典型表现：让 bot 改自己群名片，名片确实改成了，但回复「好像改不了，我没这个权限呢」，日志里是 `Tool set_self_card Result: 执行失败：set_group_card failed`。根因是成功判定读了不存在的字段：aiocqhttp 的 `call_action` 内部已由 `_handle_api_result` 拆包，**只返回 `data` 字段**，并在 `status == "failed"` 时抛 `ActionFailed`；而 OneBot 的写操作成功时 `data` 为 `null`，即 `call_action` 返回 `None`。插件此前用 `resp["status"] == "ok"` 判定，这个分支永远走不到，`None` 被当成失败。现改为「未抛异常即成功」，并保留对返回完整响应信封的适配器的兼容判定（要求同时含 `status` 与 `retcode`，避免把恰好带 `status` 字段的业务 data 误判）。此问题影响改名片、禁言、踢人、改群名、设头衔等全部写操作。
- 补全失败日志的诊断信息：`ActionFailed` 的 `str()` 只带 retcode，真正有用的 `wording` / `msg` 藏在 `result` 字典里，现已一并输出，便于区分权限不足与其他失败原因。

## [v0.1.4] - 2026-07-25

### Fixed

- 修复热重载后事件处理器崩溃：`'IdentityGuardianPlugin' object has no attribute 'get_platform_name'`。旧实例遗留的 `functools.partial` 会把插件实例再绑一次到第一个形参上，导致 `event` 形参整体错位收到插件实例本身。`on_event` 与 `on_llm_request` 现按鸭子类型从全部实参中取回真正的 event 与 ProviderRequest，取不到则安静跳过，不再抛栈污染日志。
- 修复 `_unwrap_registry_handlers` 自始未生效的问题：此前读取上游不存在的 `registry.handlers` 与 `handler.full_name`，前者恒为空、后者恒为空串，导致 partial 拆解从未匹配到任何 handler。现改用上游真实字段 `registry._handlers` 与 `handler_full_name`（缺失时回退 `handler_module_path`），并支持多层套娃一路剥到原始函数，同时严格只处理本插件自己的 handler。
- 修复 `metadata.yaml` 版本号停留在 v0.1.2 导致更新检查失效的问题。v0.1.3 只改了 `__init__.__version__`，而 AstrBot 与更新器读取的是 `metadata.yaml`，因此远端始终报告旧版本、界面上不会出现可更新提示。两处版本号现已同步，并新增回归测试防止再次漂移。

## [v0.1.3] - 2026-07-25

### Fixed

- 修复 bot 为普通成员时无法按请求修改自己群名片的问题。LLM 常把「改你自己的名片」表达为 `set_member_card(user_id=<bot 自己>)`，此前会被管理员权限门以「bot 身份(member)无此权限」拒绝。策略引擎现在在权限校验前把指向 bot 自己的 `set_member_card` 归一化为 `set_self_card`（OneBot 中改自己名片只需 member 权限）。指向他人的调用仍按原权限规则拒绝，不构成提权路径。
- 修复群成员信息查询失败时把降级结果写入缓存的问题。此前一次接口抖动就会把真实的管理员身份在整个 `identity_refresh_interval` 周期内锁死为 `member`，导致本该允许的管理动作被持续拒绝。查询失败或 `role` 字段缺失/不可识别时仍降级为 `member`，但不再写入缓存，下次调用可恢复真实身份。
- 统一 `role` 字段解析，兼容字符串、整数（1/2/3）与对象三种 OneBot 实现差异，并对大小写和空格做归一化；无法识别的取值不再被当作 `member` 缓存。
- 修复 v0.1.2 引入的工具过滤实际未生效的问题。过滤逻辑此前读写 `ProviderRequest.tools`，而 AstrBot 用 `func_tool`（`ToolSet`）承载工具列表，导致过滤恒为空操作，普通成员身份下仍会看到管理员 / 群主工具。现改为在 `func_tool` 上按名称移除不可用工具，并保留对旧 `tools` 列表结构的兼容。该 `ToolSet` 由 AstrBot 每次请求新建，原地移除不会污染全局工具表或其他会话。

### Changed

- 身份上下文新增 bot 自己的 QQ 号，并在行动边界中明确提示改自己名片应使用 `set_self_card`，减少 LLM 选错工具。

## [v0.1.2] - 2026-07-25

### Added

- 按 bot 在当前群的真实身份动态过滤 LLM 工具：普通成员看不到管理员 / 群主工具，管理员看不到群主专属工具，避免 LLM 调用注定失败的接口。过滤只作用于本次请求，不影响其他群与其他插件的工具。
- 补齐开发文档中已定义但未注册的两个只读工具：`get_group_member_info`（查询成员昵称、群名片、角色与头衔）和 `list_group_members`（列出成员总数与管理层名单）。两者无副作用，不写审计与冷却。
- 新增能力与工具注册一致性测试，防止 `CAPABILITY_MAP` 与实际注册的 `llm_tool` 再次出现漂移。

## [v0.1.1] - 2026-07-25

### Changed

- 入群问答配置改为 WebUI 友好的字符串列表，每项使用 `问题|答案1,答案2` 格式，并兼容旧对象格式。
- 统一为“凝心溯溪-序”系列命名与说明。
- OneBot 查询动作失败日志降级为 DEBUG，并补充错误信息与关键诊断参数。

### Fixed

- 补充 `metadata.yaml` 的仓库与主页地址，使 AstrBot 插件更新器能够定位远程仓库。
- 明确 QQ 号列表、会话 ID 与入群问答配置的数据格式，补齐 AstrBot list schema 的 `items` 定义。
- 修复 OneBot 写操作成功返回 `data=null` 时被误判为失败的问题，避免群名片实际修改成功后 LLM 错误重试。

## [v0.1.0] - 2026-07-23

### Added

- 身份识别：bot、发送者与目标在群中的 role（owner / admin / member）查询与缓存。
- 关系识别：主人 / 友好用户 / 普通群员关系解析，注入到 LLM 上下文。
- 行动边界注入：`on_llm_request` 钩子向 LLM 注入当前身份、关系、允许行动集与安全规则。
- 14 个 LLM 工具：`mute_current_sender`、`request_self_mute`、`mute_member`、`unmute_member`、`kick_member`、`delete_message`、`set_member_card`、`set_self_card`、`set_member_title`、`set_group_name`、`set_group_admin`、`set_whole_ban`、`approve_join_request`，按身份分级暴露并由策略引擎硬校验。
- 三层安全防护：规则预筛（关键词/刷屏）+ 独立 LLM 审核 + API 层硬拦截（白名单/频率/批量/熔断）。
- 入群审核：读取 QQ 申请问题与答案，结合配置答案与可选知识库，仅高置信度时自动通过；错误、不确定或联动失败保持待审，不自动拒绝。
- 内容审核：独立于主对话的 LLM 内容审核，违规分级处罚（默认关闭）。
- 防刷屏：同一用户 10 秒内消息数超阈值直接禁言（可配置）。
- 二次确认：高破坏性操作（踢人、全员禁言、改群名、任命管理员）走人工确认队列。
- 熔断器：1 小时内管理操作总数超阈值自动停用所有高风险工具，需 `/idg reset_breaker` 手动恢复。
- 审计日志：所有有副作用的行动写入 `audit-YYYYMMDD.jsonl`，按日切割。
- 紧急停止：`/idg stop` 一键停用所有管理工具，仅保留身份注入。
- 知识库联动：入群审核可查询 active_learner 知识库作为判定证据，失败时保持待审。
- bot 进群欢迎：监听 `group_increase` 且 `user_id == self_id` 时触发。
- bot 退群清理：监听 `group_decrease` 且 `user_id == self_id` 时清空身份缓存。
- 管理员变更刷新：监听 `notify.group_admin_change` 通知时刷新身份缓存。
- 后台任务：`initialize()` 启动身份缓存定时刷新循环与待确认条目过期清理循环。
- `/idg` 指令组：`status` / `stop` / `resume` / `reset_breaker` / `refresh` / `approve` / `reject` / `help`。
- 单元测试：59 个用例覆盖策略引擎、能力映射、配置解析、冷却与熔断、内容审核、入群审核。

### Security

- 三层防御架构确保即使 LLM 被完全注入也无法绕过 API 层硬拦截。
- 普通成员请求影响第三人时由代码层拒绝，不构成授权。
- 主人、群主、管理员默认免疫踢出与长时禁言；可配置严格限时的玩笑式短禁言例外。
- 所有身份信息来自平台事件，不信任聊天文本中的身份声明。
