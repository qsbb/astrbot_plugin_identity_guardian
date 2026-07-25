# Changelog

遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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
